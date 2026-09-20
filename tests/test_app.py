"""Tests for API-level config merging (partial updates must not reset fields)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.app as app_module
from server.config import AppConfig


def test_update_config_merges_only_provided_fields(monkeypatch):
    existing = AppConfig(
        accounts=["default", "spare"],
        query_source="trends",
        llm_model="keep-me",
        webhook_url="https://example.test/hook",
    )
    saved = {}

    monkeypatch.setattr(app_module, "get_config", lambda: existing)
    monkeypatch.setattr(app_module, "save_config", lambda c: saved.__setitem__("cfg", c))
    monkeypatch.setattr(app_module, "reload_schedule", lambda: None)

    # Client only changes query_source; everything else must survive.
    partial = AppConfig(query_source="llm")
    result = asyncio.run(app_module.update_config(partial))

    merged = saved["cfg"]
    assert merged.accounts == ["default", "spare"]
    assert merged.llm_model == "keep-me"
    assert merged.webhook_url == "https://example.test/hook"
    assert merged.query_source == "llm"
    assert result["config"].query_source == "llm"


def test_update_config_nested_schedule_stays_typed(monkeypatch):
    existing = AppConfig(accounts=["default"])
    saved = {}

    monkeypatch.setattr(app_module, "get_config", lambda: existing)
    monkeypatch.setattr(app_module, "save_config", lambda c: saved.__setitem__("cfg", c))
    monkeypatch.setattr(app_module, "reload_schedule", lambda: None)

    partial = AppConfig(schedule={"enabled": False, "mode": "interval", "interval_hours": 4})
    asyncio.run(app_module.update_config(partial))

    merged = saved["cfg"]
    assert merged.schedule.mode == "interval"
    assert merged.schedule.interval_hours == 4
    # Unset sibling fields keep their defaults from validation.
    assert merged.schedule.cron_hour == 3
