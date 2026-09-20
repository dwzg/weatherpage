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

#: Dew-point spread under which the air is close enough to saturation that
#: fog, drizzle or rain becomes the dominant signal.
SATURATION_SPREAD_C = 3.0

#: Pressure change (hPa over the trend window) separating the severity bands.
PRESSURE_DELTA_STRONG = 2.0
PRESSURE_DELTA_MODERATE = 1.0

#: Below this fraction the readings disagree so much that the direction is
#: sensor jitter rather than weather, and is treated as steady.
CONSISTENCY_MIN = 0.5
#: A strong move that is this inconsistent is squally rather than a clean front.
CONSISTENCY_SQUALLY = 0.6

#: Absolute pressure bands for a steady trend.
PRESSURE_HIGH = 1025.0
PRESSURE_LOW = 1005.0

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

    direction: str
    delta: float  # absolute pressure change over the trend window
    pressure: float
    humidity: float
    acceleration: str | None
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
        consistency = pressure_trend.get("consistency", 1.0)
        direction = pressure_trend["direction"]

        # A direction the readings do not agree on is jitter, not weather.
        if consistency < CONSISTENCY_MIN:
            direction = "steady"

        spread = (
            temperature - dew_point
            if temperature is not None and dew_point is not None
            else None
        )

        return cls(
            direction=direction,
            delta=abs(pressure_trend["delta"]),
            pressure=pressure_trend["current"],
            humidity=humidity,
            acceleration=pressure_trend.get("acceleration"),
            consistency=consistency,
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
    # Air already near saturation is the strongest rain signal there is.
    if f.near_saturation:
        return "Rain imminent" if f.delta > PRESSURE_DELTA_MODERATE else "Rain likely"

    if f.delta > PRESSURE_DELTA_STRONG:
        if f.consistency < CONSISTENCY_SQUALLY:
            return "Unsettled, possibly stormy"
        return "Storm likely" if f.humidity > 50 else "Gale approaching"

    if f.delta > PRESSURE_DELTA_MODERATE:
        if f.convective and f.temp_rising:
            return "Thunderstorm possible"
        if f.humidity > 70:
            return "Rain likely"
        return "Rain possible" if f.humidity > 40 else "Wind picking up"

    if f.acceleration == "starting":
        return "Beginning to worsen"
    if f.humidity > 70:
        return "Becoming unsettled"
    return "Slightly worsening" if f.humidity > 40 else "Turning overcast"


def _rising(f: ForecastInputs) -> str:
    """Pressure climbing — conditions improving at some rate."""
    if f.delta > PRESSURE_DELTA_STRONG:
        return "High pressure, settled"

    if f.delta > PRESSURE_DELTA_MODERATE:
        if f.humidity > 70 or f.near_saturation:
            return "Humid but clearing"
        return "Clearing up nicely"

    if f.acceleration == "starting":
        return "Beginning to improve"
    return "Slowly improving" if f.humidity > 70 else "Fair"


def _steady(f: ForecastInputs) -> str:
    """No meaningful pressure change — absolute pressure decides."""
    if f.pressure > PRESSURE_HIGH:
        if f.humidity < 50:
            return "Fair and settled"
        return "High pressure, overcast" if f.near_saturation else "High pressure, settled"

    if f.pressure < PRESSURE_LOW:
        return "Low pressure, rain risk" if f.near_saturation else "Low pressure, unsettled"

    if f.near_saturation and f.humidity_rising:
        return "Fog or drizzle possible"
    if f.humidity > 80:
        return "Overcast and humid"
    if f.humidity < 40:
        return "Fair and settled"
    if f.acceleration == "ending":
        return "Trend easing, little change"
    return "Little change"


_BRANCHES = {"falling": _falling, "rising": _rising, "steady": _steady}


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

    The pressure trend picks the branch; humidity, dew-point spread, trend
    consistency and (for thunderstorms) the time of year refine it. ``moment``
    defaults to the current local time and exists so tests can pin the clock.
    """
    if not pressure_trend:
        return NO_DATA

    if moment is None:
        from .clock import now

        moment = now()

    inputs = ForecastInputs.build(
        pressure_trend, humidity, temperature, dew_point, humidity_trend, temp_trend, moment
    )
    return _BRANCHES[inputs.direction](inputs)


#: Forecast phrase fragments mapped to the emoji shown beside them.
#:
#: Order matters and matching is case-insensitive, which the original rules
#: got wrong in both directions: "settled" is a substring of "unsettled", so
#: deteriorating forecasts were given a sun, and fragments were matched
#: case-sensitively, so "Low pressure, rain risk" missed the rain rule. The
#: worsening group is therefore tested before the improving one, and every
#: fragment here is lowercase.
_FORECAST_EMOJI: tuple[tuple[tuple[str, ...], str], ...] = (
    (("storm", "gale", "thunder"), "⛈️"),
    (("rain",), "🌧️"),
    (("wind",), "💨"),
    (("fog", "drizzle"), "🌫️"),
    (("unsettled", "worsen", "overcast"), "☁️"),
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
