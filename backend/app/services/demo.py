"""Demo sign-in (D84): session tokens for listed synthetic customers, so the
live-call page can be tried locally without the bank's own sign-in.

Stands in for the app the caller would have signed in to (like dev_token).
The routes refuse unless config.DEMO_LOGIN is on.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.app import config
from backend.app.auth.dependencies import SessionSecretNotConfigured
from backend.app.auth.session_tokens import issue_session_token
from backend.app.db.models import CustomerRecord


class NotADemoCustomer(Exception):
    """Not listed in COLLECTWISE_DEMO_CUSTOMERS, or not in the database."""


@dataclass(frozen=True)
class DemoCustomer:
    customer_id: str
    display_name: str


def demo_customers(db: Session) -> list[DemoCustomer]:
    """The listed demo customers that exist, in the configured order."""
    found = []
    for customer_id in config.DEMO_CUSTOMERS:
        record = db.get(CustomerRecord, customer_id)
        if record is not None:
            found.append(DemoCustomer(record.customer_id, record.display_name))
    return found


def sign_in_demo_customer(db: Session, customer_id: str) -> str:
    """A normal session token for a demo customer. Raises NotADemoCustomer or
    SessionSecretNotConfigured."""
    if customer_id not in config.DEMO_CUSTOMERS or db.get(CustomerRecord, customer_id) is None:
        raise NotADemoCustomer(customer_id)
    secret = config.SESSION_TOKEN_SECRET
    try:
        if not secret:
            raise ValueError("no secret")
        return issue_session_token(
            customer_id, secret=secret.encode("utf-8"), ttl_seconds=config.SESSION_TOKEN_TTL_SECONDS,
        )
    except ValueError:
        raise SessionSecretNotConfigured() from None
