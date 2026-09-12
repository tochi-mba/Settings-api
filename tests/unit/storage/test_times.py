"""How a moment in time becomes a column, and comes back unchanged.

Two properties, both of which fail silently: timezone awareness survives the round trip,
and lexicographic order matches chronological order -- which only holds because the
rendered stamp is fixed width, including for a zero microsecond count that ``isoformat``
would otherwise shorten.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from settings_api.storage.times import (
    from_column,
    from_column_optional,
    to_column,
    to_column_optional,
)

LISBON_SUMMER = timezone(timedelta(hours=1))


class TestAwareness:
    def test_a_naive_datetime_is_refused(self) -> None:
        # A naive datetime means a caller read the wall clock instead of the injected one,
        # which is a bug worth failing on rather than guessing a zone for.
        with pytest.raises(ValueError, match="naive datetime"):
            to_column(datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001 -- the point of the test

    def test_a_non_utc_datetime_is_stored_in_utc(self) -> None:
        stamp = to_column(datetime(2026, 6, 1, 13, 0, 0, tzinfo=LISBON_SUMMER))
        assert stamp == "2026-06-01T12:00:00.000000+00:00"

    def test_the_round_trip_preserves_the_instant_and_comes_back_aware(self) -> None:
        moment = datetime(2026, 6, 1, 13, 30, 15, 123456, tzinfo=LISBON_SUMMER)
        back = from_column(to_column(moment))
        assert back == moment
        assert back.tzinfo is UTC


class TestFixedWidth:
    @pytest.mark.parametrize(
        "moment",
        [
            datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
            datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 6, 1, 13, 0, 0, tzinfo=LISBON_SUMMER),
        ],
    )
    def test_every_stamp_is_the_same_width(self, moment: datetime) -> None:
        # isoformat() drops the fractional part when it is zero; timespec="microseconds"
        # is what stops it, and this is the test that would catch its removal.
        assert len(to_column(moment)) == len("2026-01-01T00:00:00.000000+00:00")

    def test_text_order_is_time_order(self) -> None:
        moments = [
            datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, 1, 0, tzinfo=UTC),  # the zero-microsecond case
            datetime(2026, 1, 1, 0, 0, 1, 1, tzinfo=UTC),
            datetime(2025, 12, 31, 23, 0, 0, tzinfo=LISBON_SUMMER),  # 22:00 UTC, earliest
        ]
        stamps = [to_column(moment) for moment in moments]
        assert sorted(stamps) == [to_column(moment) for moment in sorted(moments)]


class TestOptional:
    def test_none_passes_through_both_ways(self) -> None:
        assert to_column_optional(None) is None
        assert from_column_optional(None) is None

    def test_a_value_goes_through_the_strict_functions(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        assert to_column_optional(moment) == to_column(moment)
        assert from_column_optional(to_column(moment)) == moment
