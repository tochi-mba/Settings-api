"""A clock that only moves when a test tells it to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

DECADE_SECONDS = 10 * 365 * 24 * 3_600
"""A token lifetime long enough to survive any clock move a test makes.

Earned the hard way in user-api: advancing the clock past a ninety-day retention window
also expires the token the test is holding, and the failure reads as an authorisation bug
rather than as a test that moved time too far. Mint test tokens with this unless the test
is *about* expiry.
"""


class FakeClock:
    """Deterministic :class:`~settings_api.core.clock.Clock` implementation.

    Three rules in this service are arithmetic on a date -- a token's expiry, the JWKS
    cache's age, and how long a retired key's rows survive -- and the third is measured in
    months. Without this, testing the retention window would mean waiting ninety days.
    """

    def __init__(self, start: datetime = EPOCH) -> None:
        self._start = start
        self._now = start

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return (self._now - self._start).total_seconds()

    def advance(self, delta: timedelta | float) -> None:
        """Move time forward by a timedelta or a number of seconds."""
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        self._now += delta
