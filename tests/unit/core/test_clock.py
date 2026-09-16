"""Time enters this service through one door, and this file is the door.

`settings_api.core.clock` holds the only calls to :func:`datetime.now` and
:func:`time.monotonic` in ``src/`` (AGENTS.md invariant 9), so this is the one test module
that reads the wall clock -- everywhere else reads :class:`~tests.fakes.clock.FakeClock`.
What is pinned here is the bargain that makes that substitution safe.

Three properties, each of which fails silently if it goes:

**A stamp is timezone-aware UTC.** ``storage/times.to_column`` refuses a naive datetime,
so a clock that returned one would raise at the first write, in a code path far from the
clock that produced it. The round-trip assertion below is that refusal, stated forwards.

**A monotonic reading only ever goes up.** It is what measures the JWKS cache's age; a
reading that could jump backwards when an operator stepped the system clock would make a
cached key look freshly fetched.

**The fake is substitutable for the real one.** Not by inheritance -- nothing inherits
from :class:`~settings_api.core.clock.Clock` -- but structurally, which is exactly the
claim that goes unchecked until something checks it. If the two disagreed about the shape
of a reading, every other test in this suite would be exercising a shape production never
sees.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from settings_api.core.clock import Clock, SystemClock
from settings_api.storage.times import from_column, to_column
from tests.fakes.clock import EPOCH, FakeClock

_SAMPLES = 10_000
"""Enough readings that the ends of the list are separated by real work.

