"""The prediction log: what the page said, kept so it can be held to it.

Everything else about the models is scored at fit time against a held-out
past. That says the method works; it does not say the thing currently
deployed has been right. Only a row written at the time can say that, which
is why this cannot be reconstructed afterwards and why the table exists.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import clock, database, services

READING = {"temperature": "12.0", "humidity": "88", "pressure": "1004"}


def at(moment) -> dict:
    return {**READING, "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%S")}


async def fill(client, hours: float, end=None, step_minutes: int = 5) -> None:
    """Post a run of readings ending at ``end`` (default: the last whole hour)."""
    end = end or clock.now().replace(minute=0, second=0, microsecond=0)
    count = int(hours * 60 / step_minutes)
    for i in range(count, -1, -1):
        await client.post("/api/weather", json=at(end - timedelta(minutes=step_minutes * i)))


class TestWhatGetsLogged:
    async def test_an_hourly_reading_logs_a_prediction(self, client):
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=2, end=end)

        logged = await database.export_predictions()
        assert [row["timestamp"] for row in logged] == [
            (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:00:00"),
            (end - timedelta(hours=1)).strftime("%Y-%m-%d %H:00:00"),
            end.strftime("%Y-%m-%d %H:00:00"),
        ]

    async def test_readings_between_the_hours_log_nothing(self, client):
        """Hourly because the observations are hourly. A row every five
        minutes would be twelve times the rows and no extra scoreable hour."""
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)
        assert len(await database.export_predictions()) == 2  # both whole hours

    async def test_it_records_the_phrase_the_page_shows(self, client):
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)

        status = (await client.get("/api/weather/status")).json()
        latest = (await database.export_predictions())[-1]
        assert latest["forecast"] == status["forecast"]

    async def test_the_rules_are_logged_before_a_model_can_be(self, client):
        """A young database has no feature vector yet, so no probability —
        but the ladder still speaks, and scoring it is worth starting now."""
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)

        latest = (await database.export_predictions())[-1]
        assert latest["forecast"]
        assert latest["rain_probability"] is None

    async def test_reposting_an_hour_does_not_duplicate_it(self, client):
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)
        assert await database.count_predictions() == 2

        await client.post("/api/weather", json={**at(end), "humidity": "20"})
        assert await database.count_predictions() == 2

    async def test_a_second_write_for_an_hour_replaces_the_first(self, db):
        """Same contract as ingestion: a correction corrects, rather than
        leaving the first attempt behind.

        Driven directly, because a corrected *reading* usually will not move
        the phrase — the forecast reads 30-minute medians precisely so one
        sample cannot flip it, which is the behaviour the rest of the suite
        relies on.
        """
        for probability, phrase in ((0.2, "Unsettled"), (0.8, "Rain likely")):
            await db.insert_prediction(
                "2026-06-20 12:00:00", 120, probability, None, phrase, "2026-06-01"
            )

        logged = await db.export_predictions()
        assert len(logged) == 1
        assert logged[0]["rain_probability"] == 0.8
        assert logged[0]["forecast"] == "Rain likely"

    async def test_the_repeated_autumn_hour_holds_two_predictions(self, db):
        """02:30 happens twice that night and names two different hours of
        weather; the log keys on the offset for the same reason the readings
        do."""
        for offset, phrase in ((120, "Rain possible"), (60, "Fair and settled")):
            await db.insert_prediction(
                "2026-10-25 02:00:00", offset, 0.3, None, phrase, "2026-06-01"
            )
        logged = await db.export_predictions()
        assert [row["utc_offset"] for row in logged] == [120, 60]
        assert [row["forecast"] for row in logged] == ["Rain possible", "Fair and settled"]

    async def test_a_backfill_does_not_log(self, client):
        """build_status() describes *now*. Attaching it to a historical
        timestamp would file today's prediction under last week."""
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)
        logged_before = await database.export_predictions()

        old = end - timedelta(days=30)
        await client.post("/api/weather", json=at(old))

        assert await database.export_predictions() == logged_before

    async def test_logging_never_costs_the_reading(self, client, monkeypatch):
        """The reading is the irreplaceable thing. A failure to log must not
        hand the relay a 500 and have Home Assistant retry a stored reading."""
        async def boom(*args, **kwargs):
            raise RuntimeError("the log is broken")

        monkeypatch.setattr(services, "record_prediction", boom)
        end = clock.now().replace(minute=0, second=0, microsecond=0)

        response = await client.post("/api/weather", json=at(end))
        assert response.status_code == 200
        assert (await client.get("/api/weather/current")).json()["temperature"] == 12.0


