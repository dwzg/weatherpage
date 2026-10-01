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

from app import clock, features, nowcast, services, weather


def stump(feature: int, threshold: float, low: float, high: float, missing_left=True) -> dict:
    """One split: at or below ``threshold`` scores ``low``, above it ``high``.

    The root's value is the plain mean of its leaves, as if each had seen
    the same number of training hours.
    """
    return {
        "feature": [feature, -1, -1],
        "threshold": [threshold, 0, 0],
        "left": [1, 0, 0],
        "right": [2, 0, 0],
        "missing_left": [int(missing_left), 0, 0],
        "value": [(low + high) / 2, low, high],
    }


#: Two trees over two features, small enough to walk by hand.
#:
#: Tree one: pct30 <= 0.5 scores +0.6, above it -0.6.
#: Tree two: rh <= 80 goes on to split pct30 <= 0.2 (-0.1 / -0.3, missing
#: going right); rh above 80 scores +0.9. Its root expects 0.075, which is
#: the leaves weighted 1:2:1 by the hours that reached them.
MODEL = {
    "format": "trees",
    "features": ["pct30", "rh"],
    "base": -1.0,
    "trees": [
        stump(0, 0.5, 0.6, -0.6),
        {
            "feature": [1, 0, -1, -1, -1],
            "threshold": [80.0, 0.2, 0, 0, 0],
            "left": [1, 2, 0, 0, 0],
            "right": [4, 3, 0, 0, 0],
            "missing_left": [1, 0, 0, 0, 0],
            "value": [0.075, -0.2, -0.1, -0.3, 0.9],
        },
    ],
    "threshold": 0.3,
    "metadata": {"trained_at": "2026-09-20", "skill": {"bss": 0.2}},
}


def write(tmp_path, payload, name="model.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload) if isinstance(payload, dict) else payload)
    return path


async def stock(db, days: int = 32, gap: tuple[datetime, datetime] | None = None) -> None:
    """Enough history for every nowcast feature, including the 30-day rank.

    Half-hourly on the hour and the half hour — the pressure ranking compares
    readings taken on the hour — plus one on the five-minute grid just before
    now, so the half hour every feature starts from is never empty.
    """
    now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
    moments = []
    moment = now.replace(minute=30 if now.minute >= 30 else 0) - timedelta(days=days)
    while moment <= now:
        moments.append(moment)
        moment += timedelta(minutes=30)
    latest = now.replace(minute=now.minute - now.minute % 5)
    if latest not in moments:
        moments.append(latest)
    for i, moment in enumerate(moments):
        if gap and gap[0] <= moment < gap[1]:
            continue
        await db.insert_reading(
            20.0 + (i % 24) * 0.3, 50.0 + (i % 40), 1005.0 + (i % 50) * 0.5,
            moment.strftime(clock.TS_FORMAT),
        )


class TestLoading:
    def test_reads_a_well_formed_model(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL))
        assert model is not None
        assert model.features == ("pct30", "rh")
        assert len(model.trees) == 2
        assert model.threshold == 0.3

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert nowcast.load(tmp_path / "absent.json") is None

    def test_unreadable_file_degrades_quietly(self, tmp_path):
        assert nowcast.load(write(tmp_path, "{not json", "broken.json")) is None

    def test_the_old_logistic_format_is_not_served(self, tmp_path):
        """What shipped before the trees. Loading it as trees would mispredict."""
        old = {"features": ["pct30"], "mean": [0.5], "scale": [0.25], "coef": [-1.5],
               "intercept": -0.8, "threshold": 0.3, "metadata": {}}
        assert nowcast.load(write(tmp_path, old)) is None

    @pytest.mark.parametrize("missing", ["features", "base", "trees"])
    def test_incomplete_model_degrades_quietly(self, tmp_path, missing):
        payload = {k: v for k, v in MODEL.items() if k != missing}
        assert nowcast.load(write(tmp_path, payload)) is None

    def test_a_tree_with_ragged_arrays_is_rejected(self, tmp_path):
        """A threshold list out of step with the nodes would silently mispredict."""
        broken = dict(MODEL["trees"][0], threshold=[0.5])
        assert nowcast.load(write(tmp_path, MODEL | {"trees": [broken]})) is None

    def test_a_split_on_a_feature_the_model_does_not_have_is_rejected(self, tmp_path):
        assert nowcast.load(write(tmp_path, MODEL | {"trees": [stump(2, 0.5, 1, -1)]})) is None

    def test_a_child_that_points_back_up_the_tree_is_rejected(self, tmp_path):
        """Children always follow their parent, which is what makes a walk end."""
        looping = dict(MODEL["trees"][0], left=[0, 0, 0])
        assert nowcast.load(write(tmp_path, MODEL | {"trees": [looping]})) is None

    def test_a_model_without_trees_is_rejected(self, tmp_path):
        assert nowcast.load(write(tmp_path, MODEL | {"trees": []})) is None


