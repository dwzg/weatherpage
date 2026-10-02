"""The parts of the retraining job that can be checked without a network.

The trainer runs weekly in CI with numpy and scikit-learn installed from
ml/requirements.txt; the ordinary test environment has neither, so these
skip there. They run for anyone working on the model, which is when they
are the ones that matter. What can be checked by parsing the source instead
lives in tests/test_api.py, where it runs everywhere.
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from app import features  # noqa: E402
from ml import train  # noqa: E402


def synthetic(count: int, seed: int = 7) -> train.Samples:
    """Hours where one feature decides the label and the rest are noise.

    ``td`` carries the answer and ``dp12`` is pure noise. A leave-one-out
    that cannot tell those two apart is not measuring anything. Every value
    sits on the grid app.features rounds to, as real features do.
    """
    rng = random.Random(seed)
    start = datetime(2024, 1, 1)
    rows, rain = [], []
    for _ in range(count):
        row = []
        for name in features.NAMES:
            value = rng.gauss(0, 1) * 5
            row.append(round(value, features.DECIMALS[name]))
        rows.append(row)
        rain.append(float(row[features.NAMES.index("td")] > 2.0))
    local = [start + timedelta(hours=i) for i in range(count)]
    return train.Samples(
        np.array(rows), np.array(local, dtype="datetime64[s]"), local,
        np.array(rain), np.full(count, np.nan),
    )


@pytest.fixture
def small_trees(monkeypatch):
    """The same trees, fewer of them: the tests need the behaviour, not the skill."""
    monkeypatch.setitem(train.GBM, "max_iter", 40)
    monkeypatch.setitem(train.GBM, "min_samples_leaf", 20)


class TestLeaveOneOut:
    @pytest.fixture(autouse=True)
    def measured(self, small_trees):
        data = synthetic(3000)
        self.train, self.test = data.take(np.arange(3000) < 2000), data.take(np.arange(3000) >= 2000)
        model = train.fit(self.train.matrix(), self.train.rain)
        reference = train.score(model.predict_proba(self.test.matrix())[:, 1], self.test.rain, 0.5)
        self.worth = train.ablations(self.train, self.test, "rain", reference)

    def test_every_feature_gets_a_row(self):
        assert {row["feature"] for row in self.worth} == set(train.FEATURES)

    def test_the_feature_carrying_the_label_is_worth_the_most(self):
        assert self.worth[0]["feature"] == "td"
        assert self.worth[0]["brier_cost"] > 0.05, self.worth[0]

    def test_a_feature_that_is_only_noise_costs_nothing_to_drop(self):
        noise = next(row for row in self.worth if row["feature"] == "dp12")
        assert abs(noise["brier_cost"]) < 0.01, noise

    def test_rows_are_ordered_by_what_they_cost(self):
        costs = [row["brier_cost"] for row in self.worth]
        assert costs == sorted(costs, reverse=True)


class TestExport:
    """The file the app serves must be the model scikit-learn fitted."""

    @pytest.fixture
    def fitted(self, small_trees):
        data = synthetic(2000)
        X = data.matrix()
        # Some missing values, so the export's missing-value routing is used.
        X[::17, train.FEATURES.index("dT24")] = np.nan
        return train.fit(X, data.rain), X

    def test_the_app_reproduces_scikit_learn(self, fitted):
        model, X = fitted
        exported = train.export(model, train.FEATURES, 0.3, {"stations": 1})
        served = train.as_model(exported)
        expected = model.predict_proba(X)[:, 1]
        for row, want in zip(X[:300], expected[:300], strict=True):
            values = {
                name: None if np.isnan(v) else float(v)
                for name, v in zip(served.features, row, strict=True)
            }
            assert served.predict(values) == pytest.approx(want, abs=1e-4)

    def test_the_self_check_passes_on_a_faithful_export(self, fitted):
        model, X = fitted
        train.self_check(train.export(model, train.FEATURES, 0.3, {}), model, X, count=300)

    def test_the_self_check_refuses_a_tampered_one(self, fitted):
        model, X = fitted
        exported = train.export(model, train.FEATURES, 0.3, {})
        exported["base"] += 0.5
        with pytest.raises(SystemExit):
            train.self_check(exported, model, X, count=50)

    def test_internal_nodes_carry_the_expectation_beneath_them(self, fitted):
        """What the page's breakdown measures each step against."""
        model, _ = fitted
        tree = train.export(model, train.FEATURES, 0.3, {})["trees"][0]
        for node, feature in enumerate(tree["feature"]):
            if feature >= 0:
                low, high = sorted((tree["value"][tree["left"][node]],
                                    tree["value"][tree["right"][node]]))
                assert low - 1e-6 <= tree["value"][node] <= high + 1e-6

    def test_the_vectorised_walk_matches_the_app_s(self, fitted):
        model, X = fitted
        served = train.as_model(train.export(model, train.FEATURES, 0.3, {}))
        fast = train.raw_scores(served, X[:200])
        for row, value in zip(X[:200], fast, strict=True):
            values = {n: None if np.isnan(v) else float(v)
                      for n, v in zip(served.features, row, strict=True)}
            assert served.raw(values) == pytest.approx(value, abs=1e-9)


