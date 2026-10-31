import hmac
import logging

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from backend.app import config
from backend.app.auth.session_tokens import InvalidSessionToken, verify_session_token

logger = logging.getLogger(__name__)

# auto_error=False: we return our own 401 so every failure looks the same.
_bearer = HTTPBearer(auto_error=False)

_NOT_CONFIGURED = HTTPException(status_code=503, detail="Agent is not configured")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid or expired session token",
        headers={"WWW-Authenticate": "Bearer"},
    )


class SessionSecretNotConfigured(Exception):
    """COLLECTWISE_SESSION_SECRET is missing or too short."""


def customer_id_for_token(token: str) -> str:
    """The customer ID in a valid session token (D39). Raises
    InvalidSessionToken (the reason is for logs only) or
    SessionSecretNotConfigured. Shared by the Bearer dependency below and the
    live endpoint, whose token arrives as its first message (D83)."""
    # Read at request time, so a missing secret is caught on every request.
    secret = config.SESSION_TOKEN_SECRET
    if not secret:
        logger.error("COLLECTWISE_SESSION_SECRET is not set")
        raise SessionSecretNotConfigured()
    try:
        return verify_session_token(token, secret=secret.encode("utf-8"))
    except ValueError:
        logger.error("COLLECTWISE_SESSION_SECRET is too short")
        raise SessionSecretNotConfigured() from None


def authenticated_customer_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """The customer ID from a valid `Authorization: Bearer` session token.

    This is the only way caller identity enters the agent API (D39).
    """
    if not config.SESSION_TOKEN_SECRET:
        logger.error("COLLECTWISE_SESSION_SECRET is not set")
        raise _NOT_CONFIGURED

    if credentials is None:
        raise _unauthorized()

    try:
        return customer_id_for_token(credentials.credentials)
    except InvalidSessionToken as exc:
        # The reason stays in our logs; the client gets one generic answer.
        logger.info("Rejected session token: %s", exc)
        raise _unauthorized() from None
    except SessionSecretNotConfigured:
        raise _NOT_CONFIGURED from None


ADMIN_KEY_HEADER = "X-Admin-Key"
MIN_ADMIN_KEY_BYTES = 32

# auto_error=False for the same reason as _bearer.
_admin_key = APIKeyHeader(
    name=ADMIN_KEY_HEADER,
    scheme_name="AdminKey",
    description="Back-office key (COLLECTWISE_ADMIN_KEY)",
    auto_error=False,
)


def require_admin(supplied: str | None = Depends(_admin_key)) -> None:
    """Allow the request only with the back-office key (D70).

    The back-office routes create customers and accounts and record requests
    for any customer, so callers never use them: callers reach the agent with
    a session token. One shared key stands in for staff sign-in; it says "an
    operator", not which one.
    """
    # Read at request time, like the session secret.
    expected = config.ADMIN_API_KEY
    if not expected or len(expected.encode("utf-8")) < MIN_ADMIN_KEY_BYTES:
        logger.error("COLLECTWISE_ADMIN_KEY is not set or is too short")
        raise HTTPException(status_code=503, detail="Admin API is not configured")

    # Constant-time comparison, so response timing doesn't reveal how much of
    # a guess was right.
    if supplied is None or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")
