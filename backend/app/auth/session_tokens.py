"""Signed caller session tokens (D39).

Format: ``<payload>.<signature>``, both base64url without padding.
The payload is JSON ``{"exp": <unix seconds>, "sub": <customer_id>}`` and the
signature is HMAC-SHA256 of the payload part, keyed with a server secret.

Tokens are signed, not encrypted: anyone holding one can read the customer ID,
so never put secrets in the payload.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time

MIN_SECRET_BYTES = 32
MAX_CUSTOMER_ID_LENGTH = 50


class InvalidSessionToken(Exception):
    """The token is malformed, forged or expired.

    Callers should report one generic error; the message (the reason) is for
    server-side logs only.
    """


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _check_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"Session secret must be at least {MIN_SECRET_BYTES} bytes")


def _sign(secret: bytes, payload_part: str) -> str:
    digest = hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def _valid_customer_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_CUSTOMER_ID_LENGTH


def issue_session_token(
    customer_id: str,
    *,
    secret: bytes,
    ttl_seconds: int,
    now: float | None = None,
) -> str:
    """Issue a token for an already-authenticated customer.

    In production the app the caller logged into would do this; here it is
    used by tests and a dev helper.
    """
    _check_secret(secret)
    if not _valid_customer_id(customer_id):
        raise ValueError("Invalid customer ID")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    issued_at = time.time() if now is None else now
    payload = {"exp": int(issued_at) + ttl_seconds, "sub": customer_id}
    payload_part = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_part}.{_sign(secret, payload_part)}"


def verify_session_token(
    token: str,
    *,
    secret: bytes,
    now: float | None = None,
) -> str:
    """Return the customer ID of a valid token. Raises InvalidSessionToken."""
    _check_secret(secret)

    # Real tokens are pure ASCII (base64url plus one dot).
    if not isinstance(token, str) or not token.isascii():
        raise InvalidSessionToken("malformed")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidSessionToken("malformed")
    payload_part, signature_part = parts

    # Check the signature before reading anything from the payload, and
    # compare in constant time so timing does not reveal the signature.
    expected = _sign(secret, payload_part)
    if not hmac.compare_digest(expected.encode("ascii"), signature_part.encode("ascii")):
        raise InvalidSessionToken("bad_signature")

    try:
        payload = json.loads(_b64decode(payload_part))
    except (binascii.Error, ValueError):
        raise InvalidSessionToken("malformed") from None

    if not isinstance(payload, dict):
        raise InvalidSessionToken("malformed")

    customer_id = payload.get("sub")
    expires_at = payload.get("exp")

    # bool is a subclass of int in Python, so exclude it explicitly.
    if not _valid_customer_id(customer_id) or not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise InvalidSessionToken("malformed")

    current_time = time.time() if now is None else now
    if current_time >= expires_at:
        raise InvalidSessionToken("expired")

    return customer_id
