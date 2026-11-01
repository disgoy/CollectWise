"""The live evidence page (M6.7): the evidence report's charts inside the app,
with live-call speed from this server's own database. Plain HTML and
JavaScript under the same Content-Security-Policy as the live-call page.
GET /evidence/data is open: it returns aggregates only."""

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from backend.app.api.live_page import content_security_policy
from backend.app.db.session import get_db
from backend.app.services.evidence import live_evidence

STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "evidence"
# Building reads the eval result files, so answers are reused for a while:
# a page reloaded in a loop can't make the server re-read them each time.
CACHE_S = 30.0

router = APIRouter(tags=["Evidence"])
static_files = StaticFiles(directory=STATIC_DIR)

_clock = time.monotonic
_cached: tuple[float, dict[str, Any]] | None = None


def clear_cache() -> None:
    global _cached
    _cached = None


@router.get("/evidence", include_in_schema=False)
def evidence_page(request: Request) -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Content-Security-Policy": content_security_policy(request.url.netloc),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/evidence/data")
def evidence_data(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The evidence report's tables: counts, rates and timings only."""
    global _cached
    now = _clock()
    if _cached is None or now - _cached[0] >= CACHE_S:
        _cached = (now, live_evidence(db))
    return _cached[1]
