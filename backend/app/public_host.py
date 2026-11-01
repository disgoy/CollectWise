"""Only the phone routes answer on the public address (M7.5).

A tunnel to a laptop (or any proxy) puts every route on the internet, demo
sign-in and the call-watching socket included. When COLLECTWISE_PUBLIC_BASE_URL
is set, a request that names its host (in `Host` or any `X-Forwarded-Host`)
reaches only Twilio's two routes; anything else gets the same 404 as a route
that doesn't exist. Requests to the machine's own address are unaffected, so
the /live page keeps working locally. It only ever refuses: a spoofed header
can lock its sender out, never let it in.
"""

import logging
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from backend.app import config
from backend.app.api.telephony import STREAM_PATH, VOICE_PATH

logger = logging.getLogger(__name__)

# (scope type, method, path) a public request may reach.
PUBLIC_ROUTES = {("http", "POST", VOICE_PATH), ("websocket", None, STREAM_PATH)}


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8000
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def _named_hosts(scope: Scope) -> set[str]:
    hosts = set()
    for name, value in scope.get("headers") or ():
        if name in (b"host", b"x-forwarded-host"):
            hosts.update(_hostname(part) for part in value.decode("latin-1").split(","))
    return hosts


def is_public_request(scope: Scope) -> bool:
    public_host = urlsplit(config.PUBLIC_BASE_URL or "").hostname
    return bool(public_host) and public_host in _named_hosts(scope)


class PublicHostGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and is_public_request(scope):
            method = scope.get("method") if scope["type"] == "http" else None
            if (scope["type"], method, scope["path"]) not in PUBLIC_ROUTES:
                logger.info("Refused a public %s request outside the phone routes", scope["type"])
                if scope["type"] == "http":
                    response = JSONResponse({"detail": "Not Found"}, status_code=404)
                else:
                    response = WebSocketClose()
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
