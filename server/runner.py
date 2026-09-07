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
                sp = st.get("search_points")
                if sp and "/" in sp:
                    try:
                        pts = int(sp.split("/")[0])
                    except Exception:
                        pts = 0
                else:
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
        "current_step": "Initializing browser...",
        "step_index": 0,
        "total_steps": 6,
        "status": "QUEUED",
    }


def _recalc_points(acc_entry: Dict[str, Any]):
    """Calculate raw points earned across all tasks + searches or use raw balance diff."""
    if acc_entry.get("raw_points_earned") is not None:
        acc_entry["points_gained"] = acc_entry["raw_points_earned"]
        return

    # Task completions awards
    t_pts = 0
    tasks = acc_entry.get("tasks", {})
    if tasks.get(TASK_DAILY_SET) == "OK":
        t_pts += 30
    if tasks.get(TASK_EXPLORE) == "OK":
        t_pts += 40
    if tasks.get(TASK_VISUAL) == "OK":
        t_pts += 5
    if tasks.get(TASK_MISC) == "OK":
        t_pts += 20
    if tasks.get(TASK_BONUS) == "OK":
        t_pts += 10

    # Search quota awards
    s_pts = 0
    init_s = acc_entry.get("initial_points")
    curr_s = acc_entry.get("current_points")
    if curr_s is not None and init_s is not None:
        s_pts = max(0, curr_s - init_s)

    acc_entry["task_points"] = t_pts
    acc_entry["points_gained"] = t_pts + s_pts


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
    env["REWARDS_FARMER_LOG_FILE"] = str(log_file)
    env["LANG"] = "en_US.UTF-8"
    if config.ollama_host:
        env["OLLAMA_HOST"] = config.ollama_host

    asyncio.create_task(_run_process(env, target_accounts, log_file))
    return {"success": True, "message": f"Run started for accounts: {', '.join(target_accounts)}"}


async def _run_process(env: Dict[str, str], target_accounts: List[str], log_file: Path):
    await broadcast_line(f"--- [REWARDS-FARMER-SERVER] Starting run for: {', '.join(target_accounts)} ---")
    await broadcast_line("[NOTE] Running in human-simulation mode (human-like mouse curves & keystroke variance to prevent bot bans). Each task takes 30-90s.")
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

        returncode = await state.process.wait()
    except Exception as exc:
        err_msg = f"[FAIL] Server runner encountered an error: {exc}"
        await broadcast_line(err_msg)
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
    
    # Calculate net points gained during run
    total_gained = sum(acc.get("points_gained", 0) for acc in state.account_stats.values())
    pts_str = f" (+{total_gained} raw pts earned)" if total_gained > 0 else ""

    await broadcast_line(f"--- [REWARDS-FARMER-SERVER] Run ended: {status_str} ({duration}){pts_str} ---")
    await send_webhook(
        "Run Finished",
        f"Status: **{status_str}**\nDuration: `{duration}`{pts_str}\nAccounts: `{', '.join(target_accounts)}`",
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
