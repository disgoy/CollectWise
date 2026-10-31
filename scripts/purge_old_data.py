"""Delete old telemetry (M6.3): conversation events and voice and live timing
rows older than COLLECTWISE_TELEMETRY_RETENTION_DAYS (30 unless set).

These rows hold ids, codes and timings, never words, but nobody needs them
for ever. Servicing requests and their audit events are business records with
their own, longer retention, and this command never touches them.

Shows what it would delete unless given --yes. Run scripts.build_evidence
first if the evidence report should keep the calls about to go.

Usage, from the repo root:
    python -m scripts.purge_old_data [--days 30] [--yes]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app import config
from backend.app.db.models import ConversationEventRecord, LiveTurnMetricRecord, VoiceTurnMetricRecord
from backend.app.db.session import SessionLocal, create_tables

# Telemetry only: the servicing tables are deliberately not listed.
PURGEABLE = (ConversationEventRecord, VoiceTurnMetricRecord, LiveTurnMetricRecord)


def purge(db: Session, *, days: int, now: datetime, apply: bool) -> dict[str, int]:
    """Rows created more than `days` before `now`, per table; deleted when
    `apply`. A row exactly `days` old is kept."""
    if days < 1:
        raise ValueError("Keep at least one day")
    cutoff = now - timedelta(days=days)
    counts = {}
    for table in PURGEABLE:
        old = table.created_at < cutoff
        counts[table.__tablename__] = db.scalar(select(func.count()).select_from(table).where(old))
        if apply:
            db.execute(delete(table).where(old))
    if apply:
        db.commit()
    return counts


def main(argv: list[str] | None = None, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=config.TELEMETRY_RETENTION_DAYS,
                        help="keep rows newer than this many days")
    parser.add_argument("--yes", action="store_true", help="delete (otherwise only count)")
    args = parser.parse_args(argv)
    if session_factory is SessionLocal:
        create_tables()
    with session_factory() as db:
        counts = purge(db, days=args.days, now=datetime.now(timezone.utc), apply=args.yes)
    print(f"{'Deleted' if args.yes else 'Would delete'} rows older than {args.days} days:")
    for table, count in counts.items():
        print(f"  {table:22} {count}")
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
