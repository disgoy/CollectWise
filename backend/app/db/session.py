from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import DATABASE_URL

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


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


# --- the schema (M6.4): Alembic migrations, never create_all ---------------


class SchemaOutOfDate(Exception):
    """The database isn't at the latest migration."""


UPGRADE_HINT = "Run `alembic upgrade head` (see README: Database)."


def alembic_config(url: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    if url is not None:
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def schema_revision(bind: Engine | Connection) -> str | None:
    """The database's migration revision; None when it has none."""
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    return MigrationContext.configure(bind).get_current_revision()


def schema_is_current(bind: Engine | Connection) -> bool:
    return schema_revision(bind) == head_revision()


def require_current_schema(bind: Engine | Connection | None = None) -> None:
    """For scripts: stop with what to run, rather than work on a database
    whose tables are missing or old."""
    revision = schema_revision(engine if bind is None else bind)
    if revision != head_revision():
        found = "no migrations" if revision is None else f"revision {revision}"
        raise SchemaOutOfDate(f"The database has {found}; the code needs {head_revision()}. {UPGRADE_HINT}")


def upgrade_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
