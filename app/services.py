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

#: The sky model, same shape and same features, fitted against observed cloud
#: cover instead of rainfall. Far more likely to be ``None`` than the one
#: above: it ships only once ``ml/train.py`` has one that clears its gates,
#: and on a short or single-season archive it correctly refuses. While it is
#: absent the outlook is exactly what it always was — the threshold ladder.
SKY_MODEL = nowcast.load(nowcast.SKY_MODEL_PATH)

#: How the deployed model has actually done, from the prediction log. Loaded
#: once at import like the models: it changes weekly, in CI, and a new image
#: is what carries it — so it belongs in the page render and not in /status,
#: which the poller refetches every minute for numbers that move.
VERIFICATION = nowcast.load_verification()

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
    smooth_dew = weather.compute_dew_point(smooth_t, smooth_h)
    rain, sky = run_nowcast(features), run_sky(features)

    # Gated on the rain model alone, because only the rain rungs need it.
    #
    # This used to require the sky model too, and the sky model has never
    # shipped — it has to clear its gates and on a short archive it correctly
    # refuses. So every request fell through to the threshold ladder for the
    # banner while the pill beside it came from the rain model, and the two
    # disagreed in public: "Rain possible" next to "Rain nearby not expected
    # · 9%". The page's own skill table says which to believe, and it is not
    # the ladder. compose_forecast() reads the sky rungs from thresholds when
    # no sky model is loaded, which is what the ladder did there anyway.
    if rain is not None:
        forecast = weather.compose_forecast(
            rain["probability"], rain["threshold"],
            sky["probability"] if sky else None,
            smooth_h, smooth_t, smooth_dew, humidity_trend,
            pressure_percentile=percentile,
        )
    else:
        forecast = weather.compute_forecast(
            percentile, smooth_h, smooth_t, smooth_dew, humidity_trend,
        )

    return {
        "current": current,
        "dew_point": dew_point,
        "heat_index": weather.compute_heat_index(temperature, humidity),
        "pressure_trend": pressure_trend,
        # Already computed above to smooth the forecast inputs, and until now
        # thrown away: the pressure card carried an arrow and the other two
        # did not, from the same three queries.
        "temperature_trend": temp_trend,
        "humidity_trend": humidity_trend,
        "pressure_percentile": percentile,
        "forecast": forecast,
        "forecast_emoji": weather.forecast_emoji(forecast),
        "nowcast": rain,
        "sky": sky,
        # Which path produced the phrase above, and how much of it was
        # fitted. The explainer prints a different ladder for each, so it has
        # to be told both: whether the rain rungs are a model, and whether
        # the sky rungs are.
        "outlook_is_learned": rain is not None,
        "sky_is_learned": sky is not None,
        "frost_warning": weather.frost_alert(
            temperature, temp_trend["direction"] if temp_trend else None
        ),
        "yesterday": yesterday,
        "stale": is_stale(current["timestamp"]),
        "age_seconds": age_seconds(current["timestamp"]),
        # The gap actually measured, not the one asked for. Inside a tolerance
        # of two hours the nearest readings can be 22 or 26 hours old, and
        # near sunrise that is several degrees — so the label says what was
        # compared rather than what was intended. Falls back to the nominal
        # figure only when there is nothing to compare against at all, where
        # the caller renders no line anyway.
        # Rounded to the hour, which is the granularity the label is written
        # at; the point is not to claim 24 when it measured 22, not to print
        # a decimal.
        "comparison_hours": (
            round(yesterday["hours"]) if yesterday else COMPARISON_HOURS
        ),
    }


async def record_prediction(timestamp: str, utc_offset: int) -> bool:
    """Log what the page is showing, so it can be scored later. True if logged.

    Called after a reading is stored, and it logs by going back through
    :func:`build_status` rather than recomputing anything: what gets written
    is then, by construction, exactly what ``/status`` serves and the page
    renders. A second code path here would eventually disagree with the
    first, and the whole point of the log is that it is a faithful record.

    Two things it declines to log:

    * anything but the top of the hour, because the observations these will
      be scored against are hourly — a row every five minutes would be
      twelve times the rows and not one extra scoreable hour;
    * a reading that is not the newest, which means a backfill filling an
      outage. ``build_status`` describes *now*, so attaching it to a
      historical timestamp would file today's prediction under last week.
    """
    if not timestamp.endswith(database.PREDICTION_LOG_SUFFIX):
        return False

    status = await build_status()
    if status is None or status["current"]["timestamp"] != timestamp:
        return False

    nowcast, sky = status["nowcast"], status["sky"]
    forecast = status["forecast"]
    # A young database has no model output yet but the ladder still speaks,
    # so the rules start being scored before the model can be.
    if nowcast is None and forecast in (None, weather.NO_DATA):
        return False

    await database.insert_prediction(
        timestamp=timestamp,
        utc_offset=utc_offset,
        rain_probability=nowcast["probability"] if nowcast else None,
        sky_probability=sky["probability"] if sky else None,
        forecast=forecast,
        model_trained_at=nowcast.get("trained_at") if nowcast else None,
    )
    return True


