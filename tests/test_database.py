"""Tests for the storage layer."""

from datetime import datetime, timedelta

import pytest

from app import clock


def ts(moment: datetime) -> str:
    return moment.strftime(clock.TS_FORMAT)


class TestInsert:
    async def test_roundtrip(self, db):
        await db.insert_reading(21.5, 55.0, 1013.0, "2026-06-20T21:00:00")
        current = await db.get_current()
        assert current["temperature"] == 21.5
        assert current["timestamp"] == "2026-06-20 21:00:00"

    async def test_does_not_expose_internal_columns(self, db):
        await db.insert_reading(21.5, 55.0, 1013.0, "2026-06-20T21:00:00")
        assert set(await db.get_current()) == {"timestamp", "temperature", "humidity", "pressure"}

    async def test_same_timestamp_updates_rather_than_duplicating(self, db):
        await db.insert_reading(21.5, 55.0, 1013.0, "2026-06-20T21:00:00")
        await db.insert_reading(25.0, 60.0, 1010.0, "2026-06-20T21:00:00")
        history = await db.get_history("all")
        assert len(history) == 1
        assert history[0]["temperature"] == 25.0

    async def test_current_follows_timestamp_not_insertion_order(self, db):
        """A backfill writes old readings with fresh ids; they are not 'current'."""
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:00:00")
        await db.insert_reading(-3.0, 95.0, 980.0, "2025-01-15T03:00:00")  # backfilled
        current = await db.get_current()
        assert current["timestamp"] == "2026-06-20 12:00:00"
        assert current["temperature"] == 20.0


class TestCleanup:
    async def test_removes_off_grid_readings(self, db):
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:00:00")  # on grid
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:05:00")  # on grid
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:07:00")  # off grid
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:10:30")  # stray seconds

        assert await db.remove_off_grid_readings() == 2
        remaining = [r["timestamp"] for r in await db.get_history("all")]
        assert remaining == ["2026-06-20 12:00:00", "2026-06-20 12:05:00"]

    async def test_range_delete(self, db):
        for minute in range(0, 30, 5):
            await db.insert_reading(20.0, 50.0, 1013.0, f"2026-06-20T12:{minute:02d}:00")
        deleted = await db.remove_readings_in_range("2026-06-20T12:05:00", "2026-06-20T12:15:00")
        assert deleted == 3
        assert len(await db.get_history("all")) == 3

    async def test_range_delete_rejects_rubbish(self, db):
        with pytest.raises(ValueError):
            await db.remove_readings_in_range("nonsense", "2026-06-20T12:00:00")


class TestHistoryDownsampling:
    async def _seed(self, db, days: int, end: datetime):
        """Write readings every 5 minutes across ``days``."""
        rows = []
        step = timedelta(minutes=5)
        moment = end - timedelta(days=days)
        value = 0.0
        while moment <= end:
            rows.append((15.0 + (value % 10), 50.0, 1013.0, ts(moment)))
            moment += step
            value += 1
        async with db.acquire() as conn:
            await conn.executemany(
                "INSERT INTO weather_readings (temperature, humidity, pressure, timestamp) "
                "VALUES (?, ?, ?, ?)", rows)
            await conn.commit()
        db.invalidate_cache()
        return len(rows)

    async def test_short_period_is_not_bucketed(self, db):
        end = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        await self._seed(db, days=1, end=end)
        series = await db.get_history_series("24h")
        assert series.bucketed is False
        assert series.interval_seconds == 300
        assert all("temperature_min" not in r for r in series.readings)

    async def test_long_period_is_bucketed_and_bounded(self, db):
        end = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        raw = await self._seed(db, days=30, end=end)
        series = await db.get_history_series("30d")
        assert series.bucketed is True
        assert series.interval_seconds > 300
        assert len(series.readings) < raw
        assert len(series.readings) <= db.TARGET_CHART_POINTS

    async def test_buckets_carry_the_range_they_hide(self, db):
        end = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        await self._seed(db, days=30, end=end)
        point = (await db.get_history_series("30d")).readings[5]
        for metric in db.METRICS:
            assert point[f"{metric}_min"] <= point[metric] <= point[f"{metric}_max"]

    async def test_bucket_ladder_picks_the_finest_that_fits(self, db):
        assert db._choose_bucket_minutes(timedelta(hours=24)) == 5
        assert db._choose_bucket_minutes(timedelta(days=7)) == 10
        assert db._choose_bucket_minutes(timedelta(days=365 * 5)) == 1440


