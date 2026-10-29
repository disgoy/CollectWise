import logging

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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


def authenticated_customer_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """The customer ID from a valid `Authorization: Bearer` session token.

    This is the only way caller identity enters the agent API (D39).
    """
    # Read at request time, so a missing secret is caught on every request.
    secret = config.SESSION_TOKEN_SECRET
    if not secret:
        logger.error("COLLECTWISE_SESSION_SECRET is not set")
        raise _NOT_CONFIGURED

    if credentials is None:
        raise _unauthorized()

    try:
        return verify_session_token(credentials.credentials, secret=secret.encode("utf-8"))
    except InvalidSessionToken as exc:
        # The reason stays in our logs; the client gets one generic answer.
        logger.info("Rejected session token: %s", exc)
        raise _unauthorized() from None
    except ValueError:
        logger.error("COLLECTWISE_SESSION_SECRET is too short")
        raise _NOT_CONFIGURED from None
