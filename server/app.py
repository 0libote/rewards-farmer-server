import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.config import (
    get_config,
    save_config,
    AppConfig,
    VISUAL_SEARCH_IMAGE,
    NOUNS_FILE,
    PROFILES_DIR,
)
from server.upstream_manager import (
    ensure_upstream,
    update_upstream,
    get_upstream_info,
    generate_visual_search_image,
)
from server.runner import (
    state as runner_state,
    start_run,
    stop_run,
    get_history,
    get_lifetime_stats,
    log_subscribers,
)
from server.scheduler import init_scheduler, reload_schedule, get_schedule_info
from server.vnc_manager import start_vnc_session, stop_vnc_session, get_vnc_status
from server.account_checker import check_account_login

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    loop = asyncio.get_running_loop()
    print("[SERVER] Starting Rewards Farmer Server...")
    ensure_upstream()

    cfg = get_config()
    if cfg.auto_update_upstream:
        print("[SERVER] Checking for upstream updates...")
        update_upstream()

    init_scheduler(loop)
    yield
    # Shutdown
    print("[SERVER] Shutting down Rewards Farmer Server...")
    stop_vnc_session()
    await stop_run()


app = FastAPI(title="Rewards Farmer Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


# Request Models
class RunRequest(BaseModel):
    accounts: Optional[List[str]] = None


class VncStartRequest(BaseModel):
    account: str


class AccountActionRequest(BaseModel):
    account: str


class NounsUpdateRequest(BaseModel):
    content: str


# API Routes
@app.get("/")
async def serve_index():
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>Rewards Farmer Server</h1><p>Web dashboard is loading...</p>")


@app.get("/api/status")
async def get_system_status():
    cfg = get_config()
    account_statuses = {acc: check_account_login(acc) for acc in cfg.accounts}
    logged_in_count = sum(1 for st in account_statuses.values() if st.get("logged_in"))

    return {
        "runner": {
            "is_running": runner_state.is_running,
            "current_account": runner_state.current_account,
            "start_time": runner_state.start_time,
            "accounts": runner_state.accounts_in_run,
            "stats": runner_state.account_stats,
        },
        "lifetime_stats": get_lifetime_stats(),
        "vnc": get_vnc_status(),
        "upstream": get_upstream_info(),
        "scheduler": get_schedule_info(),
        "accounts": cfg.accounts,
        "account_statuses": account_statuses,
        "logged_in_count": logged_in_count,
        "total_accounts": len(cfg.accounts),
        "visual_search_ready": VISUAL_SEARCH_IMAGE.exists(),
    }


@app.post("/api/run")
async def trigger_run(req: RunRequest = RunRequest()):
    result = await start_run(accounts=req.accounts)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/stop")
async def trigger_stop():
    result = await stop_run()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/api/history")
async def fetch_history():
    return get_history()


@app.get("/api/config")
async def fetch_config():
    return get_config()


@app.post("/api/config")
async def update_config(new_config: AppConfig):
    save_config(new_config)
    reload_schedule()
    return {"success": True, "config": new_config}


@app.get("/api/upstream")
async def fetch_upstream():
    return get_upstream_info()


@app.post("/api/upstream/update")
async def trigger_upstream_update():
    return update_upstream()


@app.post("/api/vnc/start")
async def trigger_vnc_start(req: VncStartRequest):
    result = start_vnc_session(req.account)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/vnc/stop")
async def trigger_vnc_stop():
    return stop_vnc_session()


@app.get("/api/vnc/status")
async def fetch_vnc_status():
    return get_vnc_status()


@app.get("/api/accounts/{account}/auth")
async def check_single_account_auth(account: str):
    return check_account_login(account)


@app.get("/api/visual-search/status")
async def visual_search_status():
    return {
        "exists": VISUAL_SEARCH_IMAGE.exists(),
        "path": str(VISUAL_SEARCH_IMAGE),
        "size_bytes": VISUAL_SEARCH_IMAGE.stat().st_size if VISUAL_SEARCH_IMAGE.exists() else 0,
    }


@app.post("/api/visual-search/generate")
async def generate_visual_image():
    result = generate_visual_search_image()
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Generation failed"))
    return result


@app.post("/api/visual-search/upload")
async def upload_visual_image(file: UploadFile = File(...)):
    try:
        content = await file.read()
        with open(VISUAL_SEARCH_IMAGE, "wb") as f:
            f.write(content)
        return {"success": True, "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/nouns")
async def get_nouns():
    if not NOUNS_FILE.exists():
        return {"content": ""}
    with open(NOUNS_FILE, "r", encoding="utf-8") as f:
        return {"content": f.read()}


@app.post("/api/nouns")
async def update_nouns(req: NounsUpdateRequest):
    with open(NOUNS_FILE, "w", encoding="utf-8") as f:
        f.write(req.content)
    return {"success": True}


@app.post("/api/accounts/add")
async def add_account(req: AccountActionRequest):
    name = req.account.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Account name cannot be empty")
    cfg = get_config()
    if name in cfg.accounts:
        return {"success": True, "message": "Account already exists"}
    cfg.accounts.append(name)
    save_config(cfg)
    return {"success": True, "accounts": cfg.accounts}


@app.post("/api/accounts/remove")
async def remove_account(req: AccountActionRequest):
    name = req.account.strip()
    cfg = get_config()
    if name not in cfg.accounts:
        return {"success": True, "message": "Account does not exist"}
    cfg.accounts.remove(name)
    save_config(cfg)
    return {"success": True, "accounts": cfg.accounts}


# Real-time WebSocket Log Streamer
@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    queue = asyncio.Queue()
    log_subscribers.append(queue)

    try:
        # Replay recent log buffer on connect
        for line in runner_state.recent_logs[-100:]:
            await websocket.send_text(line)

        while True:
            line = await queue.get()
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        if queue in log_subscribers:
            log_subscribers.remove(queue)
