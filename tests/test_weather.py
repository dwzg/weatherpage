"""Tests for the pure meteorological computations."""

from datetime import datetime
from typing import ClassVar

import pytest

from app import weather


class TestDewPoint:
    def test_saturated_air_matches_temperature(self):
        assert weather.compute_dew_point(20.0, 100.0) == pytest.approx(20.0, abs=0.1)

    def test_drier_air_gives_lower_dew_point(self):
        assert weather.compute_dew_point(20.0, 40.0) < weather.compute_dew_point(20.0, 80.0)

    def test_known_value(self):
        # 25 °C at 60 % RH is ~16.7 °C by the Magnus formula.
        assert weather.compute_dew_point(25.0, 60.0) == pytest.approx(16.7, abs=0.1)

    def test_zero_humidity_does_not_raise(self):
        assert weather.compute_dew_point(20.0, 0.0) < -50


class TestHeatIndex:
    def test_none_below_threshold(self):
        assert weather.compute_heat_index(26.9, 80.0) is None

    def test_defined_at_threshold(self):
        assert weather.compute_heat_index(27.0, 80.0) is not None

    def test_humid_air_feels_hotter(self):
        assert weather.compute_heat_index(32.0, 80.0) > weather.compute_heat_index(32.0, 40.0)

    def test_matches_noaa_table(self):
        # NOAA: 90 °F / 70 % RH ≈ 105 °F  →  32.2 °C / 70 % ≈ 40.6 °C
        assert weather.compute_heat_index(32.2, 70.0) == pytest.approx(40.6, abs=0.6)

    def test_dry_air_adjustment_lowers_result(self):
        """Below 13 % RH the regression overestimates; the correction pulls it down."""
        assert weather.compute_heat_index(35.0, 10.0) < 35.0


class TestFrost:
    @pytest.mark.parametrize("temp,expected", [(-5.0, True), (1.9, True), (2.0, False), (10.0, False)])
    def test_threshold(self, temp, expected):
        assert weather.is_frost_risk(temp) is expected


class TestForecast:
    """The rules read a de-tided pressure change (see database.get_pressure_cycle).

    The thresholds are wide because they were picked from this station's own
    distribution: the old 1.0/2.0 hPa bands fired on 67% and 39% of readings
    and were measuring the daily pressure cycle rather than the weather.
    """

    SUMMER_AFTERNOON = datetime(2026, 7, 15, 14, 0)
    WINTER_NIGHT = datetime(2026, 1, 15, 2, 0)

    def trend(self, delta=0.0, current=1013.0, consistency=1.0):
        return {
            "direction": "rising" if delta > 0 else "falling" if delta < 0 else "steady",
            "delta": delta,
            "current": current,
            "acceleration": None,
            "consistency": consistency,
        }

    def test_no_trend_is_not_enough_data(self):
        assert weather.compute_forecast(None, 50.0) == "Not enough data"

    # ── The bug that made the old forecast useless ────────────────────────

    def test_dew_on_a_clear_night_is_not_rain(self):
        """A closed dew-point spread is dew forming, not rain arriving.

        The old rules returned "Rain imminent" whenever the spread fell below
        3 °C, which on a calm clear night happens every single night.
        """
        clear_night = weather.compute_forecast(
            self.trend(-0.4), 95.0, 10.0, 8.5, moment=self.WINTER_NIGHT
        )
        assert "Rain" not in clear_night

    def test_small_pressure_moves_say_nothing(self):
        """Anything inside the steady band must not claim weather is coming."""
        for delta in (-1.4, -0.5, 0.0, 0.5, 1.4):
            phrase = weather.compute_forecast(
                self.trend(delta), 50.0, 15.0, 4.0, moment=self.WINTER_NIGHT
            )
            assert phrase == "Little change", f"{delta:+} hPa produced {phrase!r}"

    def test_a_slight_fall_in_dry_air_is_not_worth_mentioning(self):
        assert weather.compute_forecast(
            self.trend(-2.0), 50.0, 15.0, 4.0, moment=self.WINTER_NIGHT
        ) == "Little change"

    # ── Falling ───────────────────────────────────────────────────────────

    def test_slight_fall_into_humid_air_is_rain_possible(self):
        assert weather.compute_forecast(
            self.trend(-2.0), 80.0, 15.0, 11.0, moment=self.WINTER_NIGHT
        ) == "Rain possible"

    def test_moderate_fall_into_humid_air_is_rain_likely(self):
        assert weather.compute_forecast(
            self.trend(-3.0), 80.0, 15.0, 11.0, moment=self.WINTER_NIGHT
        ) == "Rain likely"

    def test_moderate_fall_in_dry_air_turns_unsettled(self):
        assert weather.compute_forecast(
            self.trend(-3.0), 50.0, 15.0, 4.0, moment=self.WINTER_NIGHT
        ) == "Turning unsettled"

    def test_rapid_fall_is_a_storm(self):
        assert weather.compute_forecast(
            self.trend(-5.0), 80.0, 15.0, 5.0, moment=self.WINTER_NIGHT
        ) == "Stormy weather likely"

    def test_squally_rapid_fall_is_hedged(self):
        assert weather.compute_forecast(
            self.trend(-5.0, consistency=0.55), 60.0, 15.0, 5.0, moment=self.WINTER_NIGHT
        ) == "Unsettled, possibly stormy"

    def test_thunderstorm_needs_a_warm_summer_afternoon(self):
        warm = {"humidity": 60.0, "temperature": 28.0, "dew_point": 15.0,
                "temp_trend": {"delta": 2.0}}
        assert weather.compute_forecast(
            self.trend(-3.0), **warm, moment=self.SUMMER_AFTERNOON
        ) == "Thunderstorm possible"
        # Same air, wrong time of year: falls through to the humidity rules.
        assert weather.compute_forecast(
            self.trend(-3.0), **warm, moment=self.WINTER_NIGHT
        ) == "Turning unsettled"

    # ── Rising ────────────────────────────────────────────────────────────

    def test_moderate_rise_clears_up(self):
        assert weather.compute_forecast(
            self.trend(3.0), 40.0, 15.0, 2.0, moment=self.WINTER_NIGHT
        ) == "Clearing up nicely"

    def test_rapid_rise_clears_rapidly(self):
        assert weather.compute_forecast(
            self.trend(5.0), 40.0, 15.0, 2.0, moment=self.WINTER_NIGHT
        ) == "Clearing rapidly"

    def test_slight_rise_defers_to_the_steady_rules(self):
        assert weather.compute_forecast(
            self.trend(2.0, current=1030.0), 30.0, 15.0, -5.0, moment=self.WINTER_NIGHT
        ) == "Fair and settled"

    # ── Steady ────────────────────────────────────────────────────────────

    def test_high_steady_pressure_is_settled(self):
        assert weather.compute_forecast(
            self.trend(current=1030.0), 30.0, 15.0, -5.0, moment=self.WINTER_NIGHT
        ) == "Fair and settled"

    def test_high_pressure_with_saturated_air_is_overcast(self):
        assert weather.compute_forecast(
            self.trend(current=1030.0), 95.0, 10.0, 8.0, moment=self.WINTER_NIGHT
        ) == "High pressure, overcast"

    def test_low_steady_pressure_is_unsettled(self):
        assert weather.compute_forecast(
            self.trend(current=1000.0), 95.0, 10.0, 8.0, moment=self.WINTER_NIGHT
        ) == "Low pressure, unsettled"

    def test_fog_needs_rising_humidity(self):
        saturated = {"humidity": 95.0, "temperature": 10.0, "dew_point": 8.0}
        assert weather.compute_forecast(
            self.trend(), **saturated, humidity_trend={"delta": 2.0}, moment=self.WINTER_NIGHT
        ) == "Fog or drizzle possible"
        assert weather.compute_forecast(
            self.trend(), **saturated, humidity_trend={"delta": 0.0}, moment=self.WINTER_NIGHT
        ) == "Overcast and humid"

    def test_every_branch_returns_a_phrase(self):
        """No input combination may fall through to None."""
        for delta in (-6.0, -3.0, -2.0, 0.0, 2.0, 3.0, 6.0):
            for humidity in (10.0, 55.0, 99.0):
                for pressure in (990.0, 1013.0, 1040.0):
                    result = weather.compute_forecast(
                        self.trend(delta, pressure), humidity,
                        moment=self.SUMMER_AFTERNOON,
                    )
                    assert isinstance(result, str) and result


