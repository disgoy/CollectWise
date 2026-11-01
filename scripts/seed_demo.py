"""Create the synthetic demo customers for the live-call page (M5.6, D84), if
missing, in the local database (COLLECTWISE_DATABASE_URL, default
collectwise.db): cust_demo (US dollars) and cust_demo_in (rupees, for Hindi
calls, M7.6: the same accounts as the eval's cust_priya). No API calls,
nothing paid.

Usage, from the repo root:
    python -m scripts.seed_demo
Then start the server with demo sign-in on (local only, never public):
    COLLECTWISE_DEMO_LOGIN=1 COLLECTWISE_SESSION_SECRET=<32+ characters> uvicorn backend.app.main:app
and open http://127.0.0.1:8000/live

To call the demo customer's account from a phone (M7.2), give the number you
will call from; the PIN is asked for without echoing and stored only as a
hash. The number stays in your local database, never in the repository:
    python -m scripts.seed_demo --phone +15555550100
"""

import argparse
import getpass
import sys
from decimal import Decimal

from backend.app.db.models import CustomerRecord
from backend.app.db.session import SessionLocal, require_current_schema
from backend.app.schemas.domain import AccountStatus
from backend.app.services.accounts import create_account, get_account_details
from backend.app.services.phone_identity import (
    InvalidPhoneNumberError,
    PhoneNumberInUseError,
    WeakPinError,
    set_phone_and_pin,
)

CUSTOMER = ("cust_demo", "Demo Customer")
ACCOUNTS = [
    ("acct_demo_main", Decimal("500.00"), "USD", "main"),
    ("acct_demo_savings", Decimal("900.00"), "USD", "savings"),
]
INDIA_CUSTOMER = ("cust_demo_in", "Demo Customer (India)")
INDIA_ACCOUNTS = [
    ("acct_demo_in_savings", Decimal("25000.00"), "INR", "savings"),
    ("acct_demo_in_current", Decimal("1200.00"), "INR", "current"),
]
DEMO = [(CUSTOMER, ACCOUNTS), (INDIA_CUSTOMER, INDIA_ACCOUNTS)]


def main(argv: list[str] | None = None, ask=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", help="the number you'll call from, E.164 (e.g. +15555550100)")
    args = parser.parse_args(argv)
    require_current_schema()
    with SessionLocal() as db:
        for (customer_id, name), accounts in DEMO:
            if db.get(CustomerRecord, customer_id) is None:
                db.add(CustomerRecord(customer_id=customer_id, display_name=name))
                db.commit()
                print(f"created customer {customer_id}")
            for account_id, balance, currency, label in accounts:
                if get_account_details(db, account_id) is None:
                    create_account(
                        db, account_id=account_id, customer_id=customer_id, balance=balance,
                        currency=currency, status=AccountStatus.ACTIVE, label=label,
                    )
                    print(f"created account {label} ({balance} {currency}) for {customer_id}")
        if args.phone:
            pin = ask("Phone PIN for the demo customer (6 digits): ")
            if ask("Again: ") != pin:
                print("The PINs didn't match; nothing changed.")
                return 1
            try:
                set_phone_and_pin(db, CUSTOMER[0], phone_number=args.phone, pin=pin)
            except (InvalidPhoneNumberError, WeakPinError, PhoneNumberInUseError) as exc:
                print({InvalidPhoneNumberError: "Give the number as E.164, e.g. +15555550100.",
                       WeakPinError: "Choose 6 digits that aren't one digit repeated or a straight run.",
                       PhoneNumberInUseError: "Another customer already has that number."}[type(exc)])
                return 1
            print("Phone number and PIN set for the demo customer.")
    print("Demo customers ready. Start the server with COLLECTWISE_DEMO_LOGIN=1 and open /live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
