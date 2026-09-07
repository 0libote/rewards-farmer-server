import json
import sqlite3
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from server.config import PROFILES_DIR, is_valid_account_name

AUTH_COOKIE_NAMES = {
    ".MSA.Auth",
    "ANON",
    "RPSTAuth",
    "MSPAuth",
    "KievRPSTAuth",
    "WLSSC",
}

# Chromium stores expiry as microseconds since 1601-01-01. A value of 0 marks
# a session cookie. Offset between the Chromium epoch and the Unix epoch.
_CHROMIUM_TO_UNIX_OFFSET_S = 11644473600


def _chromium_now_us() -> int:
    return int((time.time() + _CHROMIUM_TO_UNIX_OFFSET_S) * 1_000_000)


def get_profile_dir(account_name: str) -> Path:
    base = PROFILES_DIR.resolve()
    target = (PROFILES_DIR / account_name).resolve() if account_name != "default" else base
    # Guard against path traversal (e.g. account name "../../etc").
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(f"Invalid account name: {account_name!r}")
    return target


def find_cookie_db(profile_dir: Path) -> Optional[Path]:
    """Finds the Chromium Cookies SQLite database path."""
    candidates = [
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
    ]
    for p in candidates:
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _classify_cookies(rows: List[Tuple[str, str, int]]) -> Tuple[List[str], List[str], bool]:
    """Split cookie rows into valid auth tokens, expired auth tokens, and
    whether any Microsoft/Bing cookie exists at all."""
    now_us = _chromium_now_us()
    valid_auth: List[str] = []
    expired_auth: List[str] = []
    has_ms_cookie = False
    for host, name, expires_utc in rows:
        host = host or ""
        if "bing.com" in host or "live.com" in host or "microsoft.com" in host or "microsoftonline.com" in host:
            has_ms_cookie = True
        is_auth = name in AUTH_COOKIE_NAMES or (
            "bing.com" in host and ("Auth" in name or "Token" in name)
        )
        if not is_auth:
            continue
        try:
            exp = int(expires_utc or 0)
        except (TypeError, ValueError):
            exp = 0
        # expires_utc == 0 is a session cookie: count it, it only exists
        # while a signed-in browser session created it.
        if exp == 0 or exp > now_us:
            if name not in valid_auth:
                valid_auth.append(name)
        else:
            if name not in expired_auth:
                expired_auth.append(name)
    return valid_auth, expired_auth, has_ms_cookie


def get_account_email(profile_dir: Path) -> Optional[str]:
    """Tries to extract authenticated email from Chromium Preferences or Account Manager."""
    pref_candidates = [
        profile_dir / "Default" / "Preferences",
        profile_dir / "Preferences",
    ]
    for pref_path in pref_candidates:
        if not pref_path.exists():
            continue
        try:
            with open(pref_path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)

            # Check account_info
            account_info = data.get("account_info", [])
            if isinstance(account_info, list) and account_info:
                email = account_info[0].get("email")
                if email:
                    return email

            # Check edge/msa profile
            edge_profile = data.get("profile", {})
            if isinstance(edge_profile, dict):
                user_name = edge_profile.get("name")
                if user_name and "@" in user_name:
                    return user_name

            # Check signin
            signin = data.get("signin", {})
            if isinstance(signin, dict):
                email = signin.get("username")
                if email and "@" in email:
                    return email
        except Exception:
            pass

    return None


def check_account_login(account_name: str) -> Dict[str, Any]:
    """Checks whether an account's browser profile contains valid Microsoft authentication cookies."""
    if not is_valid_account_name(account_name):
        return {
            "account": account_name,
            "logged_in": False,
            "reason": "Invalid account name.",
            "email": None,
        }
    try:
        profile_dir = get_profile_dir(account_name)
    except ValueError:
        return {
            "account": account_name,
            "logged_in": False,
            "reason": "Invalid account name.",
            "email": None,
        }
    if not profile_dir.exists():
        return {
            "account": account_name,
            "logged_in": False,
            "reason": "Profile directory does not exist yet. Please launch Interactive Login.",
            "email": None,
        }

    cookie_db = find_cookie_db(profile_dir)
    if not cookie_db:
        return {
            "account": account_name,
            "logged_in": False,
            "reason": "No browser session cookies found. Please log in first.",
            "email": None,
        }

    # Make a temporary copy of the cookie DB in case Chromium has it locked
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=True) as tmp:
        try:
            shutil.copy2(str(cookie_db), tmp.name)
            conn = sqlite3.connect(tmp.name)
            cursor = conn.cursor()

            # Check if cookies table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies';")
            if not cursor.fetchone():
                conn.close()
                return {
                    "account": account_name,
                    "logged_in": False,
                    "reason": "Cookies database is uninitialized.",
                    "email": None,
                }

            # Query only Microsoft-related cookies instead of the whole table.
            placeholders = ",".join("?" for _ in AUTH_COOKIE_NAMES)
            cursor.execute(
                f"SELECT host_key, name, expires_utc FROM cookies "
                f"WHERE name IN ({placeholders}) "
                f"OR host_key LIKE '%bing.com%' "
                f"OR host_key LIKE '%live.com%' "
                f"OR host_key LIKE '%microsoft.com%' "
                f"OR host_key LIKE '%microsoftonline.com%'",
                tuple(AUTH_COOKIE_NAMES),
            )
            rows = cursor.fetchall()
            conn.close()

            matched_auth, expired_auth, has_ms_cookie = _classify_cookies(rows)

            email = get_account_email(profile_dir)

            if matched_auth:
                return {
                    "account": account_name,
                    "logged_in": True,
                    "reason": f"Active session found ({len(matched_auth)} auth tokens).",
                    "email": email,
                    "matched_tokens": matched_auth[:5],
                }

            if expired_auth:
                return {
                    "account": account_name,
                    "logged_in": False,
                    "reason": "Microsoft authentication tokens are expired. Please sign in again via Interactive Login.",
                    "email": email,
                }

            if has_ms_cookie:
                return {
                    "account": account_name,
                    "logged_in": False,
                    "reason": "Bing cookies found but Microsoft authentication token is missing. Sign-in required.",
                    "email": email,
                }

            return {
                "account": account_name,
                "logged_in": False,
                "reason": "No Microsoft session cookies found. Please log in first.",
                "email": email,
            }
        except Exception as e:
            return {
                "account": account_name,
                "logged_in": False,
                "reason": f"Could not inspect cookies: {e}",
                "email": None,
            }
