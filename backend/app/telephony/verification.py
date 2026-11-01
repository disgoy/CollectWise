"""Verifying a phone caller before the agent exists (M7.2).

Fixed prompts in the agent's voice, a 6-digit PIN typed on the keypad, and
the check in phone_identity: the caller's number must belong to a customer
and the PIN must be theirs. Every failure sounds the same, so a caller can't
learn which numbers are customers or which PINs are locked. Digits pass
through here once and are dropped: never logged, stored or shown to the
model.
"""

from collections.abc import Awaitable, Callable

from backend.app.services.phone_identity import PIN_LENGTH, PinCheck

GREETING = "Hello, you've reached the CollectWise automated assistant."
ASK_PIN = "For your security, please enter your six-digit phone PIN on your keypad."
TRY_AGAIN = "Sorry, that didn't match. Please enter your six-digit PIN again."
NOT_VERIFIED = "Sorry, I couldn't verify you. Please try again later. Goodbye."
VERIFIED = "Thank you, you're verified. How can I help you today?"

ATTEMPTS = 3
FIRST_KEY_S = 15.0  # to start typing after the prompt
NEXT_KEY_S = 5.0  # between keys


async def collect_pin(keypad: Callable[[float], Awaitable[str | None]]) -> str | None:
    """Six digits, or None if the caller stops typing or hangs up. `*` starts
    again; anything else that isn't a digit is ignored."""
    digits: list[str] = []
    timeout = FIRST_KEY_S
    while len(digits) < PIN_LENGTH:
        key = await keypad(timeout)
        if key is None:
            return None
        if key == "*":
            digits.clear()
        elif key.isdigit():
            digits.append(key)
        timeout = NEXT_KEY_S
    return "".join(digits)


async def verify_caller(
    *,
    keypad: Callable[[float], Awaitable[str | None]],
    speak: Callable[[str], Awaitable[None]],
    check: Callable[[str], Awaitable[PinCheck]],
    hung_up: Callable[[], bool],
    failed: Callable[[str], Awaitable[None]],
    verified: Callable[[], None] = lambda: None,
) -> str | None:
    """The verified customer's id, or None. `failed(reason)` records each
    failed attempt (a code only); `verified()` runs the moment the PIN is
    accepted, before the confirmation is spoken, so whatever the caller says
    from then on is kept for the agent."""
    await speak(f"{GREETING} {ASK_PIN}")
    for attempt in range(1, ATTEMPTS + 1):
        pin = await collect_pin(keypad)
        if hung_up():
            return None
        if pin is None:
            await failed("no_entry")  # a timeout is not a wrong PIN: no lockout count
        else:
            result = await check(pin)
            if result.customer_id is not None:
                verified()
                await speak(VERIFIED)
                return result.customer_id
            await failed(result.reason)
        if attempt < ATTEMPTS:
            await speak(TRY_AGAIN)
    await speak(NOT_VERIFIED)
    return None
