from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import DATABASE_URL
from backend.app.db.base import Base


IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    # A database error names the statement but not its values (M6.3): those
    # are customer ids, account ids and amounts, and errors end up in logs.
    hide_parameters=True,
    # SQLite connections are thread-bound by default; FastAPI uses a thread pool.
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
)


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(
        dbapi_connection: object,
        connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def create_tables() -> None:
    # Import models so SQLAlchemy registers their table definitions.
    from backend.app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