class TestTrends:
    async def test_pressure_trend_needs_data(self, db):
        assert await db.get_pressure_trend() is None

    async def test_rising_pressure(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for i in range(80):
            moment = now - timedelta(minutes=5 * (79 - i))
            await db.insert_reading(20.0, 50.0, 1000.0 + i * 0.1, ts(moment))
        trend = await db.get_pressure_trend()
        assert trend["direction"] == "rising"
        assert trend["delta"] > 0
        assert trend["consistency"] > 0.9

    async def test_median_ignores_a_single_spike(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for i in range(80):
            await db.insert_reading(20.0, 50.0, 1013.0, ts(now - timedelta(minutes=5 * (79 - i))))
        await db.insert_reading(20.0, 50.0, 1200.0, ts(now))  # glitch
        assert (await db.get_pressure_trend())["current"] == 1013.0

    async def test_recent_trend_rejects_unknown_metric(self, db):
        with pytest.raises(ValueError):
            await db.get_recent_trend("wind_speed")


class TestReadingAgo:
    async def test_finds_the_closest_reading(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        target = now - timedelta(hours=24)
        await db.insert_reading(10.0, 50.0, 1013.0, ts(target))
        await db.insert_reading(20.0, 50.0, 1013.0, ts(now))
        assert (await db.get_reading_ago(24))["temperature"] == 10.0

    async def test_returns_nothing_across_an_outage(self, db):
        """Without a tolerance this would compare against a week-old reading."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        await db.insert_reading(10.0, 50.0, 1013.0, ts(now - timedelta(days=7)))
        await db.insert_reading(20.0, 50.0, 1013.0, ts(now))
        assert await db.get_reading_ago(24) is None


class TestAggregates:
    async def test_stats_on_empty_database(self, db):
        assert await db.get_stats("all") == {"count": 0}

    async def test_extremes_on_empty_database(self, db):
        assert await db.get_extremes_with_times("all") is None

    async def test_extremes_report_values_and_times(self, db):
        await db.insert_reading(10.0, 40.0, 1000.0, "2026-06-20T10:00:00")
        await db.insert_reading(30.0, 80.0, 1030.0, "2026-06-20T14:00:00")
        extremes = await db.get_extremes_with_times("all")
        assert extremes["temperature"]["max"] == {"value": 30.0, "timestamp": "2026-06-20 14:00:00"}
        assert extremes["temperature"]["min"] == {"value": 10.0, "timestamp": "2026-06-20 10:00:00"}
        assert extremes["pressure"]["max"]["value"] == 1030.0

    async def test_daily_summaries_start_on_a_month_boundary(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for days in range(0, 120, 7):
            await db.insert_reading(20.0, 50.0, 1013.0, ts(now - timedelta(days=days)))
        days = [r["day"] for r in await db.get_daily_summaries(3)]
        assert days, "expected some daily summaries"
        assert days[0] >= clock.fmt_date(clock.months_ago(3))

    async def test_daily_extremes_exclude_today(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        await db.insert_reading(5.0, 50.0, 1013.0, ts(now - timedelta(days=1)))
        await db.insert_reading(99.0, 50.0, 1013.0, ts(now))  # today, must not win
        extremes = await db.get_daily_extremes()
        assert extremes["hottest_avg"] == 5.0


class TestCaching:
    async def test_writes_invalidate_cached_aggregates(self, db):
        await db.insert_reading(10.0, 50.0, 1013.0, "2026-06-20T10:00:00")
        assert (await db.get_stats("all"))["count"] == 1
        await db.insert_reading(11.0, 50.0, 1013.0, "2026-06-20T10:05:00")
        assert (await db.get_stats("all"))["count"] == 2

    async def test_cleanup_invalidates_cached_aggregates(self, db):
        await db.insert_reading(10.0, 50.0, 1013.0, "2026-06-20T10:00:00")
        await db.insert_reading(10.0, 50.0, 1013.0, "2026-06-20T10:07:00")
        assert (await db.get_stats("all"))["count"] == 2
        await db.remove_off_grid_readings()
        assert (await db.get_stats("all"))["count"] == 1