class TestForecastEmoji:
    """Every phrase the engine can produce, and the emoji it must get."""

    EXPECTED: ClassVar[dict[str, str]] = {
        "Rain likely": "🌧️",
        "Rain possible": "🌧️",
        "Stormy weather likely": "⛈️",
        "Unsettled, possibly stormy": "⛈️",
        "Thunderstorm possible": "⛈️",
        "Fog or drizzle possible": "🌫️",
        "Turning unsettled": "☁️",
        "High pressure, overcast": "☁️",
        "Low pressure, unsettled": "☁️",
        "Overcast and humid": "☁️",
        "Humid but clearing": "☀️",
        "Clearing up nicely": "☀️",
        "Clearing rapidly": "☀️",
        "Fair and settled": "☀️",
        "Little change": "🌤️",
    }

    @pytest.mark.parametrize("forecast,emoji", sorted(EXPECTED.items()))
    def test_mapping(self, forecast, emoji):
        assert weather.forecast_emoji(forecast) == emoji

    def test_worsening_never_gets_a_sun(self):
        """"settled" is a substring of "unsettled", which the original got wrong."""
        for forecast, emoji in self.EXPECTED.items():
            if "unsettled" in forecast.lower():
                assert emoji != "☀️"
                assert weather.forecast_emoji(forecast) != "☀️"

    def test_matching_is_case_insensitive(self):
        assert weather.forecast_emoji("LOW PRESSURE, UNSETTLED") == "☁️"

    def test_missing_forecast_falls_back(self):
        assert weather.forecast_emoji(None) == weather.DEFAULT_FORECAST_EMOJI
        assert weather.forecast_emoji("") == weather.DEFAULT_FORECAST_EMOJI

    def test_covers_every_phrase_the_engine_emits(self):
        """Guards against a new forecast phrase with no emoji rule."""
        emitted = set()
        for delta in (-6.0, -3.0, -2.0, 0.0, 2.0, 3.0, 6.0):
            for humidity in (10.0, 45.0, 60.0, 75.0, 90.0):
                for pressure in (1000.0, 1013.0, 1030.0):
                    for consistency in (0.3, 1.0):
                        for dew in (None, 8.0):
                            emitted.add(weather.compute_forecast(
                                TestForecast().trend(delta, pressure, consistency),
                                humidity, 10.0, dew,
                                humidity_trend={"delta": 2.0},
                                temp_trend={"delta": 2.0},
                                moment=TestForecast.SUMMER_AFTERNOON,
                            ))
        unknown = {p for p in emitted if p not in self.EXPECTED}
        assert not unknown, f"forecast phrases with no expected emoji: {unknown}"
