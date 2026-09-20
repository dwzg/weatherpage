"""Tests for the learned rain nowcast.

The model file arrives by automation — a weekly CI job commits it — so these
lean on two things: that a bad file degrades to no nowcast rather than to a
broken page, and that the file currently shipped is one the app can read.
"""

import json
import math
from datetime import datetime, timedelta

import pytest

from app import clock, nowcast, services

MODEL = {
    "features": ["pct30", "rh"],
    "mean": [0.5, 60.0],
    "scale": [0.25, 15.0],
    "coef": [-1.5, 1.2],
    "intercept": -0.8,
    "threshold": 0.3,
    "metadata": {"trained_at": "2026-09-20", "skill": {"bss": 0.2}},
}


def write(tmp_path, payload, name="model.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload) if isinstance(payload, dict) else payload)
    return path


class TestLoading:
    def test_reads_a_well_formed_model(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL))
        assert model is not None
        assert model.features == ("pct30", "rh")
        assert model.threshold == 0.3

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert nowcast.load(tmp_path / "absent.json") is None

    def test_unreadable_file_degrades_quietly(self, tmp_path):
        assert nowcast.load(write(tmp_path, "{not json", "broken.json")) is None

    @pytest.mark.parametrize("missing", ["features", "coef", "intercept", "mean", "scale"])
    def test_incomplete_model_degrades_quietly(self, tmp_path, missing):
        payload = {k: v for k, v in MODEL.items() if k != missing}
        assert nowcast.load(write(tmp_path, payload)) is None

    def test_mismatched_feature_arrays_are_rejected(self, tmp_path):
        """A coefficient list out of step with the names would silently mispredict."""
        payload = MODEL | {"coef": [0.1]}
        assert nowcast.load(write(tmp_path, payload)) is None

    def test_empty_model_is_rejected(self, tmp_path):
        payload = MODEL | {"features": [], "mean": [], "scale": [], "coef": []}
        assert nowcast.load(write(tmp_path, payload)) is None


class TestPrediction:
    def model(self, tmp_path):
        return nowcast.load(write(tmp_path, MODEL))

    def test_matches_the_logistic_by_hand(self, tmp_path):
        model = self.model(tmp_path)
        values = {"pct30": 0.75, "rh": 75.0}
        z = -0.8 + (-1.5) * ((0.75 - 0.5) / 0.25) + 1.2 * ((75.0 - 60.0) / 15.0)
        assert model.predict(values) == pytest.approx(1 / (1 + math.exp(-z)))

    def test_probability_stays_in_range(self, tmp_path):
        model = self.model(tmp_path)
        for pct, rh in ((0.0, 100.0), (1.0, 0.0), (0.5, 60.0), (-5.0, 500.0)):
            assert 0.0 <= model.predict({"pct30": pct, "rh": rh}) <= 1.0

    def test_extreme_inputs_do_not_overflow(self, tmp_path):
        """A sensor glitch must not take the page down with a math error."""
        model = self.model(tmp_path)
        assert model.predict({"pct30": 1e9, "rh": -1e9}) == pytest.approx(0.0, abs=1e-9)

    def test_lower_pressure_rank_raises_the_probability(self, tmp_path):
        """The fitted sign: rain goes with the low end of the station's range."""
        model = self.model(tmp_path)
        low = model.predict({"pct30": 0.1, "rh": 80.0})
        high = model.predict({"pct30": 0.9, "rh": 80.0})
        assert low > high

    def test_zero_scale_does_not_divide_by_zero(self, tmp_path):
        """A constant feature comes out of training with scale 0."""
        model = nowcast.load(write(tmp_path, MODEL | {"scale": [0.0, 15.0]}))
        assert 0.0 <= model.predict({"pct30": 0.5, "rh": 60.0}) <= 1.0


class TestLabel:
    @pytest.mark.parametrize("probability,expected", [
        (0.95, "likely"), (0.60, "likely"), (0.40, "possible"),
        (0.30, "possible"), (0.20, "unlikely"), (0.05, "not expected"),
    ])
    def test_wording_follows_the_probability(self, probability, expected):
        assert nowcast.describe(probability, threshold=0.3) == expected

    def test_wording_is_ordered_across_the_whole_range(self):
        order = ["not expected", "unlikely", "possible", "likely"]
        seen = [nowcast.describe(p / 100, 0.3) for p in range(0, 101, 5)]
        ranks = [order.index(w) for w in seen]
        assert ranks == sorted(ranks), seen