class TestPrediction:
    def model(self, tmp_path):
        return nowcast.load(write(tmp_path, MODEL))

    def test_matches_the_trees_walked_by_hand(self, tmp_path):
        model = self.model(tmp_path)
        # pct30 0.1 goes left in tree one (+0.6); rh 95 goes right in tree two (+0.9).
        z = -1.0 + 0.6 + 0.9
        assert model.raw({"pct30": 0.1, "rh": 95.0}) == pytest.approx(z)
        assert model.predict({"pct30": 0.1, "rh": 95.0}) == pytest.approx(1 / (1 + math.exp(-z)))

    def test_a_value_on_the_threshold_goes_left(self, tmp_path):
        """scikit-learn's rule, and the trainer's self-check holds the app to it."""
        model = self.model(tmp_path)
        assert model.raw({"pct30": 0.5, "rh": 95.0}) == pytest.approx(-1.0 + 0.6 + 0.9)

    def test_a_missing_value_follows_the_branch_training_sent_it(self, tmp_path):
        model = self.model(tmp_path)
        # Tree one sends a missing pct30 left (+0.6); tree two's inner split
        # sends it right (-0.3).
        assert model.raw({"pct30": None, "rh": 50.0}) == pytest.approx(-1.0 + 0.6 - 0.3)

    def test_probability_stays_in_range(self, tmp_path):
        model = self.model(tmp_path)
        for pct, rh in ((0.0, 100.0), (1.0, 0.0), (0.5, 60.0), (-5.0, 500.0)):
            assert 0.0 <= model.predict({"pct30": pct, "rh": rh}) <= 1.0

    def test_extreme_inputs_do_not_overflow(self, tmp_path):
        """A sensor glitch must not take the page down with a math error."""
        model = self.model(tmp_path)
        assert 0.0 <= model.predict({"pct30": 1e9, "rh": -1e9}) <= 1.0

    def test_lower_pressure_rank_raises_the_probability(self, tmp_path):
        """The fitted direction: rain goes with the low end of the station's range."""
        model = self.model(tmp_path)
        low = model.predict({"pct30": 0.1, "rh": 80.0})
        high = model.predict({"pct30": 0.9, "rh": 80.0})
        assert low > high


