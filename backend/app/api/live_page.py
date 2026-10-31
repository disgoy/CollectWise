"""The live-call demo page (M5.6, D84): plain HTML and JavaScript served by the
app, with a Content-Security-Policy that lets it load code only from this
server and connect only back to it."""

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "live"

router = APIRouter()

# A Host header we're willing to echo into the policy.
_PLAIN_HOST = re.compile(r"[A-Za-z0-9.-]+(:\d{1,5})?")


def content_security_policy(host: str) -> str:
    # 'self' covers ws:// to the same host in current browsers, but not all
    # older ones, so the WebSocket origin is named too when the host is plain.
    sockets = f" ws://{host} wss://{host}" if _PLAIN_HOST.fullmatch(host) else ""
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        f"connect-src 'self'{sockets}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


@router.get("/live", include_in_schema=False)
def live_page(request: Request) -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Content-Security-Policy": content_security_policy(request.url.netloc),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


static_files = StaticFiles(directory=STATIC_DIR)
