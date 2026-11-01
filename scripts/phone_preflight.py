"""Phone call preflight (M7.5): is everything ready for a real call through a
tunnel? Free: no Twilio API calls and nothing paid. Prints which settings are
missing by name, never their values, and never the phone number.

It checks the settings in this shell (load .env the same way as for the
server), the local database and the demo customer's phone number and PIN,
then, through the tunnel, that the public address reaches this server with
its Twilio signature check on and that nothing but the phone routes answers
there. Last, it prints the webhook URL to paste into Twilio.

Usage, from the repo root, with ngrok and the server running:
    set -a; source .env; set +a
    python -m scripts.phone_preflight
"""

import argparse
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError

from backend.app import config
from backend.app.api.telephony import VOICE_PATH, public_url
from backend.app.auth.session_tokens import MIN_SECRET_BYTES
from backend.app.db.models import CustomerRecord, PhoneCredentialRecord
from backend.app.db.session import SessionLocal, schema_is_current
from scripts.seed_demo import CUSTOMER

TIMEOUT_S = 10.0
# ngrok's free plan shows browsers a warning page first; this skips it.
TUNNEL_HEADERS = {"ngrok-skip-browser-warning": "1"}


@dataclass(frozen=True)
class Check:
    ok: bool
    label: str
    fix: str = ""


def fetch(method: str, url: str) -> int | None:
    """The status code, or None when nothing answers."""
    request = urllib.request.Request(url, method=method, headers=TUNNEL_HEADERS,
                                     data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def public_base_ok() -> bool:
    base = urlsplit(config.PUBLIC_BASE_URL or "")
    return base.scheme == "https" and bool(base.hostname) and base.path in ("", "/")


def settings_checks(environ: Mapping[str, str]) -> list[Check]:
    checks = [
        Check(bool(config.TWILIO_AUTH_TOKEN), "TWILIO_AUTH_TOKEN is set",
              "add it to .env (Twilio console, Account info), then load .env again"),
        Check(len((config.SESSION_TOKEN_SECRET or "").encode()) >= MIN_SECRET_BYTES,
              "COLLECTWISE_SESSION_SECRET is set (32+ characters)",
              "add 32+ random characters to .env"),
    ]
    checks.append(Check(public_base_ok(), "COLLECTWISE_PUBLIC_BASE_URL is an https address with no path",
                        "set it to your ngrok domain, e.g. https://<your-domain>.ngrok-free.app"))
    for name in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY"):
        checks.append(Check(bool(environ.get(name)), f"{name} is set", "add it to .env, then load .env again"))
    return checks


def database_checks(session_factory: Callable) -> list[Check]:
    customer_id = CUSTOMER[0]
    seed = "run: python -m scripts.seed_demo --phone <the number you'll call from, E.164>"
    try:
        with session_factory() as db:
            if not schema_is_current(db.connection()):
                return [Check(False, "the database schema is current", "run: alembic upgrade head")]
            customer = db.get(CustomerRecord, customer_id)
            credential = db.get(PhoneCredentialRecord, customer_id)
    except SQLAlchemyError as exc:
        return [Check(False, "the database can be read", f"{type(exc).__name__}; check COLLECTWISE_DATABASE_URL")]
    checks = [
        Check(True, "the database schema is current"),
        Check(customer is not None and customer.phone_number is not None,
              "the demo customer has a phone number", seed),
        Check(credential is not None, "the demo customer has a phone PIN", seed),
    ]
    if credential is not None and credential.locked_until is not None:
        checks.append(Check(credential.locked_until <= datetime.now(timezone.utc), "the phone PIN isn't locked",
                            "three wrong PINs locked it for 15 minutes; wait, or set it again with seed_demo"))
    return checks


def tunnel_checks(fetch: Callable[[str, str], int | None]) -> list[Check]:
    webhook = fetch("POST", public_url(VOICE_PATH))
    if webhook is None:
        return [Check(False, "the public address answers",
                      "nothing answered: is ngrok running (ngrok http --url=<your domain> 8000) and the server up?")]
    checks = [Check(webhook == 403, "the webhook answers through the tunnel and refuses unsigned calls", {
        503: "the server isn't set up for phone calls: start it with .env loaded",
        404: "something else answered: is ngrok pointed at port 8000, and the server started with the same "
             "COLLECTWISE_PUBLIC_BASE_URL?",
    }.get(webhook, f"unexpected status {webhook}: is ngrok pointed at this server?"))]
    for path in ("/docs", "/live"):
        status = fetch("GET", public_url(path))
        checks.append(Check(status == 404, f"{path} is hidden on the public address",
                            "it answered: start the server with the same COLLECTWISE_PUBLIC_BASE_URL, and don't "
                            "let the tunnel rewrite the host header"))
    return checks


def main(argv: list[str] | None = None, *, session_factory: Callable = SessionLocal,
         fetch: Callable[[str, str], int | None] = fetch, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    checks = settings_checks(environ) + database_checks(session_factory)
    base_ok = public_base_ok()
    if base_ok:
        checks += tunnel_checks(fetch)
    for check in checks:
        print(f"ok    {check.label}" if check.ok else f"FIX   {check.label}: {check.fix}")
    if not base_ok:
        print("(the tunnel wasn't checked: no public address)")
    if all(check.ok for check in checks):
        print(f"\nReady. In Twilio, set the number's 'A call comes in' webhook (HTTP POST) to:\n  "
              f"{public_url(VOICE_PATH)}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