class TestBreakdown:
    """The per-feature decomposition the page prints.

    Its whole claim is that it *is* the prediction: if the rows stopped
    summing to the logit the table would be a decorative illustration of a
    number computed somewhere else.
    """

    def model(self, tmp_path):
        return nowcast.load(write(tmp_path, MODEL))

    def test_the_starting_point_is_the_model_s_expected_output(self, tmp_path):
        assert self.model(tmp_path).expected == pytest.approx(-1.0 + 0.0 + 0.075)

    def test_the_effects_and_the_starting_point_are_the_logit(self, tmp_path):
        model = self.model(tmp_path)
        for values in ({"pct30": 0.75, "rh": 75.0}, {"pct30": 0.1, "rh": 95.0},
                       {"pct30": None, "rh": 50.0}):
            total = model.expected + sum(c.effect for c in model.contributions(values))
            assert total == pytest.approx(model.raw(values))
            assert model.logit(values) == pytest.approx(model.raw(values))

    def test_each_step_is_credited_to_the_feature_that_split(self, tmp_path):
        model = self.model(tmp_path)
        effects = {c.name: c.effect for c in model.contributions({"pct30": 0.1, "rh": 95.0})}
        assert effects["pct30"] == pytest.approx(0.6 - 0.0)
        assert effects["rh"] == pytest.approx(0.9 - 0.075)

    def test_a_feature_split_on_twice_collects_both_steps(self, tmp_path):
        model = self.model(tmp_path)
        effects = {c.name: c.effect for c in model.contributions({"pct30": 0.1, "rh": 50.0})}
        # Tree one: +0.6. Tree two: rh steps 0.075 -> -0.2, then pct30 -0.2 -> -0.1.
        assert effects["pct30"] == pytest.approx(0.6 + 0.1)
        assert effects["rh"] == pytest.approx(-0.2 - 0.075)

    def test_the_logit_squashes_to_the_prediction(self, tmp_path):
        model = self.model(tmp_path)
        values = {"pct30": 0.2, "rh": 88.0}
        squashed = 1 / (1 + math.exp(-model.logit(values)))
        assert model.predict(values) == pytest.approx(squashed)

    def test_one_row_per_feature_in_model_order(self, tmp_path):
        model = self.model(tmp_path)
        rows = model.contributions({"pct30": 0.5, "rh": 60.0})
        assert [c.name for c in rows] == list(model.features)

    def test_a_missing_value_still_gets_its_row(self, tmp_path):
        rows = self.model(tmp_path).contributions({"pct30": None, "rh": 60.0})
        assert rows[0].value is None
        assert math.isfinite(rows[0].effect)

    def test_an_unknown_feature_still_gets_a_row(self, tmp_path):
        """A newer model may carry a name this code has never heard of."""
        fmt = nowcast.describe_feature("whatever_is_next")
        assert fmt.label == "whatever_is_next"
        assert fmt.digits == 2

    def test_every_computed_feature_is_described(self):
        """A feature with no entry prints as a bare name on the live page."""
        missing = [n for n in features.NAMES if n not in nowcast.FEATURE_FORMATS]
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

    @pytest.fixture
    def model(self):
        model = nowcast.load()
        if model is None:
            pytest.skip("no model shipped in this checkout")
        return model

    def test_the_shipped_model_loads(self, model):
        assert model.features
        assert model.trees
        assert 0.0 < model.threshold < 1.0

    def test_the_shipped_model_names_features_the_app_can_supply(self, model):
        assert set(model.features) <= set(features.NAMES), \
            set(model.features) - set(features.NAMES)

    def test_the_shipped_model_records_its_measured_skill(self, model):
        """A model with no recorded skill has bypassed the gates in ml/train.py."""
        assert model.metadata.get("skill", {}).get("bss") is not None

    def test_the_shipped_model_says_nothing_about_where_it_was_trained(self, model):
        """The training stations are the ones nearest the balcony, so naming
        them would say where it is. The file carries a count and nothing else."""
        text = nowcast.MODEL_PATH.read_text()
        assert isinstance(model.metadata.get("stations"), int)
        assert "latitude" not in text and "longitude" not in text
        assert '"km"' not in text

    def test_its_breakdown_adds_up_on_a_real_vector(self, model):
        values = dict.fromkeys(model.features, 0.5) | {"rh": 99.0, "dT1": -2.0}
        total = model.expected + sum(c.effect for c in model.contributions(values))
        assert total == pytest.approx(model.raw(values), abs=1e-6)

    def test_it_copes_with_every_lag_missing(self, model):
        """An outage behind the current half hour leaves the lags None."""
        values = dict.fromkeys(model.features) | {"rh": 70.0, "pct30": 0.4}
        assert 0.0 < model.predict(values) < 1.0


