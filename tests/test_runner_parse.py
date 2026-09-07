"""Unit tests for log parsing, points math and log hygiene."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.runner as runner
from server.runner import (
    _prune_old_logs,
    is_safe_log_filename,
    parse_log_line,
    state,
)


def _fresh(account="default"):
    state.reset()
    state.accounts_in_run = [account]


def test_safe_log_filenames():
    assert is_safe_log_filename("run_20250101_030000.log")
    assert not is_safe_log_filename("history.json")
    assert not is_safe_log_filename("run_x.log")
    assert not is_safe_log_filename("../run_20250101_030000.log")
    assert not is_safe_log_filename("/etc/passwd")
    assert not is_safe_log_filename("run_20250101_030000.log\ninjected")


def test_account_transition_initializes_stats():
    _fresh()
    parse_log_line("=== account: spare ===")
    assert state.current_account == "spare"
    assert state.account_stats["spare"]["status"] == "RUNNING"
    assert state.account_stats["spare"]["step_index"] == 1


def test_failed_tasks_still_advance_progress():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("[FAIL] Bing daily set failed")
    assert state.account_stats["default"]["step_index"] == 2
    assert state.account_stats["default"]["tasks"]["Bing daily set"] == "FAIL"
    parse_log_line("[SKIP] Explore on Bing not available")
    assert state.account_stats["default"]["step_index"] == 3
    parse_log_line("[OK] Visual search done")
    assert state.account_stats["default"]["step_index"] == 4
    parse_log_line("[FAIL] Misc cards blew up")
    assert state.account_stats["default"]["step_index"] == 5
    parse_log_line("[FAIL] Required searches incomplete")
    assert state.account_stats["default"]["step_index"] == 6
    parse_log_line("[FAIL] Bonus points failed")
    assert state.account_stats["default"]["status"] == "COMPLETED"


def test_search_points_tracking():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("Search points before: 15/90")
    entry = state.account_stats["default"]
    assert entry["initial_points"] == 15
    assert entry["max_points"] == 90
    parse_log_line("Round 1: 5 searches -> 30/90")
    assert entry["current_points"] == 30
    assert entry["search_points"] == "30/90"
    assert entry["points_gained"] >= 15


def test_raw_balance_overrides_estimate():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("[POINTS] Balance before run: 1000 pts")
    parse_log_line("[POINTS] Balance after run: 1160 pts")
    parse_log_line("[POINTS] Raw points earned this run: +160 pts")
    assert state.account_stats["default"]["points_gained"] == 160


def test_prune_keeps_newest_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    for i in range(35):
        (tmp_path / f"run_202501{i:02d}_030000.log").write_text("x")
    (tmp_path / "history.json").write_text("[]")
    _prune_old_logs()
    remaining = sorted(p.name for p in tmp_path.glob("run_*.log"))
    assert len(remaining) == 30
    assert remaining[0] == "run_20250105_030000.log"
    # Non-run files untouched.
    assert (tmp_path / "history.json").exists()
