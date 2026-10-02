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
    y = np.full((count, len(train.TARGET_NAMES)), np.nan)
    y[:, train.TARGET_NAMES.index("rain")] = rain
    return train.Samples(np.array(rows), np.array(local, dtype="datetime64[s]"), local, y)


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
        model = train.fit(self.train.matrix(), self.train.labels("rain"))
        reference = train.score(model.predict_proba(self.test.matrix())[:, 1], self.test.labels("rain"), 0.5)
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
        return train.fit(X, data.labels("rain")), X

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


class TestHourlyLabels:
    """Cloud, fog and thunder come from the hourly record, top-of-hour stamps."""

    START = datetime(2021, 7, 1, 12, 0, tzinfo=UTC)

    def hours(self, *values):
        return {self.START + timedelta(hours=k + 1): v for k, v in enumerate(values)}

    def test_overcast_is_the_mean_over_the_horizon(self):
        assert train.sky_label(self.hours(8, 8, 8, 8, 4, 4), self.START) == 1.0  # 6.67 octas
        assert train.sky_label(self.hours(8, 8, 8, 4, 4, 4), self.START) == 0.0  # 6.0

    def test_a_sky_hidden_by_fog_is_overcast(self):
        assert train.sky_label(self.hours(-1, -1, -1, -1, -1, -1), self.START) == 1.0

    def test_the_hour_ending_at_the_moment_is_the_past(self):
        series = self.hours(0, 0, 0, 0, 0, 0) | {self.START: 8.0}
        assert train.sky_label(series, self.START) == 0.0

    def test_one_foggy_hour_is_a_foggy_window(self):
        assert train.fog_label(self.hours(20000, 20000, 800, 20000, 20000, 20000),
                               self.START) == 1.0
        assert train.fog_label(self.hours(*[1000] * 6), self.START) == 0.0

    def test_a_gap_is_no_label(self):
        series = self.hours(20000, 20000, 800, 20000, 20000, 20000)
        del series[self.START + timedelta(hours=4)]
        assert train.fog_label(series, self.START) is None
        assert train.sky_label(series, self.START) is None

    def test_thunder_in_any_hour_counts_and_nothing_reported_is_no_thunder(self):
        assert train.thunder_label(self.hours(-1, 61, 95, -1, -1, -1), self.START) == 1.0
        assert train.thunder_label(self.hours(-1, 61, 63, -1, -1, -1), self.START) == 0.0

    def test_thunder_is_labelled_only_where_and_when_observers_reported_it(self):
        storm = self.hours(-1, 95, -1, -1, -1, -1)
        later = {t.replace(year=2023): v for t, v in storm.items()}
        kept = train.observed_thunder(storm | later)
        assert set(kept) == set(storm)
        assert train.labeller("thunder", dict.fromkeys(storm, -1.0)) is None
        bound = train.labeller("thunder", storm | later)
        assert bound(self.START) == 1.0
        assert bound(self.START.replace(year=2023)) is None


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
        return air, gauge, train.build_samples(air, {"rain": train.labeller("rain", gauge)})

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
        wet = [m for m, y in zip(samples.local, samples.labels("rain"), strict=True) if y == 1.0]
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

    def archive(self, bss=0.15, auc=0.79, hours=2000, events=150):
        return {"hours": hours, "events": events,
                "skill": {"bss": bss, "auc": auc}, "rules": {"auc": 0.61}}

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