class TestShippedModel:
    """Guards on the file the retraining job commits."""

    def test_the_shipped_model_loads(self):
        model = nowcast.load()
        if model is None:
            pytest.skip("no model shipped in this checkout")
        assert model.features
        assert 0.0 < model.threshold < 1.0

    def test_the_shipped_model_names_features_the_app_can_supply(self):
        model = nowcast.load()
        if model is None:
            pytest.skip("no model shipped in this checkout")
        supplied = {"pct30", "pct7", "rh", "rh_max6", "drh3", "drh6", "spread",
                    "dp6", "dp12", "temp"}
        assert set(model.features) <= supplied, set(model.features) - supplied

    def test_the_shipped_model_records_its_measured_skill(self):
        """A model with no recorded skill has bypassed the gates in ml/train.py."""
        model = nowcast.load()
        if model is None:
            pytest.skip("no model shipped in this checkout")
        assert model.metadata.get("skill", {}).get("bss") is not None


class TestServiceIntegration:
    def test_no_model_means_no_nowcast(self):
        assert services.run_nowcast({"pct30": 0.5}, model=None) is None

    def test_no_features_means_no_nowcast(self, tmp_path):
        assert services.run_nowcast(None, model=nowcast.load(write(tmp_path, MODEL))) is None

    def test_a_missing_feature_means_no_nowcast(self, tmp_path):
        """Better to show nothing than to guess at an input the model was fitted on."""
        model = nowcast.load(write(tmp_path, MODEL))
        assert services.run_nowcast({"pct30": 0.5}, model=model) is None

    def test_payload_carries_what_the_page_shows(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL))
        payload = services.run_nowcast({"pct30": 0.1, "rh": 95.0}, model=model)
        assert set(payload) >= {"probability", "threshold", "label", "horizon_hours",
                                "rain_mm", "trained_at", "skill"}
        assert 0.0 <= payload["probability"] <= 1.0
        assert payload["horizon_hours"] == nowcast.HORIZON_HOURS

    async def test_young_database_has_no_feature_vector(self, db):
        """The pressure ranks need weeks behind them; until then, no nowcast."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for i in range(60):
            moment = now - timedelta(minutes=5 * (59 - i))
            await db.insert_reading(20.0, 55.0, 1013.0, moment.strftime(clock.TS_FORMAT))
        assert await services.nowcast_features() is None

    async def test_a_stocked_database_produces_every_feature(self, db):
        now = clock.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
        moment = now - timedelta(days=32)
        step = timedelta(minutes=30)
        i = 0
        while moment <= now:
            await db.insert_reading(
                20.0 + (i % 24) * 0.3, 50.0 + (i % 40), 1005.0 + (i % 50) * 0.5,
                moment.strftime(clock.TS_FORMAT),
            )
            moment += step
            i += 1

        features = await services.nowcast_features()
        assert features is not None
        assert set(features) == {"pct30", "pct7", "rh", "rh_max6", "drh3", "drh6",
                                 "spread", "dp6", "dp12", "temp"}
        assert all(isinstance(v, (int, float)) for v in features.values())
        assert 0.0 <= features["pct30"] <= 1.0
        assert 0.0 <= features["pct7"] <= 1.0

    async def test_a_short_outage_does_not_switch_the_nowcast_off(self, db):
        """A few missing hours must not silently drop the pressure ranking."""
        now = clock.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
        gap_start, gap_end = now - timedelta(hours=8), now - timedelta(hours=4)
        moment = now - timedelta(days=32)
        i = 0
        while moment <= now:
            if not (gap_start <= moment < gap_end):
                await db.insert_reading(
                    20.0 + (i % 24) * 0.3, 50.0 + (i % 40), 1005.0 + (i % 50) * 0.5,
                    moment.strftime(clock.TS_FORMAT),
                )
            moment += timedelta(minutes=30)
            i += 1

        assert await services.nowcast_features() is not None


class TestStatusPayload:
    async def test_status_reports_the_nowcast_field(self, client, db):
        now = datetime.now().replace(second=0, microsecond=0)
        await db.insert_reading(20.0, 55.0, 1013.0, now.strftime(clock.TS_FORMAT))
        body = (await client.get("/api/weather/status")).json()
        assert "nowcast" in body  # present, even when there is not enough history
