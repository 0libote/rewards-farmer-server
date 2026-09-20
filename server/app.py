import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import os
import tempfile
from pathlib import Path
from typing import List, Optional

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import auth

from server.config import (
    get_config,
    save_config,
    AppConfig,
    VISUAL_SEARCH_IMAGE,
    NOUNS_FILE,
    DATA_DIR,
    MAX_ACCOUNTS,
    is_valid_account_name,
)
from server.upstream_manager import (
    ensure_upstream,
    update_upstream,
    get_upstream_info,
    get_edge_version,
    generate_visual_search_image,
    link_persistent_data,
    sync_nouns_to_upstream,
)
from server.runner import (
    state as runner_state,
    start_run,
    stop_run,
    get_history,
    get_lifetime_stats,
    get_diagnostics,
    should_skip_scheduled_run,
    is_safe_log_filename,
    log_subscribers,
    LOGS_DIR,
)
from server.scheduler import init_scheduler, reload_schedule, get_schedule_info, shutdown_scheduler
from server.vnc_manager import start_vnc_session, stop_vnc_session, get_vnc_status
from server.account_checker import check_account_login, invalidate_account_cache

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

MAX_VISUAL_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_NOUNS_BYTES = 200 * 1024

# noVNC/websockify run inside the container on this port; the dashboard proxies
# them under /vnc so the interactive login works on the dashboard's own
# scheme/port (including behind an HTTPS reverse proxy) instead of requiring a
# second exposed port.
VNC_INTERNAL_HOST = "127.0.0.1"
VNC_INTERNAL_PORT = int(os.getenv("VNC_PORT", "6345"))

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


def _filter_proxy_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}

# Shared OpenAPI error docs for endpoints raising HTTPException.
_ERROR_400 = {"description": "Invalid request"}
_ERROR_404 = {"description": "Not found"}
_ERROR_413 = {"description": "Payload too large"}
_ERROR_500 = {"description": "Server error"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    loop = asyncio.get_running_loop()
    print("[SERVER] Starting Rewards Farmer Server...")
    await asyncio.to_thread(ensure_upstream)

    cfg = get_config()
    if cfg.auto_update_upstream:
        print("[SERVER] Checking for upstream updates...")
        await asyncio.to_thread(update_upstream)

    init_scheduler(loop)
    yield
    # Shutdown
    print("[SERVER] Shutting down Rewards Farmer Server...")
    shutdown_scheduler()
    await asyncio.to_thread(stop_vnc_session)
    await stop_run()


app = FastAPI(title="Rewards Farmer Server", lifespan=lifespan)


@app.middleware("http")
async def dashboard_auth_middleware(request: Request, call_next):
    """Gate the API and VNC proxy behind DASHBOARD_TOKEN when it is set."""
    if auth.auth_enabled() and auth.is_protected_path(request.url.path):
        if not auth.token_valid(auth.token_from_request(request)):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

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


class AuthRequest(BaseModel):
    token: str


# Authentication
@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "enabled": auth.auth_enabled(),
        "authenticated": auth.token_valid(auth.token_from_request(request)),
    }


@app.post("/api/auth/login", responses={401: _ERROR_400})
async def auth_login(req: AuthRequest, response: Response):
    if not auth.auth_enabled():
        return {"success": True, "enabled": False}
    if not auth.token_valid(req.token):
        raise HTTPException(status_code=401, detail="Invalid token")
    # Cookie so the noVNC iframe and the WebSocket handshake are authenticated
    # automatically without custom headers.
    response.set_cookie(
        auth.COOKIE_NAME,
        req.token,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"success": True, "enabled": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"success": True}


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
    # Cookie inspection does blocking sqlite/file I/O: keep it off the event loop.
    checks = await asyncio.gather(*(asyncio.to_thread(check_account_login, acc) for acc in cfg.accounts))
    account_statuses = dict(zip(cfg.accounts, checks))
    logged_in_count = sum(1 for st in account_statuses.values() if st.get("logged_in"))

    return {
        "runner": {
            "is_running": runner_state.is_running,
            "current_account": runner_state.current_account,
            "start_time": runner_state.start_time,
            "accounts": runner_state.accounts_in_run,
            "stats": runner_state.account_stats,
        },
        "lifetime_stats": await asyncio.to_thread(get_lifetime_stats),
        "vnc": get_vnc_status(),
        "upstream": await asyncio.to_thread(get_upstream_info),
        "edge_version": await asyncio.to_thread(get_edge_version),
        "scheduler": get_schedule_info(),
        "accounts": cfg.accounts,
        "account_statuses": account_statuses,
        "logged_in_count": logged_in_count,
        "total_accounts": len(cfg.accounts),
        "query_source": cfg.query_source,
        "webhook_configured": bool(cfg.webhook_url),
        "visual_search_ready": VISUAL_SEARCH_IMAGE.exists(),
    }


