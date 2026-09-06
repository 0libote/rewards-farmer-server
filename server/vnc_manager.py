import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Any

from server.config import PROFILES_DIR, DATA_DIR
from server.runner import state as runner_state

DISPLAY = ":99"
VNC_PORT = 5900
WEBSOCKIFY_PORT = int(os.getenv("VNC_PORT", "6345"))


class VncSessionState:
    def __init__(self):
        self.active: bool = False
        self.account: Optional[str] = None
        self.xvfb_proc: Optional[subprocess.Popen] = None
        self.fluxbox_proc: Optional[subprocess.Popen] = None
        self.x11vnc_proc: Optional[subprocess.Popen] = None
        self.websockify_proc: Optional[subprocess.Popen] = None
        self.edge_proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None


vnc_state = VncSessionState()


def get_profile_dir(account_name: str) -> Path:
    if account_name == "default":
        return PROFILES_DIR.resolve()
    return (PROFILES_DIR / account_name).resolve()


def clean_chromium_locks(profile_dir: Path):
    """Remove stale SingletonLock if Edge crashed previously."""
    for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lock_path = profile_dir / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            try:
                lock_path.unlink()
                print(f"Removed stale Chromium lock: {lock_path}")
            except Exception as e:
                print(f"Could not remove {lock_path}: {e}")


def is_process_running(proc: Optional[subprocess.Popen]) -> bool:
    return proc is not None and proc.poll() is None


