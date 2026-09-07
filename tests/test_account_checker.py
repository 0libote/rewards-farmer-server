"""Unit tests for login detection, cookie expiry and path safety."""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.account_checker as checker
from server.account_checker import (
    _chromium_now_us,
    _classify_cookies,
    check_account_login,
    get_profile_dir,
)


def _future_us() -> int:
    return _chromium_now_us() + 30 * 24 * 3600 * 1_000_000


def _past_us() -> int:
    return _chromium_now_us() - 24 * 3600 * 1_000_000


def test_classify_cookies_expiry():
    rows = [
        (".bing.com", "ANON", _future_us()),
        (".live.com", "RPSTAuth", _past_us()),
        (".bing.com", "session_pref", 0),
    ]
    valid, expired, has_ms = _classify_cookies(rows)
    assert valid == ["ANON"]
    assert expired == ["RPSTAuth"]
    assert has_ms is True


def test_classify_cookies_empty():
    assert _classify_cookies([]) == ([], [], False)


def _make_profile(base: Path, account: str, cookies, email=None) -> Path:
    """Build a fake Chromium profile dir with a Cookies sqlite db."""
    profile = base if account == "default" else base / account
    net_dir = profile / "Default" / "Network"
    net_dir.mkdir(parents=True, exist_ok=True)
    db = net_dir / "Cookies"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
    conn.executemany(
        "INSERT INTO cookies (host_key, name, expires_utc) VALUES (?, ?, ?)", cookies
    )
    conn.commit()
    conn.close()
    if email:
        prefs = profile / "Default" / "Preferences"
        prefs.write_text(json.dumps({"account_info": [{"email": email}]}), encoding="utf-8")
    return profile


def test_logged_in_with_valid_token(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, "PROFILES_DIR", tmp_path / "profiles")
    _make_profile(
        tmp_path / "profiles",
        "default",
        [(".bing.com", "ANON", _future_us())],
        email="user@example.com",
    )
    result = check_account_login("default")
    assert result["logged_in"] is True
    assert result["email"] == "user@example.com"


def test_expired_token_reports_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, "PROFILES_DIR", tmp_path / "profiles")
    _make_profile(
        tmp_path / "profiles", "spare", [(".live.com", "RPSTAuth", _past_us())]
    )
    result = check_account_login("spare")
    assert result["logged_in"] is False
    assert "expired" in result["reason"].lower()


def test_non_auth_cookies_not_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, "PROFILES_DIR", tmp_path / "profiles")
    _make_profile(
        tmp_path / "profiles", "default", [(".bing.com", "session_pref", _future_us())]
    )
    result = check_account_login("default")
    assert result["logged_in"] is False


def test_missing_profile_and_invalid_names(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, "PROFILES_DIR", tmp_path / "profiles")
    assert check_account_login("ghost")["logged_in"] is False
    assert check_account_login("../evil")["logged_in"] is False
    assert check_account_login("")["logged_in"] is False


def test_profile_dir_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, "PROFILES_DIR", tmp_path / "profiles")
    with pytest.raises(ValueError):
        get_profile_dir("..")
    with pytest.raises(ValueError):
        get_profile_dir("a/../../b")
