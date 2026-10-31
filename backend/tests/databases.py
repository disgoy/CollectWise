"""Test databases (M6.4), in one ordinary module so there is one of each.

Every test database comes from make_test_engine(): SQLite in memory by
default, or with COLLECTWISE_TEST_POSTGRES=1 a fresh database in a throwaway
Postgres cluster this test run starts and deletes. The cluster has its own
folder, port and socket and no network listener, so no other Postgres
server on the machine is touched.

(It lives here, not in conftest.py: pytest loads conftest under its own
module name, so tests importing it would get a second copy of the cluster
that nothing stops.)
"""

import atexit
import os
import shutil
import socket
import subprocess
import tempfile

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.pool import StaticPool

from backend.app.db import models  # noqa: F401  registers the tables
from backend.app.db.base import Base
from backend.app.db.session import alembic_config, upgrade_to_head

POSTGRES = os.environ.get("COLLECTWISE_TEST_POSTGRES") == "1"


class ThrowawayPostgres:
    def __init__(self) -> None:
        missing = [tool for tool in ("initdb", "pg_ctl") if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"COLLECTWISE_TEST_POSTGRES=1 needs {', '.join(missing)} on PATH")
        self.dir = tempfile.mkdtemp(prefix="cw-pg-")
        data = f"{self.dir}/data"
        subprocess.run(["initdb", "-D", data, "-U", "collectwise", "--auth=trust", "-E", "UTF8", "--no-locale"],
                       check=True, capture_output=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        subprocess.run(["pg_ctl", "-D", data, "-l", f"{self.dir}/server.log", "-w", "-o",
                        f"-p {self.port} -k {self.dir} -c listen_addresses=''", "start"],
                       check=True, capture_output=True)
        self._admin = create_engine(self.url("postgres"), isolation_level="AUTOCOMMIT")
        with self._admin.connect() as connection:
            self.version = connection.execute(text("SHOW server_version")).scalar()
            connection.execute(text("CREATE DATABASE template_head"))
        upgrade_to_head(self.url("template_head"))
        self._count = 0
        self._stopped = False
        atexit.register(self.stop)  # even if the run is interrupted

    def url(self, database: str) -> str:
        return f"postgresql+psycopg://collectwise@/{database}?host={self.dir}&port={self.port}"

    def new_database(self) -> str:
        """A fresh copy of the migrated template, for one test."""
        self._count += 1
        name = f"test_{self._count}"
        with self._admin.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {name} TEMPLATE template_head"))
        return self.url(name)

    def empty_database(self) -> str:
        """A database with no tables at all, for migration tests."""
        self._count += 1
        name = f"empty_{self._count}"
        with self._admin.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {name}"))
        return self.url(name)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._admin.dispose()
        subprocess.run(["pg_ctl", "-D", f"{self.dir}/data", "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)


_postgres: ThrowawayPostgres | None = None


def postgres() -> ThrowawayPostgres:
    global _postgres
    if _postgres is None:
        _postgres = ThrowawayPostgres()
    return _postgres


def stop_postgres() -> None:
    if _postgres is not None:
        _postgres.stop()


def _sqlite_foreign_keys(engine: Engine) -> Engine:
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine


def make_test_engine(*, concurrent: bool = False) -> Engine:
    """A database with the current schema, empty, for one test.

    `concurrent` asks for real separate connections (SQLite: a file instead
    of one shared in-memory connection). Postgres always has them."""
    if POSTGRES:
        return create_engine(postgres().new_database())
    if concurrent:
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp(prefix='cw-db-')}/test.db",
                               connect_args={"check_same_thread": False, "timeout": 10})
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _sqlite_foreign_keys(engine)
    # The models' tables, stamped as the latest migration (test_migrations
    # proves the migrations build exactly these tables).
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        MigrationContext.configure(connection).stamp(ScriptDirectory.from_config(alembic_config()), "head")
    return engine
