import base64
import hashlib
import hmac
import json

import pytest

from backend.app.auth.session_tokens import (
    InvalidSessionToken,
    issue_session_token,
    verify_session_token,
)

SECRET = b"test-secret-that-is-at-least-32-bytes!!"
OTHER_SECRET = b"another-secret-that-is-32-bytes-long!!!"
NOW = 1_800_000_000


def issue(customer_id: str = "cust_a", ttl: int = 600, secret: bytes = SECRET) -> str:
    return issue_session_token(customer_id, secret=secret, ttl_seconds=ttl, now=NOW)


def verify(token: str, now: float = NOW) -> str:
    return verify_session_token(token, secret=SECRET, now=now)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def forge(payload: bytes, secret: bytes = SECRET) -> str:
    """A correctly signed token with an arbitrary payload, signed independently
    of the module under test."""
    payload_part = b64(payload)
    signature = hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{b64(signature)}"


def test_issued_token_verifies_to_its_customer() -> None:
    assert verify(issue("cust_a")) == "cust_a"


def test_token_is_valid_until_just_before_expiry() -> None:
    token = issue(ttl=600)

    assert verify(token, now=NOW + 599) == "cust_a"
    with pytest.raises(InvalidSessionToken, match="expired"):
        verify(token, now=NOW + 600)


def test_token_payload_is_readable_but_holds_nothing_secret() -> None:
    payload_part = issue("cust_a").split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(payload_part + "=="))

    assert payload == {"exp": NOW + 600, "sub": "cust_a"}


def test_changing_the_customer_breaks_the_signature() -> None:
    payload_part, signature_part = issue("cust_a").split(".")
    forged_payload = b64(json.dumps({"exp": NOW + 600, "sub": "cust_b"}).encode())

    with pytest.raises(InvalidSessionToken, match="bad_signature"):
        verify(f"{forged_payload}.{signature_part}")


def test_extending_the_expiry_breaks_the_signature() -> None:
    _, signature_part = issue("cust_a").split(".")
    forged_payload = b64(json.dumps({"exp": NOW + 10**9, "sub": "cust_a"}).encode())

    with pytest.raises(InvalidSessionToken, match="bad_signature"):
        verify(f"{forged_payload}.{signature_part}")


def test_altered_signature_is_rejected() -> None:
    payload_part, signature_part = issue("cust_a").split(".")
    flipped = ("A" if signature_part[0] != "A" else "B") + signature_part[1:]

    with pytest.raises(InvalidSessionToken, match="bad_signature"):
        verify(f"{payload_part}.{flipped}")


def test_token_signed_with_another_secret_is_rejected() -> None:
    with pytest.raises(InvalidSessionToken, match="bad_signature"):
        verify(issue(secret=OTHER_SECRET))


@pytest.mark.parametrize(
    "token",
    [
        "",
        "no-dot-at-all",
        "a.b.c",
        "payload.",
        ".signature",
        "pàyload.signature",
        None,
        12345,
    ],
)
def test_malformed_tokens_are_rejected(token: object) -> None:
    with pytest.raises(InvalidSessionToken):
        verify(token)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[1, 2, 3]",
        json.dumps({"sub": "cust_a"}).encode(),
        json.dumps({"exp": NOW + 600}).encode(),
        json.dumps({"exp": str(NOW + 600), "sub": "cust_a"}).encode(),
        json.dumps({"exp": True, "sub": "cust_a"}).encode(),
        json.dumps({"exp": NOW + 600, "sub": ""}).encode(),
        json.dumps({"exp": NOW + 600, "sub": "x" * 51}).encode(),
        json.dumps({"exp": NOW + 600, "sub": 42}).encode(),
    ],
)
def test_correctly_signed_but_invalid_payload_is_rejected(payload: bytes) -> None:
    with pytest.raises(InvalidSessionToken, match="malformed"):
        verify(forge(payload))


def test_correctly_signed_valid_payload_is_accepted() -> None:
    # Confirms the forge helper matches the real format, so the
    # rejections above are about the payload, not the signing.
    token = forge(json.dumps({"exp": NOW + 600, "sub": "cust_a"}).encode())

    assert verify(token) == "cust_a"


@pytest.mark.parametrize("secret", [b"", b"short", b"x" * 31, "a-str-not-bytes" * 4])
def test_weak_or_wrong_type_secret_is_refused(secret: object) -> None:
    with pytest.raises(ValueError):
        issue_session_token("cust_a", secret=secret, ttl_seconds=60, now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        verify_session_token("a.b", secret=secret, now=NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("customer_id", "ttl"),
    [("", 60), ("x" * 51, 60), ("cust_a", 0), ("cust_a", -5)],
)
def test_issue_rejects_invalid_input(customer_id: str, ttl: int) -> None:
    with pytest.raises(ValueError):
        issue_session_token(customer_id, secret=SECRET, ttl_seconds=ttl, now=NOW)
