import asyncio
import datetime
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
import httpx

from server.config import (
    UPSTREAM_DIR,
    DATA_DIR,
    get_config,
    AppConfig,
)
from server.upstream_manager import ensure_upstream
from server.account_checker import check_account_login

LOGS_DIR = DATA_DIR / "logs"
HISTORY_FILE = LOGS_DIR / "history.json"

TASK_DAILY_SET = "Bing daily set"
TASK_EXPLORE = "Explore on Bing"
TASK_VISUAL = "Visual search"
TASK_MISC = "Misc cards"
TASK_SEARCHES = "Required searches"
TASK_BONUS = "Bonus points"

TASK_NAMES = [
    TASK_DAILY_SET,
    TASK_EXPLORE,
    TASK_VISUAL,
    TASK_MISC,
    TASK_SEARCHES,
    TASK_BONUS,
]


class RunState:
    def __init__(self):
        self.is_running: bool = False
        self.process: Optional[asyncio.subprocess.Process] = None
        self.current_account: Optional[str] = None
        self.start_time: Optional[str] = None
        self.accounts_in_run: List[str] = []
        self.account_stats: Dict[str, Dict[str, Any]] = {}
        self.recent_logs: List[str] = []
        self.active_log_file: Optional[Path] = None

    def reset(self):
        self.is_running = False
        self.process = None
        self.current_account = None
        self.start_time = None
        self.accounts_in_run = []
        self.account_stats = {}
        self.active_log_file = None


state = RunState()
log_subscribers: List[asyncio.Queue] = []

# Serializes run setup so two simultaneous POST /api/run calls can't both
# pass the is_running check and launch duplicate automation processes.
_run_lock = asyncio.Lock()

# Cap per-client websocket backlog so a slow/disconnected browser tab can't
# grow server memory without bound; oldest lines are dropped first.
MAX_CLIENT_QUEUE = 500

# Only files matching this pattern may be served via /api/logs/{filename}.
LOG_FILENAME_RE = re.compile(r"^run_\d{8}_\d{6}\.log$")
MAX_KEPT_LOG_FILES = 30


def is_safe_log_filename(filename: str) -> bool:
    return bool(LOG_FILENAME_RE.match(Path(filename).name)) and filename == Path(filename).name


