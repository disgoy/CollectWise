"""Allow the payment_cancelled conversation event (D93).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

BEFORE = "event_type IN ('session_started', 'turn_started', 'model_call', 'tool_call', 'turn_completed')"
AFTER = (
    "event_type IN ('session_started', 'turn_started', 'model_call', 'tool_call', 'turn_completed', "
    "'payment_cancelled')"
)


def _replace_check(sql: str) -> None:
    # SQLite can't alter a constraint: batch mode rebuilds the table, rows kept.
    with op.batch_alter_table("conversation_events") as batch:
        batch.drop_constraint("ck_conversation_events_type", type_="check")
        batch.create_check_constraint("ck_conversation_events_type", sql)


def upgrade() -> None:
    _replace_check(AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM conversation_events WHERE event_type = 'payment_cancelled'")
    _replace_check(BEFORE)
