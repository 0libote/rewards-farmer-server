"""Optional dashboard authentication.

Disabled unless the DASHBOARD_TOKEN environment variable is set, so existing
deployments keep working unchanged. When enabled, every /api/* endpoint except
the public ones below, plus the /vnc/* proxy, requires the token.

The token may arrive as a Bearer header, an X-Auth-Token header, the rf_token
cookie, or a ?token= query parameter. The cookie and query forms exist for
clients that cannot set headers: the noVNC iframe and the WebSocket log stream.
"""
import hmac
import os
from typing import Any, Optional

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
COOKIE_NAME = "rf_token"

# Reachable without a token: the dashboard shell, health probes, and the auth
# handshake itself. The shell contains no data; everything real is under /api.
PUBLIC_PATHS = {
    "/",
    "/health",
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
}


def auth_enabled() -> bool:
    return bool(DASHBOARD_TOKEN)


def token_valid(candidate: Optional[str]) -> bool:
    if not DASHBOARD_TOKEN:
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, DASHBOARD_TOKEN)


def token_from_request(request: Any) -> Optional[str]:
    """Extract a candidate token from a Request or WebSocket."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    header_token = request.headers.get("x-auth-token")
    if header_token:
        return header_token.strip()
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    try:
        return request.query_params.get("token")
    except Exception:
        return None


def is_protected_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    return path.startswith("/api/") or path.startswith("/vnc/")
