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


class TestPressurePercentile:
    """Where the current pressure sits in the station's own recent range.

    This is what the forecast reads. A fixed hPa threshold does not survive
    the seasons — scored against observed rain, "below 1020 hPa" ranged from
    a critical success index of 0.05 in one month of the sample to 0.44 in
    another — so the reading is ranked against the station's own history
    instead.
    """

    async def fill(self, db, days: int, end: datetime, low: float = 1000.0, high: float = 1030.0):
        """``days`` of hourly readings sweeping evenly between two pressures."""
        hours = days * 24
        for i in range(hours):
            moment = end - timedelta(hours=hours - 1 - i)
            await db.insert_reading(20.0, 50.0, round(low + (high - low) * (i % 24) / 23, 1),
                                    ts(moment))

    async def test_needs_history_before_it_will_rank(self, db):
        now = clock.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
        await self.fill(db, days=3, end=now)
        assert await db.get_pressure_percentile(1013.0) is None

    async def test_ranks_within_the_recent_range(self, db):
        now = clock.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
        await self.fill(db, days=20, end=now, low=1000.0, high=1030.0)

        assert await db.get_pressure_percentile(999.0) == 0.0
        assert await db.get_pressure_percentile(1031.0) == 1.0
        middle = await db.get_pressure_percentile(1015.0)
        assert 0.4 < middle < 0.6, middle

    async def test_the_same_reading_ranks_differently_in_a_different_regime(self, db):
        """The point of ranking: 1015 hPa is low in one month, high in another."""
        now = clock.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
        await self.fill(db, days=20, end=now, low=1015.0, high=1040.0)
        in_a_high = await db.get_pressure_percentile(1017.0)

        from app import cache

        await db.insert_reading(20.0, 50.0, 1013.0, ts(now + timedelta(hours=1)))
        cache.invalidate()
        await self.fill(db, days=20, end=now + timedelta(hours=2), low=995.0, high=1020.0)
        cache.invalidate()
        in_a_low = await db.get_pressure_percentile(1017.0)

        assert in_a_high < 0.2, in_a_high
        assert in_a_low > 0.7, in_a_low


