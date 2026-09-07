"""Unit tests for server.config validation and persistence."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import (
    AppConfig,
    ScheduleConfig,
    get_config,
    is_valid_account_name,
    save_config,
)
import server.config as config_module


def test_account_name_validation():
    assert is_valid_account_name("default")
    assert is_valid_account_name("work-1")
    assert is_valid_account_name("a_b")
    assert is_valid_account_name("A" * 32)
    assert not is_valid_account_name("")
    assert not is_valid_account_name("../evil")
    assert not is_valid_account_name("a/b")
    assert not is_valid_account_name(" leading")
    assert not is_valid_account_name("semi;colon")
    assert not is_valid_account_name("A" * 33)
    assert not is_valid_account_name(None)
    assert not is_valid_account_name(123)


def test_accounts_filtered_to_valid_names():
    cfg = AppConfig(accounts=["ok", "../evil", "", "  spaced  ", "second"])
    assert cfg.accounts == ["ok", "spaced", "second"]


def test_empty_accounts_falls_back_to_default():
    cfg = AppConfig(accounts=["../nope"])
    assert cfg.accounts == ["default"]


def test_schedule_mode_coerced_not_rejected():
    assert ScheduleConfig(mode="bogus").mode == "daily"
    assert ScheduleConfig(mode="interval").mode == "interval"
    assert ScheduleConfig(mode="custom_times").mode == "custom_times"


def test_schedule_bounds_clamped():
    assert ScheduleConfig(interval_hours=0).interval_hours == 1
    assert ScheduleConfig(interval_hours=500).interval_hours == 168
    assert ScheduleConfig(cron_hour=99).cron_hour == 23
    assert ScheduleConfig(cron_minute=-5).cron_minute == 0


def test_custom_times_invalid_entries_dropped():
    sched = ScheduleConfig(mode="custom_times", custom_times=["25:00", "09:30", "nope"])
    assert sched.custom_times == ["09:30"]
    # Never ends up empty (scheduler would have zero jobs with no warning otherwise).
    assert ScheduleConfig(mode="custom_times", custom_times=["bogus"]).custom_times == ["03:00"]


def test_query_source_and_log_level_coerced():
    assert AppConfig(query_source="weird").query_source == "trends"
    assert AppConfig(log_level="VERBOSE").log_level == "INFO"
    assert AppConfig(query_source="llm").query_source == "llm"


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    cfg = AppConfig(accounts=["default", "spare"], query_source="llm")
    cfg.schedule.mode = "interval"
    cfg.schedule.interval_hours = 12
    save_config(cfg)
    loaded = get_config()
    assert loaded.accounts == ["default", "spare"]
    assert loaded.query_source == "llm"
    assert loaded.schedule.mode == "interval"
    assert loaded.schedule.interval_hours == 12
    # Atomic write leaves no temp files behind.
    assert list(tmp_path.glob("config.*.tmp")) == []
