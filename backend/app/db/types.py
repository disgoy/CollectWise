from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A datetime column that always stores and returns timezone-aware UTC.

    SQLite does not keep a UTC offset, so plain DateTime(timezone=True) reads
    values back without a timezone. This type converts every value to UTC on
    write, rejects naive datetimes (their timezone is unknown), and attaches
    UTC on read, so callers get the same result on every database.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime; store timezone-aware UTC values")
        return value.astimezone(timezone.utc)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # Stored values are always UTC (see process_bind_param).
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
