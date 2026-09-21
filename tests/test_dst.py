"""The autumn DST fallback: one local hour, two hours of readings.

Europe/Berlin puts the clock back at 03:00 on 2026-10-25, so 02:00-02:59
happens twice — first at +02:00, then again at +01:00. The stored timestamp
is a local wall clock and cannot tell those apart on its own; the reading's
UTC offset is what does.
"""

from datetime import UTC, datetime

import pytest

from app import clock

#: The repeated hour, as the database stores it.
REPEATED = "2026-10-25 02:30:00"

#: Real instants inside each pass, for standing the clock up at.
IN_FIRST_PASS = datetime(2026, 10, 25, 0, 31, tzinfo=UTC)
IN_SECOND_PASS = datetime(2026, 10, 25, 1, 31, tzinfo=UTC)

CEST, CET = 120, 60


@pytest.fixture
def at(monkeypatch):
    """Pin ``clock.now`` so ingestion sees a chosen arrival time."""

    def _at(moment: datetime):
        monkeypatch.setattr(clock, "now", lambda: moment.astimezone(get_tz()))

    def get_tz():
        from app.config import get_settings

        return get_settings().timezone

    return _at


class TestStorage:
    async def test_both_passes_survive(self, db):
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED, utc_offset=CEST)
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED, utc_offset=CET)

        rows = await db.get_history("all")
        assert [r["temperature"] for r in rows] == [9.0, 8.0], (
            "the repeated hour holds two readings, not one overwriting the other"
        )

    async def test_the_earlier_instant_sorts_first(self, db):
        # Inserted the wrong way round on purpose: id order is not time order.
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED, utc_offset=CET)
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED, utc_offset=CEST)

        rows = await db.get_history("all")
        assert [r["temperature"] for r in rows] == [9.0, 8.0], (
            "+02:00 is the earlier instant of a repeated local hour"
        )

    async def test_current_is_the_later_instant(self, db):
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED, utc_offset=CEST)
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED, utc_offset=CET)

        current = await db.get_current()
        assert current["temperature"] == 8.0

    async def test_a_repost_within_one_pass_still_corrects(self, db):
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED, utc_offset=CEST)
        await db.insert_reading(7.5, 91.0, 1010.5, REPEATED, utc_offset=CEST)

        rows = await db.get_history("all")
        assert len(rows) == 1
        assert rows[0]["temperature"] == 7.5

    async def test_aggregates_count_both(self, db):
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED, utc_offset=CEST)
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED, utc_offset=CET)

        stats = await db.get_stats("all")
        assert stats["count"] == 2
        assert stats["temperature"]["min"] == 8.0
        assert stats["temperature"]["max"] == 9.0

    async def test_cleanup_clears_the_whole_local_hour(self, db):
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED, utc_offset=CEST)
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED, utc_offset=CET)

        deleted = await db.remove_readings_in_range(
            "2026-10-25 02:00:00", "2026-10-25 02:59:59"
        )
        assert deleted == 2
        assert await db.get_history("all") == []


class TestLiveIngestion:
    """Nothing in the payload says which pass it is — the arrival time does."""

    async def test_the_second_pass_does_not_overwrite_the_first(self, db, at):
        at(IN_FIRST_PASS)
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED)
        at(IN_SECOND_PASS)
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED)

        rows = await db.get_history("all")
        assert [(r["temperature"]) for r in rows] == [9.0, 8.0]

    async def test_offsets_are_resolved_from_the_arrival(self, db, at):
        at(IN_FIRST_PASS)
        await db.insert_reading(9.0, 90.0, 1010.0, REPEATED)
        at(IN_SECOND_PASS)
        await db.insert_reading(8.0, 92.0, 1011.0, REPEATED)

        async with db.acquire() as conn:
            cursor = await conn.execute(
                f"SELECT utc_offset FROM weather_readings {db.ORDER_OLDEST_FIRST}"
            )
            assert [row[0] for row in await cursor.fetchall()] == [CEST, CET]

    async def test_an_ordinary_hour_is_unaffected(self, db, at):
        at(IN_SECOND_PASS)
        await db.insert_reading(9.0, 90.0, 1010.0, "2026-10-25 04:00:00")
        await db.insert_reading(7.0, 90.0, 1010.0, "2026-10-25 04:00:00")
        assert len(await db.get_history("all")) == 1, "a repost still corrects"


class TestThroughTheApi:
    async def test_an_explicit_offset_targets_a_pass(self, client):
        for temperature, suffix in ((9.0, "+02:00"), (8.0, "+01:00")):
            response = await client.post(
                "/api/weather",
                json={
                    "temperature": temperature,
                    "humidity": 90.0,
                    "pressure": 1010.0,
                    "timestamp": f"2026-10-25T02:30:00{suffix}",
                },
            )
            assert response.status_code == 200
            assert response.json()["timestamp"] == REPEATED

        history = (await client.get("/api/weather/history?period=all")).json()
        assert [r["temperature"] for r in history["readings"]] == [9.0, 8.0]

    async def test_a_bad_timestamp_still_reports_itself(self, client):
        response = await client.post(
            "/api/weather",
            json={
                "temperature": 9.0, "humidity": 90.0, "pressure": 1010.0,
                "timestamp": "unavailable",
            },
        )
        assert response.status_code == 422
        assert "ISO-8601" in response.text
