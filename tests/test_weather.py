"""Tests for the pure meteorological computations."""

import re
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
    """The rules read where the pressure sits, never which way it is moving.

    Scored against observed rainfall over 90 days, the barometric tendency
    had negative skill at this station while the pressure's rank within the
    station's own recent range had real skill. See the note in app.weather.
    """

    SUMMER_AFTERNOON = datetime(2026, 7, 15, 14, 0)
    WINTER_NIGHT = datetime(2026, 1, 15, 2, 0)

    def test_no_history_to_rank_against_describes_the_air(self):
        """Before the station has a range to rank against, it does not predict."""
        assert weather.compute_forecast(None, 50.0, 15.0, 4.0,
                                        moment=self.WINTER_NIGHT) == "Little change"
        assert weather.compute_forecast(None, 95.0, 10.0, 8.0,
                                        moment=self.WINTER_NIGHT) == "Overcast and humid"

    # ── The wet tiers ─────────────────────────────────────────────────────

    def test_low_pressure_and_damp_air_is_rain_likely(self):
        assert weather.compute_forecast(
            0.1, 90.0, 12.0, 10.5, moment=self.WINTER_NIGHT
        ) == "Rain likely"

    def test_low_pressure_and_humid_air_is_rain_possible(self):
        assert weather.compute_forecast(
            0.3, 70.0, 12.0, 6.5, moment=self.WINTER_NIGHT
        ) == "Rain possible"

    def test_low_pressure_in_dry_air_is_only_unsettled(self):
        assert weather.compute_forecast(
            0.3, 50.0, 12.0, 2.0, moment=self.WINTER_NIGHT
        ) == "Unsettled"

    def test_high_pressure_in_dry_air_is_settled(self):
        assert weather.compute_forecast(
            0.9, 50.0, 12.0, 2.0, moment=self.WINTER_NIGHT
        ) == "Fair and settled"

    def test_high_pressure_in_humid_air_is_hedged(self):
        assert weather.compute_forecast(
            0.9, 75.0, 12.0, 7.5, moment=self.WINTER_NIGHT
        ) == "Settled but humid"

    def test_middle_of_the_range_says_little(self):
        assert weather.compute_forecast(
            0.5, 50.0, 12.0, 2.0, moment=self.WINTER_NIGHT
        ) == "Little change"

    #: How strong a claim each phrase makes; descriptions of the air share
    #: a rank because none of them predicts anything.
    WETNESS: ClassVar[dict[str, int]] = {
        "Fair and settled": 0,
        "Settled but humid": 1, "Overcast and humid": 1, "Little change": 1,
        "Fog or drizzle possible": 2, "Unsettled": 2,
        "Rain possible": 3,
        "Rain likely": 4, "Thunderstorm possible": 4,
    }

    @pytest.mark.parametrize("humidity", [30.0, 60.0, 75.0, 90.0])
    def test_falling_down_the_pressure_range_never_reads_drier(self, humidity):
        seen = [weather.compute_forecast(pct, humidity, 12.0, humidity / 10,
                                         moment=self.WINTER_NIGHT)
                for pct in (0.9, 0.7, 0.5, 0.3, 0.1)]
        ranks = [self.WETNESS[p] for p in seen]
        assert ranks == sorted(ranks), seen

    # ── The old bug this replaces ─────────────────────────────────────────

    def test_dew_on_a_clear_night_is_not_rain(self):
        """A closed dew-point spread is dew forming, not rain arriving.

        The rules this replaces returned "Rain imminent" whenever the spread
        fell below 3 °C, which on a calm clear night happens every night.
        """
        phrase = weather.compute_forecast(
            0.8, 95.0, 10.0, 8.5, humidity_trend={"delta": 2.0}, moment=self.WINTER_NIGHT
        )
        assert "Rain" not in phrase

    # ── Refinements ───────────────────────────────────────────────────────

    def test_fog_needs_a_tight_spread_and_rising_humidity(self):
        saturated = {"humidity": 95.0, "temperature": 10.0, "dew_point": 8.0}
        assert weather.compute_forecast(
            0.8, **saturated, humidity_trend={"delta": 2.0}, moment=self.WINTER_NIGHT
        ) == "Fog or drizzle possible"
        assert weather.compute_forecast(
            0.8, **saturated, humidity_trend={"delta": 0.0}, moment=self.WINTER_NIGHT
        ) == "Overcast and humid"

    def test_thunderstorm_needs_a_warm_summer_afternoon(self):
        warm = {"humidity": 60.0, "temperature": 28.0, "dew_point": 19.0}
        assert weather.compute_forecast(
            0.5, **warm, moment=self.SUMMER_AFTERNOON
        ) == "Thunderstorm possible"
        # Same air, wrong time of year: nothing convective about it.
        assert weather.compute_forecast(
            0.5, **warm, moment=self.WINTER_NIGHT
        ) == "Little change"

    def test_the_wettest_tier_outranks_a_thunderstorm(self):
        assert weather.compute_forecast(
            0.1, 90.0, 28.0, 26.0, moment=self.SUMMER_AFTERNOON
        ) == "Rain likely"

    def test_every_branch_returns_a_phrase(self):
        """No input combination may fall through to None."""
        for pct in (None, 0.0, 0.25, 0.4, 0.5, 0.6, 1.0):
            for humidity in (10.0, 55.0, 70.0, 99.0):
                for dew in (None, 8.0):
                    result = weather.compute_forecast(
                        pct, humidity, 10.0, dew,
                        humidity_trend={"delta": 2.0}, moment=self.SUMMER_AFTERNOON,
                    )
                    assert isinstance(result, str) and result


