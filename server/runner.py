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

TASK_NAMES = [
    "Bing daily set",
    "Explore on Bing",
    "Visual search",
    "Misc cards",
    "Required searches",
    "Bonus points",
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


def get_history() -> List[Dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history_entry(entry: Dict[str, Any]):
    history = get_history()
    history.insert(0, entry)
    history = history[:100]  # keep last 100 runs
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


async def broadcast_line(line: str):
    state.recent_logs.append(line)
    if len(state.recent_logs) > 1000:
        state.recent_logs.pop(0)

    for queue in list(log_subscribers):
        try:
            queue.put_nowait(line)
        except Exception:
            pass


async def send_webhook(title: str, description: str, color: int = 3447003):
    config = get_config()
    if not config.webhook_url:
        return

    payload = {
        "embeds": [
            {
                "title": f"🌾 Rewards Farmer: {title}",
                "description": description,
                "color": color,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(config.webhook_url, json=payload)
    except Exception as e:
        print(f"Failed to send webhook: {e}")


def parse_log_line(line: str):
    """Extract account transitions, points, and task outcomes from stdout."""
    # Check account switch: === account: <name> ===
    acc_match = re.search(r"===\s*account:\s*([^\s=]+)\s*===", line)
    if acc_match:
        account_name = acc_match.group(1).strip()
        state.current_account = account_name
        if account_name not in state.account_stats:
            state.account_stats[account_name] = {
                "tasks": {t: "PENDING" for t in TASK_NAMES},
                "search_points": None,
                "status": "RUNNING",
            }
        return

    # If single account without header
    if state.current_account is None and state.accounts_in_run:
        state.current_account = state.accounts_in_run[0]
        if state.current_account not in state.account_stats:
            state.account_stats[state.current_account] = {
                "tasks": {t: "PENDING" for t in TASK_NAMES},
                "search_points": None,
                "status": "RUNNING",
            }

    curr = state.current_account
    if not curr:
        return

    # Check task tags: [OK], [SKIP], [FAIL]
    for tag in ["[OK]", "[SKIP]", "[FAIL]"]:
        if tag in line:
            clean_tag = tag[1:-1]
            for task_name in TASK_NAMES:
                if task_name.lower() in line.lower():
                    state.account_stats[curr]["tasks"][task_name] = clean_tag

    # Check search points: "Search points before: 15/90" or "quota complete: 90/90"
    pts_match = re.search(r"(\d+)/(\d+)", line)
    if pts_match and ("search" in line.lower() or "round" in line.lower() or "quota" in line.lower()):
        earned, total = pts_match.groups()
        state.account_stats[curr]["search_points"] = f"{earned}/{total}"


async def start_run(accounts: Optional[List[str]] = None) -> Dict[str, Any]:
    if state.is_running:
        return {"success": False, "error": "A run is already currently in progress."}

    # Make sure upstream exists
    info = ensure_upstream()
    if not info.get("installed"):
        return {"success": False, "error": f"Upstream repo not ready: {info.get('error')}"}

    config = get_config()
    target_accounts = accounts if accounts else config.accounts

    if not target_accounts:
        return {"success": False, "error": "No accounts configured to run."}

    # Verify that requested accounts are logged in first
    unauthenticated = []
    for acc in target_accounts:
        auth_info = check_account_login(acc)
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
        state.account_stats[acc] = {
            "tasks": {t: "PENDING" for t in TASK_NAMES},
            "search_points": None,
            "status": "QUEUED",
        }

    # Setup log file
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"run_{timestamp}.log"
    state.active_log_file = log_file

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["REWARDS_HEADLESS"] = "1"
    env["QUERY_SOURCE"] = config.query_source
    env["REWARDS_ACCOUNTS"] = ",".join(target_accounts)
    env["REWARDS_FARMER_LOG_LEVEL"] = config.log_level
    env["REWARDS_FARMER_LOG_FILE"] = str(log_file)
    if config.ollama_host:
        env["OLLAMA_HOST"] = config.ollama_host

    asyncio.create_task(_run_process(env, target_accounts, log_file))
    return {"success": True, "message": f"Run started for accounts: {', '.join(target_accounts)}"}


async def _run_process(env: Dict[str, str], target_accounts: List[str], log_file: Path):
    await broadcast_line(f"--- [REWARDS-FARMER-SERVER] Starting run for: {', '.join(target_accounts)} ---")
    await send_webhook("Run Started", f"Accounts: `{', '.join(target_accounts)}`", color=3447003)

    main_py = UPSTREAM_DIR / "src" / "main.py"
    try:
        state.process = await asyncio.create_subprocess_exec(
            "python3",
            str(main_py),
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

    status_str = "Completed successfully" if returncode == 0 else f"Finished with warnings/exit code {returncode}"
    await broadcast_line(f"--- [REWARDS-FARMER-SERVER] Run ended: {status_str} ({duration}) ---")
    await send_webhook(
        "Run Finished",
        f"Status: **{status_str}**\nDuration: `{duration}`\nAccounts: `{', '.join(target_accounts)}`",
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
