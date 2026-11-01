"""One-time passes for Twilio media streams (M7.1, M7.2).

The voice webhook (signature-checked) issues a pass for one call, carrying
the caller's number as Twilio reported it; the stream must present it, for
that exact call, within a minute, once. Who the caller is gets decided on the
stream, by their phone PIN (M7.2), so the pass names no customer. So a stream can
only start from a call our webhook accepted, whatever the subtleties of
signing a WebSocket handshake. HMAC-SHA256 with the session secret, under a
purpose of its own so it can't be confused with a session token.
"""

import base64
import hashlib
import hmac
import time

PURPOSE = b"collectwise-twilio-stream"
TTL_S = 60


class InvalidStreamToken(Exception):
    """The pass is malformed, forged, for another call, expired or used."""


def _signature(secret: bytes, payload: str) -> str:
    digest = hmac.new(secret, PURPOSE + b"|" + payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue(call_sid: str, caller_number: str, *, secret: bytes, now: float | None = None) -> str:
    """`caller_number` is E.164 or "" (withheld); it holds no dots."""
    expires = int((time.time() if now is None else now) + TTL_S)
    payload = f"{call_sid}.{caller_number}.{expires}"
    return f"{payload}.{_signature(secret, payload)}"


class StreamTokens:
    """Checks passes and remembers the ones used, until they would expire."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._used: dict[str, float] = {}

    def redeem(self, token: str, call_sid: str, *, secret: bytes) -> str:
        """The caller's number from a valid, unused pass for this call."""
        now = self._clock()
        self._used = {t: expiry for t, expiry in self._used.items() if expiry > now}
        try:
            for_call, caller_number, expires, signature = token.split(".")
            expiry = int(expires)
        except ValueError:
            raise InvalidStreamToken("malformed") from None
        payload = f"{for_call}.{caller_number}.{expires}"
        if not hmac.compare_digest(signature, _signature(secret, payload)):
            raise InvalidStreamToken("bad signature")
        if for_call != call_sid:
            raise InvalidStreamToken("another call")
        if expiry <= now:
            raise InvalidStreamToken("expired")
        if token in self._used:
            raise InvalidStreamToken("used")
        self._used[token] = expiry
        return caller_number
