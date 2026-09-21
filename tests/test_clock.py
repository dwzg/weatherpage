"""Tests for the timestamp conventions."""

from datetime import UTC, datetime, timedelta

import pytest

from app import clock


class TestNormalise:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-06-20T21:00:00", "2026-06-20 21:00:00"),
        ("2026-06-20 21:00:00", "2026-06-20 21:00:00"),
        ("2026-06-20T21:00:00.500", "2026-06-20 21:00:00"),
        ("  2026-06-20T21:00:00  ", "2026-06-20 21:00:00"),
        ("2026-06-20T21:00:00+02:00", "2026-06-20 21:00:00"),
    ])
    def test_accepted_forms(self, raw, expected):
        assert clock.normalise_ts(raw) == expected

    @pytest.mark.parametrize("raw", ["not-a-date", "", "unavailable", "2026-13-45"])
    def test_rejects_rubbish(self, raw):
        with pytest.raises(ValueError):
            clock.normalise_ts(raw)

    def test_output_sorts_lexicographically(self):
        """Range queries are string comparisons, so ordering must hold."""
        stamps = [clock.normalise_ts(f"2026-0{m}-0{d}T0{h}:00:00")
                  for m in (1, 9) for d in (1, 9) for h in (1, 9)]
        assert stamps == sorted(stamps)


class TestPeriodCutoff:
    REF = datetime(2026, 6, 20, 14, 30, 0)

    @pytest.mark.parametrize("period,delta", [
        ("3h", timedelta(hours=3)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)),
    ])
    def test_relative_periods(self, period, delta):
        assert clock.period_cutoff(period, self.REF) == self.REF - delta

    def test_today_is_local_midnight(self):
        assert clock.period_cutoff("today", self.REF) == datetime(2026, 6, 20, 0, 0, 0)

    def test_all_is_unbounded(self):
        assert clock.period_cutoff("all", self.REF) is None

    def test_cutoff_formats_to_the_stored_shape(self):
        """A cutoff with a 'T' or a UTC offset would silently match nothing."""
        formatted = clock.fmt_ts(clock.period_cutoff("24h", self.REF))
        assert formatted == "2026-06-19 14:30:00"
        assert "T" not in formatted and "+" not in formatted


class TestMonthsAgo:
    def test_exact_boundaries(self):
        ref = datetime(2026, 6, 20, 14, 30)
        assert clock.months_ago(0, ref) == datetime(2026, 6, 1)
        assert clock.months_ago(3, ref) == datetime(2026, 3, 1)
        assert clock.months_ago(6, ref) == datetime(2025, 12, 1)
        assert clock.months_ago(24, ref) == datetime(2024, 6, 1)

    def test_does_not_drift_like_30_day_months(self):
        """The old code used months*30 days, which slid off the boundary."""
        ref = datetime(2026, 3, 31, 12, 0)
        assert clock.months_ago(1, ref) == datetime(2026, 2, 1)


class TestResolveOffset:
    """The offset is what tells the two passes of a repeated hour apart."""

    # Europe/Berlin falls back at 03:00 on 2026-10-25: 02:00-02:59 runs twice,
    # first at +02:00 and again at +01:00.
    AMBIGUOUS = "2026-10-25 02:30:00"
    FIRST_PASS = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    SECOND_PASS = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("timestamp", "expected"),
        [
            ("2026-06-20 12:00:00", 120),  # summer time
            ("2026-01-20 12:00:00", 60),   # winter time
            ("2026-03-29 02:30:00", 60),   # never happened; read as pre-transition
        ],
    )
    def test_unambiguous_times_need_no_arrival(self, timestamp, expected):
        assert clock.resolve_offset(timestamp) == expected

    def test_arrival_picks_the_pass(self):
        assert clock.resolve_offset(self.AMBIGUOUS, self.FIRST_PASS) == 120
        assert clock.resolve_offset(self.AMBIGUOUS, self.SECOND_PASS) == 60

    def test_without_an_arrival_it_is_the_first_pass(self):
        assert clock.resolve_offset(self.AMBIGUOUS) == 120

    @pytest.mark.parametrize("suffix, expected", [("+02:00", 120), ("+01:00", 60)])
    def test_an_explicit_offset_wins_over_the_arrival(self, suffix, expected):
        # The arrival says second pass; the timestamp says otherwise and knows.
        assert clock.resolve_offset(
            f"2026-10-25T02:30:00{suffix}", self.SECOND_PASS
        ) == expected

    def test_an_offset_the_zone_never_uses_is_ignored(self):
        # fmt_ts keeps the wall clock rather than converting, so a Z-suffixed
        # value is read as local time — and gets local time's offset.
        assert clock.resolve_offset("2026-06-20T12:00:00Z") == 120

    def test_rejects_something_that_is_not_a_timestamp(self):
        with pytest.raises(ValueError):
            clock.resolve_offset("unavailable")
