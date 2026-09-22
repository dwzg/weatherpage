"""Tests for the learned rain nowcast.

The model file arrives by automation — a weekly CI job commits it — so these
lean on two things: that a bad file degrades to no nowcast rather than to a
broken page, and that the file currently shipped is one the app can read.
"""

import json
import math
import re
from datetime import datetime, timedelta

import pytest

from app import clock, nowcast, services, weather

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


async def stock(db, days: int = 32) -> None:
    """Enough history for every nowcast feature, including the 30-day rank."""
    now = clock.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
    moment = now - timedelta(days=days)
    i = 0
    while moment <= now:
        await db.insert_reading(
            20.0 + (i % 24) * 0.3, 50.0 + (i % 40), 1005.0 + (i % 50) * 0.5,
            moment.strftime(clock.TS_FORMAT),
        )
        moment += timedelta(minutes=30)
        i += 1


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


class TestBreakdown:
    """The per-feature decomposition the page prints.

    Its whole claim is that it *is* the prediction: if the rows stopped
    summing to the logit the table would be a decorative illustration of a
    number computed somewhere else.
    """

    def model(self, tmp_path):
        return nowcast.load(write(tmp_path, MODEL))

    def test_the_weights_and_the_intercept_are_the_logit(self, tmp_path):
        model = self.model(tmp_path)
        values = {"pct30": 0.75, "rh": 75.0}
        total = model.intercept + sum(c.weight for c in model.contributions(values))
        assert total == pytest.approx(model.logit(values))

    def test_the_logit_squashes_to_the_prediction(self, tmp_path):
        model = self.model(tmp_path)
        values = {"pct30": 0.2, "rh": 88.0}
        squashed = 1 / (1 + math.exp(-model.logit(values)))
        assert model.predict(values) == pytest.approx(squashed)

    def test_one_row_per_feature_in_model_order(self, tmp_path):
        model = self.model(tmp_path)
        rows = model.contributions({"pct30": 0.5, "rh": 60.0})
        assert [c.name for c in rows] == list(model.features)

    def test_standardising_is_relative_to_the_training_mean(self, tmp_path):
        model = self.model(tmp_path)
        at_mean = model.contributions({"pct30": 0.5, "rh": 60.0})
        assert [c.standardised for c in at_mean] == [0.0, 0.0]
        assert [c.weight for c in at_mean] == [0.0, 0.0]

    def test_a_constant_feature_does_not_divide_by_zero(self, tmp_path):
        """scale 0 is what a feature that never varied comes out of training as."""
        model = nowcast.load(write(tmp_path, MODEL | {"scale": [0.0, 15.0]}))
        assert all(math.isfinite(c.weight) for c in
                   model.contributions({"pct30": 0.9, "rh": 60.0}))

    def test_an_unknown_feature_still_gets_a_row(self, tmp_path):
        """A newer model may carry a name this code has never heard of."""
        fmt = nowcast.describe_feature("whatever_is_next")
        assert fmt.label == "whatever_is_next"
        assert fmt.digits == 2

    def test_every_shipped_feature_is_described(self):
        """A feature with no entry prints as a bare name on the live page."""
        model = nowcast.load()
        if model is None:
            pytest.skip("no model shipped")
        missing = [n for n in model.features if n not in nowcast.FEATURE_FORMATS]
        assert not missing, f"no display format for: {missing}"


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
        await stock(db)
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