@app.get("/api/health")
@app.get("/health")
async def health_check():
    cfg = get_config()
    upstream = await asyncio.to_thread(get_upstream_info)
    return {
        "status": "ok",
        "runner_active": runner_state.is_running,
        "accounts": len(cfg.accounts),
        "upstream": upstream.get("installed", False),
    }


@app.post("/api/run", responses={400: _ERROR_400})
async def trigger_run(req: Optional[RunRequest] = None):
    req = req if req is not None else RunRequest()
    if req.accounts:
        invalid = [a for a in req.accounts if not is_valid_account_name(a.strip())]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid account name(s): {', '.join(invalid)}")
        req_accounts = [a.strip() for a in req.accounts]
    else:
        req_accounts = None
    result = await start_run(accounts=req_accounts)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/stop", responses={400: _ERROR_400})
async def trigger_stop():
    result = await stop_run()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/api/history")
async def fetch_history():
    return await asyncio.to_thread(get_history)


@app.get("/api/diagnostics")
async def fetch_diagnostics():
    """Aggregated miss-patterns across recent runs: visual SKIP rate, explore
    cards incomplete after searching, empty-run rate, plus next steps. No
    browser is launched; purely history analysis so it is safe anytime."""
    diag = await asyncio.to_thread(get_diagnostics)
    skip = await asyncio.to_thread(should_skip_scheduled_run)
    diag["smart_skip"] = skip
    return diag


@app.get("/api/logs/{filename}", responses={404: _ERROR_404})
async def fetch_log_file(filename: str):
    """Serve a single persisted run log. The filename is strictly validated
    (run_YYYYMMDD_HHMMSS.log) so callers can't escape the logs directory."""
    if not is_safe_log_filename(filename):
        raise HTTPException(status_code=404, detail="Log file not found")
    log_path = LOGS_DIR / filename
    if not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(str(log_path), media_type="text/plain; charset=utf-8")


@app.get("/api/config")
async def fetch_config():
    return get_config()


@app.post("/api/config")
async def update_config(new_config: AppConfig):
    # Merge only the fields the client actually sent, so an older dashboard
    # (or a future one with new fields) can't silently reset settings to
    # defaults by omitting them. Re-validate so nested models stay typed.
    current = get_config()
    merged_data = current.model_dump()
    merged_data.update(new_config.model_dump(include=new_config.model_fields_set))
    merged = AppConfig(**merged_data)
    save_config(merged)
    reload_schedule()
    return {"success": True, "config": merged}


@app.get("/api/upstream")
async def fetch_upstream():
    return await asyncio.to_thread(get_upstream_info)


@app.post("/api/upstream/update")
async def trigger_upstream_update():
    return await asyncio.to_thread(update_upstream)


@app.post("/api/vnc/start", responses={400: _ERROR_400})
async def trigger_vnc_start(req: VncStartRequest):
    if not is_valid_account_name(req.account.strip()):
        raise HTTPException(status_code=400, detail="Invalid account name")
    result = await asyncio.to_thread(start_vnc_session, req.account.strip())
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/vnc/stop")
async def trigger_vnc_stop():
    return await asyncio.to_thread(stop_vnc_session)


@app.get("/api/vnc/status")
async def fetch_vnc_status():
    return get_vnc_status()


@app.api_route("/vnc/{path:path}", methods=["GET", "POST"])
async def vnc_http_proxy(path: str, request: Request):
    """Proxy noVNC's static assets from websockify onto the dashboard origin."""
    target = f"http://{VNC_INTERNAL_HOST}:{VNC_INTERNAL_PORT}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(
                request.method,
                target,
                params=request.query_params,
                content=await request.body(),
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Interactive browser is not reachable: {e}")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_filter_proxy_headers(upstream.headers),
    )


