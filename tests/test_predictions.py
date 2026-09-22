"""The prediction log: what the page said, kept so it can be held to it.

Everything else about the models is scored at fit time against a held-out
past. That says the method works; it does not say the thing currently
deployed has been right. Only a row written at the time can say that, which
is why this cannot be reconstructed afterwards and why the table exists.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

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


class TestReliabilityBins:
    """The arithmetic behind the table the page shows.

    It lives in app/nowcast.py rather than in ml/train.py — which is what
    writes it — precisely so it can be checked here, without numpy or
    scikit-learn, which the image does not carry.
    """

    def test_a_perfectly_calibrated_run_lands_on_the_line(self):
        from app import nowcast

        # Ten hours in the 20-30% bin, two of which were wet.
        pairs = [(0.25, 1.0), (0.25, 1.0)] + [(0.25, 0.0)] * 8
        bins = nowcast.reliability_bins(pairs)
        assert len(bins) == 1
        assert bins[0] == {
            "from": 0.2, "to": 0.3, "hours": 10,
            "predicted": 0.25, "observed": 0.2, "thin": False,
        }

    def test_every_tenth_is_its_own_bin(self):
        from app import nowcast

        bins = nowcast.reliability_bins([(p / 100, 0.0) for p in range(0, 100, 5)])
        assert [b["from"] for b in bins] == [round(i / 10, 2) for i in range(10)]

    def test_certainty_is_counted_rather_than_dropped(self):
        """1.0 is not less than the top bin's upper edge, so a naive
        half-open test would silently discard it."""
        from app import nowcast

        bins = nowcast.reliability_bins([(1.0, 1.0)])
        assert len(bins) == 1 and bins[0]["hours"] == 1
        assert bins[0]["to"] == 1.0

    def test_empty_bins_are_dropped_not_zeroed(self):
        """A zero would draw the curve through hours that never happened."""
        from app import nowcast

        bins = nowcast.reliability_bins([(0.05, 0.0), (0.85, 1.0)])
        assert [(b["from"], b["hours"]) for b in bins] == [(0.0, 1), (0.8, 1)]

    def test_a_thin_bin_is_flagged_rather_than_hidden(self):
        from app import nowcast

        bins = nowcast.reliability_bins([(0.35, 1.0)] * 3 + [(0.55, 0.0)] * 40)
        thin = {b["from"]: b["thin"] for b in bins}
        assert thin == {0.3: True, 0.5: False}

    def test_nothing_scored_yields_no_bins(self):
        from app import nowcast

        assert nowcast.reliability_bins([]) == []


class TestLoadingTheVerification:
    def test_a_missing_file_is_simply_absent(self, tmp_path):
        from app import nowcast

        assert nowcast.load_verification(tmp_path / "nope.json") is None

    def test_a_malformed_file_costs_a_section_not_the_page(self, tmp_path):
        from app import nowcast

        broken = tmp_path / "verification.json"
        broken.write_text("{ not json")
        assert nowcast.load_verification(broken) is None

    def test_an_empty_verification_is_not_shown(self, tmp_path):
        """Zero scored hours is not a result, and rendering it as one would
        put an empty table under a heading promising evidence."""
        from app import nowcast

        empty = tmp_path / "verification.json"
        empty.write_text(json.dumps({"hours": 0}))
        assert nowcast.load_verification(empty) is None

    def test_a_real_one_loads(self, tmp_path):
        from app import nowcast

        path = tmp_path / "verification.json"
        path.write_text(json.dumps({"hours": 900, "base_rate": 0.21}))
        assert nowcast.load_verification(path)["hours"] == 900


VERIFICATION = {
    "scored_at": "2026-12-15", "from": "2026-09-22", "to": "2026-12-14",
    "hours": 2600, "base_rate": 0.208,
    "models": ["2026-09-21", "2026-11-23"],
    "horizon_hours": 6, "rain_mm": 0.2,
    "model": {"brier": 0.147, "bss": 0.109, "auc": 0.812, "csi": 0.398,
              "kss": 0.441, "hours": 2600, "base_rate": 0.208},
    "reliability": [
        {"from": 0.0, "to": 0.1, "hours": 539, "predicted": 0.05,
         "observed": 0.07, "thin": False},
        {"from": 0.4, "to": 0.5, "hours": 191, "predicted": 0.44,
         "observed": 0.39, "thin": False},
        {"from": 0.8, "to": 0.9, "hours": 1, "predicted": 0.85,
         "observed": 1.0, "thin": True},
    ],
    "rules": {"csi": 0.344, "kss": 0.372, "hours": 2600},
}


#: The catalogue the render embeds is keyed by the English source strings, so
#: it contains every English phrase by construction. Strip it before asking
#: whether English leaked onto a German page.
CATALOGUE_BLOB = re.compile(
    r'<script id="i18n-data".*?</script>', re.DOTALL
)


def visible(markup: str) -> str:
    return CATALOGUE_BLOB.sub("", markup)


@pytest.fixture
def with_a_nowcast(monkeypatch):
    """A page with a working model, without growing 30 days of pressure.

    The verification table sits inside the rain model's own section, so it
    needs a nowcast to be there at all — and the real feature vector needs a
    month of history behind its pressure ranks.
    """
    model = services.NOWCAST_MODEL
    if model is None:
        pytest.skip("no model shipped in this checkout")

    async def vector():
        return dict.fromkeys(model.features, 0.0) | {"rh": 90.0, "temp": 12.0}

    monkeypatch.setattr(services, "nowcast_features", vector)
    monkeypatch.setattr(services, "VERIFICATION", VERIFICATION)


class TestTheReliabilityTableOnThePage:
    async def test_it_is_absent_until_there_is_something_to_show(
        self, client, monkeypatch
    ):
        """Absent is the honest state. An empty table under a heading
        promising evidence would be worse than no heading."""
        monkeypatch.setattr(services, "VERIFICATION", None)
        await client.post("/api/weather", json=at(clock.now()))
        assert "reliability-table" not in (await client.get("/")).text

    async def test_it_renders_a_row_per_bin(self, client, with_a_nowcast):
        await client.post("/api/weather", json=at(clock.now()))

        markup = (await client.get("/")).text
        assert "reliability-table" in markup
        assert "And has it been right?" in visible(markup)
        # The thin bin is flagged rather than dropped.
        assert 'class="is-thin"' in markup
        rows = re.findall(r"<tr[^>]*>\s*<td>(\d+)", markup)
        assert {"0", "40", "80"} <= set(rows), rows

    async def test_it_is_translated(self, client, with_a_nowcast):
        await client.post("/api/weather", json=at(clock.now()))

        markup = visible(
            (await client.get("/", headers={"Accept-Language": "de"})).text
        )
        assert "Und hatte es recht?" in markup
        assert "And has it been right?" not in markup
        # Dates and numbers go through the same formatters as the rest.
        assert "22.09.2026" in markup
        assert "0,147" in markup

    async def test_the_poller_leaves_it_alone(self, client, with_a_nowcast):
        """It changes weekly, in CI, and a new image is what carries it — so
        it belongs in the render and not in /status, which the poller
        refetches every minute for numbers that actually move."""
        await client.post("/api/weather", json=at(clock.now()))
        assert "verification" not in (await client.get("/api/weather/status")).json()


class TestTheTrainerScoresTheLog:
    """ml/train.py must read the log, and must not reinvent the bins.

    Parsed rather than imported: the trainer needs numpy and scikit-learn,
    which the image deliberately does not carry, so the ordinary suite cannot
    import it — the same reason the binning itself lives in app/nowcast.py.
    """

    SOURCE = Path(__file__).resolve().parent.parent / "ml" / "train.py"

    def test_it_reads_the_prediction_log(self):
        assert "/api/weather/predictions" in self.SOURCE.read_text()

    def test_it_uses_the_app_s_binning(self):
        """A second copy of the bin edges would eventually disagree with the
        table the page draws from them."""
        source = self.SOURCE.read_text()
        assert "from app.nowcast import reliability_bins" in source

    def test_verification_is_written_outside_the_shipping_decision(self):
        """Refusing to ship is a normal outcome. If the verification were
        written only when a model shipped, it would go stale behind a run
        that correctly declined."""
        import ast

        tree = ast.parse(self.SOURCE.read_text())
        main = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls_verify = [
            node for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "verify"
        ]
        assert calls_verify, "main() should verify the log"
        ships = [
            node for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "ship"
        ]
        # The verify call must not sit inside any branch guarded by a ship().
        for call in calls_verify:
            for shipped in ships:
                assert shipped not in ast.walk(call)


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