A single pair of calls could legitimately land in the same tick of a coarse clock, and a
test that failed for that reason would be a flake rather than a finding.
"""

_PATIENCE_SECONDS = 1.0
"""How long to wait for a coarse monotonic clock to tick before calling it frozen."""

CLOCK_FACTORIES: list[Callable[[], Clock]] = [SystemClock, FakeClock]
CLOCK_IDS = ["system", "fake"]


class _ReportsOnlyATimestamp:
    """Half a clock: it can say when it is, but not how long something took."""

    def now(self) -> datetime:
        return EPOCH


class _ReportsOnlyADuration:
    """The other half: elapsed seconds, but no timestamp to record or compare."""

    def monotonic(self) -> float:
        return 0.0


class TestTheTimeTheSystemClockReports:
    """`SystemClock.now` is what every stored timestamp and every expiry check starts as."""

    def test_the_time_it_reports_is_timezone_aware_utc(self) -> None:
        stamp = SystemClock().now()

        assert stamp.tzinfo is not None
        # UTC as an offset rather than as a name: a fixed +00:00 zone is a correct answer
        # and a named local zone that happens to be at +00:00 today is not, because it
        # would silently become +01:00 in summer.
        assert stamp.utcoffset() == timedelta(0)

    def test_a_stamp_it_produces_survives_the_trip_through_a_column(self) -> None:
        stamp = SystemClock().now()

        # This is what "timezone-aware" buys, stated as behaviour: `to_column` raises on a
        # naive datetime, so a clock that lost its zone would fail here rather than at the
        # first write in some unrelated store.
        assert from_column(to_column(stamp)) == stamp

    def test_the_time_it_reports_never_runs_backwards(self) -> None:
        readings = [SystemClock().now() for _ in range(_SAMPLES)]

        # Non-decreasing rather than strictly increasing on purpose: a wall clock may be
        # stepped or held, and demanding that it always advance is what `monotonic` is
        # for. What must never happen is a stamp older than one already handed out.
        assert readings == sorted(readings)

    def test_a_system_clock_cannot_be_given_state_to_go_stale(self) -> None:
        clock = SystemClock()

        # `__slots__ = ()` is why one shared instance is safe to inject everywhere: there
        # is no attribute on it where a reading could be cached and then served again.
        with pytest.raises(AttributeError):
            clock.cached = EPOCH  # type: ignore[attr-defined]


class TestTheDurationTheSystemClockMeasures:
    """`SystemClock.monotonic` is what ages the JWKS cache, so it must only go up."""

    def test_a_reading_is_a_plain_number_of_seconds(self) -> None:
        reading: object = SystemClock().monotonic()

        # Seconds as a float, not a datetime and not nanoseconds: every caller subtracts
        # two readings and compares the difference against a TTL expressed in seconds.
        assert isinstance(reading, float)

    def test_successive_readings_never_run_backwards(self) -> None:
        readings = [SystemClock().monotonic() for _ in range(_SAMPLES)]

        assert readings == sorted(readings)

    def test_the_reading_advances_as_work_is_done(self) -> None:
        clock = SystemClock()
        first = clock.monotonic()

        # A frozen monotonic clock would satisfy "never runs backwards" while making every
        # cache immortal, so measure that time actually passes. Read until it moves rather
        # than a fixed number of times: Windows ticks its monotonic clock roughly every
        # 15.6 ms, and ten thousand calls can finish inside one tick on a fast machine. The
        # bound is on real elapsed time, so a clock that truly never moves still fails.
        deadline = time.perf_counter() + _PATIENCE_SECONDS
        while clock.monotonic() == first and time.perf_counter() < deadline:
            pass

        assert clock.monotonic() > first


class TestWhatCountsAsAClock:
    """The port is structural: satisfying the shape is the whole of the membership test."""

    @pytest.mark.parametrize("build", CLOCK_FACTORIES, ids=CLOCK_IDS)
    def test_an_implementation_is_a_clock_without_inheriting_from_the_protocol(
        self, build: Callable[[], Clock]
    ) -> None:
        candidate: object = build()

        assert isinstance(candidate, Clock)
        # Nothing subclasses `Clock`, and requiring that it did would mean tests/fakes
        # importing production code to be allowed to stand in for it.
        assert Clock not in type(candidate).__mro__

    @pytest.mark.parametrize("implementation", CLOCK_FACTORIES, ids=CLOCK_IDS)
    def test_a_class_is_recognised_without_being_instantiated(
        self, implementation: Callable[[], Clock]
    ) -> None:
        # `issubclass` against a runtime-checkable Protocol is only allowed while every
        # member is a method. If someone adds a non-method attribute to `Clock`, this call
        # starts raising TypeError -- which is the warning worth having.
        assert issubclass(implementation, Clock)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "candidate",
        [_ReportsOnlyATimestamp(), _ReportsOnlyADuration(), object()],
        ids=["no-monotonic", "no-now", "neither"],
    )
    def test_an_object_missing_half_the_protocol_is_not_a_clock(self, candidate: object) -> None:
        # Both readings are required together because they answer different questions: a
        # stand-in offering only one would type-check at the seam and fail at the call.
        assert not isinstance(candidate, Clock)

    @pytest.mark.parametrize("build", CLOCK_FACTORIES, ids=CLOCK_IDS)
    def test_being_recognised_is_not_evidence_that_the_methods_answer(
        self, build: Callable[[], Clock]
    ) -> None:
        clock = build()

        stamp: object = clock.now()
        reading: object = clock.monotonic()

        # `isinstance` against a runtime-checkable Protocol only looks for the names; from
        # 3.12 it executes nothing at all, where until 3.11 it ran property getters. So
        # the methods are called here rather than trusting the membership check to have
        # called anything -- that difference is why `make matrix` runs both interpreters.
        assert isinstance(stamp, datetime)
        assert isinstance(reading, float)


class TestTheFakeIsSubstitutableForTheReal:
    """Every other test in this suite runs against the fake, on the strength of this one."""

    @pytest.mark.parametrize("build", CLOCK_FACTORIES, ids=CLOCK_IDS)
    def test_both_clocks_report_a_storable_utc_timestamp(self, build: Callable[[], Clock]) -> None:
        stamp = build().now()

        # The two must be indistinguishable to a caller: a fake whose stamps were naive
        # would let a store that rejects naive datetimes pass its whole unit suite.
        assert stamp.utcoffset() == timedelta(0)
        assert from_column(to_column(stamp)) == stamp

    @pytest.mark.parametrize("build", CLOCK_FACTORIES, ids=CLOCK_IDS)
    def test_both_clocks_report_elapsed_seconds_as_a_float(
        self, build: Callable[[], Clock]
    ) -> None:
        reading: object = build().monotonic()

        assert isinstance(reading, float)

    def test_the_fake_does_not_drift_while_a_test_reads_it(self) -> None:
        clock = FakeClock()

        before = clock.now()
        elapsed = {clock.monotonic() for _ in range(_SAMPLES)}

        # Ten thousand reads move a real clock and must move this one not at all: a fake
        # that drifted would put wall-clock flakiness back into every test that pinned
        # time precisely to get rid of it.
        assert clock.now() == before
        assert elapsed == {0.0}

    def test_the_fake_starts_where_it_is_told_to(self) -> None:
        start = datetime(2019, 3, 4, 5, 6, 7, tzinfo=UTC)

        clock = FakeClock(start)

        assert clock.now() == start
        # Elapsed time is measured from the start it was given, so a test that begins in
        # 2019 does not begin with a monotonic reading of several years.
        assert clock.monotonic() == 0.0

    @pytest.mark.parametrize(
        ("delta", "expected_seconds"),
        [
            (timedelta(minutes=5), 300.0),
            (90.0, 90.0),
            (timedelta(days=90), 7_776_000.0),
            (timedelta(0), 0.0),
        ],
        ids=["timedelta", "bare-seconds", "retention-window", "no-move"],
    )
    def test_advancing_the_fake_moves_both_readings_together(
        self, delta: timedelta | float, expected_seconds: float
    ) -> None:
        clock = FakeClock()
        before_stamp = clock.now()
        before_elapsed = clock.monotonic()

        clock.advance(delta)

        # The two readings have to agree, and `advance` has to accept seconds as readily
        # as a timedelta: the retention window is ninety days and the JWKS TTL is a number
        # of seconds, and a test should be able to state either one the way it is written.
        assert clock.now() - before_stamp == timedelta(seconds=expected_seconds)
        assert clock.monotonic() - before_elapsed == expected_seconds