class TestRareEvents:
    """Fog and thunder are rare, and the gates have to know it."""

    def evaluation(self, archive):
        return {
            "skill": {"bss": 0.25, "auc": 0.93},
            "baselines": {"rules": {"auc": 0.66}},
            "archive": archive,
        }

    def test_a_handful_of_events_is_not_a_test(self):
        thin = {"hours": 2000, "events": train.MIN_ARCHIVE_EVENTS - 1,
                "skill": {"bss": -0.4, "auc": 0.5}, "rules": {"auc": 0.6}}
        assert train.gates(self.evaluation(thin), "sky", None) == []

    def test_fog_does_not_ship_untested_on_this_sensor(self):
        """A sensor that sits at 100 % whenever it is wet looks like fog to a
        model fitted on screens, so the balcony must have had its say."""
        assert any("not yet tested" in r for r in train.gates(self.evaluation(None), "fog", None))
        thin = {"hours": 2000, "events": 3, "skill": {"bss": 0.3, "auc": 0.9},
                "rules": {"auc": 0.6}}
        assert any("not yet tested" in r for r in train.gates(self.evaluation(thin), "fog", None))

    def test_fog_tested_on_this_sensor_may_ship(self):
        tested = {"hours": 2000, "events": 60, "skill": {"bss": 0.2, "auc": 0.9},
                  "rules": {"auc": 0.6}}
        assert train.gates(self.evaluation(tested), "fog", None) == []

    def test_a_level_shift_at_its_bound_is_refused(self):
        evaluation = self.evaluation(None) | {
            "level": {"shift": -train.LEVEL_BOUND, "said": 0.2, "climate": 0.02},
        }
        assert any("climatology" in r for r in train.gates(evaluation, "thunder", None))


class TestLevel:
    """Thunder's only calibration: one shift, to the stations' climatology."""

    def test_the_shift_brings_the_mean_to_the_rate(self):
        z = np.random.default_rng(1).normal(-2.0, 1.0, 5000)
        shift = train.match_level(z, 0.05)
        assert train.sigmoid(z + shift).mean() == pytest.approx(0.05, abs=1e-4)

    def test_it_never_reorders_hours(self):
        z = np.random.default_rng(2).normal(-2.0, 1.0, 100)
        shifted = z + train.match_level(z, 0.3)
        assert list(np.argsort(shifted)) == list(np.argsort(z))

    def test_beyond_the_bound_it_stops_at_the_bound(self):
        z = np.full(100, -8.0)
        assert train.match_level(z, 0.5) == pytest.approx(train.LEVEL_BOUND, abs=1e-6)

    def test_rates_are_by_calendar_month(self):
        data = synthetic(24 * 70)  # January, February (2024: 29 days) and a bit of March
        y = np.zeros(len(data))
        months = np.array([m.month for m in data.local])
        y[months == 2] = 1.0
        data.y[:, train.TARGET_NAMES.index("thunder")] = y
        assert train.monthly_rates(data, "thunder") == {1: 0.0, 2: 1.0, 3: 0.0}


class TestIncumbents:
    """Each model is compared with the hand-made rung it replaces."""

    def samples(self, **columns):
        row = [np.nan] * len(features.NAMES)
        for name, value in columns.items():
            row[features.NAMES.index(name)] = value
        local = [datetime(2026, 7, 15, 15, 0)]
        return train.Samples(np.array([row]), np.array(local, dtype="datetime64[s]"), local,
                             np.full((1, len(train.TARGET_NAMES)), np.nan))

    def test_thunder_is_the_convective_rule(self):
        hot = self.samples(rh=60.0, temp=28.0, spread=10.0, drh3=0.0)
        cool = self.samples(rh=60.0, temp=20.0, spread=10.0, drh3=0.0)
        assert train.incumbent(hot, "thunder")[0] == 1.0
        assert train.incumbent(cool, "thunder")[0] == 0.0

    def test_fog_is_saturated_air_still_wetting(self):
        wetting = self.samples(rh=95.0, temp=8.0, spread=1.0, drh3=3.0)
        steady = self.samples(rh=95.0, temp=8.0, spread=1.0, drh3=0.0)
        assert train.incumbent(wetting, "fog")[0] == 1.0
        assert train.incumbent(steady, "fog")[0] == 0.0


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
        y[: train.MIN_ARCHIVE_EVENTS - 1] = 1
        assert not train.can_calibrate(y)
        y[: train.MIN_ARCHIVE_EVENTS] = 1
        assert train.can_calibrate(y)

    def test_the_export_carries_it_and_the_trees_are_checked_under_it(self, small_trees):
        data = synthetic(2000)
        fitted = train.fit(data.matrix(), data.labels("rain"))
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
                "hours": 1700, "events": 140, "skill": {"bss": bss, "auc": 0.80},
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
