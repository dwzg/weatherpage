"""Assembly of the dashboard payload.

The page render and the polling endpoint used to build the same values
independently; both now go through :func:`build_status` so they cannot drift.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from . import clock, database, weather
from .config import STALE_AFTER_MINUTES

#: How far back the sparklines and the trend arrows look.
SPARK_PERIOD = "3h"
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

    pressure_trend, humidity_trend, temp_trend, yesterday = await asyncio.gather(
        database.get_pressure_trend(),
        database.get_recent_trend("humidity", TREND_HOURS),
        database.get_recent_trend("temperature", TREND_HOURS),
        database.get_reading_ago(COMPARISON_HOURS),
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
        pressure_trend,
        smooth_h,
        smooth_t,
        weather.compute_dew_point(smooth_t, smooth_h),
        humidity_trend,
        temp_trend,
    )

    return {
        "current": current,
        "dew_point": dew_point,
        "heat_index": weather.compute_heat_index(temperature, humidity),
        "pressure_trend": pressure_trend,
        "forecast": forecast,
        "forecast_emoji": weather.forecast_emoji(forecast),
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
