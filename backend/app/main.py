
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.customers import router as customers_router
from backend.app.db.session import create_tables
from backend.app.api.accounts import router as accounts_router
from backend.app.api.servicing import router as servicing_router


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


@app.get("/health", tags=["Health"])
def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "collectwise-api",
    }
