"""Alembic environment (M6.4): the app's models and database URL.

Batch mode lets SQLite change constraints by rebuilding the table. Tests and
the app's own helpers pass the URL in; the command line uses
COLLECTWISE_DATABASE_URL, like the app.
"""

import logging

from alembic import context
from sqlalchemy import create_engine

from backend.app import config
from backend.app.db import models  # noqa: F401  registers the tables
from backend.app.db.base import Base

if not logging.getLogger().handlers:  # the command line: say what runs
    logging.basicConfig(level=logging.INFO, format="%(message)s")

url = context.config.get_main_option("sqlalchemy.url") or config.DATABASE_URL


def run() -> None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run()
