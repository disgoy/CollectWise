import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.api.customers import router as customers_router
from backend.app.db.session import UPGRADE_HINT, engine, get_db, head_revision, schema_is_current, schema_revision
from backend.app.api.accounts import router as accounts_router
from backend.app.api.servicing import router as servicing_router
from backend.app.api.agent import router as agent_router
from backend.app.api.voice import router as voice_router
from backend.app.api.live import router as live_router
from backend.app.api.demo import router as demo_router
from backend.app.api.live_page import router as live_page_router
from backend.app.api.live_page import static_files as live_static_files
from backend.app import config, telemetry
from backend.app.logging_setup import configure_logging
from backend.app.voice import live_call

logger = logging.getLogger(__name__)

# At import, after uvicorn has set up its own loggers, so ours replace them.
configure_logging(config.LOG_FORMAT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The schema comes from migrations, run on purpose (M6.4); a server on an
    # old schema says so and reports not ready rather than half-working.
    if not schema_is_current(engine):
        logger.error("Database schema is out of date (%s, need %s). %s",
                     schema_revision(engine) or "no migrations", head_revision(), UPGRADE_HINT)
    telemetry.configure(config.TRACING)
    if config.LOAD_TEST:
        logger.warning(
            "Load-test mode is ON (COLLECTWISE_LOAD_TEST=1): simulated model and speech may answer callers. "
            "Never use it on a real server.")
    if config.DEMO_LOGIN:
        logger.warning(
            "Demo sign-in is ON (COLLECTWISE_DEMO_LOGIN=1): anyone who can reach this server can act as "
            "the demo customers %s. Never use it on a public server.", ", ".join(config.DEMO_CUSTOMERS),
        )
    yield
    live_call.shutdown_agent_workers()
    telemetry.shutdown()


def _untraced(scope) -> bool:
    # Health probes and the page's files would only bury the calls.
    path = scope.get("path", "")
    return path.startswith("/health") or path.startswith("/live/static")


app = FastAPI(
    title="CollectWise API",
    description="A reliability-focused AI servicing system using synthetic data.",
    version="0.1.0",
    lifespan=lifespan,
    # FastAPI's own spans (M6.2) go through our provider, so they follow the
    # COLLECTWISE_TRACING switch. Its exception logs would include messages
    # that may quote a caller; its metrics have no exporter here.
    telemetry={
        "tracer_provider": telemetry.PROVIDER,
        "tracing": True,
        "metrics": False,
        "logs": False,
        "operation_spans": False,
        "auto_configure": False,
        "exclude": _untraced,
    },
)

app.include_router(customers_router)
app.include_router(accounts_router)
app.include_router(servicing_router)
app.include_router(agent_router)
app.include_router(voice_router)
app.include_router(live_router)
app.include_router(demo_router)
app.include_router(live_page_router)
app.mount("/live/static", live_static_files, name="live-static")


# Liveness: the process answers. No dependencies, so a database outage never
# makes a platform restart the server.
@app.get("/health", tags=["Health"])
def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "collectwise-api",
    }


# Readiness: the server can do its job, so traffic can be sent to it (H5).
# Open, like /health; it says only up or down.
@app.get("/health/ready", tags=["Health"])
def readiness_check(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
        current = schema_is_current(db.connection())
    except SQLAlchemyError as exc:
        # The type only: a database error message can include the connection
        # string, credentials and all.
        logger.error("Readiness check failed: database unavailable (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    if not current:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "schema_out_of_date"})
    return JSONResponse(content={"status": "ready"})
