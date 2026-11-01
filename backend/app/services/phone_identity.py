"""Who is on the phone (M7.2): a customer's phone number and phone PIN.

Caller ID only narrows a call down to one customer (it can be spoofed); the
6-digit PIN, typed on the keypad, decides. PINs are stored as salted scrypt
hashes in their own table and compared in constant time. Three wrong PINs in
a row lock a customer's PIN for 15 minutes, across calls. Nothing here logs
or returns a PIN, and a failed check says nothing about why.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import CustomerRecord, PhoneCredentialRecord

PIN_LENGTH = 6
MAX_FAILED_ATTEMPTS = 3
LOCKOUT = timedelta(minutes=15)
_E164 = re.compile(r"\+[1-9]\d{7,14}")
# scrypt cost: about 16 MB and tens of milliseconds a check.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class CustomerNotFoundError(Exception):
    pass


class InvalidPhoneNumberError(Exception):
    """Not an E.164 number such as +15555550100."""


class PhoneNumberInUseError(Exception):
    """Another customer already has this number."""


class WeakPinError(Exception):
    """Not 6 digits, or easy to guess (one digit repeated, or a straight run)."""


def normalize_phone(raw: str) -> str:
    number = re.sub(r"[\s().-]", "", raw or "")
    if not _E164.fullmatch(number):
        raise InvalidPhoneNumberError()
    return number


def check_pin_strength(pin: str) -> None:
    if not (len(pin) == PIN_LENGTH and pin.isascii() and pin.isdigit()):
        raise WeakPinError()
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if steps in ({0}, {1}, {-1}):
        raise WeakPinError()


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.scrypt(pin.encode(), salt=salt, dklen=32, **_SCRYPT).hex()


def set_phone_and_pin(db: Session, customer_id: str, *, phone_number: str, pin: str) -> None:
    customer = db.get(CustomerRecord, customer_id)
    if customer is None:
        raise CustomerNotFoundError()
    number = normalize_phone(phone_number)
    check_pin_strength(pin)
    salt = secrets.token_bytes(16)
    customer.phone_number = number
    credential = db.get(PhoneCredentialRecord, customer_id)
    if credential is None:
        credential = PhoneCredentialRecord(customer_id=customer_id, failed_attempts=0)
        db.add(credential)
    credential.pin_hash = _hash(pin, salt)
    credential.pin_salt = salt.hex()
    credential.failed_attempts = 0
    credential.locked_until = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise PhoneNumberInUseError() from None


@dataclass(frozen=True)
class PinCheck:
    customer_id: str | None  # set only when the caller is verified
    # Why it failed, for events and logs only: never said to the caller.
    reason: str  # "verified", "unknown_number", "no_pin", "locked", "wrong_pin"


def check_phone_pin(db: Session, caller_number: str | None, pin: str, *, now: datetime) -> PinCheck:
    """Whether `pin` is the PIN of the customer who has `caller_number`."""
    try:
        number = normalize_phone(caller_number or "")
    except InvalidPhoneNumberError:
        number = None
    customer = db.scalar(select(CustomerRecord).where(CustomerRecord.phone_number == number)) if number else None
    credential = db.get(PhoneCredentialRecord, customer.customer_id) if customer else None
    if customer is None:
        _hash(pin, b"\x00" * 16)  # the same work either way, so timing doesn't tell
        return PinCheck(None, "unknown_number")
    if credential is None:
        _hash(pin, b"\x00" * 16)
        return PinCheck(None, "no_pin")
    if credential.locked_until is not None:
        if credential.locked_until > now:
            _hash(pin, b"\x00" * 16)
            return PinCheck(None, "locked")
        credential.locked_until = None
        credential.failed_attempts = 0
    if hmac.compare_digest(_hash(pin, bytes.fromhex(credential.pin_salt)), credential.pin_hash):
        credential.failed_attempts = 0
        db.commit()
        return PinCheck(customer.customer_id, "verified")
    credential.failed_attempts += 1
    if credential.failed_attempts >= MAX_FAILED_ATTEMPTS:
        credential.locked_until = now + LOCKOUT
    db.commit()
    return PinCheck(None, "wrong_pin")
