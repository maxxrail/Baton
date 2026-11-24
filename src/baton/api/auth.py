"""Operator auth for the console: one shared secret (`BATON_CONSOLE_SECRET`) until Part 07.

Two ways in:

- the console logs in with a name and the secret (`POST /ops/login`) and gets an HttpOnly,
  SameSite=Strict session cookie `name|issued_at|hmac`. The HMAC is keyed with the secret, so
  the cookie can't be forged, and changing the secret logs everyone out. Sessions last
  `SESSION_TTL`. The cookie also authenticates the console's WebSocket, which can't send
  headers from a browser.
- scripts and curl send `Authorization: Bearer <secret>` and act as operator `api`.

With no secret configured, every operator endpoint answers 503: the console is closed, not
open. Comparisons are constant-time.

Cookie-authenticated writes and the inbox WebSocket also check `Origin` against the web app
and the API's own origin. SameSite=Strict stops other *sites*, but not other origins on the
same site (another localhost port, a sibling subdomain); the Origin check covers those.
Requests without an Origin (curl, scripts) are not browsers and are let through.
"""

import hashlib
import hmac
import re
import time
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, WebSocket

from baton.config import Settings

COOKIE = "baton_console"
SESSION_TTL_S = 12 * 3600
API_OPERATOR = "api"
_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")
NOT_CONFIGURED = "The console is disabled: set BATON_CONSOLE_SECRET to enable it."


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and name.strip() == name


def _sign(secret: str, name: str, issued: int) -> str:
    msg = f"{name}|{issued}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def session_token(secret: str, name: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    return f"{name}|{issued}|{_sign(secret, name, issued)}"


def operator_from_token(secret: str, token: str, now: float | None = None) -> str | None:
    parts = token.split("|")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    name, issued_raw, sig = parts
    issued = int(issued_raw)
    if not hmac.compare_digest(
        sig.encode("utf-8", "replace"), _sign(secret, name, issued).encode()
    ):
        return None
    now = now if now is not None else time.time()
    if not 0 <= now - issued <= SESSION_TTL_S:
        return None
    return name


def secret_matches(secret: str, candidate: str) -> bool:
    return hmac.compare_digest(secret.encode(), candidate.encode())


def _secret(settings: Settings) -> str:
    secret = settings.baton_console_secret
    value = secret.get_secret_value() if secret else ""
    if not value:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
    return value


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else None


def origin_allowed(settings: Settings, origin: str | None, own_origin: str) -> bool:
    """A browser request's Origin must be the web app, the public API URL, or this host."""
    if origin is None:
        return True  # not a browser; cookie theft isn't the threat here
    allowed = {
        own_origin.lower(),
        _origin(settings.baton_web_url),
        _origin(settings.baton_public_url),
    }
    return origin.lower() in allowed


def _operator(
    settings: Settings, authorization: str | None, cookie: str | None
) -> tuple[str | None, bool]:
    """The operator's name (or None) and whether it came from the session cookie."""
    secret = _secret(settings)
    if authorization and authorization.lower().startswith("bearer "):
        if secret_matches(secret, authorization[7:].strip()):
            return API_OPERATOR, False
        return None, False
    if cookie:
        return operator_from_token(secret, cookie), True
    return None, False


def require_operator(request: Request) -> str:
    """FastAPI dependency: the logged-in operator's name, or 401/403/503."""
    settings: Settings = request.app.state.settings
    name, by_cookie = _operator(
        settings, request.headers.get("authorization"), request.cookies.get(COOKIE)
    )
    if name is None:
        raise HTTPException(status_code=401, detail="log in to the console first")
    own = f"{request.url.scheme}://{request.url.netloc}"
    writes = request.method not in ("GET", "HEAD", "OPTIONS")
    if by_cookie and writes and not origin_allowed(settings, request.headers.get("origin"), own):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    return name


def websocket_operator(websocket: WebSocket) -> str | None:
    settings: Settings = websocket.app.state.settings
    try:
        name, by_cookie = _operator(
            settings, websocket.headers.get("authorization"), websocket.cookies.get(COOKIE)
        )
    except HTTPException:
        return None
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    own = f"{scheme}://{websocket.url.netloc}"
    if by_cookie and not origin_allowed(settings, websocket.headers.get("origin"), own):
        return None
    return name


def cookie_secure(settings: Settings, request_scheme: str) -> bool:
    """Mark the cookie Secure when the console is served over https, including behind a
    TLS-terminating proxy (where the request itself arrives as plain http)."""
    public = (settings.baton_web_url, settings.baton_public_url)
    return request_scheme == "https" or any(u.startswith("https://") for u in public)


Operator = Annotated[str, Depends(require_operator)]
