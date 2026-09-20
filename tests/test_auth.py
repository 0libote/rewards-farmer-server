"""Integration tests for the FastAPI app: auth gating and config endpoints."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.account_checker as checker
import server.app as app_module
import server.auth as auth
import server.config as config_module
import server.runner as runner


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Redirect all persisted state into the tmp dir.
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(checker, "PROFILES_DIR", tmp_path / "data-dir")
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "HISTORY_FILE", tmp_path / "logs" / "history.json")
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "NOUNS_FILE", tmp_path / "nouns.txt")
    monkeypatch.setattr(app_module, "VISUAL_SEARCH_IMAGE", tmp_path / "visual_search.jpg")

    # Keep lifespan from touching git/network/scheduler/browser.
    monkeypatch.setattr(app_module, "ensure_upstream", lambda: {"installed": True})
    monkeypatch.setattr(app_module, "update_upstream", lambda: {"success": True})
    monkeypatch.setattr(app_module, "init_scheduler", lambda loop: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "reload_schedule", lambda: None)
    monkeypatch.setattr(app_module, "stop_vnc_session", lambda: {"success": True})

    async def _stop_run():
        return {"success": True}

    monkeypatch.setattr(app_module, "stop_run", _stop_run)

    with TestClient(app_module.app) as c:
        yield c


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_disabled_allows_api(client, monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "")
    assert client.get("/api/status").status_code == 200


def test_auth_enabled_blocks_until_login(client, monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "s3cret")

    assert client.get("/api/status").status_code == 401
    # Health and the auth handshake stay public.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").json() == {"enabled": True, "authenticated": False}

    assert client.post("/api/auth/login", json={"token": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"token": "s3cret"}).status_code == 200
    # The login cookie now authenticates requests.
    assert client.get("/api/status").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/status").status_code == 401
    # Header auth works without the cookie.
    assert client.get("/api/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/api/status", headers={"X-Auth-Token": "s3cret"}).status_code == 200


def test_config_endpoint_merges_partial_update(client):
    before = client.get("/api/config").json()
    assert before["accounts"] == ["default"]

    r = client.post("/api/config", json={"query_source": "llm", "llm_provider": "openrouter"})
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["query_source"] == "llm"
    assert cfg["llm_provider"] == "openrouter"
    # Fields not sent are preserved, not reset.
    assert cfg["accounts"] == ["default"]
    assert cfg["log_level"] == "INFO"


def test_nouns_endpoint_writes_file(client, tmp_path):
    r = client.post("/api/nouns", json={"content": "apple\nbanana\n"})
    assert r.status_code == 200
    assert (tmp_path / "nouns.txt").read_text(encoding="utf-8") == "apple\nbanana\n"


def test_vendor_assets_are_served_locally(client):
    r = client.get("/static/vendor/lucide-0.544.0.min.js")
    assert r.status_code == 200
    assert len(r.content) > 1000


def test_log_endpoint_rejects_traversal(client):
    assert client.get("/api/logs/..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/api/logs/history.json").status_code == 404


def test_vnc_proxy_requires_auth_when_enabled(client, monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "s3cret")
    assert client.get("/vnc/vnc.html").status_code == 401


def test_vnc_proxy_reports_502_when_stack_down(client, monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "")
    monkeypatch.setattr(app_module, "VNC_INTERNAL_PORT", 1)  # nothing listens here
    assert client.get("/vnc/vnc.html").status_code == 502


def test_vnc_http_proxy_forwards_upstream(client, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"noVNC-stub"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "")
        monkeypatch.setattr(app_module, "VNC_INTERNAL_PORT", srv.server_address[1])
        r = client.get("/vnc/vnc.html")
        assert r.status_code == 200
        assert "noVNC-stub" in r.text
    finally:
        srv.shutdown()


