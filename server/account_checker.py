import json
import sqlite3
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional

from server.config import PROFILES_DIR

AUTH_COOKIE_NAMES = {
    ".MSA.Auth",
    "ANON",
    "RPSTAuth",
    "MSPAuth",
    "KievRPSTAuth",
    "WLSSC",
}


def get_profile_dir(account_name: str) -> Path:
    if account_name == "default":
        return PROFILES_DIR.resolve()
    return (PROFILES_DIR / account_name).resolve()


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
    profile_dir = get_profile_dir(account_name)
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

            # Query for Microsoft Auth cookies
            cursor.execute("SELECT host_key, name FROM cookies;")
            rows = cursor.fetchall()
            conn.close()

            matched_auth = []
            has_bing_cookie = False
            for host, name in rows:
                if "bing.com" in host or "live.com" in host or "microsoft.com" in host:
                    has_bing_cookie = True
                if name in AUTH_COOKIE_NAMES:
                    matched_auth.append(name)
                elif "bing.com" in host and ("Auth" in name or "Token" in name):
                    matched_auth.append(name)

            email = get_account_email(profile_dir)

            if matched_auth:
                return {
                    "account": account_name,
                    "logged_in": True,
                    "reason": f"Active session found ({len(matched_auth)} auth tokens).",
                    "email": email,
                    "matched_tokens": matched_auth[:5],
                }

            return {
                "account": account_name,
                "logged_in": False,
                "reason": "Bing cookies found but Microsoft authentication token is missing or expired. Sign-in required.",
                "email": email,
            }
        except Exception as e:
            return {
                "account": account_name,
                "logged_in": False,
                "reason": f"Could not inspect cookies: {e}",
                "email": None,
            }
