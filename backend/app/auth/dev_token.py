"""Print a session token for a synthetic customer. Local testing only.

Stands in for the app the caller would have signed in to.

Usage:
    COLLECTWISE_SESSION_SECRET=<at least 32 characters> \\
        python -m backend.app.auth.dev_token <customer_id>
"""

import sys

from backend.app import config
from backend.app.auth.session_tokens import issue_session_token


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python -m backend.app.auth.dev_token <customer_id>", file=sys.stderr)
        return 2

    if not config.SESSION_TOKEN_SECRET:
        print("COLLECTWISE_SESSION_SECRET is not set", file=sys.stderr)
        return 1

    try:
        token = issue_session_token(
            argv[1],
            secret=config.SESSION_TOKEN_SECRET.encode("utf-8"),
            ttl_seconds=config.SESSION_TOKEN_TTL_SECONDS,
        )
    except ValueError as exc:
        print(f"Cannot issue token: {exc}", file=sys.stderr)
        return 1

    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
