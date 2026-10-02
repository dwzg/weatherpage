"""The nowcast's feature vector, as a pure function of the readings.

app.features is the one place the vector is computed, for the app and for
the trainer alike, so these pin down what each feature means — in time, not
in readings, because the balcony reports every five minutes and the weather
service's stations every ten.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app import features
from app.weather import compute_dew_point

NOW = datetime(2026, 10, 1, 20, 0)


def series(step_minutes: int = 5, days: int = 32, end: datetime = NOW, **signal) -> features.Series:
    """A run of readings ending at ``end``, each metric a function of hours before it."""
    temperature = signal.get("temperature", lambda h: 15.0)
    humidity = signal.get("humidity", lambda h: 70.0)
    pressure = signal.get("pressure", lambda h: 1013.0 + math.sin(h / 17))
    rows = []
    moment = end - timedelta(days=days)
    while moment <= end:
        h = (end - moment).total_seconds() / 3600
        rows.append((moment, temperature(h), humidity(h), pressure(h)))
        moment += timedelta(minutes=step_minutes)
    return features.Series.from_rows(rows)


class TestWhenThereIsNothingToSay:
    def test_no_reading_in_the_last_half_hour(self):
        stale = series(end=NOW - timedelta(minutes=45))
        assert features.compute(stale, NOW, {}) is None

    def test_under_a_week_to_rank_the_pressure_against(self):
        young = series(days=6)
        assert features.compute(young, NOW, {}) is None

    def test_a_week_is_enough(self):
        assert features.compute(series(days=8), NOW, {}) is not None


class TestTheVector:
    def test_it_names_every_feature(self):
        vector = features.compute(series(), NOW, {})
        assert set(vector) == set(features.NAMES)

    def test_values_sit_on_the_grid_the_trees_split_between(self):
        vector = features.compute(series(temperature=lambda h: 15.0 + h / 7), NOW, {})
        for name, value in vector.items():
            assert value == round(value, features.DECIMALS[name]), name

    def test_changes_are_now_minus_then(self):
        """Temperature falling by a degree an hour reads as -1 per hour."""
        vector = features.compute(series(temperature=lambda h: 15.0 + h), NOW, {})
        assert vector["dT1"] == pytest.approx(-1.0)
        assert vector["dT3"] == pytest.approx(-3.0)
        assert vector["dT24"] == pytest.approx(-24.0)

    def test_pressure_changes_too(self):
        vector = features.compute(series(pressure=lambda h: 1013.0 + 0.5 * h), NOW, {})
        assert vector["dp1"] == pytest.approx(-0.5)
        assert vector["dp12"] == pytest.approx(-6.0)

    def test_the_dew_point_is_the_weather_module_s(self):
        """One Magnus formula, shared with the cards."""
        vector = features.compute(series(temperature=lambda h: 12.0, humidity=lambda h: 90.0),
                                  NOW, {})
        assert vector["td"] == pytest.approx(round(compute_dew_point(12.0, 90.0), 2))
        assert vector["spread"] == pytest.approx(round(12.0 - compute_dew_point(12.0, 90.0), 2))

    def test_the_hour_is_the_local_wall_clock_hour(self):
        assert features.compute(series(), NOW, {})["hour"] == 20

    def test_saturation_is_time_spent_at_the_top(self):
        """The last 3 h, of which the final hour sat at 100 %."""
        vector = features.compute(series(humidity=lambda h: 100.0 if h < 1 else 80.0), NOW, {})
        assert vector["rh_max1"] == 100.0
        assert vector["sat3"] == pytest.approx(12 / 36, abs=0.01)

    def test_the_rank_is_the_share_strictly_below(self):
        assert features.rank(5.0, [1.0, 2.0, 5.0, 9.0]) == 0.5

    def test_the_highest_pressure_of_the_month_ranks_at_the_top(self):
        vector = features.compute(series(pressure=lambda h: 1030.0 - h / 10), NOW, {})
        assert vector["pct30"] >= 0.99
        assert vector["pct7"] >= 0.99


class TestCadence:
    """The same weather, sampled every five or every ten minutes.

    Not identical: the median of a half hour sits a few minutes further back
    on the five-minute grid, so a level drifting at a weather-like rate
    differs by a few hundredths. A change is unaffected — both of its ends
    move back together.
    """

    @pytest.mark.parametrize("name", ["dT1", "dT3", "dp1", "dp6", "pct30", "rh", "td"])
    def test_five_and_ten_minute_readings_agree(self, name):
        signal = {
            "temperature": lambda h: 15.0 + 0.5 * h,
            "pressure": lambda h: 1013.0 + 0.2 * h,
            "humidity": lambda h: 60.0 + 0.3 * h,
        }
        five = features.compute(series(step_minutes=5, **signal), NOW, {})
        ten = features.compute(series(step_minutes=10, **signal), NOW, {})
        # Up to one step of the grid the feature is rounded to, as well.
        tolerance = 0.05 + 10 ** -features.DECIMALS[name]
        assert five[name] == pytest.approx(ten[name], abs=tolerance)


class TestOutages:
    def test_a_lag_in_a_gap_is_missing_on_its_own(self):
        full = series()
        keep = [i for i, t in enumerate(full.times)
                if not NOW - timedelta(hours=8) <= t < NOW - timedelta(hours=4)]
        gappy = features.Series.from_rows(
            (full.times[i], full.temperature[i], full.humidity[i], full.pressure[i])
            for i in keep
        )
        vector = features.compute(gappy, NOW, {})
        assert vector is not None
        assert vector["dp6"] is None
        assert vector["dT1"] is not None


class TestPressureCycle:
    """The daily rhythm a sun-warmed BME280 writes into its own pressure."""

    def rows(self, days: int, step_minutes: int = 10, amplitude: float = 1.5):
        start = datetime(2026, 9, 1)
        moment = start
        while moment < start + timedelta(days=days):
            hour = moment.hour + moment.minute / 60
            yield (moment.date(), features.slot_of(moment),
                   1013.0 + amplitude * math.cos((hour - 11) / 24 * 2 * math.pi))
            moment += timedelta(minutes=step_minutes)

    def test_too_few_complete_days_learn_nothing(self):
        assert features.pressure_cycle(self.rows(features.CYCLE_MIN_DAYS - 1)) == {}

    def test_it_recovers_the_injected_swing(self):
        cycle = features.pressure_cycle(self.rows(20))
        assert max(cycle.values()) - min(cycle.values()) == pytest.approx(3.0, abs=0.1)
        assert max(cycle, key=cycle.get) in (21, 22, 23)  # around 11:00

    def test_a_day_with_a_long_outage_does_not_count(self):
        days = features.summarise_days(self.rows(20))
        first = min(days)
        days[first].counts[:20] = [0] * 20
        assert not days[first].complete

    def test_five_and_ten_minute_stations_learn_the_same_cycle(self):
        five = features.pressure_cycle(self.rows(20, step_minutes=5))
        ten = features.pressure_cycle(self.rows(20, step_minutes=10))
        assert five.keys() == ten.keys()
        for slot in five:
            assert five[slot] == pytest.approx(ten[slot], abs=0.05)

    def test_detiding_removes_it(self):
        cycle = features.pressure_cycle(self.rows(20))
        noon, midnight = datetime(2026, 10, 1, 11, 0), datetime(2026, 10, 1, 23, 0)
        raw_noon, raw_midnight = 1014.5, 1011.5
        assert features.detide(noon, raw_noon, cycle) == pytest.approx(
            features.detide(midnight, raw_midnight, cycle), abs=0.1)


class TestSwings:
    """How far temperature and humidity ranged: the sky's fingerprint.

    A clear sky heats by day and cools by night, so the temperature swing is
    what the cloud model reads the sky from, without seeing it.
    """

    def test_the_swing_is_top_to_bottom_over_the_window(self):
        # Falling a degree an hour into the present. The window is (now - 3 h,
        # now], so its oldest five-minute reading is 2 h 55 back: 2.9 °C.
        vector = features.compute(series(temperature=lambda h: 15.0 + h), NOW, {})
        assert vector["t_range3"] == pytest.approx(2.9)
        assert vector["t_range24"] == pytest.approx(23.9)

    def test_humidity_has_its_swing_and_its_floor(self):
        vector = features.compute(series(humidity=lambda h: 90.0 - h), NOW, {})
        assert vector["rh_range6"] == pytest.approx(6.0, abs=0.1)
        assert vector["rh_min24"] == pytest.approx(66.0, abs=0.1)

    def test_a_window_mostly_lost_to_an_outage_has_no_swing(self):
        full = series()
        keep = [i for i, t in enumerate(full.times) if not NOW - timedelta(hours=20) <= t < NOW - timedelta(minutes=40)]
        gappy = features.Series.from_rows(
            (full.times[i], full.temperature[i], full.humidity[i], full.pressure[i]) for i in keep
        )
        vector = features.compute(gappy, NOW, {})
        assert vector["t_range3"] is None, "40 minutes of a three-hour window is not its swing"
        assert vector["t_range24"] is not None, "the day's window still spans most of it"

    @pytest.mark.parametrize("name", ["t_range6", "rh_range6", "rh_min24"])
    def test_five_and_ten_minute_readings_agree(self, name):
        signal = {"temperature": lambda h: 15.0 + 0.5 * h, "humidity": lambda h: 60.0 + 0.3 * h}
        five = features.compute(series(step_minutes=5, **signal), NOW, {})
        ten = features.compute(series(step_minutes=10, **signal), NOW, {})
        assert five[name] == pytest.approx(ten[name], abs=0.2)
