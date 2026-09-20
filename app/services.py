"""Assembly of the dashboard payload.

The page render and the polling endpoint used to build the same values
independently; both now go through :func:`build_status` so they cannot drift.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from . import clock, database, nowcast, weather
from .config import STALE_AFTER_MINUTES

#: How far back the sparklines and the trend arrows look.
SPARK_PERIOD = "3h"

#: The trained nowcast, loaded once at import. ``None`` when the image ships
#: without a usable model, in which case the dashboard shows the rule-based
#: forecast alone.
NOWCAST_MODEL = nowcast.load()

#: Windows the nowcast's features are measured over. They must match the
#: ones ml/train.py replays, which it guarantees by calling this module.
NOWCAST_PRESSURE_HOURS = (6, 12)
NOWCAST_HUMIDITY_HOURS = (3, 6)
NOWCAST_PEAK_HOURS = 6
NOWCAST_SHORT_PERCENTILE_DAYS = 7
TREND_HOURS = 3
COMPARISON_HOURS = 24


async def build_status() -> dict | None:
    """Everything the dashboard shows about *right now*.

    Returns ``None`` when there are no readings at all. The independent
    queries are issued concurrently — the pool has room for them, and it
    turns a chain of round trips into one.
    """
    current = await database.get_current()
    if not current:
        return None

    pressure_trend, humidity_trend, temp_trend, yesterday, features = await asyncio.gather(
        database.get_pressure_trend(),
        database.get_recent_trend("humidity", TREND_HOURS),
        database.get_recent_trend("temperature", TREND_HOURS),
        database.get_reading_ago(COMPARISON_HOURS),
        nowcast_features(),
    )

    # Where this pressure sits in the station's recent range is what the
    # forecast reads; the trend above is shown on the pressure card but is
    # not evidence about what happens next (see app.weather).
    percentile = (
        await database.get_pressure_percentile(pressure_trend["current"])
        if pressure_trend
        else None
    )

    temperature = current["temperature"]
    humidity = current["humidity"]
    dew_point = weather.compute_dew_point(temperature, humidity)

    # The forecast reads the smoothed half-hour values, not the latest
    # sample: its thresholds are close enough together that one noisy
    # reading would otherwise flip the phrase on the banner. The displayed
    # dew point stays on the live reading; the forecast gets its own from
    # the smoothed pair so the spread is consistent with them.
    smooth_t = temp_trend["current"] if temp_trend else temperature
    smooth_h = humidity_trend["current"] if humidity_trend else humidity
    forecast = weather.compute_forecast(
        percentile,
        smooth_h,
        smooth_t,
        weather.compute_dew_point(smooth_t, smooth_h),
        humidity_trend,
    )

    return {
        "current": current,
        "dew_point": dew_point,
        "heat_index": weather.compute_heat_index(temperature, humidity),
        "pressure_trend": pressure_trend,
        "pressure_percentile": percentile,
        "forecast": forecast,
        "forecast_emoji": weather.forecast_emoji(forecast),
        "nowcast": run_nowcast(features),
        "frost_warning": weather.is_frost_risk(temperature),
        "yesterday": yesterday,
        "stale": is_stale(current["timestamp"]),
        "comparison_hours": COMPARISON_HOURS,
    }


def is_stale(timestamp: str, reference: datetime | None = None) -> bool:
    """Whether the newest reading is old enough to mean the feed has stopped.

    Readings arrive every few minutes, so silence is worth surfacing rather
    than showing a stale number as if it were live.
    """
    ref = (reference if reference is not None else clock.now()).replace(tzinfo=None)
    try:
        latest = datetime.strptime(timestamp, clock.TS_FORMAT)
    except ValueError:
        return True
    return ref - latest > timedelta(minutes=STALE_AFTER_MINUTES)


async def build_page_context() -> dict:
    """The full server-rendered page context, including history aggregates."""
    status = await build_status()
    if status is None:
        return {"current": None, "now": clock.now()}

    stats_all, stats_today, extremes_today, extremes_all = await asyncio.gather(
        database.get_stats("all"),
        database.get_stats("today"),
        database.get_extremes_with_times("today"),
        database.get_extremes_with_times("all"),
    )

    daily_extremes = climate = None
    if stats_all.get("count"):
        daily_extremes, climate = await asyncio.gather(
            database.get_daily_extremes(),
            database.get_climate_stats(),
        )

    return {
        **status,
        "stats_all": stats_all,
        "stats_today": stats_today,
        "extremes_today": extremes_today,
        "extremes_all": extremes_all,
        "daily_extremes": daily_extremes,
        "climate": climate,
        "now": clock.now(),
    }



async def nowcast_features() -> dict[str, float] | None:
    """The nowcast's feature vector, or ``None`` if it cannot be built yet.

    This is the only place the vector is assembled. ``ml/train.py`` replays
    history through this same function with the clock pinned, so a feature
    cannot come to mean one thing in training and another in the browser.

    Every input is a measurement the station makes itself. It returns
    ``None`` whenever any of them is missing — which is the honest answer on
    a young database, since the pressure ranks need weeks of history behind
    them before they mean anything.
    """
    pressure_trend, pressure_trend_long, humidity_trend, humidity_trend_long, peak, temp_trend = (
        await asyncio.gather(
            database.get_pressure_trend(NOWCAST_PRESSURE_HOURS[0]),
            database.get_pressure_trend(NOWCAST_PRESSURE_HOURS[1]),
            database.get_recent_trend("humidity", NOWCAST_HUMIDITY_HOURS[0]),
            database.get_recent_trend("humidity", NOWCAST_HUMIDITY_HOURS[1]),
            database.get_extreme("humidity", NOWCAST_PEAK_HOURS),
            database.get_recent_trend("temperature", TREND_HOURS),
        )
    )
    if not (pressure_trend and humidity_trend and temp_trend):
        return None

    long_percentile, short_percentile = await asyncio.gather(
        database.get_pressure_percentile(pressure_trend["current"]),
        database.get_pressure_percentile(
            pressure_trend["current"], NOWCAST_SHORT_PERCENTILE_DAYS
        ),
    )

    smooth_t, smooth_h = temp_trend["current"], humidity_trend["current"]
    values = {
        "pct30": long_percentile,
        "pct7": short_percentile,
        "rh": smooth_h,
        "rh_max6": peak,
        "drh3": humidity_trend["delta"],
        "drh6": humidity_trend_long["delta"] if humidity_trend_long else None,
        "spread": smooth_t - weather.compute_dew_point(smooth_t, smooth_h),
        "dp6": pressure_trend["delta"],
        "dp12": pressure_trend_long["delta"] if pressure_trend_long else None,
        "temp": smooth_t,
    }
    return None if any(v is None for v in values.values()) else values


def run_nowcast(
    features: dict[str, float] | None, model: nowcast.Model | None = None
) -> dict | None:
    """Turn a feature vector into the payload the dashboard renders."""
    model = model if model is not None else NOWCAST_MODEL
    if model is None or features is None:
        return None
    if any(name not in features for name in model.features):
        return None

    probability = model.predict(features)
    return {
        "probability": round(probability, 3),
        "threshold": model.threshold,
        "label": nowcast.describe(probability, model.threshold),
        "horizon_hours": nowcast.HORIZON_HOURS,
        "rain_mm": nowcast.RAIN_MM,
        "trained_at": model.metadata.get("trained_at"),
        "skill": model.metadata.get("skill"),
    }
