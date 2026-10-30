"""Per-customer rate limits on the agent routes (D72), so a signed-in client
can't run up model and speech costs.

In memory and per process: several server processes would need a shared
store (M6). Each limit is checked right after the session token, before the
database, the model, speech or the request body are touched.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Depends, HTTPException

from backend.app import config
from backend.app.auth.dependencies import authenticated_customer_id


class SlidingWindowLimiter:
    """At most `limit` hits per key in any `window_s` seconds."""

    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_s <= 0:
            raise ValueError("Rate limits must be positive")
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = clock()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """Keys currently tracked."""
        with self._lock:
            return len(self._hits)

    def hit(self, key: str) -> float | None:
        """Count a hit for `key` and return None if it's within the limit.

        Otherwise return the seconds until the next hit would be allowed; a
        refused hit isn't counted, so retrying doesn't extend the wait.
        """
        now = self._clock()
        cutoff = now - self._window_s
        with self._lock:
            if now - self._last_sweep >= self._window_s:
                # Forget keys with no recent hits, so memory follows the
                # customers active in the last window, not all customers ever.
                for stale in [k for k, hits in self._hits.items() if hits[-1] <= cutoff]:
                    del self._hits[stale]
                self._last_sweep = now

            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                return hits[0] + self._window_s - now
            hits.append(now)
            return None


_turn_limiter = SlidingWindowLimiter(config.TURNS_PER_MINUTE)
_session_start_limiter = SlidingWindowLimiter(config.SESSION_STARTS_PER_MINUTE)


# Tests override these with fresh limiters.
def get_turn_limiter() -> SlidingWindowLimiter:
    return _turn_limiter


def get_session_start_limiter() -> SlidingWindowLimiter:
    return _session_start_limiter


def _enforce(limiter: SlidingWindowLimiter, customer_id: str) -> None:
    wait = limiter.hit(customer_id)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )


def limit_turns(
    customer_id: str = Depends(authenticated_customer_id),
    limiter: SlidingWindowLimiter = Depends(get_turn_limiter),
) -> None:
    """Typed and spoken turns share one allowance."""
    _enforce(limiter, customer_id)


def limit_session_starts(
    customer_id: str = Depends(authenticated_customer_id),
    limiter: SlidingWindowLimiter = Depends(get_session_start_limiter),
) -> None:
    _enforce(limiter, customer_id)