def age_seconds(timestamp: str, reference: datetime | None = None) -> int | None:
    """How old the newest reading is, in seconds, or ``None`` if unparseable.

    Computed here rather than in the browser because the stored timestamp is
    a naive local wall clock: a reader in another timezone would have their
    browser read it as their own local time and make a reading from a minute
    ago look an hour old. The server is the one that knows which clock the
    string belongs to.
    """
    ref = (reference if reference is not None else clock.now()).replace(tzinfo=None)
    try:
        latest = datetime.strptime(timestamp, clock.TS_FORMAT)
    except ValueError:
        return None
    return int((ref - latest).total_seconds())


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
    archive_months = 0
    if stats_all.get("count"):
        daily_extremes, climate, archive_months = await asyncio.gather(
            database.get_daily_extremes(),
            database.get_climate_stats(),
            database.get_archive_months(),
        )

    return {
        **status,
        "stats_all": stats_all,
        "stats_today": stats_today,
        "extremes_today": extremes_today,
        "extremes_all": extremes_all,
        "daily_extremes": daily_extremes,
        "climate": climate,
        # What the calendar should ask for, and what it will actually get:
        # the second is the first until the archive is ten years old, at
        # which point the card says the calendar starts later than the data.
        "archive_months": archive_months,
        "calendar_months": min(archive_months, database.MAX_CALENDAR_MONTHS) or 1,
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
    meta = model.metadata
    return {
        "probability": round(probability, 3),
        "threshold": model.threshold,
        "label": nowcast.describe(probability, model.threshold),
        "horizon_hours": nowcast.HORIZON_HOURS,
        "rain_mm": nowcast.RAIN_MM,
        "trained_at": meta.get("trained_at"),
        "skill": meta.get("skill"),
        # The model card the explainer prints. All of it is metadata the
        # trainer already writes, passed through rather than restated here,
        # so a retrain updates the page without a code change.
        "samples": meta.get("samples"),
        "trained_through": meta.get("trained_through"),
        "base_rate": meta.get("base_rate"),
        "baselines": meta.get("baselines"),
        "cross_check": meta.get("cross_check_2km"),
        "intercept": round(model.intercept, 3),
        "logit": round(model.logit(features), 3),
        "contributions": nowcast_breakdown(model, features),
    }


def run_sky(
    features: dict[str, float] | None, model: nowcast.Model | None = None
) -> dict | None:
    """Turn a feature vector into the sky half of the outlook.

    Deliberately thinner than :func:`run_nowcast`. The rain probability is a
    number the page shows in its own right, with a model card and a live
    breakdown beside it; the sky probability exists to decide a word on the
    banner, so it carries what the explainer needs to justify that word and
    nothing more.
    """
    model = model if model is not None else SKY_MODEL
    if model is None or features is None:
        return None
    if any(name not in features for name in model.features):
        return None

    probability = model.predict(features)
    meta = model.metadata
    return {
        "probability": round(probability, 3),
        "band": weather.sky_band(probability),
        "label": nowcast.describe_sky(probability),
        "overcast_percent": meta.get("overcast_percent", nowcast.OVERCAST_PERCENT),
        "horizon_hours": nowcast.HORIZON_HOURS,
        "trained_at": meta.get("trained_at"),
        "samples": meta.get("samples"),
        "base_rate": meta.get("base_rate"),
        "skill": meta.get("skill"),
        "baselines": meta.get("baselines"),
        "contributions": nowcast_breakdown(model, features),
    }


def nowcast_breakdown(model: nowcast.Model, features: dict[str, float]) -> list[dict]:
    """The per-feature decomposition, ready to print.

    The value is scaled and the decimals are chosen here rather than in the
    browser, for the same reason the numbers elsewhere are: the poller
    rewrites what the render produced, and a value that changes shape after
    sixty seconds reads as a bug.
    """
    rows = []
    for c in model.contributions(features):
        fmt = nowcast.describe_feature(c.name)
        rows.append({
            "name": c.name,
            "label": fmt.label,
            "unit": fmt.unit,
            "digits": fmt.digits,
            "sign": fmt.sign,
            "value": round(c.value * fmt.factor, 6),
            "standardised": round(c.standardised, 2),
            "weight": round(c.weight, 3),
        })
    # Biggest movers first: the point of the table is which signals are
    # driving this number, and ten rows in training order does not say.
    rows.sort(key=lambda r: abs(r["weight"]), reverse=True)
    return rows
