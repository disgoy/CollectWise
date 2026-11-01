"""Phone callers (M7.2): customers' phone numbers, phone PIN credentials, and
the verification events.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

import backend.app.db.types

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

EVENTS_BEFORE = (
    "event_type IN ('session_started', 'turn_started', 'model_call', 'tool_call', 'turn_completed', "
    "'payment_cancelled')"
)
EVENTS_AFTER = (
    "event_type IN ('session_started', 'turn_started', 'model_call', 'tool_call', 'turn_completed', "
    "'payment_cancelled', 'caller_verified', 'verification_failed')"
)


def _replace_events_check(sql: str) -> None:
    with op.batch_alter_table("conversation_events") as batch:
        batch.drop_constraint("ck_conversation_events_type", type_="check")
        batch.create_check_constraint("ck_conversation_events_type", sql)


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("phone_number", sa.String(length=16), nullable=True))
        batch.create_unique_constraint("uq_customers_phone_number", ["phone_number"])
    op.create_table(
        "phone_credentials",
        sa.Column("customer_id", sa.String(length=50), sa.ForeignKey("customers.customer_id"), primary_key=True),
        sa.Column("pin_hash", sa.String(length=128), nullable=False),
        sa.Column("pin_salt", sa.String(length=64), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", backend.app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_phone_credentials_attempts"),
    )
    _replace_events_check(EVENTS_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM conversation_events WHERE event_type IN ('caller_verified', 'verification_failed')")
    _replace_events_check(EVENTS_BEFORE)
    op.drop_table("phone_credentials")
    with op.batch_alter_table("customers") as batch:
        batch.drop_constraint("uq_customers_phone_number", type_="unique")
        batch.drop_column("phone_number")
