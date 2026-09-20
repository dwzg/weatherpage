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
    SUMMER_AFTERNOON = datetime(2026, 7, 15, 14, 0)
    WINTER_NIGHT = datetime(2026, 1, 15, 2, 0)

    def trend(self, direction="steady", delta=0.0, current=1013.0, **kw):
        return {"direction": direction, "delta": delta, "current": current,
                "acceleration": kw.get("acceleration"), "consistency": kw.get("consistency", 1.0)}

    def test_no_trend_is_not_enough_data(self):
        assert weather.compute_forecast(None, 50.0) == "Not enough data"

    def test_near_saturation_while_falling_predicts_rain(self):
        result = weather.compute_forecast(
            self.trend("falling", -1.5), 95.0, 10.0, 8.5, moment=self.WINTER_NIGHT
        )
        assert result == "Rain imminent"

    def test_sharp_fall_in_humid_air_is_a_storm(self):
        result = weather.compute_forecast(
            self.trend("falling", -3.0), 80.0, 15.0, 5.0, moment=self.WINTER_NIGHT
        )
        assert result == "Storm likely"

    def test_inconsistent_trend_is_treated_as_steady(self):
        """A jittery sensor must not produce a storm warning."""
        noisy = self.trend("falling", -3.0, consistency=0.3)
        assert weather.compute_forecast(noisy, 60.0, 15.0, 5.0, moment=self.WINTER_NIGHT) == "Little change"

    def test_squally_strong_fall_is_hedged(self):
        squally = self.trend("falling", -3.0, consistency=0.55)
        result = weather.compute_forecast(squally, 60.0, 15.0, 5.0, moment=self.WINTER_NIGHT)
        assert result == "Unsettled, possibly stormy"

    def test_thunderstorm_needs_a_warm_summer_afternoon(self):
        falling = self.trend("falling", -1.5)
        warm = {"humidity": 60.0, "temperature": 28.0, "dew_point": 15.0,
                "temp_trend": {"delta": 2.0}}
        assert weather.compute_forecast(falling, **warm, moment=self.SUMMER_AFTERNOON) == "Thunderstorm possible"
        # Same air, wrong time of year: falls through to the humidity rules.
        assert weather.compute_forecast(falling, **warm, moment=self.WINTER_NIGHT) == "Rain possible"

    def test_rising_pressure_clears_up(self):
        assert weather.compute_forecast(
            self.trend("rising", 1.5), 40.0, 15.0, 2.0, moment=self.WINTER_NIGHT
        ) == "Clearing up nicely"

    def test_high_steady_pressure_is_settled(self):
        assert weather.compute_forecast(
            self.trend(current=1030.0), 30.0, 15.0, -5.0, moment=self.WINTER_NIGHT
        ) == "Fair and settled"

    def test_low_steady_pressure_near_saturation_risks_rain(self):
        assert weather.compute_forecast(
            self.trend(current=1000.0), 95.0, 10.0, 8.0, moment=self.WINTER_NIGHT
        ) == "Low pressure, rain risk"

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
        for direction in ("rising", "falling", "steady"):
            for delta in (0.0, 1.5, 3.0):
                for humidity in (10.0, 55.0, 99.0):
                    for pressure in (990.0, 1013.0, 1040.0):
                        result = weather.compute_forecast(
                            self.trend(direction, delta, pressure), humidity,
                            moment=self.SUMMER_AFTERNOON,
                        )
                        assert isinstance(result, str) and result


class TestForecastEmoji:
    """Every phrase the engine can produce, and the emoji it must get.

    The original rules matched case-sensitively and tested "settled" before
    "unsettled", so nine of these showed the wrong picture — a storm warning
    with a sun beside it, among others.
    """

    EXPECTED: ClassVar[dict[str, str]] = {
        "Rain imminent": "🌧️",
        "Rain likely": "🌧️",
        "Rain possible": "🌧️",
        "Low pressure, rain risk": "🌧️",
        "Unsettled, possibly stormy": "⛈️",
        "Storm likely": "⛈️",
        "Gale approaching": "⛈️",
        "Thunderstorm possible": "⛈️",
        "Wind picking up": "💨",
        "Fog or drizzle possible": "🌫️",
        "Beginning to worsen": "☁️",
        "Becoming unsettled": "☁️",
        "Slightly worsening": "☁️",
        "Turning overcast": "☁️",
        "High pressure, overcast": "☁️",
        "Low pressure, unsettled": "☁️",
        "Overcast and humid": "☁️",
        "High pressure, settled": "☀️",
        "Humid but clearing": "☀️",
        "Clearing up nicely": "☀️",
        "Beginning to improve": "☀️",
        "Slowly improving": "☀️",
        "Fair": "☀️",
        "Fair and settled": "☀️",
        "Trend easing, little change": "🌤️",
        "Little change": "🌤️",
    }

    @pytest.mark.parametrize("forecast,emoji", sorted(EXPECTED.items()))
    def test_mapping(self, forecast, emoji):
        assert weather.forecast_emoji(forecast) == emoji

    def test_worsening_never_gets_a_sun(self):
        for forecast, emoji in self.EXPECTED.items():
            if "unsettled" in forecast.lower() or "worsen" in forecast.lower():
                assert emoji != "☀️"
                assert weather.forecast_emoji(forecast) != "☀️"

    def test_missing_forecast_falls_back(self):
        assert weather.forecast_emoji(None) == weather.DEFAULT_FORECAST_EMOJI
        assert weather.forecast_emoji("") == weather.DEFAULT_FORECAST_EMOJI

    def test_covers_every_phrase_the_engine_emits(self):
        """Guards against a new forecast phrase with no emoji rule."""
        emitted = set()
        for direction in ("rising", "falling", "steady"):
            for delta in (0.0, 1.5, 3.0):
                for humidity in (10.0, 45.0, 60.0, 75.0, 90.0):
                    for pressure in (1000.0, 1013.0, 1030.0):
                        for accel in (None, "starting", "ending"):
                            for dew in (None, 8.0):
                                emitted.add(weather.compute_forecast(
                                    {"direction": direction, "delta": delta,
                                     "current": pressure, "acceleration": accel,
                                     "consistency": 1.0},
                                    humidity, 10.0, dew,
                                    humidity_trend={"delta": 2.0},
                                    temp_trend={"delta": 2.0},
                                    moment=TestForecast.SUMMER_AFTERNOON,
                                ))
        unknown = {p for p in emitted if p not in self.EXPECTED}
        assert not unknown, f"forecast phrases with no expected emoji: {unknown}"
