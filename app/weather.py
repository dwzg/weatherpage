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

# Pressure bands, in hPa of change over the trend window, measured on the
# de-tided series (see database.get_pressure_cycle). They were chosen from
# this station's own distribution so each band means something: over 89 days
# a rapid move fires about 2% of the time and a moderate one about 14%,
# where the previous 1.0/2.0 thresholds fired on 67% and 39% of readings —
# they were measuring the daily cycle, not the weather.

#: A move this large is a front going through.
PRESSURE_DELTA_RAPID = 4.0
#: Enough movement to expect the weather to change.
PRESSURE_DELTA_MODERATE = 2.5
#: Below this the barometer is not saying anything; absolute pressure and
#: humidity decide instead.
PRESSURE_DELTA_STEADY = 1.5

#: Below this fraction the readings disagree so much that a strong move is
#: squally rather than a clean front.
CONSISTENCY_SQUALLY = 0.6

#: Absolute pressure bands, used when the trend is flat.
PRESSURE_HIGH = 1025.0
PRESSURE_LOW = 1005.0

#: Humidity above which a falling barometer means rain rather than just wind.
HUMIDITY_WET = 70.0
#: Humidity at which the air itself is the story.
HUMIDITY_MUGGY = 85.0
#: Humidity below which nothing is going to fall out of the sky soon.
HUMIDITY_DRY = 40.0

#: Conditions under which a falling trend is convective (thundery) rather
#: than frontal: a warm, humid summer afternoon.
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


@dataclass(frozen=True)
class ForecastInputs:
    """Everything the rules below consult, derived once from raw readings."""

    delta: float  # signed pressure change over the trend window
    pressure: float
    humidity: float
    consistency: float
    near_saturation: bool
    humidity_rising: bool
    temp_rising: bool
    convective: bool

    @classmethod
    def build(
        cls,
        pressure_trend: dict,
        humidity: float,
        temperature: float | None,
        dew_point: float | None,
        humidity_trend: dict | None,
        temp_trend: dict | None,
        moment: datetime,
    ) -> ForecastInputs:
        spread = (
            temperature - dew_point
            if temperature is not None and dew_point is not None
            else None
        )

        return cls(
            delta=pressure_trend["delta"],
            pressure=pressure_trend["current"],
            humidity=humidity,
            consistency=pressure_trend.get("consistency", 1.0),
            near_saturation=spread is not None and spread < SATURATION_SPREAD_C,
            humidity_rising=_is_rising(humidity_trend),
            temp_rising=_is_rising(temp_trend),
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


def _falling(f: ForecastInputs) -> str:
    """Pressure dropping — how fast, and into how much moisture."""
    drop = -f.delta

    if drop > PRESSURE_DELTA_RAPID:
        if f.consistency < CONSISTENCY_SQUALLY:
            return "Unsettled, possibly stormy"
        return "Stormy weather likely"

    if drop > PRESSURE_DELTA_MODERATE:
        if f.convective and f.temp_rising:
            return "Thunderstorm possible"
        if f.humidity > HUMIDITY_WET:
            return "Rain likely"
        return "Turning unsettled"

    # A slight fall is only worth mentioning if there is moisture for it to
    # act on. Otherwise the barometer is within its own noise, and saying
    # "slowly worsening" every time it drifts down is how the old rules
    # ended up changing their mind 42 times a day.
    if f.humidity > HUMIDITY_WET:
        return "Rain possible"
    return _steady(f)


def _rising(f: ForecastInputs) -> str:
    """Pressure climbing — conditions improving at some rate."""
    if f.delta > PRESSURE_DELTA_RAPID:
        return "Clearing rapidly"
    if f.delta > PRESSURE_DELTA_MODERATE:
        return "Humid but clearing" if f.humidity > HUMIDITY_WET else "Clearing up nicely"

    # A slight rise says no more than a flat barometer does.
    return _steady(f)


def _steady(f: ForecastInputs) -> str:
    """No meaningful pressure change — the air itself decides."""
    if f.pressure > PRESSURE_HIGH:
        if f.near_saturation:
            return "High pressure, overcast"
        return "Fair and settled"

    if f.pressure < PRESSURE_LOW:
        return "Low pressure, unsettled"

    if f.near_saturation and f.humidity_rising:
        return "Fog or drizzle possible"
    if f.humidity > HUMIDITY_MUGGY:
        return "Overcast and humid"
    if f.humidity < HUMIDITY_DRY:
        return "Fair and settled"
    return "Little change"


def _branch(delta: float):
    """Which rule set applies — a flat barometer is its own case."""
    if delta < -PRESSURE_DELTA_STEADY:
        return _falling
    if delta > PRESSURE_DELTA_STEADY:
        return _rising
    return _steady


def compute_forecast(
    pressure_trend: dict | None,
    humidity: float,
    temperature: float | None = None,
    dew_point: float | None = None,
    humidity_trend: dict | None = None,
    temp_trend: dict | None = None,
    moment: datetime | None = None,
) -> str:
    """A short forecast phrase from the pressure trend and current conditions.

    The size of the pressure change picks the branch; humidity, dew-point
    spread, trend consistency and (for thunderstorms) the time of year refine
    it. ``moment`` defaults to the current local time and exists so tests can
    pin the clock.

    This is a barometric outlook from one sensor, not a meteorological
    forecast: it has no wind direction and nothing upstream of the balcony,
    so it distinguishes settling from deteriorating and declines to be more
    specific than that. It expects the de-tided trend that
    :func:`database.get_pressure_trend` produces — handed the raw change it
    will mostly report the time of day.
    """
    if not pressure_trend:
        return NO_DATA

    if moment is None:
        from .clock import now

        moment = now()

    inputs = ForecastInputs.build(
        pressure_trend, humidity, temperature, dew_point, humidity_trend, temp_trend, moment
    )
    return _branch(inputs.delta)(inputs)


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
    (("unsettled", "overcast"), "☁️"),
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