class TestTheLogIsNotReconstructable:
    """Why the table exists at all, asserted rather than only argued.

    The features are a pure function of the readings, so a replay could
    recompute them — but it would attribute every hour to today's model, and
    the model is refitted weekly. The provenance column is what makes the
    difference, so it has to actually be written.
    """

    async def test_each_row_records_which_model_produced_it(self, client, monkeypatch):
        """Without this column the log would be a series of numbers from an
        unknown sequence of models, and a replay could have produced it."""
        from app import nowcast

        model = services.NOWCAST_MODEL
        if model is None:
            pytest.skip("no model shipped in this checkout")

        stamped = nowcast.Model(
            features=model.features, mean=model.mean, scale=model.scale,
            coef=model.coef, intercept=model.intercept, threshold=model.threshold,
            metadata={**model.metadata, "trained_at": "2026-01-02"},
        )
        monkeypatch.setattr(services, "NOWCAST_MODEL", stamped)

        # The real vector needs 30 days of pressure to rank against. This is
        # about the provenance column, not about the features, so it is
        # supplied rather than grown.
        async def vector():
            return dict.fromkeys(model.features, 0.0) | {"rh": 90.0, "temp": 12.0}

        monkeypatch.setattr(services, "nowcast_features", vector)

        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)

        latest = (await database.export_predictions())[-1]
        assert latest["rain_probability"] is not None
        assert latest["model_trained_at"] == "2026-01-02"


class TestTheExportEndpoint:
    async def test_it_pages_with_a_timestamp_and_offset_cursor(self, client):
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=4, end=end)

        first = (await client.get("/api/weather/predictions?limit=2")).json()
        assert len(first["predictions"]) == 2
        assert first["next"]["after"] and first["next"]["after_offset"] is not None

        cursor = first["next"]
        second = (await client.get(
            f"/api/weather/predictions?limit=2&after={cursor['after']}"
            f"&after_offset={cursor['after_offset']}"
        )).json()
        assert len(second["predictions"]) >= 1
        assert second["predictions"][0]["timestamp"] > first["predictions"][-1]["timestamp"]

    async def test_a_short_page_ends_the_walk(self, client):
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=2, end=end)
        body = (await client.get("/api/weather/predictions?limit=500")).json()
        assert body["next"] is None

    async def test_half_a_cursor_is_rejected(self, client):
        response = await client.get("/api/weather/predictions?after=2026-06-20%2012:00:00")
        assert response.status_code == 400

    async def test_it_is_behind_the_api_key(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("API_KEY", "secret")
        get_settings.cache_clear()
        try:
            assert (await client.get("/api/weather/predictions")).status_code == 401
            ok = await client.get(
                "/api/weather/predictions", headers={"X-API-Key": "secret"}
            )
            assert ok.status_code == 200
        finally:
            get_settings.cache_clear()

    async def test_it_carries_no_observations(self, client):
        """Those belong to Open-Meteo and the trainer already fetches them;
        a second copy here would be a cache that could silently go stale."""
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)
        row = (await client.get("/api/weather/predictions")).json()["predictions"][0]
        assert set(row) == {
            "timestamp", "utc_offset", "rain_probability", "sky_probability",
            "forecast", "model_trained_at",
        }


class TestItDoesNotDisturbTheReadings:
    async def test_the_rollup_is_untouched(self, client):
        """The rule that every write path refreshes the rollup is about the
        readings. Nothing here changes one."""
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=2, end=end)

        before = await database.get_stats("all")
        await database.insert_prediction(
            end.strftime("%Y-%m-%d %H:00:00"), 120, 0.4, None, "Rain possible", "2026-01-01"
        )
        assert await database.get_stats("all") == before

    async def test_a_prediction_row_is_not_a_reading(self, client):
        end = clock.now().replace(minute=0, second=0, microsecond=0)
        await fill(client, hours=1, end=end)
        assert len(await database.get_history("all")) == 13
        assert await database.count_predictions() == 2
