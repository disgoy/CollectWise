import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.api.customers import router as customers_router
from backend.app.db.session import create_tables, get_db
from backend.app.api.accounts import router as accounts_router
from backend.app.api.servicing import router as servicing_router
from backend.app.api.agent import router as agent_router
from backend.app.api.voice import router as voice_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    yield


app = FastAPI(
    title="CollectWise API",
    description="A reliability-focused AI servicing system using synthetic data.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(customers_router)
app.include_router(accounts_router)
app.include_router(servicing_router)
app.include_router(agent_router)
app.include_router(voice_router)


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
    except SQLAlchemyError as exc:
        # The type only: a database error message can include the connection
        # string, credentials and all.
        logger.error("Readiness check failed: database unavailable (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(content={"status": "ready"})