def start_vnc_session(account_name: str) -> Dict[str, Any]:
    """Starts an interactive browser session inside Xvfb + noVNC for logging in."""
    if runner_state.is_running:
        return {
            "success": False,
            "error": "Cannot open interactive browser while an automation run is active.",
        }

    if vnc_state.active:
        if vnc_state.account == account_name and is_process_running(vnc_state.edge_proc):
            return {
                "success": True,
                "message": f"Interactive session for {account_name} is already active.",
                "port": WEBSOCKIFY_PORT,
            }
        # Stop existing session first
        stop_vnc_session()

    profile_dir = get_profile_dir(account_name)
    profile_dir.mkdir(parents=True, exist_ok=True)
    clean_chromium_locks(profile_dir)

    logs_dir = DATA_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    edge_log_path = logs_dir / "edge_vnc.log"

    try:
        env = os.environ.copy()
        env["DISPLAY"] = DISPLAY

        # 1. Start Xvfb if not already running
        if not is_process_running(vnc_state.xvfb_proc):
            subprocess.run(["pkill", "-f", f"Xvfb {DISPLAY}"], stderr=subprocess.DEVNULL)
            vnc_state.xvfb_proc = subprocess.Popen(
                ["Xvfb", DISPLAY, "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)

        # 2. Start window manager (fluxbox)
        if not is_process_running(vnc_state.fluxbox_proc):
            vnc_state.fluxbox_proc = subprocess.Popen(
                ["fluxbox"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)

        # 3. Start x11vnc
        if not is_process_running(vnc_state.x11vnc_proc):
            subprocess.run(["pkill", "-f", f"x11vnc.*{DISPLAY}"], stderr=subprocess.DEVNULL)
            vnc_state.x11vnc_proc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    DISPLAY,
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-rfbport",
                    str(VNC_PORT),
                    "-quiet",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)

        # 4. Start websockify for noVNC
        if not is_process_running(vnc_state.websockify_proc):
            novnc_web_dir = "/usr/share/novnc"
            if not os.path.exists(novnc_web_dir):
                novnc_web_dir = "/app/novnc"

            vnc_state.websockify_proc = subprocess.Popen(
                [
                    "websockify",
                    "--web",
                    novnc_web_dir,
                    str(WEBSOCKIFY_PORT),
                    f"localhost:{VNC_PORT}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)

        # 5. Launch Microsoft Edge with flags required for headless Linux Docker
        edge_bin = "microsoft-edge"
        if not shutil_which(edge_bin):
            edge_bin = "microsoft-edge-stable"

        edge_cmd = [
            edge_bin,
            f"--user-data-dir={str(profile_dir)}",
            "--profile-directory=Default",
            "--no-sandbox",  # MANDATORY inside Docker containers running as root!
            "--test-type",   # Suppresses "You're using an unsupported command-line flag: --no-sandbox"
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-gpu",  # Virtual Xvfb display without hardware acceleration
            "--disable-software-rasterizer",
            "--password-store=basic",  # Avoids DBus keyring dependency inside minimal container
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=en-US",
            "--disable-features=Translate,OptimizationHints,MediaRouter,CommandLineFlagSecurityWarnings",
            "--window-position=0,0",
            "--window-size=1280,770",
            "--start-maximized",
            "https://rewards.bing.com/",
        ]

        with open(edge_log_path, "w", encoding="utf-8") as edge_log_file:
            vnc_state.edge_proc = subprocess.Popen(
                edge_cmd,
                env=env,
                stdout=edge_log_file,
                stderr=subprocess.STDOUT,
            )

        time.sleep(1.2)

        # Verify Edge didn't immediately exit
        if vnc_state.edge_proc.poll() is not None:
            err_output = ""
            try:
                with open(edge_log_path, "r", encoding="utf-8") as f:
                    err_output = f.read()[-500:]
            except Exception:
                pass
            return {
                "success": False,
                "error": f"Microsoft Edge exited immediately (code {vnc_state.edge_proc.returncode}): {err_output.strip()}",
            }

        vnc_state.active = True
        vnc_state.account = account_name
        vnc_state.started_at = time.time()

        return {
            "success": True,
            "message": f"Interactive session started for account '{account_name}'.",
            "account": account_name,
            "port": WEBSOCKIFY_PORT,
        }

    except Exception as e:
        stop_vnc_session()
        return {"success": False, "error": f"Failed to start interactive browser: {e}"}


def shutil_which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


def stop_vnc_session() -> Dict[str, Any]:
    """Gracefully terminates Edge and VNC stack to flush cookies to disk."""
    if not vnc_state.active and not is_process_running(vnc_state.edge_proc):
        return {"success": True, "message": "No session active."}

    account_name = vnc_state.account

    # Gracefully shut down Edge first so it flushes sqlite db and cookies
    if is_process_running(vnc_state.edge_proc):
        try:
            vnc_state.edge_proc.terminate()
            vnc_state.edge_proc.wait(timeout=5)
        except Exception:
            try:
                vnc_state.edge_proc.kill()
            except Exception:
                pass
        vnc_state.edge_proc = None

    # Kill leftover chromium processes
    subprocess.run(["pkill", "-f", "microsoft-edge"], stderr=subprocess.DEVNULL)

    # Clean locks
    if account_name:
        clean_chromium_locks(get_profile_dir(account_name))

    # Stop x11vnc, websockify, fluxbox, Xvfb
    for proc in [
        vnc_state.websockify_proc,
        vnc_state.x11vnc_proc,
        vnc_state.fluxbox_proc,
        vnc_state.xvfb_proc,
    ]:
        if is_process_running(proc):
            try:
                proc.terminate()
            except Exception:
                pass

    vnc_state.active = False
    vnc_state.account = None
    vnc_state.started_at = None
    vnc_state.edge_proc = None
    vnc_state.websockify_proc = None
    vnc_state.x11vnc_proc = None
    vnc_state.fluxbox_proc = None
    vnc_state.xvfb_proc = None

    return {"success": True, "message": f"Interactive session for '{account_name}' closed cleanly."}


def get_vnc_status() -> Dict[str, Any]:
    return {
        "active": vnc_state.active and is_process_running(vnc_state.edge_proc),
        "account": vnc_state.account,
        "port": WEBSOCKIFY_PORT,
    }