class TestLabels:
    START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    def gauge(self, **amounts):
        series = {self.START + train.dwd.STEP * k: 0.0 for k in range(1, 37)}
        for k, mm in amounts.items():
            series[self.START + train.dwd.STEP * int(k[1:])] = mm
        return series

    def test_a_dry_window_is_dry(self):
        assert train.rain_label(self.gauge(), self.START) == 0.0

    def test_rain_anywhere_in_the_six_hours_counts(self):
        assert train.rain_label(self.gauge(k36=0.2), self.START) == 1.0

    def test_it_adds_up_across_the_window(self):
        assert train.rain_label(self.gauge(k1=0.1, k20=0.1), self.START) == 1.0

    def test_the_ten_minutes_ending_at_the_moment_are_the_past(self):
        series = self.gauge() | {self.START: 5.0}
        assert train.rain_label(series, self.START) == 0.0

    def test_a_window_with_a_gap_is_no_label(self):
        """An hour the gauge was down is not an hour it stayed dry."""
        series = self.gauge()
        del series[self.START + train.dwd.STEP * 12]
        assert train.rain_label(series, self.START) is None


class TestSamples:
    """build_samples is app.features at every hour, on the app's own clock."""

    @pytest.fixture
    def built(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        air, gauge = [], {}
        moment = start
        i = 0
        while moment < start + timedelta(days=40):
            air.append((moment, 5.0 + (i % 144) / 20, 80.0 + (i % 7), 1010.0 + (i % 300) / 30))
            gauge[moment] = 0.3 if (moment.month, moment.day, moment.hour) == (2, 5, 6) else 0.0
            moment += timedelta(minutes=10)
            i += 1
        return air, gauge, train.build_samples(air, gauge, None)

    def test_nothing_before_a_month_of_history(self, built):
        air, _, samples = built
        first = air[0][0].replace(tzinfo=None) + timedelta(days=train.MIN_HISTORY_DAYS)
        assert samples.moments[0] >= np.datetime64(first)

    def test_every_row_is_what_the_app_would_have_computed(self, built):
        air, _, samples = built
        tz = train.ZoneInfo(train.TIMEZONE)
        series = features.Series.from_rows(
            (u.astimezone(tz).replace(tzinfo=None), t, h, p) for u, t, h, p in air
        )
        for index in (0, len(samples) // 2, len(samples) - 1):
            local = samples.local[index]
            day = local.date()
            days = features.summarise_days(
                (t.date(), features.slot_of(t), p)
                for t, p in zip(series.times, series.pressure, strict=True)
                if day - timedelta(days=features.CYCLE_LEARN_DAYS) <= t.date() < day
            )
            vector = features.compute(series, local, features.cycle_from_days(days.values()))
            assert list(samples.X[index]) == pytest.approx(
                [np.nan if vector[n] is None else vector[n] for n in features.NAMES],
                nan_ok=True,
            )

    def test_local_time_is_berlin_wall_clock(self, built):
        _, _, samples = built
        utc = samples.moments[0].astype(datetime)
        assert samples.local[0] == utc + timedelta(hours=1)  # CET in January

    def test_the_hours_before_the_rain_are_wet(self, built):
        _, _, samples = built
        wet = [m for m, y in zip(samples.local, samples.rain, strict=True) if y == 1.0]
        assert wet, "the burst on 5 February should label the hours before it"
        # 06:00-06:50 UTC, so the six hours before it start at 00:00 UTC,
        # which is 01:00 on the local wall clock.
        assert all((m.month, m.day) == (2, 5) and 1 <= m.hour <= 7 for m in wet), wet


def test_an_app_row_becomes_its_utc_instant():
    """The offset is what separates the two passes through 02:30 in October."""
    first = train.instant({"timestamp": "2026-10-25 02:30:00", "utc_offset": 120})
    second = train.instant({"timestamp": "2026-10-25 02:30:00", "utc_offset": 60})
    assert second - first == timedelta(hours=1)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


class TestGates:
    """What a candidate must clear, on both tests, before it may ship."""

    def evaluation(self, bss=0.22, auc=0.83, archive=None):
        return {
            "skill": {"bss": bss, "auc": auc},
            "baselines": {"rules": {"auc": 0.66}},
            "archive": archive,
        }

    def archive(self, bss=0.15, auc=0.79, hours=2000):
        return {"hours": hours, "skill": {"bss": bss, "auc": auc}, "rules": {"auc": 0.61}}

    def shipped(self, bss=0.226, archive_bss=None):
        meta = {"skill": {"bss": bss}}
        if archive_bss is not None:
            meta["archive"] = {"skill": {"bss": archive_bss}}
        return {"metadata": meta}

    def test_a_good_candidate_ships(self):
        assert train.gates(self.evaluation(archive=self.archive()), "rain", self.shipped()) == []

    def test_too_little_skill_on_the_held_out_year(self):
        assert train.gates(self.evaluation(bss=0.01), "rain", None)

    def test_ranking_worse_than_the_ladder(self):
        assert train.gates(self.evaluation(auc=0.60), "rain", None)

    def test_failing_on_this_balcony_is_failing(self):
        """Good on weather-service screens and bad on this sensor is bad here."""
        reasons = train.gates(self.evaluation(archive=self.archive(bss=-0.02)), "rain", None)
        assert any("balcony archive" in r for r in reasons)

    def test_a_short_archive_is_not_gated_on(self):
        thin = self.archive(bss=-0.5, hours=train.MIN_ARCHIVE_HOURS - 1)
        assert train.gates(self.evaluation(archive=thin), "rain", None) == []

    def test_a_sharp_step_down_from_what_the_shipped_model_recorded(self):
        reasons = train.gates(self.evaluation(bss=0.15), "rain", self.shipped(bss=0.226))
        assert any("step down" in r for r in reasons)

    def test_a_wobble_inside_the_tolerance_ships(self):
        assert train.gates(self.evaluation(bss=0.20), "rain", self.shipped(bss=0.226)) == []

    def test_the_archive_is_compared_with_the_archive(self):
        reasons = train.gates(
            self.evaluation(archive=self.archive(bss=0.08)), "rain",
            self.shipped(archive_bss=0.2),
        )
        assert any("balcony archive" in r and "step down" in r for r in reasons)


class TestCalibrationFit:
    """The two numbers fitted to the balcony's record, and how they are scored."""

    def miscalibrated(self, count=4000, slope=0.6, intercept=-0.8, seed=3):
        """Log-odds that run high: the truth is a rescaling of what they say."""
        rng = np.random.default_rng(seed)
        z = rng.normal(-1.5, 1.5, count)
        y = (rng.random(count) < train.sigmoid(slope * z + intercept)).astype(float)
        return z, y

    def test_it_recovers_the_rescaling(self):
        z, y = self.miscalibrated()
        slope, intercept = train.platt(z, y)
        assert slope == pytest.approx(0.6, abs=0.1)
        assert intercept == pytest.approx(-0.8, abs=0.15)

    def test_calibrating_never_changes_the_ranking(self):
        z, y = self.miscalibrated()
        from sklearn.metrics import roc_auc_score

        assert roc_auc_score(y, train.out_of_fold(z, y)) == pytest.approx(
            roc_auc_score(y, z), abs=0.02)  # each fold is monotone; only fold seams move

    def test_out_of_fold_beats_straight_on_a_miscalibrated_record(self):
        z, y = self.miscalibrated()
        straight = train.score(train.sigmoid(z), y, 0.3)["brier"]
        assert train.score(train.out_of_fold(z, y), y, 0.3)["brier"] < straight

    def test_no_hour_is_scored_by_a_fit_that_saw_it(self, monkeypatch):
        """Each contiguous stretch is calibrated by a fit on the others alone."""
        z, y = self.miscalibrated(count=400)
        seen = []
        real = train.platt

        def spy(zz, yy):
            seen.append(len(zz))
            return real(zz, yy)

        monkeypatch.setattr(train, "platt", spy)
        train.out_of_fold(z, y, folds=4)
        assert seen == [300, 300, 300, 300]

    def test_too_few_wet_hours_are_not_calibrated_on(self):
        y = np.zeros(train.MIN_ARCHIVE_HOURS)
        y[: train.MIN_CALIBRATION_WET - 1] = 1
        assert not train.can_calibrate(y)
        y[: train.MIN_CALIBRATION_WET] = 1
        assert train.can_calibrate(y)

    def test_the_export_carries_it_and_the_trees_are_checked_under_it(self, small_trees):
        data = synthetic(2000)
        fitted = train.fit(data.matrix(), data.rain)
        exported = train.export(fitted, train.FEATURES, 0.3, {}, calibration=(0.7, -0.3))
        assert exported["calibration"] == {"slope": 0.7, "intercept": -0.3}
        train.self_check(exported, fitted, data.matrix(), count=200)
        # The check reads the calibration from the file it is checking, so
        # what it holds to account is the trees under that calibration.
        exported["base"] += 0.5
        with pytest.raises(SystemExit):
            train.self_check(exported, fitted, data.matrix(), count=50)


class TestReplacingAnUntestedModel:
    """The first tree model was trained where no balcony archive could be
    scored, so it has no archive record to be measured against. A candidate
    short of the bar may still replace it — but only for real."""

    def evaluation(self, bss, live_bss):
        return {
            "skill": {"bss": 0.25, "auc": 0.85},
            "baselines": {"rules": {"auc": 0.68}},
            "archive": {
                "hours": 1700, "skill": {"bss": bss, "auc": 0.80},
                "rules": {"auc": 0.73}, "shipped": {"bss": live_bss},
            },
        }

    def test_better_than_live_and_better_than_nothing_ships(self):
        assert train.gates(self.evaluation(0.03, -0.11), "rain", None) == []

    def test_better_than_live_but_worse_than_nothing_does_not(self):
        assert train.gates(self.evaluation(-0.02, -0.11), "rain", None)

    def test_short_of_the_bar_and_no_better_than_live_does_not(self):
        assert train.gates(self.evaluation(0.03, 0.04), "rain", None)

    def test_a_calibration_that_would_reverse_the_ranking_is_refused(self):
        evaluation = self.evaluation(0.12, -0.11) | {"calibration": (-0.4, 0.1)}
        assert any("backwards" in r for r in train.gates(evaluation, "rain", None))