def _prune_old_logs() -> None:
    """Delete oldest run_*.log files, keeping disk usage bounded."""
    try:
        logs = sorted(LOGS_DIR.glob("run_*.log"), key=lambda p: p.name)
        for old in logs[:-MAX_KEPT_LOG_FILES] if len(logs) > MAX_KEPT_LOG_FILES else []:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def get_history() -> List[Dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def get_log_content(filename: str) -> Optional[str]:
    safe_name = Path(filename).name
    log_path = LOGS_DIR / safe_name
    if not log_path.exists() or not log_path.is_file():
        return None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Error reading log file: {e}"


def get_lifetime_stats() -> Dict[str, Any]:
    history = get_history()
    total_runs = len(history)
    successful_runs = sum(1 for r in history if r.get("exit_code") == 0)
    total_points = 0
    today_points = 0
    # start_time values are recorded with the local clock (datetime.now()),
    # so compare against the local date rather than UTC.
    today_prefix = datetime.datetime.now().strftime("%Y-%m-%d")

    for r in history:
        stats = r.get("stats", {})
        is_today = (r.get("start_time") or "").startswith(today_prefix)
        for acc, st in stats.items():
            pts = st.get("points_gained")
            if pts is None:
                # Legacy entries (pre-warnings) stored no points_gained and
                # only a "30/30" style search_points string. The first number
                # is the lifetime quota position, NOT points earned that run,
                # so counting it inflates totals (every 30/30 run counted +30).
                # Prefer measured search diff, else raw balance diff, else 0.
                try:
                    raw = st.get("raw_points_earned")
                    if raw is not None:
                        pts = int(raw)
                    elif st.get("current_points") is not None and st.get("initial_points") is not None:
                        pts = max(0, int(st["current_points"]) - int(st["initial_points"]))
                    else:
                        pts = 0
                except (TypeError, ValueError):
                    pts = 0
            try:
                pts = int(pts)
            except (TypeError, ValueError):
                pts = 0
            total_points += pts
            if is_today:
                today_points += pts

    return {
        "total_runs": total_runs,
        "successful_runs": successful_runs,
        "total_points_gained": total_points,
        "today_points_gained": today_points,
    }


def save_history_entry(entry: Dict[str, Any]):
    history = get_history()
    history.insert(0, entry)
    history = history[:100]  # keep last 100 runs
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    _prune_old_logs()


def should_skip_scheduled_run(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Decide whether a scheduled (not manual) run should be skipped.

    Skips when the most recent run ended recently with search quota complete
    and 0 raw points earned: the daily cap is hit, so another run now can
    only earn 0 while adding automation footprint. Manual runs always proceed;
    callers (scheduler) check this, start_run() does not.
    """
    cfg = get_config()
    sched = cfg.schedule
    if not getattr(sched, "skip_empty_runs", True):
        return {"skip": False, "reason": "smart-skip disabled"}
    history = get_history()
    if not history:
        return {"skip": False, "reason": "no previous runs"}
    last = history[0]
    try:
        end = datetime.datetime.fromisoformat(last.get("end_time") or last.get("start_time") or "")
    except (ValueError, TypeError):
        return {"skip": False, "reason": "last run time unparseable"}
    now = now or datetime.datetime.now()
    try:
        hours = getattr(sched, "empty_skip_hours", 12)
        window = datetime.timedelta(hours=max(1, int(hours)))
    except (TypeError, ValueError):
        window = datetime.timedelta(hours=12)
    if now - end > window:
        return {"skip": False, "reason": "last run outside skip window"}

    stats = last.get("stats", {})
    if not stats:
        return {"skip": False, "reason": "no stats in last run"}
    total_gained = 0
    quota_complete_all = True
    for st in stats.values():
        try:
            total_gained += int(st.get("points_gained", 0) or 0)
        except (TypeError, ValueError):
            pass
        sp = st.get("search_points")
        init_p, curr_p, max_p = st.get("initial_points"), st.get("current_points"), st.get("max_points")
        if sp and "/" in str(sp):
            try:
                a, b = str(sp).split("/")
                if int(a) < int(b):
                    quota_complete_all = False
            except (TypeError, ValueError):
                pass
        elif max_p is not None and curr_p is not None:
            try:
                if int(curr_p) < int(max_p):
                    quota_complete_all = False
            except (TypeError, ValueError):
                pass
        else:
            # No quota info (e.g. searches never reached) -> don't skip.
            quota_complete_all = False
        _ = init_p
    if total_gained > 0:
        return {"skip": False, "reason": f"last run earned +{total_gained}"}
    if not quota_complete_all:
        return {"skip": False, "reason": "search quota not complete"}
    return {
        "skip": True,
        "reason": f"last run {last.get('log_file', '')} earned 0 with quota complete ({window} window)",
        "last_log": last.get("log_file"),
    }


def get_diagnostics() -> Dict[str, Any]:
    """Aggregate failure patterns across recent history for the dashboard.

    Answers "are we hitting the correct spots" without launching a browser:
    visual SKIP rate, explore/misc incomplete-card frequency, empty-run rate,
    and actionable next steps. Cheap to compute from history.json.
    """
    history = get_history()
    total = len(history)
    recent = history[:20]
    visual_skip = sum(
        1 for r in recent
        for st in (r.get("stats", {}) or {}).values()
        if isinstance(st, dict) and (st.get("tasks") or {}).get(TASK_VISUAL) == "SKIP"
    )
    explore_ok_with_issues = 0
    explore_warn_examples: List[str] = []
    misc_warn_examples: List[str] = []
    empty_runs = 0
    quota_complete_runs = 0
    accounts_seen: List[str] = []
    for r in recent:
        stats = r.get("stats", {}) or {}
        gained = 0
        for acc, st in stats.items():
            if acc not in accounts_seen:
                accounts_seen.append(acc)
            if not isinstance(st, dict):
                continue
            try:
                gained += int(st.get("points_gained", 0) or 0)
            except (TypeError, ValueError):
                pass
            warns = st.get("warnings") or []
            has_explore_warn = any(str(w).startswith("Explore card") for w in warns)
            has_misc_warn = any(str(w).startswith("Misc card") for w in warns)
            tasks = st.get("tasks") or {}
            if tasks.get(TASK_EXPLORE) == "OK" and has_explore_warn:
                explore_ok_with_issues += 1
            for w in warns:
                if str(w).startswith("Explore card") and len(explore_warn_examples) < 3:
                    if str(w) not in explore_warn_examples:
                        explore_warn_examples.append(str(w))
                if str(w).startswith("Misc card") and len(misc_warn_examples) < 3:
                    if str(w) not in misc_warn_examples:
                        misc_warn_examples.append(str(w))
            sp = st.get("search_points")
            if sp and "/" in str(sp):
                try:
                    a, b = str(sp).split("/")
                    if int(a) >= int(b):
                        quota_complete_runs += 1
                except (TypeError, ValueError):
                    pass
        if gained == 0 and r.get("exit_code") == 0:
            empty_runs += 1

    suggestions: List[str] = []
    if recent and visual_skip == len(recent):
        suggestions.append(
            "Visual search SKIP on every recent run: this market variant has no "
            "'visual search streak' entry (see upstream issue #80). No action in "
            "automation - verify once manually whether rewards.bing.com/earn shows "
            "a Visual Search streak; if not, this SKIP is correct, not a miss."
        )
    if explore_ok_with_issues:
        suggestions.append(
            f"Explore on Bing reports [OK] but {explore_ok_with_issues}/{len(recent)} recent "
            "runs left cards incomplete after searching. Those searches counted toward "
            "the daily quota without clearing the card - check the card descriptions "
            "manually once; if they need a specific click-through (not just a search), "
            "upstream needs a selector fix (paste diagnostics into a rewards-farmer issue)."
        )
    if misc_warn_examples:
        suggestions.append(
            "Misc cards incomplete (e.g. 'Download the Bing app' promos) cannot complete "
            "from headless Edge - expected. They are correctly reported as warnings, not failures."
        )
    if recent and empty_runs >= max(3, len(recent) // 2):
        suggestions.append(
            f"{empty_runs}/{len(recent)} recent runs earned 0 pts (quota already complete). "
            "Enable smart-skip (schedule.skip_empty_runs) so the scheduler stops firing "
            "empty runs every 3h - they add ban-surface for zero gain. Manual runs always work."
        )
    if recent and quota_complete_runs == len(recent):
        suggestions.append(
            "Search quota reads complete (e.g. 30/30) on every recent run. Under the 2026 "
            "Member/Silver/Gold system 30 is the full Silver daily cap (shared PC+mobile), "
            "not a bug - the old 90/150 Level-2 caps no longer apply in migrated regions."
        )

    return {
        "total_runs_analyzed": total,
        "recent_runs_analyzed": len(recent),
        "accounts": accounts_seen,
        "visual_skip_recent": visual_skip,
        "explore_ok_with_issues_recent": explore_ok_with_issues,
        "empty_runs_recent": empty_runs,
        "explore_examples": explore_warn_examples,
        "misc_examples": misc_warn_examples,
        "suggestions": suggestions,
    }


async def broadcast_line(line: str):
    state.recent_logs.append(line)
    if len(state.recent_logs) > 1000:
        state.recent_logs.pop(0)

    for queue in list(log_subscribers):
        try:
            if queue.qsize() >= MAX_CLIENT_QUEUE:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(line)
        except Exception:
            pass


async def send_webhook(title: str, description: str, color: int = 3447003):
    config = get_config()
    if not config.webhook_url:
        return

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    discord_payload = {
        "embeds": [
            {
                "title": f"🌾 Rewards Farmer: {title}",
                "description": description,
                "color": color,
                "timestamp": timestamp,
            }
        ]
    }
    # Plain-text fallback for generic webhook receivers (Gotify, ntfy, ...).
    plain_text = f"Rewards Farmer: {title}\n{description}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(config.webhook_url, json=discord_payload)
            if resp.status_code >= 400:
                await client.post(config.webhook_url, json={"text": plain_text})
    except Exception as e:
        print(f"Failed to send webhook: {e}")


def init_account_stat_entry(name: str) -> Dict[str, Any]:
    return {
        "tasks": {t: "PENDING" for t in TASK_NAMES},
        "task_points": 0,
        "search_points": None,
        "initial_points": None,
        "current_points": None,
        "max_points": None,
        "initial_balance": None,
        "final_balance": None,
        "raw_points_earned": None,
        "points_gained": 0,
        # Card-level issues within an [OK] task, e.g. "Explore card X not
        # complete after searching". Upstream marks the task [OK] anyway, so
        # without this the dashboard reports full success while points are
        # left behind. Kept as a plain list of short strings for the API/UI.
        "warnings": [],
        "incomplete_cards": 0,
        "current_step": "Initializing browser...",
        "step_index": 0,
        "total_steps": 6,
        "status": "QUEUED",
    }


def _recalc_points(acc_entry: Dict[str, Any]):
    """Honest points math: raw balance diff is truth, search diff is fallback.

    The old version added hardcoded pre-2026 task estimates (30+40+5+20+10)
    on top of the search diff, so a run where every task reported [OK] but
    earned nothing still showed +100 pts. Since the 2026 Member/Silver/Gold
    overhaul those estimates are wrong anyway (daily set 30->15, caps
    15/30/60 shared across PC+mobile). Now:
      - raw balance diff (from farm_wrapper [POINTS] lines) wins when present,
      - otherwise use measured search progress only (current - initial),
      - task_points is kept as a count of OK tasks for display, not earnings.
    """
    if acc_entry.get("raw_points_earned") is not None:
        acc_entry["points_gained"] = acc_entry["raw_points_earned"]
        return

    tasks = acc_entry.get("tasks", {})
    ok_count = sum(1 for v in tasks.values() if v == "OK")
    # Informational only: number of tasks reporting OK, not points.
    acc_entry["task_points"] = ok_count

    s_pts = 0
    init_s = acc_entry.get("initial_points")
    curr_s = acc_entry.get("current_points")
    if curr_s is not None and init_s is not None:
        try:
            s_pts = max(0, int(curr_s) - int(init_s))
        except (TypeError, ValueError):
            s_pts = 0

    acc_entry["points_gained"] = s_pts


def parse_log_line(line: str):
    """Extract account transitions, points, current step and task outcomes from stdout."""
    # Check account switch: === account: <name> ===
    acc_match = re.search(r"===\s*account:\s*([^\s=]+)\s*===", line)
    if acc_match:
        account_name = acc_match.group(1).strip()
        state.current_account = account_name
        if account_name not in state.account_stats:
            state.account_stats[account_name] = init_account_stat_entry(account_name)
        state.account_stats[account_name]["status"] = "RUNNING"
        state.account_stats[account_name]["current_step"] = "Bing daily set (quizzes & polls)"
        state.account_stats[account_name]["step_index"] = 1
        return

    # If single account without header
    if state.current_account is None and state.accounts_in_run:
        state.current_account = state.accounts_in_run[0]
        if state.current_account not in state.account_stats:
            state.account_stats[state.current_account] = init_account_stat_entry(state.current_account)
        state.account_stats[state.current_account]["status"] = "RUNNING"
        state.account_stats[state.current_account]["current_step"] = "Bing daily set (quizzes & polls)"
        state.account_stats[state.current_account]["step_index"] = 1

    curr = state.current_account
    if not curr or curr not in state.account_stats:
        return

    acc_entry = state.account_stats[curr]

    # Check raw balance telemetry from farm_wrapper:
    # "[POINTS] Balance before run: 14250 pts"
    bal_before_match = re.search(r"\[POINTS\] Balance before run:\s*(\d+)\s*pts", line)
    if bal_before_match:
        acc_entry["initial_balance"] = int(bal_before_match.group(1))

    # "[POINTS] Balance after run: 14410 pts"
    bal_after_match = re.search(r"\[POINTS\] Balance after run:\s*(\d+)\s*pts", line)
    if bal_after_match:
        acc_entry["final_balance"] = int(bal_after_match.group(1))

    # "[POINTS] Raw points earned this run: +160 pts"
    raw_pts_match = re.search(r"\[POINTS\] Raw points earned this run:\s*\+(\d+)\s*pts", line)
    if raw_pts_match:
        raw_val = int(raw_pts_match.group(1))
        acc_entry["raw_points_earned"] = raw_val
        acc_entry["points_gained"] = raw_val

    # Card-level misses inside an otherwise [OK] task. These are the "not
    # hitting the correct spots" lines, e.g.:
    #   Explore on Bing Card [desc='...'] is not complete after searching.
    #   Misc Card [desc='...'] is not complete after clicking.
    # Track them so the dashboard can show PARTIAL instead of false-OK.
    if "is not complete after" in line:
        desc_m = re.search(r"\[desc='([^']+)'\]", line)
        kind = "Explore card" if "Explore on Bing Card" in line else (
            "Misc card" if "Misc Card" in line else "Card"
        )
        short = desc_m.group(1)[:80] if desc_m else line.strip()[:80]
        entry = f"{kind}: {short}"
        warnings = acc_entry.setdefault("warnings", [])
        if entry not in warnings:
            warnings.append(entry)
            # Cap memory: keep most recent 20.
            del warnings[:-20]
        acc_entry["incomplete_cards"] = len(warnings)
        # Demote a false [OK] later? No - upstream emits [OK] after these
        # warnings, so instead the API/UI uses incomplete_cards to display
        # "OK with issues". Recalc to keep points honest (search diff only).
        _recalc_points(acc_entry)

    # Task tags: [OK], [SKIP], [FAIL]
    for tag in ["[OK]", "[SKIP]", "[FAIL]"]:
        if tag in line:
            clean_tag = tag[1:-1]
            for task_name in TASK_NAMES:
                if task_name.lower() in line.lower():
                    acc_entry["tasks"][task_name] = clean_tag
            _recalc_points(acc_entry)

    # Step progress tracking based on completed tasks. Advance on any terminal
    # tag ([OK]/[SKIP]/[FAIL]) so a failed task doesn't stall the banner.
    def _tagged(task: str) -> bool:
        return any(f"[{t}] {task}" in line for t in ("OK", "SKIP", "FAIL"))

    if _tagged(TASK_DAILY_SET):
        acc_entry["current_step"] = "Explore on Bing (promotional cards)"
        acc_entry["step_index"] = 2
    elif _tagged(TASK_EXPLORE):
        acc_entry["current_step"] = "Visual search"
        acc_entry["step_index"] = 3
    elif _tagged(TASK_VISUAL):
        acc_entry["current_step"] = "Misc cards"
        acc_entry["step_index"] = 4
    elif _tagged(TASK_MISC):
        acc_entry["current_step"] = "Required searches (measuring quota breakdown)"
        acc_entry["step_index"] = 5

    # Check search points breakdown before searches: "Search points before: 15/90"
    sp_before = re.search(r"Search points before:\s*(\d+)/(\d+)", line)
    if sp_before:
        earned = int(sp_before.group(1))
        max_pts = int(sp_before.group(2))
        acc_entry["initial_points"] = earned
        acc_entry["current_points"] = earned
        acc_entry["max_points"] = max_pts
        acc_entry["search_points"] = f"{earned}/{max_pts}"
        acc_entry["current_step"] = f"Required searches: starting at {earned}/{max_pts} pts"
        _recalc_points(acc_entry)

    # Check search round progress: "Round 1: 5 searches -> 30/90"
    round_match = re.search(r"Round\s*(\d+):\s*(\d+)\s*searches\s*->\s*(\d+)/(\d+)", line)
    if round_match:
        rnd = round_match.group(1)
        earned = int(round_match.group(3))
        max_pts = int(round_match.group(4))
        init_pts = acc_entry.get("initial_points")
        if init_pts is None:
            init_pts = earned
            acc_entry["initial_points"] = init_pts
        acc_entry["current_points"] = earned
        acc_entry["max_points"] = max_pts
        acc_entry["search_points"] = f"{earned}/{max_pts}"
        acc_entry["current_step"] = f"Searching Bing (Round {rnd}: {earned}/{max_pts} pts)"
        _recalc_points(acc_entry)

    # Check quota completions
    quota_match = re.search(r"Search quota (?:complete|not filled):\s*(\d+)/(\d+)", line)
    if quota_match:
        earned = int(quota_match.group(1))
        max_pts = int(quota_match.group(2))
        init_pts = acc_entry.get("initial_points") or 0
        acc_entry["current_points"] = earned
        acc_entry["max_points"] = max_pts
        acc_entry["search_points"] = f"{earned}/{max_pts}"
        _recalc_points(acc_entry)

    if _tagged(TASK_SEARCHES):
        acc_entry["current_step"] = "Claiming bonus points"
        acc_entry["step_index"] = 6
        _recalc_points(acc_entry)
    elif _tagged(TASK_BONUS):
        acc_entry["current_step"] = "Finished"
        acc_entry["status"] = "COMPLETED"
        _recalc_points(acc_entry)


async def start_run(accounts: Optional[List[str]] = None) -> Dict[str, Any]:
    async with _run_lock:
        return await _start_run_locked(accounts)


async def _start_run_locked(accounts: Optional[List[str]] = None) -> Dict[str, Any]:
    if state.is_running:
        return {"success": False, "error": "A run is already currently in progress."}

    # The interactive browser holds the same Edge profile open; running
    # automation against it concurrently would corrupt session cookies.
    try:
        from server.vnc_manager import get_vnc_status

        vnc = get_vnc_status()
        if vnc.get("active"):
            return {
                "success": False,
                "error": f"Cannot start automation while the interactive browser is open for '{vnc.get('account')}'. Click 'Finish & Save Login' first.",
            }
    except ImportError:
        pass

    # Make sure upstream exists (blocking git I/O: keep off the event loop)
    info = await asyncio.to_thread(ensure_upstream)
    if not info.get("installed"):
        return {"success": False, "error": f"Upstream repo not ready: {info.get('error')}"}

    config = get_config()
    target_accounts = accounts if accounts else config.accounts

    if not target_accounts:
        return {"success": False, "error": "No accounts configured to run."}

    # Verify that requested accounts are logged in first (blocking sqlite I/O).
    auth_results = await asyncio.gather(
        *(asyncio.to_thread(check_account_login, acc) for acc in target_accounts)
    )
    unauthenticated = []
    for acc, auth_info in zip(target_accounts, auth_results):
        if not auth_info.get("logged_in"):
            unauthenticated.append(f"{acc} ({auth_info.get('reason', 'Sign-in required')})")

    if unauthenticated:
        err_detail = "Cannot start automation. The following account(s) are not signed in yet:\n- " + "\n- ".join(unauthenticated) + "\n\nPlease click 'Interactive Login' to sign in to Microsoft Rewards first."
        return {"success": False, "error": err_detail}

    state.reset()
    state.is_running = True
    state.start_time = datetime.datetime.now().isoformat()
    state.accounts_in_run = target_accounts
    for acc in target_accounts:
        state.account_stats[acc] = init_account_stat_entry(acc)

    # Setup log file (ensure the directory exists before the child writes to it)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"run_{timestamp}.log"
    state.active_log_file = log_file

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["REWARDS_HEADLESS"] = "1"
    env["SE_AVOID_STATS"] = "true"  # Suppresses Plausible analytics warning in selenium-manager
    env["QUERY_SOURCE"] = config.query_source
    env["REWARDS_ACCOUNTS"] = ",".join(target_accounts)
    env["REWARDS_FARMER_LOG_LEVEL"] = config.log_level
    # NOTE: intentionally NOT setting REWARDS_FARMER_LOG_FILE. Upstream would
    # then write to both stdout and the file, while this runner tees stdout
    # to the same file below -> every line duplicated, and print() telemetry
    # like [POINTS] (which bypasses logging) would still be missing from disk.
    # Single-writer instead: upstream logs to stdout only, we persist it.
    env.pop("REWARDS_FARMER_LOG_FILE", None)
    env["LANG"] = "en_US.UTF-8"
    if config.ollama_host:
        env["OLLAMA_HOST"] = config.ollama_host

    asyncio.create_task(_run_process(env, target_accounts, log_file))
    return {"success": True, "message": f"Run started for accounts: {', '.join(target_accounts)}"}


def _append_to_log_file(log_file: Path, line: str) -> None:
    """Best-effort append of one stdout line to the persisted run log."""
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


async def _run_process(env: Dict[str, str], target_accounts: List[str], log_file: Path):
    start_marker = f"--- [REWARDS-FARMER-SERVER] Starting run for: {', '.join(target_accounts)} ---"
    note = "[NOTE] Running in human-simulation mode (human-like mouse curves & keystroke variance to prevent bot bans). Each task takes 30-90s."
    await broadcast_line(start_marker)
    await broadcast_line(note)
    _append_to_log_file(log_file, start_marker)
    _append_to_log_file(log_file, note)
    await send_webhook("Run Started", f"Accounts: `{', '.join(target_accounts)}`", color=3447003)

    wrapper_py = Path(__file__).resolve().parent / "farm_wrapper.py"
    target_script = str(wrapper_py) if wrapper_py.exists() else str(UPSTREAM_DIR / "src" / "main.py")

    pythonpath_parts = [str(UPSTREAM_DIR / "src"), str(Path(__file__).resolve().parent)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = ":".join(pythonpath_parts)

    try:
        state.process = await asyncio.create_subprocess_exec(
            "python3",
            target_script,
            cwd=str(UPSTREAM_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        while True:
            line_bytes = await state.process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            parse_log_line(line)
            await broadcast_line(line)
            _append_to_log_file(log_file, line)

        returncode = await state.process.wait()
    except Exception as exc:
        err_msg = f"[FAIL] Server runner encountered an error: {exc}"
        await broadcast_line(err_msg)
        _append_to_log_file(log_file, err_msg)
        returncode = -1

    end_time = datetime.datetime.now().isoformat()
    duration = "N/A"
    try:
        st = datetime.datetime.fromisoformat(state.start_time)
        et = datetime.datetime.fromisoformat(end_time)
        duration = f"{int((et - st).total_seconds())}s"
    except Exception:
        pass

    summary_entry = {
        "start_time": state.start_time,
        "end_time": end_time,
        "duration": duration,
        "accounts": target_accounts,
        "stats": state.account_stats,
        "exit_code": returncode,
        "log_file": log_file.name,
    }
    save_history_entry(summary_entry)

    status_str = "Completed successfully" if returncode == 0 else f"Finished with exit code {returncode}"
    
    # Calculate net points gained during run (raw balance diff when available,
    # else measured search progress - never inflated task estimates).
    total_gained = 0
    total_issues = 0
    for acc in state.account_stats.values():
        try:
            total_gained += int(acc.get("points_gained", 0) or 0)
        except (TypeError, ValueError):
            pass
        total_issues += int(acc.get("incomplete_cards", 0) or 0)
    pts_str = f" (+{total_gained} raw pts earned)" if total_gained > 0 else ""
    issues_str = f" ({total_issues} card(s) need attention)" if total_issues else ""

    end_marker = f"--- [REWARDS-FARMER-SERVER] Run ended: {status_str} ({duration}){pts_str}{issues_str} ---"
    await broadcast_line(end_marker)
    _append_to_log_file(log_file, end_marker)
    await send_webhook(
        "Run Finished",
        f"Status: **{status_str}**\nDuration: `{duration}`{pts_str}{issues_str}\nAccounts: `{', '.join(target_accounts)}`",
        color=5763719 if returncode == 0 else 15548997,
    )

    state.is_running = False
    state.process = None


async def stop_run() -> Dict[str, Any]:
    if not state.is_running or not state.process:
        return {"success": False, "error": "No run is currently in progress."}

    try:
        state.process.send_signal(signal.SIGINT)
        await broadcast_line("--- [REWARDS-FARMER-SERVER] Sent stop signal (SIGINT)... ---")
        return {"success": True, "message": "Stop signal sent."}
    except Exception as e:
        try:
            state.process.terminate()
        except Exception:
            pass
        return {"success": False, "error": str(e)}