class TestServiceIntegration:
    def test_no_model_means_no_nowcast(self):
        assert services.run_nowcast({"pct30": 0.5}, model=None) is None

    def test_no_features_means_no_nowcast(self, tmp_path):
        assert services.run_nowcast(None, model=nowcast.load(write(tmp_path, MODEL))) is None

    def test_a_feature_the_app_does_not_compute_means_no_nowcast(self, tmp_path):
        """Better to show nothing than to guess at an input the model was fitted on."""
        model = nowcast.load(write(tmp_path, MODEL))
        assert services.run_nowcast({"pct30": 0.5}, model=model) is None

    def test_payload_carries_what_the_page_shows(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL))
        payload = services.run_nowcast({"pct30": 0.1, "rh": 95.0}, model=model)
        assert set(payload) >= {"probability", "threshold", "label", "horizon_hours",
                                "rain_mm", "trained_at", "skill", "trees", "baseline"}
        assert 0.0 <= payload["probability"] <= 1.0
        assert payload["horizon_hours"] == nowcast.HORIZON_HOURS
        assert payload["trees"] == 2

    async def test_young_database_has_no_feature_vector(self, db):
        """The pressure ranks need a week behind them; until then, no nowcast."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        for i in range(60):
            moment = now - timedelta(minutes=5 * (59 - i))
            await db.insert_reading(20.0, 55.0, 1013.0, moment.strftime(clock.TS_FORMAT))
        assert await services.nowcast_features() is None

    async def test_a_stocked_database_produces_every_feature(self, db):
        await stock(db)
        vector = await services.nowcast_features()
        assert vector is not None
        assert set(vector) == set(features.NAMES)
        assert all(isinstance(v, (int, float)) for v in vector.values()), vector
        assert 0.0 <= vector["pct30"] <= 1.0
        assert 0.0 <= vector["pct7"] <= 1.0

    async def test_a_short_outage_does_not_switch_the_nowcast_off(self, db):
        """A gap where a lag falls costs that feature, not the whole vector."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        await stock(db, gap=(now - timedelta(hours=8), now - timedelta(hours=4)))
        vector = await services.nowcast_features()
        assert vector is not None
        assert vector["dp6"] is None, "the six-hour lag fell in the gap"
        assert vector["dp1"] is not None

    async def test_a_stopped_feed_has_no_vector(self, db):
        """Readings that stopped an hour ago describe a moment already gone."""
        now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
        await stock(db, gap=(now - timedelta(hours=1), now + timedelta(minutes=1)))
        assert await services.nowcast_features() is None


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

    def payload(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL))
        return services.run_nowcast({"pct30": 0.2, "rh": 88.0}, model)

    def test_every_feature_gets_a_row(self, tmp_path):
        rows = self.payload(tmp_path)["contributions"]
        assert {r["name"] for r in rows} == {"pct30", "rh"}

    def test_each_row_carries_its_own_formatting(self, tmp_path):
        for row in self.payload(tmp_path)["contributions"]:
            assert set(row) == {"name", "label", "unit", "digits", "sign", "value", "effect"}
            assert isinstance(row["digits"], int)
            assert isinstance(row["sign"], bool)

    def test_percentile_rows_are_sent_as_percentages(self, tmp_path):
        """The model carries a fraction; the page prints 20 %, not 0.2."""
        row = next(r for r in self.payload(tmp_path)["contributions"]
                   if r["name"] == "pct30")
        assert row["value"] == pytest.approx(20.0)
        assert row["unit"] == "%"

    def test_a_missing_value_is_sent_as_none(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL))
        rows = services.run_nowcast({"pct30": None, "rh": 88.0}, model)["contributions"]
        assert next(r for r in rows if r["name"] == "pct30")["value"] is None

    def test_rows_are_ordered_by_influence(self, tmp_path):
        """By what each is doing now, not by the order the trainer listed them."""
        effects = [abs(r["effect"]) for r in self.payload(tmp_path)["contributions"]]
        assert effects == sorted(effects, reverse=True)

    def test_the_baseline_and_effects_reach_the_quoted_probability(self, tmp_path):
        """The table has to add up, or it is describing a different number."""
        payload = self.payload(tmp_path)
        total = payload["baseline"] + sum(r["effect"] for r in payload["contributions"])
        assert total == pytest.approx(payload["logit"], abs=0.01)
        assert 1 / (1 + math.exp(-payload["logit"])) == pytest.approx(
            payload["probability"], abs=0.01)

    def test_the_model_card_passes_the_trainer_s_metadata_through(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL | {"metadata": {
            "samples": 882221, "base_rate": 0.184, "trained_from": "2014-02-01",
            "trained_through": "2026-09-30", "stations": 5,
            "baselines": {"rules": {"brier": 0.19}},
            "seasons": {"DJF": 0.22, "MAM": 0.26, "JJA": 0.2, "SON": 0.22},
            "holdout": {"from": "2025-09-30", "to": "2026-09-30", "hours": 69746},
            "archive": {"from": "2026-06-20", "to": "2026-09-30", "hours": 2300},
        }}))
        payload = services.run_nowcast({"pct30": 0.5, "rh": 60.0}, model)
        assert payload["samples"] == 882221
        assert payload["base_rate"] == 0.184
        assert payload["trained_from"] == "2014-02-01"
        assert payload["trained_through"] == "2026-09-30"
        assert payload["stations"] == 5
        assert payload["baselines"]["rules"]["brier"] == 0.19
        assert payload["seasons"]["DJF"] == 0.22
        assert payload["holdout"]["hours"] == 69746
        assert payload["archive"]["hours"] == 2300

    def test_the_leave_one_out_table_is_passed_through_and_labelled(self, tmp_path):
        """How hard the trees lean on a signal is not whether leaning on it
        helps. The trainer measures the second thing; the page prints it
        under the same names as the breakdown."""
        model = nowcast.load(write(tmp_path, MODEL | {"metadata": {
            "ablations": [
                {"feature": "rh", "brier": 0.21, "auc": 0.70,
                 "bss": 0.05, "brier_cost": 0.02, "auc_cost": 0.09},
                {"feature": "pct30", "brier": 0.19, "auc": 0.82,
                 "bss": 0.14, "brier_cost": 0.0001, "auc_cost": 0.0},
            ],
        }}))
        payload = services.run_nowcast({"pct30": 0.5, "rh": 60.0}, model)
        rows = payload["ablations"]
        assert [r["feature"] for r in rows] == ["rh", "pct30"]
        assert rows[0]["label"] == nowcast.describe_feature("rh").label
        assert rows[0]["brier_cost"] == 0.02

    def test_an_older_model_without_one_simply_has_none(self, tmp_path):
        model = nowcast.load(write(tmp_path, MODEL | {"metadata": {}}))
        assert services.run_nowcast({"pct30": 0.5, "rh": 60.0}, model)["ablations"] is None

    def test_metadata_the_trainer_did_not_write_is_simply_absent(self, tmp_path):
        """A sparser model file must not take the explainer down with it."""
        model = nowcast.load(write(tmp_path, MODEL | {"metadata": {}}))
        payload = services.run_nowcast({"pct30": 0.5, "rh": 60.0}, model)
        assert payload["samples"] is None
        assert payload["baselines"] is None
        assert payload["archive"] is None
        assert payload["seasons"] is None
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
        total = nowcast_payload["baseline"] + sum(r["effect"] for r in rows)
        assert total == pytest.approx(nowcast_payload["logit"], abs=0.05)

    async def test_the_page_renders_the_breakdown_it_was_given(self, client, db):
        """The table is server-rendered first; the poller only maintains it."""
        if services.NOWCAST_MODEL is None:
            pytest.skip("no model shipped")
        await stock(db)
        text = (await client.get("/")).text
        body = re.search(r'id="deep-features".*?<tbody>(.*?)</tbody>', text, re.S)
        assert body, "the contribution table did not render"
        assert body.group(1).count("<tr>") == len(services.NOWCAST_MODEL.features)


SKY_MODEL = MODEL | {
    "trees": [stump(0, 0.5, 0.4, -0.4), stump(1, 85.0, -0.5, 1.1)],
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
        for vector in ({"pct30": 0.9, "rh": 20.0}, {"pct30": 0.1, "rh": 99.0}):
            payload = services.run_sky(vector, model=model)
            assert payload["label"] == nowcast.describe_sky(payload["probability"])

    def test_the_shipped_sky_model_is_readable_if_it_exists(self):
        """It is absent until CI first ships one, which is a normal state."""
        if nowcast.SKY_MODEL_PATH.exists():
            model = nowcast.load(nowcast.SKY_MODEL_PATH)
            assert model is not None, "a shipped sky model must be loadable"
            assert set(model.features) <= set(features.NAMES)


class TestOutlookSelection:
    """Which of the two ladders produced the phrase, and when."""

    async def test_the_rain_model_alone_is_enough_to_lead(self, client, db):
        """With no sky_model.json shipped, which is a normal state.

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