class TestRenderedBreakdown:
    """What ``run_nowcast`` hands the page and the poller.

    The explainer's table is rendered twice — by Jinja and, a minute later,
    by ``poll.js`` — from this one payload. The server picks the scale and
    the decimals so both print the same shape; a row that left them out
    would change under the reader after sixty seconds.
    """

    MODEL_FILE = MODEL | {"features": ["pct30", "rh"]}

    def payload(self, tmp_path):
        model = nowcast.load(write(tmp_path, self.MODEL_FILE))
        return services.run_nowcast({"pct30": 0.2, "rh": 88.0}, model)

    def test_every_feature_gets_a_row(self, tmp_path):
        rows = self.payload(tmp_path)["contributions"]
        assert {r["name"] for r in rows} == {"pct30", "rh"}

    def test_each_row_carries_its_own_formatting(self, tmp_path):
        for row in self.payload(tmp_path)["contributions"]:
            assert set(row) >= {"label", "unit", "digits", "sign", "value",
                                "standardised", "weight"}
            assert isinstance(row["digits"], int)
            assert isinstance(row["sign"], bool)

    def test_percentile_rows_are_sent_as_percentages(self, tmp_path):
        """The model carries a fraction; the page prints 20 %, not 0.2."""
        row = next(r for r in self.payload(tmp_path)["contributions"]
                   if r["name"] == "pct30")
        assert row["value"] == pytest.approx(20.0)
        assert row["unit"] == "%"

    def test_rows_are_ordered_by_influence(self, tmp_path):
        weights = [abs(r["weight"]) for r in self.payload(tmp_path)["contributions"]]
        assert weights == sorted(weights, reverse=True)

    def test_the_intercept_and_weights_reach_the_quoted_probability(self, tmp_path):
        """The table has to add up, or it is describing a different number."""
        payload = self.payload(tmp_path)
        total = payload["intercept"] + sum(r["weight"] for r in payload["contributions"])
        assert total == pytest.approx(payload["logit"], abs=0.01)
        assert 1 / (1 + math.exp(-payload["logit"])) == pytest.approx(
            payload["probability"], abs=0.01)

    def test_the_model_card_passes_the_trainer_s_metadata_through(self, tmp_path):
        model = nowcast.load(write(tmp_path, self.MODEL_FILE | {"metadata": {
            "samples": 1454, "base_rate": 0.193, "trained_through": "2026-09-20",
            "baselines": {"rules": {"brier": 0.19}},
            "cross_check_2km": {"brier": 0.11, "bss": -0.25},
        }}))
        payload = services.run_nowcast({"pct30": 0.5, "rh": 60.0}, model)
        assert payload["samples"] == 1454
        assert payload["base_rate"] == 0.193
        assert payload["trained_through"] == "2026-09-20"
        assert payload["baselines"]["rules"]["brier"] == 0.19
        assert payload["cross_check"]["bss"] == -0.25

    def test_metadata_the_trainer_did_not_write_is_simply_absent(self, tmp_path):
        """An older model file must not take the explainer down with it."""
        model = nowcast.load(write(tmp_path, self.MODEL_FILE | {"metadata": {}}))
        payload = services.run_nowcast({"pct30": 0.5, "rh": 60.0}, model)
        assert payload["samples"] is None
        assert payload["baselines"] is None
        assert payload["cross_check"] is None
        assert payload["contributions"]

    async def test_the_poller_is_given_the_breakdown(self, client, db):
        """poll.js rebuilds the table from /status, so it has to be in there."""
        if services.NOWCAST_MODEL is None:
            pytest.skip("no model shipped")
        await stock(db)
        nowcast_payload = (await client.get("/api/weather/status")).json()["nowcast"]
        assert nowcast_payload is not None
        rows = nowcast_payload["contributions"]
        assert {r["name"] for r in rows} == set(services.NOWCAST_MODEL.features)
        total = nowcast_payload["intercept"] + sum(r["weight"] for r in rows)
        assert total == pytest.approx(nowcast_payload["logit"], abs=0.01)

    async def test_the_page_renders_the_breakdown_it_was_given(self, client, db):
        """The table is server-rendered first; the poller only maintains it."""
        if services.NOWCAST_MODEL is None:
            pytest.skip("no model shipped")
        await stock(db)
        text = (await client.get("/")).text
        body = re.search(r'id="deep-features".*?<tbody>(.*?)</tbody>', text, re.S)
        assert body, "the contribution table did not render"
        assert body.group(1).count("<tr>") == len(services.NOWCAST_MODEL.features)


