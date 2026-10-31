"""Create the synthetic demo customer for the live-call page (M5.6, D84), if
missing, in the local database (COLLECTWISE_DATABASE_URL, default
collectwise.db). No API calls, nothing paid.

Usage, from the repo root:
    python -m scripts.seed_demo
Then start the server with demo sign-in on (local only, never public):
    COLLECTWISE_DEMO_LOGIN=1 COLLECTWISE_SESSION_SECRET=<32+ characters> uvicorn backend.app.main:app
and open http://127.0.0.1:8000/live
"""

import sys
from decimal import Decimal

from backend.app.db.models import CustomerRecord
from backend.app.db.session import SessionLocal, create_tables
from backend.app.schemas.domain import AccountStatus
from backend.app.services.accounts import create_account, get_account_details

CUSTOMER = ("cust_demo", "Demo Customer")
ACCOUNTS = [
    ("acct_demo_main", Decimal("500.00"), "USD", "main"),
    ("acct_demo_savings", Decimal("900.00"), "USD", "savings"),
]


def main() -> int:
    create_tables()
    with SessionLocal() as db:
        customer_id, name = CUSTOMER
        if db.get(CustomerRecord, customer_id) is None:
            db.add(CustomerRecord(customer_id=customer_id, display_name=name))
            db.commit()
            print(f"created customer {customer_id}")
        for account_id, balance, currency, label in ACCOUNTS:
            if get_account_details(db, account_id) is None:
                create_account(
                    db, account_id=account_id, customer_id=customer_id, balance=balance,
                    currency=currency, status=AccountStatus.ACTIVE, label=label,
                )
                print(f"created account {label} ({balance} {currency})")
    print("Demo customer ready. Start the server with COLLECTWISE_DEMO_LOGIN=1 and open /live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
