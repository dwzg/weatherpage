"""The daily rollup must always say exactly what the readings say.

Every whole-archive aggregate now reads per-day summaries instead of scanning
every reading. That is only worth anything if the summaries cannot drift from
the table they summarise, so most of what follows recomputes an aggregate
from the raw rows and demands the same answer.
"""

from datetime import datetime, timedelta

import pytest

DAY = "2026-06-20"
NEXT = "2026-06-21"


async def rollup_rows(db) -> list[dict]:
    async with db.acquire() as conn:
        cursor = await conn.execute("SELECT * FROM daily_rollup ORDER BY day ASC")
        return [dict(row) for row in await cursor.fetchall()]


async def expected_rollup(db) -> dict[str, dict]:
    """The rollup recomputed from the readings, independently of the app."""
    by_day: dict[str, dict] = {}
    for reading in await db.get_history("all"):
        day = by_day.setdefault(reading["timestamp"][:10], {"readings": 0, "values": []})
        day["readings"] += 1
        day["values"].append(reading)
    return by_day


async def assert_consistent(db) -> None:
    stored = {row["day"]: row for row in await rollup_rows(db)}
    expected = await expected_rollup(db)
    assert stored.keys() == expected.keys(), "a day is summarised iff it has readings"

    for day, source in expected.items():
        row, values = stored[day], source["values"]
        assert row["readings"] == source["readings"]
        for metric in ("temperature", "humidity", "pressure"):
            numbers = [v[metric] for v in values]
            assert row[f"{metric}_sum"] == pytest.approx(sum(numbers))
            assert row[f"{metric}_min"] == min(numbers)
            assert row[f"{metric}_max"] == max(numbers)
            # The earliest reading that reached the extreme.
            assert row[f"{metric}_min_at"] == next(
                v["timestamp"] for v in values if v[metric] == min(numbers)
            )
            assert row[f"{metric}_max_at"] == next(
                v["timestamp"] for v in values if v[metric] == max(numbers)
            )


async def seed(db, readings):
    for reading in readings:
        await db.insert_reading(**reading)


@pytest.fixture
def today(monkeypatch):
    """Pin the local date, which is what "exclude today" is measured against."""

    def _today(day: str):
        from app import cache, clock

        moment = datetime.strptime(f"{day} 12:00:00", "%Y-%m-%d %H:%M:%S")
        monkeypatch.setattr(
            clock, "now",
            lambda: moment.replace(tzinfo=clock.get_settings().timezone),
        )
        cache.invalidate()

    return _today


class TestMaintenance:
    async def test_an_empty_database_has_no_summaries(self, db):
        assert await rollup_rows(db) == []

    async def test_a_reading_summarises_its_own_day(self, db, make_readings):
        await seed(db, make_readings(6, datetime(2026, 6, 20, 12, 0)))
        await assert_consistent(db)
        assert [row["day"] for row in await rollup_rows(db)] == [DAY]

    async def test_several_days(self, db, make_readings):
        await seed(db, make_readings(200, datetime(2026, 6, 21, 12, 0), step_minutes=30))
        await assert_consistent(db)
        assert len(await rollup_rows(db)) > 3

    async def test_a_backfilled_day_is_summarised_too(self, db, make_readings):
        await seed(db, make_readings(3, datetime(2026, 6, 21, 12, 0)))
        await seed(db, make_readings(3, datetime(2026, 3, 1, 12, 0)))
        await assert_consistent(db)
        assert [row["day"] for row in await rollup_rows(db)] == ["2026-03-01", NEXT]

    async def test_correcting_a_reading_corrects_the_summary(self, db):
        await db.insert_reading(20.0, 50.0, 1013.0, f"{DAY} 12:00:00")
        await db.insert_reading(30.0, 50.0, 1013.0, f"{DAY} 12:05:00")
        await db.insert_reading(21.0, 50.0, 1013.0, f"{DAY} 12:05:00")  # the 30 was wrong

        await assert_consistent(db)
        assert (await rollup_rows(db))[0]["temperature_max"] == 21.0

    async def test_deleting_a_range_updates_the_days_it_touched(self, db, make_readings):
        await seed(db, make_readings(200, datetime(2026, 6, 21, 12, 0), step_minutes=30))
        await db.remove_readings_in_range("2026-06-20 00:00:00", "2026-06-20 23:59:59")

        await assert_consistent(db)
        assert DAY not in {row["day"] for row in await rollup_rows(db)}

    async def test_deleting_everything_empties_the_rollup(self, db, make_readings):
        await seed(db, make_readings(20, datetime(2026, 6, 20, 12, 0)))
        await db.remove_readings_in_range("2026-01-01 00:00:00", "2026-12-31 23:59:59")

        assert await db.get_history("all") == []
        assert await rollup_rows(db) == []

    async def test_off_grid_cleanup_rebuilds_from_what_is_left(self, db):
        await db.insert_reading(20.0, 50.0, 1013.0, f"{DAY} 12:00:00")
        await db.insert_reading(99.0, 50.0, 1013.0, f"{DAY} 12:03:17")  # off the grid

        assert (await rollup_rows(db))[0]["temperature_max"] == 99.0
        await db.remove_off_grid_readings()
        await assert_consistent(db)
        assert (await rollup_rows(db))[0]["temperature_max"] == 20.0


