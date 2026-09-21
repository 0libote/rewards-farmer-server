"""Run upstream's read-only selector check from the dashboard.

`check_selectors.py` walks every selector against the Rewards UI the account
actually gets and reports OK / ABSENT / FAILED. It completes no activities and
claims no points, so it is a safe way to answer "why did this run skip
everything?" without waiting for a full run.
"""
import asyncio
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from server.config import UPSTREAM_DIR
from server.account_checker import get_profile_dir
from server.vnc_manager import reset_edge_state

RUNNER = Path(__file__).resolve().parent / "selector_check_runner.py"
SERVER_DIR = Path(__file__).resolve().parent

# Selector checks open a browser, so they must not overlap a run or a VNC
# session. The endpoint enforces that; this flag lets start_run() refuse while
# a check is mid-flight.
_state: Dict[str, Any] = {
    "checking": False,
    "account": None,
    "output": None,
    "summary": None,
}
_lock = asyncio.Lock()

_SUMMARY_RE = re.compile(r"OK=(\d+)\s+ABSENT=(\d+)\s+FAILED=(\d+)")


def is_checking() -> bool:
    return bool(_state["checking"])


def get_state() -> Dict[str, Any]:
    return {
        "checking": _state["checking"],
        "account": _state["account"],
        "summary": _state["summary"],
    }


def _parse_summary(output: str) -> Optional[Dict[str, int]]:
    match = _SUMMARY_RE.search(output)
    if not match:
        return None
    return {
        "ok": int(match.group(1)),
        "absent": int(match.group(2)),
        "failed": int(match.group(3)),
    }


def _build_env(account: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["REWARDS_HEADLESS"] = "1"
    env["REWARDS_CHECK_PROFILE_DIR"] = str(get_profile_dir(account))
    # Single account so upstream's own logging does not print a header.
    env["REWARDS_ACCOUNTS"] = account
    if os.path.exists("/usr/local/bin/msedgedriver"):
        env["MSEDGEDRIVER_PATH"] = "/usr/local/bin/msedgedriver"
    env.pop("REWARDS_FARMER_LOG_FILE", None)
    env["PYTHONPATH"] = os.pathsep.join([str(UPSTREAM_DIR / "src"), str(SERVER_DIR)])
    return env


async def run_selector_check(account: str, timeout: int = 300) -> Dict[str, Any]:
    if _state["checking"]:
        return {"success": False, "error": "A selector check is already running."}

    async with _lock:
        if _state["checking"]:
            return {"success": False, "error": "A selector check is already running."}
        _state["checking"] = True
        _state["account"] = account
        _state["summary"] = None
        try:
            # A leftover Edge process would hold the profile and make the check
            # report everything absent.
            await asyncio.to_thread(reset_edge_state, [account])
            env = _build_env(account)
            proc = await asyncio.create_subprocess_exec(
                "python3",
                str(RUNNER),
                cwd=str(UPSTREAM_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"success": False, "error": f"Selector check timed out after {timeout}s."}

            output = out.decode("utf-8", errors="replace")
            summary = _parse_summary(output)
            _state["output"] = output
            _state["summary"] = summary
            return {
                "success": proc.returncode in (0, 1),  # 1 = some selectors FAILED
                "account": account,
                "exit_code": proc.returncode,
                "summary": summary,
                "output": output,
            }
        finally:
            _state["checking"] = False
