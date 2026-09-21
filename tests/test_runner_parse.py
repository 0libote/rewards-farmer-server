"""Unit tests for log parsing, points math and log hygiene."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.runner as runner
from server.runner import (
    _llm_env,
    _prune_old_logs,
    is_safe_log_filename,
    parse_log_line,
    state,
)
from server.config import AppConfig


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


def test_ok_tasks_without_raw_earn_search_diff_only():
    """No inflated pre-2026 task estimates: OKs alone earn 0 without raw/diff."""
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("[OK] Bing daily set")
    parse_log_line("[OK] Explore on Bing")
    parse_log_line("[OK] Misc cards")
    assert state.account_stats["default"]["points_gained"] == 0
    parse_log_line("Search points before: 9/30")
    parse_log_line("Round 1: 7 searches -> 30/30")
    assert state.account_stats["default"]["points_gained"] == 21


def test_incomplete_card_warnings_tracked():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line(
        "Explore on Bing Card [desc='Search on Bing to reserve airport parking'] "
        "is not complete after searching. Please check manually."
    )
    parse_log_line(
        "Misc Card [desc='Download the Bing app now'] is not complete after clicking."
    )
    entry = state.account_stats["default"]
    assert entry["incomplete_cards"] == 2
    assert any(w.startswith("Explore card") for w in entry["warnings"])
    assert any(w.startswith("Misc card") for w in entry["warnings"])
    # Task still OK upstream, but warnings survive the OK line.
    parse_log_line("[OK] Explore on Bing")
    assert state.account_stats["default"]["incomplete_cards"] == 2
    assert state.account_stats["default"]["tasks"]["Explore on Bing"] == "OK"


def test_unknown_task_name_is_tracked_from_logger():
    _fresh()
    parse_log_line("=== account: default ===")
    # A task upstream might add later; tracked so the dashboard does not go stale.
    parse_log_line("12:00:00 INFO     rewards_tasks: [OK] Watch a video")
    assert state.account_stats["default"]["tasks"]["Watch a video"] == "OK"


def test_account_start_failure_is_not_a_task():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("[FAIL] default: could not start Edge with this profile.")
    assert "default" not in state.account_stats["default"]["tasks"]


def test_not_signed_in_warning_detected():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line(
        "12:00:00 WARNING  rewards_tasks: Microsoft Rewards is NOT signed in "
        "on rewards.bing.com for this profile!"
    )
    assert state.account_stats["default"]["not_signed_in"] is True


def test_upstream_warning_captured_as_notice():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("12:00:00 WARNING  rewards_tasks: Search quota not filled: 30/60")
    notices = state.account_stats["default"]["notices"]
    assert any("Search quota not filled" in n for n in notices)


def test_diagnostics_flags_not_signed_in(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    now = dt.datetime.now()
    runner.save_history_entry(
        {
            "start_time": now.isoformat(),
            "end_time": now.isoformat(),
            "duration": "10s",
            "accounts": ["default"],
            "stats": {
                "default": {
                    "tasks": {"Bing daily set": "SKIP"},
                    "not_signed_in": True,
                    "points_gained": 0,
                }
            },
            "exit_code": 0,
            "log_file": "run_20250101_030000.log",
        }
    )
    diag = runner.get_diagnostics()
    assert diag["not_signed_in_recent"] == 1
    assert any("NOT signed in" in s for s in diag["suggestions"])


def test_lifetime_stats_not_inflated_by_quota_position(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    now = dt.datetime.now()
    entry = {
        "start_time": now.isoformat(),
        "end_time": now.isoformat(),
        "duration": "10s",
        "accounts": ["default"],
        # 30/30 with 0 gained must count 0, not 30.
        "stats": {
            "default": {
                "search_points": "30/30",
                "initial_points": 30,
                "current_points": 30,
                "max_points": 30,
                "points_gained": 0,
            }
        },
        "exit_code": 0,
        "log_file": "run_20250101_030000.log",
    }
    runner.save_history_entry(entry)
    stats = runner.get_lifetime_stats()
    assert stats["total_points_gained"] == 0
    assert stats["today_points_gained"] == 0


def test_smart_skip_when_quota_complete_and_empty(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    now = dt.datetime.now()
    entry = {
        "start_time": (now - dt.timedelta(hours=1)).isoformat(),
        "end_time": (now - dt.timedelta(minutes=50)).isoformat(),
        "duration": "10s",
        "accounts": ["default"],
        "stats": {
            "default": {
                "search_points": "30/30",
                "initial_points": 30,
                "current_points": 30,
                "max_points": 30,
                "points_gained": 0,
            }
        },
        "exit_code": 0,
        "log_file": "run_20250101_030000.log",
    }
    runner.save_history_entry(entry)
    decision = runner.should_skip_scheduled_run(now=now)
    assert decision["skip"] is True

    # A run that earned points must not skip.
    entry["stats"]["default"]["points_gained"] = 60
    runner.save_history_entry(entry)
    decision2 = runner.should_skip_scheduled_run(now=now)
    assert decision2["skip"] is False


def test_diagnostics_flags_visual_skip_and_empty(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    now = dt.datetime.now()
    for i in range(3):
        runner.save_history_entry(
            {
                "start_time": (now - dt.timedelta(hours=i)).isoformat(),
                "end_time": (now - dt.timedelta(hours=i)).isoformat(),
                "duration": "10s",
                "accounts": ["default"],
                "stats": {
                    "default": {
                        "tasks": {"Visual search": "SKIP"},
                        "search_points": "30/30",
                        "points_gained": 0,
                        "warnings": [],
                    }
                },
                "exit_code": 0,
                "log_file": f"run_2025010{i}_030000.log",
            }
        )
    diag = runner.get_diagnostics()
    assert diag["visual_skip_recent"] == 3
    assert diag["empty_runs_recent"] == 3
    assert diag["tasks_ok_recent"] == 0
    assert diag["tasks_seen_recent"] == 3
    assert any("Visual search" in s for s in diag["suggestions"])
    # Nothing reported [OK], so the "selectors changed" hint must fire too.
    assert any("No task reported [OK]" in s for s in diag["suggestions"])


def test_diagnostics_no_ok_hint_absent_when_a_task_succeeds(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    now = dt.datetime.now()
    runner.save_history_entry(
        {
            "start_time": now.isoformat(),
            "end_time": now.isoformat(),
            "duration": "10s",
            "accounts": ["default"],
            "stats": {
                "default": {
                    "tasks": {"Bing daily set": "OK", "Visual search": "SKIP"},
                    "points_gained": 30,
                }
            },
            "exit_code": 0,
            "log_file": "run_20250101_030000.log",
        }
    )
    diag = runner.get_diagnostics()
    assert diag["tasks_ok_recent"] == 1
    assert not any("No task reported [OK]" in s for s in diag["suggestions"])


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


def test_history_write_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    runner.save_history_entry({"exit_code": 0, "stats": {}})
    assert (tmp_path / "history.json").exists()
    # No stray temp files left behind by the atomic replace.
    assert list(tmp_path.glob("history.*.tmp")) == []
    assert len(runner.get_history()) == 1


def test_parse_ignores_implausible_raw_points():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("[POINTS] Raw points earned this run: +8972 pts")
    assert state.account_stats["default"]["raw_points_earned"] is None
    assert state.account_stats["default"]["points_gained"] == 0


def test_parse_accepts_plausible_raw_points():
    _fresh()
    parse_log_line("=== account: default ===")
    parse_log_line("[POINTS] Raw points earned this run: +105 pts")
    assert state.account_stats["default"]["raw_points_earned"] == 105


def test_history_sanitizes_implausible_points(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    runner.save_history_entry(
        {"exit_code": 0, "stats": {"default": {"points_gained": 8972, "raw_points_earned": 8972}}}
    )
    entry = runner.get_history()[0]
    assert entry["stats"]["default"]["points_gained"] == 0
    assert entry["stats"]["default"]["raw_points_earned"] is None
    assert runner.get_lifetime_stats()["total_points_gained"] == 0


def test_llm_env_local_provider():
    cfg = AppConfig(
        query_source="llm",
        llm_provider="local",
        llm_base_url="http://ollama:11434/v1",
        llm_model="llama3.1",
        llm_api_key="secret",
    )
    env = _llm_env(cfg)
    assert env["LLM_PROVIDER"] == "local"
    assert env["LOCAL_LLM_BASE_URL"] == "http://ollama:11434/v1"
    assert env["LOCAL_LLM_MODEL"] == "llama3.1"
    assert env["LOCAL_LLM_API_KEY"] == "secret"
    assert "OPENROUTER_BASE_URL" not in env


def test_llm_env_openrouter_provider():
    cfg = AppConfig(
        query_source="llm",
        llm_provider="openrouter",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_model="openrouter/free",
        llm_api_key="key",
    )
    env = _llm_env(cfg)
    assert env["LLM_PROVIDER"] == "openrouter"
    assert env["OPENROUTER_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["OPENROUTER_MODEL"] == "openrouter/free"
    assert env["OPENROUTER_API_KEY"] == "key"


def test_llm_env_openrouter_extras():
    cfg = AppConfig(
        query_source="llm",
        llm_provider="openrouter",
        openrouter_http_referer="https://example.test",
        openrouter_title="rewards-farmer",
        llm_request_timeout=90,
    )
    env = _llm_env(cfg)
    assert env["OPENROUTER_HTTP_REFERER"] == "https://example.test"
    assert env["OPENROUTER_TITLE"] == "rewards-farmer"
    assert env["LLM_REQUEST_TIMEOUT_SECONDS"] == "90"


def test_llm_env_legacy_ollama_host_and_trends():
    # Old configs stored ollama_host; it must still reach upstream (new + old vars).
    cfg = AppConfig(query_source="llm", ollama_host="host.docker.internal:11434")
    env = _llm_env(cfg)
    assert env["LOCAL_LLM_BASE_URL"] == "host.docker.internal:11434"
    assert env["OLLAMA_HOST"] == "host.docker.internal:11434"
    # Trends mode must not leak any LLM configuration.
    assert _llm_env(AppConfig(query_source="trends")) == {}


def test_diagnostics_multi_account_ratios(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "history.json")
    now = dt.datetime.now()
    # Two accounts per run, both SKIP visual and quota complete. Ratios must be
    # computed against account-entries (6), not run count (3).
    for i in range(3):
        runner.save_history_entry(
            {
                "start_time": (now - dt.timedelta(hours=i)).isoformat(),
                "end_time": (now - dt.timedelta(hours=i)).isoformat(),
                "duration": "10s",
                "accounts": ["default", "spare"],
                "stats": {
                    "default": {
                        "tasks": {"Visual search": "SKIP"},
                        "search_points": "30/30",
                        "points_gained": 0,
                    },
                    "spare": {
                        "tasks": {"Visual search": "SKIP"},
                        "search_points": "30/30",
                        "points_gained": 0,
                    },
                },
                "exit_code": 0,
                "log_file": f"run_2025010{i}_030000.log",
            }
        )
    diag = runner.get_diagnostics()
    assert diag["visual_skip_recent"] == 6
    assert any("Visual search" in s for s in diag["suggestions"])
    assert any("30 is the full Silver daily cap" in s for s in diag["suggestions"])