SKY_MODEL = {
    "features": ["pct30", "rh"],
    "mean": [0.5, 60.0],
    "scale": [0.25, 15.0],
    "coef": [-0.9, 1.4],
    "intercept": -0.3,
    "threshold": 0.4,
    "metadata": {
        "trained_at": "2026-09-21",
        "samples": 8010,
        "base_rate": 0.425,
        "overcast_percent": 80,
        "skill": {"bss": 0.214, "auc": 0.773},
        "baselines": {"rules": {"auc": 0.665}},
    },
}


class TestSkyModel:
    """The second fitted model, and the outlook it composes.

    It is optional in a way the rain model is not: until ml/train.py ships
    one, everything here must degrade to the threshold ladder rather than to
    a broken page.
    """

    def test_it_loads_through_the_same_loader(self, tmp_path):
        model = nowcast.load(write(tmp_path, SKY_MODEL))
        assert model is not None
        assert model.features == ("pct30", "rh")

    def test_a_missing_file_is_not_fatal(self, tmp_path):
        assert nowcast.load(tmp_path / "absent.json") is None

    def test_no_model_means_no_sky(self):
        assert services.run_sky({"pct30": 0.5, "rh": 60.0}, model=None) is None

    def test_a_missing_feature_means_no_sky(self, tmp_path):
        model = nowcast.load(write(tmp_path, SKY_MODEL))
        assert services.run_sky({"pct30": 0.5}, model=model) is None

    def test_payload_carries_what_the_explainer_shows(self, tmp_path):
        model = nowcast.load(write(tmp_path, SKY_MODEL))
        payload = services.run_sky({"pct30": 0.1, "rh": 95.0}, model=model)
        assert set(payload) >= {"probability", "band", "label", "overcast_percent",
                                "horizon_hours", "samples", "skill", "baselines"}
        assert 0.0 <= payload["probability"] <= 1.0
        assert payload["band"] in {"overcast", "mixed", "clear"}

    def test_the_word_and_the_band_cannot_disagree(self, tmp_path):
        """The label beside the percentage is derived from the same band the
        banner phrase is, so one cannot say cloudy while the other says clear."""
        model = nowcast.load(write(tmp_path, SKY_MODEL))
        for features in ({"pct30": 0.9, "rh": 20.0}, {"pct30": 0.1, "rh": 99.0}):
            payload = services.run_sky(features, model=model)
            assert payload["label"] == nowcast.describe_sky(payload["probability"])

    def test_the_shipped_sky_model_is_readable_if_it_exists(self):
        """It is absent until CI first ships one, which is a normal state."""
        if nowcast.SKY_MODEL_PATH.exists():
            model = nowcast.load(nowcast.SKY_MODEL_PATH)
            assert model is not None, "a shipped sky model must be loadable"
            assert set(model.features) <= {
                "pct30", "pct7", "rh", "rh_max6", "drh3", "drh6",
                "spread", "dp6", "dp12", "temp",
            }


