"""Time as an injectable dependency.

Nothing in this codebase calls :func:`datetime.now` or :func:`time.monotonic` directly,
and here that rule earns its keep twice.

The ordinary reason first: a token expires, a JWKS cache goes stale, and neither rule is
testable if time is what the wall clock happens to say.

The reason particular to this service is the retention window. A retired catalogue key's
rows survive for ``retired_retention_days`` -- ninety by default -- before the sweeper
destroys them, because deleting on sight would destroy a person's choice the instant
somebody merged a catalogue typo. A test that had to wait ninety days would not be
written, and a rule that is not tested is a rule that is not true.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Reads the current time.

    Two readings are exposed because they answer different questions: :meth:`now` is a
    timestamp fit to record and compare against an expiry, while :meth:`monotonic`
    measures elapsed duration and is immune to system clock adjustments.
    """

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonically increasing number of seconds from an arbitrary origin."""
        ...


class SystemClock:
    """The real clock, used everywhere outside tests."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