class TestDailyPressureCycle:
    """The station's sea-level pressure carries a daily swing of its own.

    A BME280 reduces to sea level using temperature, so a sensor that warms
    in the afternoon writes a day/night rhythm into the pressure it reports.
    On this station it is about 3 hPa — bigger than the real tide and bigger
    than the changes the forecast thresholds look for — so it is learned and
    subtracted before any trend is taken.
    """

    AMPLITUDE = 1.5  # hPa, half of a 3 hPa daily swing
    #: 17:00 sits six hours after the cycle's peak, where the uncorrected
    #: six-hour change is at its most misleading.
    NOW = datetime(2026, 9, 15, 17, 0)
    STEP = timedelta(minutes=15)

    @pytest.fixture(autouse=True)
    def _pinned(self, monkeypatch, db):
        """Pin the clock, and count a 15-minute day as complete."""
        from app import database

        monkeypatch.setattr(clock, "now", lambda: self.NOW)
        monkeypatch.setattr(database, "CYCLE_MIN_READINGS_PER_DAY", 90)

    def cycle_pressure(self, moment: datetime, base: float = 1013.0) -> float:
        """A pure daily cycle: peak at 11:00, trough at 23:00, no weather."""
        import math

        hour = moment.hour + moment.minute / 60
        return base + self.AMPLITUDE * math.cos((hour - 11) / 24 * 2 * math.pi)

    async def fill(self, db, days: int, falling_hours: float = 0.0, fall_rate: float = 0.0):
        """``days`` of quiet readings, optionally ending in a real pressure fall."""
        moment = self.NOW - timedelta(days=days)
        fall_starts = self.NOW - timedelta(hours=falling_hours)
        while moment <= self.NOW:
            pressure = self.cycle_pressure(moment)
            if falling_hours and moment > fall_starts:
                pressure += fall_rate * (moment - fall_starts).total_seconds() / 3600
            await db.insert_reading(20.0, 50.0, round(pressure, 1), ts(moment))
            moment += self.STEP

    async def test_young_database_learns_nothing(self, db):
        await self.fill(db, days=5)
        assert await db.get_pressure_cycle() == {}

    async def test_learned_cycle_matches_the_injected_one(self, db):
        await self.fill(db, days=20)
        cycle = await db.get_pressure_cycle()
        assert cycle, "20 complete days should be enough to learn from"

        def slots_apart(a: int, b: int) -> int:
            """Distance between two half-hour slots, the short way round."""
            return min((a - b) % 48, (b - a) % 48)

        peak = max(cycle, key=lambda slot: cycle[slot])
        trough = min(cycle, key=lambda slot: cycle[slot])
        # Within an hour of the injected extremes: pressure is stored to
        # 0.1 hPa, and near a turning point several slots round to the same
        # value, so which one comes out on top is arbitrary.
        assert slots_apart(peak, 11 * 2) <= 2, f"peak landed in slot {peak}"
        assert slots_apart(trough, 23 * 2) <= 2, f"trough landed in slot {trough}"
        assert cycle[peak] - cycle[trough] == pytest.approx(2 * self.AMPLITUDE, abs=0.2)

    async def test_pure_daily_cycle_produces_no_trend(self, db):
        """The whole point: an artefact-only day must not look like weather."""
        await self.fill(db, days=20)

        trend = await db.get_pressure_trend()
        assert trend["detided"] is True
        assert abs(trend["delta"]) < 0.5, f"the cycle leaked a {trend['delta']} hPa trend"
        assert trend["direction"] == "steady"

    async def test_uncorrected_trend_is_dominated_by_the_cycle(self, db):
        """What the forecast used to see on that same quiet day.

        Five days is too few to learn a cycle from, so no correction is
        applied and the daily swing alone reads as a pressure change — which
        is how the old rules came to announce gales on quiet evenings.
        """
        await self.fill(db, days=5)

        trend = await db.get_pressure_trend()
        assert trend["detided"] is False
        assert trend["delta"] < -1.0

    async def test_real_change_still_shows_through(self, db):
        """De-tiding must not flatten an actual synoptic fall."""
        await self.fill(db, days=20, falling_hours=6, fall_rate=-0.8)

        trend = await db.get_pressure_trend()
        assert trend["direction"] == "falling"
        assert trend["delta"] == pytest.approx(-4.0, abs=1.0)  # -0.8 hPa/h over 6h


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

    async def test_it_is_a_median_not_a_single_sample(self, db):
        """Every other delta on the page is smoothed at both ends so one
        noisy reading cannot move it. This one is on all three cards and was
        the exception — two raw instantaneous readings, subtracted."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        target = now - timedelta(hours=24)
        for offset in range(-10, 15, 5):
            await db.insert_reading(
                10.0, 50.0, 1013.0, ts(target + timedelta(minutes=offset))
            )
        # A spike exactly on the target instant, which a nearest-reading
        # lookup would pick up and hand to all three cards.
        await db.insert_reading(40.0, 50.0, 1013.0, ts(target))

        ago = await db.get_reading_ago(24)
        assert ago["temperature"] == 10.0, "the spike should not survive a median"

    async def test_it_reports_the_gap_it_actually_measured(self, db):
        """With a two-hour tolerance the nearest readings can be 22 or 26
        hours old; calling that "24h ago" is a claim the data cannot support."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for minutes in range(0, 30, 5):
            await db.insert_reading(
                10.0, 50.0, 1013.0,
                ts(now - timedelta(hours=22) + timedelta(minutes=minutes)),
            )
        await db.insert_reading(20.0, 50.0, 1013.0, ts(now))

        ago = await db.get_reading_ago(24)
        assert ago is not None
        assert 21.5 <= ago["hours"] <= 22.5, ago["hours"]
        assert ago["readings"] >= 1

    async def test_a_clean_run_still_reports_about_24_hours(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for minutes in range(-15, 20, 5):
            await db.insert_reading(
                12.0, 50.0, 1013.0, ts(now - timedelta(hours=24) + timedelta(minutes=minutes))
            )
        ago = await db.get_reading_ago(24)
        assert round(ago["hours"]) == 24

    async def test_every_metric_comes_back_smoothed(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        target = now - timedelta(hours=24)
        for i, minutes in enumerate(range(-10, 15, 5)):
            await db.insert_reading(
                10.0 + i, 50.0 + i, 1013.0 + i, ts(target + timedelta(minutes=minutes))
            )
        ago = await db.get_reading_ago(24)
        assert ago["temperature"] == 12.0
        assert ago["humidity"] == 52.0
        assert ago["pressure"] == 1015.0


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

    @pytest.fixture
    def noon(self, monkeypatch):
        """Pin the clock: every one of these tests is about "so far today",
        which is otherwise whatever time the suite happens to run at — and
        at 00:30 there is no "today" to compare."""
        pinned = datetime(2026, 9, 15, 12, 0)
        monkeypatch.setattr(clock, "now", lambda: pinned)
        return pinned

    async def test_today_is_compared_against_the_same_hours(self, db, noon):
        """Whole past days would say more about the hour than the weather:
        by noon today's mean is a mean of night and morning."""
        for day in range(1, 15):
            past = noon - timedelta(days=day)
            for hour in range(24):
                # 10 °C up to noon, 30 °C for the rest of the day. Only the
                # morning may be compared against today.
                await db.insert_reading(
                    10.0 if hour <= 12 else 30.0, 50.0, 1013.0,
                    ts(past.replace(hour=hour)),
                )
        for hour in range(13):
            await db.insert_reading(12.0, 50.0, 1013.0, ts(noon.replace(hour=hour)))

        anomaly = await db.get_today_anomaly()
        assert anomaly is not None
        assert anomaly["normal"] == 10.0, "the afternoons must not count"
        assert anomaly["delta"] == 2.0
        assert anomaly["warmer_than"] == anomaly["days"] == 14

    async def test_it_declines_without_enough_behind_it(self, db, noon):
        for day in range(0, 3):
            for hour in range(13):
                moment = (noon - timedelta(days=day)).replace(hour=hour)
                await db.insert_reading(15.0, 50.0, 1013.0, ts(moment))
        assert await db.get_today_anomaly() is None

    async def test_an_outage_day_is_not_compared_with_a_full_one(self, db, noon):
        """Two readings of a morning have a mean for reasons that have
        nothing to do with the weather."""
        for day in range(1, 12):
            past = noon - timedelta(days=day)
            hours = range(13) if day > 1 else range(2)
            for hour in hours:
                await db.insert_reading(10.0, 50.0, 1013.0, ts(past.replace(hour=hour)))
        for hour in range(13):
            await db.insert_reading(10.0, 50.0, 1013.0, ts(noon.replace(hour=hour)))

        anomaly = await db.get_today_anomaly()
        assert anomaly is not None
        assert anomaly["days"] == 10, "the outage day should have been dropped"

    async def test_archive_months_counts_both_ends(self, db):
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        assert await db.get_archive_months() == 0

        await db.insert_reading(20.0, 50.0, 1013.0, ts(now))
        assert await db.get_archive_months() == 1

        first = clock.months_ago(5, now)
        await db.insert_reading(20.0, 50.0, 1013.0, ts(first))
        assert await db.get_archive_months() == 6

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