class TestAgreesWithTheReadings:
    """The aggregates that changed source must not have changed answer."""

    @pytest.fixture(autouse=True)
    async def archive(self, db, make_readings):
        """A month of readings with a varied shape, including a thin day."""
        end = datetime(2026, 6, 20, 23, 30)
        for index, reading in enumerate(make_readings(1400, end, step_minutes=30)):
            moment = datetime.strptime(reading["timestamp"], "%Y-%m-%d %H:%M:%S")
            # One day carries a single, extreme reading: a mean of daily means
            # would let it count as much as a complete day.
            if moment.date() == datetime(2026, 6, 1).date() and moment.hour != 3:
                continue
            reading["temperature"] = 20.0 + (index % 17) - 8
            reading["humidity"] = 40.0 + (index % 23)
            reading["pressure"] = 1000.0 + (index % 29)
            await db.insert_reading(**reading)

    async def test_the_rollup_matches_the_readings(self, db):
        await assert_consistent(db)

    async def test_all_time_stats(self, db):
        readings = await db.get_history("all")
        stats = await db.get_stats("all")

        assert stats["count"] == len(readings)
        for metric in ("temperature", "humidity", "pressure"):
            numbers = [r[metric] for r in readings]
            assert stats[metric]["min"] == round(min(numbers), 1)
            assert stats[metric]["max"] == round(max(numbers), 1)
            assert stats[metric]["avg"] == round(sum(numbers) / len(numbers), 1)

    async def test_all_time_extremes_report_when_they_happened(self, db):
        readings = await db.get_history("all")
        extremes = await db.get_extremes_with_times("all")

        for metric in ("temperature", "humidity", "pressure"):
            coldest = min(r[metric] for r in readings)
            assert extremes[metric]["min"]["value"] == round(coldest, 1)
            assert extremes[metric]["min"]["timestamp"] == next(
                r["timestamp"] for r in readings if r[metric] == coldest
            )

    async def test_monthly_means_are_weighted_by_readings(self, db, today):
        today("2026-07-01")
        readings = [r for r in await db.get_history("all") if r["timestamp"][5:7] == "06"]
        june = next(m for m in (await db.get_climate_stats())["monthly_all"]
                    if m["month"] == 6)

        assert june["readings"] == len(readings)
        assert june["temp_avg"] == round(
            sum(r["temperature"] for r in readings) / len(readings), 1
        )
        assert june["temp_min"] == round(min(r["temperature"] for r in readings), 1)

    async def test_daily_summaries_match_their_days(self, db):
        readings = await db.get_history("all")
        for summary in await db.get_daily_summaries(12):
            day = [r for r in readings if r["timestamp"][:10] == summary["day"]]
            assert summary["temp_min"] == round(min(r["temperature"] for r in day), 1)
            assert summary["temp_max"] == round(max(r["temperature"] for r in day), 1)

    async def test_today_is_left_out_of_the_records(self, db, today):
        """Today is summarised, but still cannot claim a record."""
        full = await db.get_climate_stats()
        today("2026-06-20")
        partial = await db.get_climate_stats()

        assert "2026-06-20" in {row["day"] for row in await rollup_rows(db)}
        assert (await db.get_daily_extremes())["hottest_avg_day"] != "2026-06-20"

        june_full = next(m for m in full["monthly_all"] if m["month"] == 6)
        june_partial = next(m for m in partial["monthly_all"] if m["month"] == 6)
        held_back = len([r for r in await db.get_history("all")
                         if r["timestamp"][:10] == "2026-06-20"])
        assert june_partial["readings"] == june_full["readings"] - held_back


class TestBuiltOnMigration:
    async def test_an_existing_archive_is_summarised_at_startup(self, tmp_path):
        """A database that predates the rollup gets one built from what it holds."""
        import sqlite3

        from app import cache, database

        path = tmp_path / "weather.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE weather_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                temperature REAL NOT NULL, humidity REAL NOT NULL,
                pressure REAL NOT NULL, timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        start = datetime(2026, 6, 20, 0, 0)
        con.executemany(
            "INSERT INTO weather_readings (temperature, humidity, pressure, timestamp) "
            "VALUES (?, ?, ?, ?)",
            [
                (20.0 + i % 7, 50.0, 1013.0,
                 (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S"))
                for i in range(600)
            ],
        )
        con.commit()
        con.close()

        await database.connect()
        cache.invalidate()
        try:
            await assert_consistent(database)
            assert (await database.get_stats("all"))["count"] == 600
        finally:
            await database.disconnect()