@app.websocket("/vnc/websockify")
async def vnc_ws_proxy(websocket: WebSocket):
    """Bridge the browser's noVNC WebSocket to websockify inside the container."""
    if not auth.token_valid(auth.token_from_request(websocket)):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    target = f"ws://{VNC_INTERNAL_HOST}:{VNC_INTERNAL_PORT}/websockify"
    try:
        async with websockets.connect(target, max_size=None, open_timeout=10) as upstream:
            async def client_to_upstream():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:
        # The VNC stack may not be running or the client may have gone away.
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/accounts/{account}/auth")
async def check_single_account_auth(account: str):
    if not is_valid_account_name(account):
        raise HTTPException(status_code=400, detail="Invalid account name")
    return await asyncio.to_thread(check_account_login, account)


@app.get("/api/visual-search/status")
async def visual_search_status():
    return {
        "exists": VISUAL_SEARCH_IMAGE.exists(),
        "path": str(VISUAL_SEARCH_IMAGE),
        "size_bytes": VISUAL_SEARCH_IMAGE.stat().st_size if VISUAL_SEARCH_IMAGE.exists() else 0,
    }


@app.post("/api/visual-search/generate", responses={500: _ERROR_500})
async def generate_visual_image():
    result = await asyncio.to_thread(generate_visual_search_image)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Generation failed"))
    return result


@app.post(
    "/api/visual-search/upload",
    responses={400: _ERROR_400, 413: _ERROR_413, 500: _ERROR_500},
)
async def upload_visual_image(file: UploadFile = File(...)):
    try:
        if file.content_type and not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"Expected an image upload, got {file.content_type}")
        # Read with a hard cap so a huge upload can't exhaust memory before the
        # size check.
        content = await file.read(MAX_VISUAL_UPLOAD_BYTES + 1)
        if len(content) > MAX_VISUAL_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Image too large (max 10 MB)")
        # Verify the bytes actually decode as an image before persisting.
        try:
            from PIL import Image

            with Image.open(BytesIO(content)) as img:
                img.verify()
        except Exception:
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid image")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix="visual.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp_path, VISUAL_SEARCH_IMAGE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # Re-link into upstream in case it did not exist at startup.
        await asyncio.to_thread(link_persistent_data)
        return {"success": True, "size": len(content)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/nouns")
async def get_nouns():
    if not NOUNS_FILE.exists():
        return {"content": ""}
    with open(NOUNS_FILE, "r", encoding="utf-8") as f:
        return {"content": f.read()}


@app.post("/api/nouns", responses={413: _ERROR_413})
async def update_nouns(req: NounsUpdateRequest):
    if len(req.content) > MAX_NOUNS_BYTES:
        raise HTTPException(status_code=413, detail="Wordlist too large (max 200 KB)")
    NOUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(NOUNS_FILE.parent), prefix="nouns.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(req.content)
        os.replace(tmp_path, NOUNS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # Upstream reads its own nouns.txt, so push the edit through immediately.
    await asyncio.to_thread(sync_nouns_to_upstream)
    return {"success": True}


@app.post("/api/accounts/add", responses={400: _ERROR_400})
async def add_account(req: AccountActionRequest):
    name = req.account.strip()
    if not is_valid_account_name(name):
        raise HTTPException(
            status_code=400,
            detail="Account name must start with a letter or digit and contain only letters, digits, dashes or underscores (max 32 chars)",
        )
    cfg = get_config()
    if name in cfg.accounts:
        return {"success": True, "message": "Account already exists"}
    if len(cfg.accounts) >= MAX_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"Account limit reached ({MAX_ACCOUNTS})")
    cfg.accounts.append(name)
    save_config(cfg)
    invalidate_account_cache()
    return {"success": True, "accounts": cfg.accounts}


@app.post("/api/accounts/remove", responses={400: _ERROR_400})
async def remove_account(req: AccountActionRequest):
    name = req.account.strip()
    if not is_valid_account_name(name):
        raise HTTPException(status_code=400, detail="Invalid account name")
    cfg = get_config()
    if name not in cfg.accounts:
        return {"success": True, "message": "Account does not exist"}
    cfg.accounts.remove(name)
    save_config(cfg)
    invalidate_account_cache(name)
    return {"success": True, "accounts": cfg.accounts}


# Real-time WebSocket Log Streamer
@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    if not auth.token_valid(auth.token_from_request(websocket)):
        await websocket.close(code=1008)
        return
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
    except RuntimeError:
        # Client went away mid-send.
        pass
    finally:
        if queue in log_subscribers:
            log_subscribers.remove(queue)
