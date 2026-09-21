"""Derived meteorological values.

Everything here is a pure function of its arguments — no database, no clock
except what the caller passes in — so it can be reasoned about and tested in
isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

# ── Thresholds ─────────────────────────────────────────────────────────────
# Named rather than inlined so the forecast rules below read as prose.

#: Dew-point spread under which the air is close enough to saturation for
#: fog or drizzle. It is NOT a rain signal: on a clear, calm night the
#: temperature falls to the dew point and this closes every time, which is
#: dew forming on the balcony rather than rain on the way.
SATURATION_SPREAD_C = 3.0

#: Humidity above which the air is damp enough to rain out of.
HUMIDITY_WET = 70.0
#: Humidity at which the air itself is the story.
HUMIDITY_MUGGY = 85.0

#: A warm, humid summer afternoon, when a shower can build with no warning
#: from the barometer at all.
CONVECTIVE_TEMP_C = 25.0
CONVECTIVE_HUMIDITY = 50.0
CONVECTIVE_HOURS = range(12, 19)
CONVECTIVE_MONTHS = frozenset({4, 5, 6, 7, 8, 9})

#: A trend delta larger than this counts as a real rise or fall.
TREND_SIGNIFICANT = 1.0

#: Below this temperature the plants on the balcony are at risk.
FROST_WARNING_C = 2.0

#: The heat index is only meaningful in warm air; below this it is not shown.
HEAT_INDEX_MIN_C = 27.0

NO_DATA = "Not enough data"


def compute_dew_point(temp_c: float, humidity: float) -> float:
    """Dew point in °C via the Magnus formula."""
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(max(humidity, 1e-6) / 100.0)
    return round((b * gamma) / (a - gamma), 1)


def compute_heat_index(temp_c: float, humidity: float) -> float | None:
    """Apparent ("feels like") temperature in °C, or ``None`` when not meaningful.

    Implements NOAA's Rothfusz regression including the two correction terms
    for very dry and very humid air, which the regression alone gets wrong.
    Returns ``None`` below :data:`HEAT_INDEX_MIN_C`, where the heat index is
    not defined.
    """
    if temp_c < HEAT_INDEX_MIN_C:
        return None

    t = temp_c * 9 / 5 + 32  # Rothfusz is defined in Fahrenheit
    rh = humidity

    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * rh
        - 0.22475541 * t * rh
        - 6.83783e-3 * t * t
        - 5.481717e-2 * rh * rh
        + 1.22874e-3 * t * t * rh
        + 8.5282e-4 * t * rh * rh
        - 1.99e-6 * t * t * rh * rh
    )

    # NOAA adjustments outside the regression's comfortable range.
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)

    return round((hi - 32) * 5 / 9, 1)


def is_frost_risk(temp_c: float) -> bool:
    """Whether the current temperature warrants a frost warning."""
    return temp_c < FROST_WARNING_C


# ── Forecast ───────────────────────────────────────────────────────────────
#
# These rules were fitted against 90 days of this station's readings scored
# on observed hourly rainfall at Rheinfelden. That reference data was used
# offline, for calibration only: the app itself fetches nothing and forecasts
# from its own sensor. What the scoring showed:
#
#   * The barometric tendency every weather-lore rule reaches for first has
#     NEGATIVE skill here. "Pressure fell more than 1 hPa in 6 hours" scored
#     a Hanssen-Kuipers score of -0.09 against rain within six hours, and
#     inside the wettest conditions rising pressure was followed by rain
#     more often than falling pressure (64% against 39%). The rules this
#     replaces were built entirely on tendency and scored -0.074: worse than
#     saying nothing at all.
#   * Where the pressure sits does carry signal, but no fixed threshold
#     transfers — "below 1020 hPa" scored a critical success index of 0.05 in
#     one month of the sample and 0.44 in another. Ranking the reading
#     against the station's own recent range is stable across all 90 days.
#   * Humidity is the second axis. Together the two separate conditions that
#     saw rain within six hours 6% of the time from ones that saw it 69% of
#     the time, against a 21% base rate.
#
# So the barometer is read as a level, not a tendency. The trend is still
# measured and shown next to the pressure card, because it is a fact about
# the last six hours; it just is not evidence about the next six.

#: Pressure percentile and humidity bounding each tier. Observed
#: rain-within-6h over the 90-day sample, against a 21% base rate:
#: rain likely 69%, rain possible 32%, unsettled 28%, changeable 15%,
#: settled 6%.
RAIN_LIKELY_PERCENTILE = 0.25
RAIN_LIKELY_HUMIDITY = 80.0
RAIN_POSSIBLE_PERCENTILE = 0.40
RAIN_POSSIBLE_HUMIDITY = 65.0
SETTLED_PERCENTILE = 0.60

# ── What the calibration measured ──────────────────────────────────────────
# These are the numbers the tiers above were chosen against, kept here as
# constants because the page prints them: a threshold that moves without its
# measured hit rate moving with it is how an explainer starts lying.

#: How often rain followed within six hours over the whole 90-day sample.
CALIBRATION_BASE_RATE = 0.21
#: How many days of this station's readings the tiers were scored on.
CALIBRATION_DAYS = 90
#: The binary "says rain" forecast, scored over that sample.
CALIBRATION_CSI = 0.27
CALIBRATION_KSS = 0.27
#: What the tendency-based rules this replaced scored, same sample. Negative:
#: worse than saying nothing.
SUPERSEDED_KSS = -0.074
#: The barometric tendency on its own, as a rain signal. Also negative.
TENDENCY_KSS = -0.09
#: Inside the wettest conditions, how often rain followed a *rising* against a
#: *falling* barometer. The wrong way round, which is the whole argument for
#: reading pressure as a level.
TENDENCY_WET_RISING = 0.64
TENDENCY_WET_FALLING = 0.39
#: A fixed hPa threshold ("below 1020") scored this in the worst and best
#: month of the sample. The spread is why the reading is ranked instead.
FIXED_THRESHOLD_CSI_RANGE = (0.05, 0.44)


#: The same figures gathered for the page. A view of the constants above, not
#: a second copy of them: the explainer prints what the rules were fitted to.
CALIBRATION: dict[str, float] = {
    "days": CALIBRATION_DAYS,
    "base_rate": CALIBRATION_BASE_RATE,
    "csi": CALIBRATION_CSI,
    "kss": CALIBRATION_KSS,
    "superseded_kss": SUPERSEDED_KSS,
    "tendency_kss": TENDENCY_KSS,
    "wet_rising": TENDENCY_WET_RISING,
    "wet_falling": TENDENCY_WET_FALLING,
    "fixed_csi_low": FIXED_THRESHOLD_CSI_RANGE[0],
    "fixed_csi_high": FIXED_THRESHOLD_CSI_RANGE[1],
}


@dataclass(frozen=True)
class Tier:
    """One rung of the ladder :func:`compute_forecast` walks, for display.

    This is documentation that the page renders, not something the rules
    consult — :func:`compute_forecast` below is still the implementation, and
    reads the same constants. Keeping the two beside each other is the point:
    a tier that is retuned without its row being retuned is visible here.
    """

    phrase: str                 #: the forecast phrase this rung produces
    condition: str              #: message id describing when it fires
    fields: dict                #: placeholder values for that message
    observed: float | None      #: measured rain-within-6h, or None
    note: str = ""              #: shown instead, when nothing cleaner was measured


#: The tiers in the order :func:`compute_forecast` tests them. The observed
#: rates are that function's own calibration: 69% at the top down to 6% at the
#: bottom, against a 21% base rate.
RULE_LADDER: tuple[Tier, ...] = (
    Tier(
        "Rain likely",
        "Pressure in the lowest {pct}% of 30 days, humidity above {rh}%",
        {"pct": RAIN_LIKELY_PERCENTILE * 100, "rh": RAIN_LIKELY_HUMIDITY},
        0.69,
    ),
    Tier(
        "Thunderstorm possible",
        "Above {temp}°C and humidity above {rh}%, {start}:00 to {end}:59, {first} to {last}",
        {
            "temp": CONVECTIVE_TEMP_C,
            "rh": CONVECTIVE_HUMIDITY,
            "start": min(CONVECTIVE_HOURS),
            "end": max(CONVECTIVE_HOURS),
            "month_first": min(CONVECTIVE_MONTHS),
            "month_last": max(CONVECTIVE_MONTHS),
        },
        None,
        note="about twice the base rate, whatever the barometer says",
    ),
    Tier(
        "Rain possible",
        "Pressure in the lowest {pct}% of 30 days, humidity above {rh}%",
        {"pct": RAIN_POSSIBLE_PERCENTILE * 100, "rh": RAIN_POSSIBLE_HUMIDITY},
        0.32,
    ),
    Tier(
        "Unsettled",
        "Pressure in the lowest {pct}% of 30 days, drier than that",
        {"pct": RAIN_POSSIBLE_PERCENTILE * 100},
        0.28,
    ),
    Tier(
        "Little change",
        "Pressure in the middle of its range, {low}% to {high}%",
        {"low": RAIN_POSSIBLE_PERCENTILE * 100, "high": SETTLED_PERCENTILE * 100},
        0.15,
    ),
    Tier(
        "Fair and settled",
        "Pressure above the {pct}th percentile, humidity below {rh}%",
        {"pct": SETTLED_PERCENTILE * 100, "rh": HUMIDITY_WET},
        0.06,
    ),
)


@dataclass(frozen=True)
class ForecastInputs:
    """Everything the rules below consult, derived once from raw readings."""

    percentile: float | None  # where the pressure sits in its recent range
    humidity: float
    near_saturation: bool
    humidity_rising: bool
    convective: bool

    @classmethod
    def build(
        cls,
        percentile: float | None,
        humidity: float,
        temperature: float | None,
        dew_point: float | None,
        humidity_trend: dict | None,
        moment: datetime,
    ) -> ForecastInputs:
        spread = (
            temperature - dew_point
            if temperature is not None and dew_point is not None
            else None
        )

        return cls(
            percentile=percentile,
            humidity=humidity,
            near_saturation=spread is not None and spread < SATURATION_SPREAD_C,
            humidity_rising=_is_rising(humidity_trend),
            convective=(
                temperature is not None
                and temperature > CONVECTIVE_TEMP_C
                and humidity > CONVECTIVE_HUMIDITY
                and moment.hour in CONVECTIVE_HOURS
                and moment.month in CONVECTIVE_MONTHS
            ),
        )


def _is_rising(trend: dict | None) -> bool:
    return trend is not None and trend["delta"] > TREND_SIGNIFICANT


def _air_now(f: ForecastInputs) -> str:
    """What the air is doing, for when the barometer has nothing to add."""
    if f.near_saturation and f.humidity_rising:
        return "Fog or drizzle possible"
    if f.humidity > HUMIDITY_MUGGY:
        return "Overcast and humid"
    return "Little change"


def compute_forecast(
    pressure_percentile: float | None,
    humidity: float,
    temperature: float | None = None,
    dew_point: float | None = None,
    humidity_trend: dict | None = None,
    moment: datetime | None = None,
) -> str:
    """A short outlook from where the pressure sits and how humid the air is.

    ``pressure_percentile`` is the current pressure's rank within the
    station's recent range, from :func:`database.get_pressure_percentile`;
    ``None`` means there is not enough history to rank against yet, and the
    phrase then describes the air rather than predicting anything.

    This is one sensor on a balcony, with no wind and nothing upstream, so
    it distinguishes settling from deteriorating and declines to be more
    specific. ``moment`` defaults to the current local time and exists so
    tests can pin the clock.
    """
    if moment is None:
        from .clock import now

        moment = now()

    f = ForecastInputs.build(
        pressure_percentile, humidity, temperature, dew_point, humidity_trend, moment
    )

    if f.percentile is None:
        return _air_now(f)

    if f.percentile < RAIN_LIKELY_PERCENTILE and f.humidity > RAIN_LIKELY_HUMIDITY:
        return "Rain likely"

    # Warm, humid summer afternoons carry their own risk, whatever the
    # barometer says: twice the base rate of rain over the sample.
    if f.convective:
        return "Thunderstorm possible"

    if f.percentile < RAIN_POSSIBLE_PERCENTILE:
        if f.humidity > RAIN_POSSIBLE_HUMIDITY:
            return "Rain possible"
        if f.near_saturation and f.humidity_rising:
            return "Fog or drizzle possible"
        return "Unsettled"

    if f.percentile > SETTLED_PERCENTILE:
        if f.near_saturation and f.humidity_rising:
            return "Fog or drizzle possible"
        if f.humidity > HUMIDITY_MUGGY:
            return "Overcast and humid"
        if f.humidity < HUMIDITY_WET:
            return "Fair and settled"
        return "Settled but humid"

    return _air_now(f)


#: Forecast phrase fragments mapped to the emoji shown beside them.
#:
#: Order matters and matching is case-insensitive, which the original rules
#: got wrong in both directions: "settled" is a substring of "unsettled", so
#: deteriorating forecasts were given a sun, and fragments were matched
#: case-sensitively, so "Low pressure, rain risk" missed the rain rule. The
#: worsening group is therefore tested before the improving one, and every
#: fragment here is lowercase.
_FORECAST_EMOJI: tuple[tuple[tuple[str, ...], str], ...] = (
    (("storm", "thunder"), "⛈️"),
    (("rain",), "🌧️"),
    (("fog", "drizzle"), "🌫️"),
    (("unsettled", "overcast", "humid"), "☁️"),
    (("clearing", "improv", "fair", "settled"), "☀️"),
)

DEFAULT_FORECAST_EMOJI = "🌤️"


def forecast_emoji(forecast: str | None) -> str:
    """Pick the emoji that goes with a forecast phrase."""
    if not forecast:
        return DEFAULT_FORECAST_EMOJI
    lowered = forecast.lower()
    for fragments, emoji in _FORECAST_EMOJI:
        if any(fragment in lowered for fragment in fragments):
            return emoji
    return DEFAULT_FORECAST_EMOJI