class TestOutlookSelection:
    """Which of the two ladders produced the phrase, and when."""

    async def test_the_rain_model_alone_is_enough_to_lead(self, client, db):
        """Today's state: no sky_model.json has ever cleared its gates.

        The outlook is still learned, because the rain rungs only ever needed
        the rain model. Requiring both was what put "Rain possible" on the
        banner beside "Rain nearby not expected" in the pill.
        """
        await stock(db)
        body = (await client.get("/api/weather/status")).json()
        assert body["sky"] is None
        assert body["nowcast"] is not None
        assert body["outlook_is_learned"] is True
        assert body["sky_is_learned"] is False

    async def test_the_flags_match_what_the_payload_carries(self, client, db):
        await stock(db)
        body = (await client.get("/api/weather/status")).json()
        assert body["outlook_is_learned"] is (body["nowcast"] is not None)
        assert body["sky_is_learned"] is (body["sky"] is not None)

    async def test_the_banner_and_the_pill_cannot_disagree(self, client, db):
        """The whole point of composing: one decision, read twice.

        The phrase comes from nowcast.describe() on the same probability the
        pill prints, so a pill saying rain is not expected can no longer sit
        beside a banner saying rain is possible.
        """
        from app import nowcast as nowcast_module

        await stock(db)
        body = (await client.get("/api/weather/status")).json()
        assert body["nowcast"] is not None

        word = nowcast_module.describe(
            body["nowcast"]["probability"], body["nowcast"]["threshold"]
        )
        says_rain = "Rain" in body["forecast"] or "Thunder" in body["forecast"]
        if word in ("not expected", "unlikely"):
            # A convective afternoon may still raise a thunderstorm, which is
            # hand-made and outranks the model on purpose.
            assert not says_rain or "Thunder" in body["forecast"], body["forecast"]
        if word == "likely":
            assert body["forecast"] == "Rain likely"

    async def test_the_forecast_is_always_a_phrase_either_way(self, client, db):
        await stock(db)
        body = (await client.get("/api/weather/status")).json()
        assert isinstance(body["forecast"], str) and body["forecast"]


class TestComposedOutlookOnThePage:
    """The composed path, driven end to end with a sky model installed.

    Nothing else covers it until CI first ships app/sky_model.json, and by
    then it would be covered by being live — which is the wrong time to find
    out that the explainer renders the wrong ladder.
    """

    @pytest.fixture
    def with_sky(self, monkeypatch, tmp_path):
        model = nowcast.load(write(tmp_path, SKY_MODEL))
        monkeypatch.setattr(services, "SKY_MODEL", model)
        return model

    async def test_the_status_reports_a_learned_outlook(self, client, db, with_sky):
        await stock(db)
        body = (await client.get("/api/weather/status")).json()
        assert body["sky"] is not None
        assert body["outlook_is_learned"] is True
        assert body["forecast"]

    async def test_the_page_prints_the_composed_ladder(self, client, db, with_sky):
        await stock(db)
        threshold = (await client.get("/api/weather/status")).json()["nowcast"]["threshold"]
        text = (await client.get("/")).text
        phrases = set(re.findall(r'data-phrase="([^"]+)"', text))
        assert phrases == {tier.phrase for tier in weather.learned_ladder(threshold)}

    async def test_the_page_does_not_print_the_threshold_ladder(self, client, db, with_sky):
        """The two ladders describe different reasoning; showing the rule one
        beside a composed phrase would describe work the page did not do."""
        await stock(db)
        text = (await client.get("/")).text
        assert "Pressure in the lowest" not in text
        assert "Any hour at all, for comparison" not in text

    async def test_the_highlighted_rung_is_the_one_the_banner_says(self, client, db, with_sky):
        await stock(db)
        body = (await client.get("/api/weather/status")).json()
        text = (await client.get("/")).text
        marked = re.findall(r'data-phrase="([^"]+)" class="is-active"', text)
        ladder = {t.phrase for t in weather.learned_ladder(body["nowcast"]["threshold"])}
        assert marked == ([body["forecast"]] if body["forecast"] in ladder else [])

    async def test_the_live_sky_sentence_is_rendered(self, client, db, with_sky):
        await stock(db)
        text = (await client.get("/")).text
        assert 'id="deep-sky-live"' in text

    async def test_the_german_page_still_carries_english_identifiers(
        self, client, db, with_sky
    ):
        """Same contract as the rule ladder: data-phrase is what the poller
        matches status.forecast against, so it stays English."""
        await stock(db)
        text = (await client.get("/", headers={"accept-language": "de-DE,de;q=0.9"})).text
        assert 'data-phrase="Rain likely"' in text
        assert "Regenmodell über" in text