class TestRuleLadder:
    """The ladder the page prints must still describe the code that runs.

    ``RULE_LADDER`` is documentation, so nothing breaks if it drifts — the
    forecast keeps working and the explainer quietly starts lying. These
    drive :func:`compute_forecast` with inputs that satisfy each rung and
    check it answers with that rung's phrase.
    """

    #: One set of inputs per rung, chosen to sit inside it and outside the
    #: rungs above. (percentile, humidity, temperature, dew point, moment)
    CASES: ClassVar[dict] = {
        "Rain likely": (0.1, 90.0, 12.0, 10.5, TestForecast.WINTER_NIGHT),
        "Thunderstorm possible": (0.3, 60.0, 28.0, 20.0,
                                  TestForecast.SUMMER_AFTERNOON),
        "Rain possible": (0.3, 70.0, 12.0, 6.5, TestForecast.WINTER_NIGHT),
        "Unsettled": (0.3, 50.0, 12.0, 2.0, TestForecast.WINTER_NIGHT),
        "Little change": (0.5, 50.0, 12.0, 2.0, TestForecast.WINTER_NIGHT),
        "Fair and settled": (0.9, 50.0, 12.0, 2.0, TestForecast.WINTER_NIGHT),
    }

    def test_every_rung_has_a_worked_case(self):
        assert {tier.phrase for tier in weather.RULE_LADDER} == set(self.CASES)

    @pytest.mark.parametrize("tier", weather.RULE_LADDER, ids=lambda t: t.phrase)
    def test_the_rung_produces_the_phrase_it_advertises(self, tier):
        pct, humidity, temperature, dew, moment = self.CASES[tier.phrase]
        assert weather.compute_forecast(
            pct, humidity, temperature, dew, moment=moment
        ) == tier.phrase

    def test_the_rungs_are_in_the_order_the_code_tests_them(self):
        """Ordered by the pressure rank they admit, wettest rung first."""
        observed = [t.observed for t in weather.RULE_LADDER if t.observed is not None]
        assert observed == sorted(observed, reverse=True)

    def test_every_condition_names_only_fields_it_supplies(self):
        """A placeholder with no value renders the sentence with a hole in it."""
        for tier in weather.RULE_LADDER:
            named = set(re.findall(r"\{(\w+)\}", tier.condition))
            supplied = {k.removeprefix("month_") for k in tier.fields}
            assert named == supplied, tier.phrase

    def test_the_top_rung_beat_the_base_rate(self):
        """The ladder is only worth printing if its tiers separated anything."""
        rates = [t.observed for t in weather.RULE_LADDER if t.observed is not None]
        assert max(rates) > weather.CALIBRATION_BASE_RATE > min(rates)


class TestForecastEmoji:
    """Every phrase the engine can produce, and the emoji it must get."""

    EXPECTED: ClassVar[dict[str, str]] = {
        "Rain likely": "🌧️",
        "Rain possible": "🌧️",
        "Thunderstorm possible": "⛈️",
        "Fog or drizzle possible": "🌫️",
        "Unsettled": "☁️",
        "Overcast and humid": "☁️",
        "Settled but humid": "☁️",
        "Fair and settled": "☀️",
        "Little change": "🌤️",
    }

    @pytest.mark.parametrize("forecast,emoji", sorted(EXPECTED.items()))
    def test_mapping(self, forecast, emoji):
        assert weather.forecast_emoji(forecast) == emoji

    def test_worsening_never_gets_a_sun(self):
        """"settled" is a substring of "unsettled", which the original got wrong."""
        assert weather.forecast_emoji("Unsettled") != "☀️"

    def test_matching_is_case_insensitive(self):
        assert weather.forecast_emoji("RAIN LIKELY") == "🌧️"

    def test_missing_forecast_falls_back(self):
        assert weather.forecast_emoji(None) == weather.DEFAULT_FORECAST_EMOJI
        assert weather.forecast_emoji("") == weather.DEFAULT_FORECAST_EMOJI

    def test_covers_every_phrase_the_engine_emits(self):
        """Guards against a new forecast phrase with no emoji rule."""
        emitted = set()
        for pct in (None, 0.0, 0.1, 0.3, 0.5, 0.7, 1.0):
            for humidity in (10.0, 45.0, 60.0, 75.0, 90.0, 99.0):
                for temperature in (5.0, 28.0):
                    for dew in (None, 4.0, 27.0):
                        for rising in ({"delta": 2.0}, {"delta": 0.0}):
                            for moment in (TestForecast.SUMMER_AFTERNOON,
                                           TestForecast.WINTER_NIGHT):
                                emitted.add(weather.compute_forecast(
                                    pct, humidity, temperature, dew,
                                    humidity_trend=rising, moment=moment,
                                ))
        unknown = {p for p in emitted if p not in self.EXPECTED}
        assert not unknown, f"forecast phrases with no expected emoji: {unknown}"
        assert emitted == set(self.EXPECTED), f"unreachable phrases: {set(self.EXPECTED) - emitted}"
