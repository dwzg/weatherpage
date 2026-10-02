"""Assembly of the dashboard payload.

The page render and the polling endpoint used to build the same values
independently; both now go through :func:`build_status` so they cannot drift.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from . import clock, database, features, nowcast, weather
from .config import STALE_AFTER_MINUTES

#: How far back the sparklines and the trend arrows look.
SPARK_PERIOD = "3h"

#: The trained nowcast, loaded once at import. ``None`` when the image ships
#: without a usable model, in which case the dashboard shows the rule-based
#: forecast alone.
NOWCAST_MODEL = nowcast.load()

#: The other three, same shape and same features, fitted against observed
#: cloud cover, visibility and reported thunder instead of rainfall. Each may
#: be ``None``: it ships only once ``ml/train.py`` has one that clears its
#: gates, and while it is absent its rung of the outlook is exactly what it
#: always was — the hand-made threshold.
SKY_MODEL = nowcast.load(nowcast.SKY_MODEL_PATH)
FOG_MODEL = nowcast.load(nowcast.FOG_MODEL_PATH)
THUNDER_MODEL = nowcast.load(nowcast.THUNDER_MODEL_PATH)

#: How the deployed model has actually done, from the prediction log. Loaded
#: once at import like the models: it changes weekly, in CI, and a new image
#: is what carries it — so it belongs in the page render and not in /status,
#: which the poller refetches every minute for numbers that move.
VERIFICATION = nowcast.load_verification()

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

    pressure_trend, humidity_trend, temp_trend, yesterday, vector = await asyncio.gather(
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
    rain, sky = run_nowcast(vector), run_sky(vector)
    fog = run_event(vector, FOG_MODEL)
    thunder = run_event(vector, THUNDER_MODEL)

    # Gated on the rain model alone, because only the rain rungs need it.
    #
    # This used to require the sky model too, and the sky model has never
    # shipped — it has to clear its gates and on a short archive it correctly
    # refuses. So every request fell through to the threshold ladder for the
    # banner while the pill beside it came from the rain model, and the two
    # disagreed in public: "Rain possible" next to "Rain nearby not expected
    # · 9%". The page's own skill table says which to believe, and it is not
    # the ladder. compose_forecast() reads the sky rungs from thresholds when
    # no sky model is loaded, which is what the ladder did there anyway, and
    # the same holds for fog and thunder.
    if rain is not None:
        forecast = weather.compose_forecast(
            rain["probability"], rain["threshold"],
            sky["probability"] if sky else None,
            smooth_h, smooth_t, smooth_dew, humidity_trend,
            pressure_percentile=percentile,
            thunder=(thunder["probability"], thunder["threshold"]) if thunder else None,
            fog=(fog["probability"], fog["threshold"]) if fog else None,
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
        "fog": fog,
        "thunder": thunder,
        # Which path produced the phrase above, and how much of it was
        # fitted. The explainer prints a different ladder for each, so it has
        # to be told: whether the rain rungs are a model, and whether each of
        # the others is. A model that is loaded but whose rung the rain model
        # never reaches still counts — the ladder is what the page would read.
        "outlook_is_learned": rain is not None,
        "sky_is_learned": sky is not None,
        "fog_is_learned": fog is not None,
        "thunder_is_learned": thunder is not None,
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
        fog_probability=status["fog"]["probability"] if status["fog"] else None,
        thunder_probability=status["thunder"]["probability"] if status["thunder"] else None,
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


async def history_payload(period: str) -> dict:
    """The chart series, with the dew point each point implies.

    Derived here rather than in the browser so the Magnus formula keeps one
    home: a second copy in JavaScript is a formula that can drift from the
    one the cards are computed with.

    On a bucketed series this is the dew point *of* the bucket's mean
    temperature and mean humidity, not the mean of the dew points — the raw
    rows that would need were averaged away. Over a bucket's spread the two
    differ by hundredths of a degree, well inside the line's own width.
    """
    payload = (await database.get_history_series(period)).as_dict()
    for row in payload["readings"]:
        temperature, humidity = row.get("temperature"), row.get("humidity")
        row["dew_point"] = (
            weather.compute_dew_point(temperature, humidity)
            if temperature is not None and humidity is not None
            else None
        )
    return payload


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

    daily_extremes = climate = today_anomaly = None
    archive_months = 0
    if stats_all.get("count"):
        daily_extremes, climate, archive_months, today_anomaly = await asyncio.gather(
            database.get_daily_extremes(),
            database.get_climate_stats(),
            database.get_archive_months(),
            database.get_today_anomaly(),
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
        # Is today unusual? Rendered, never polled, like the records it sits
        # among: it moves over hours, not over a minute.
        "today_anomaly": today_anomaly,
        "archive_months": archive_months,
        "calendar_months": min(archive_months, database.MAX_CALENDAR_MONTHS) or 1,
        "now": clock.now(),
    }



async def nowcast_features() -> dict[str, float | None] | None:
    """The nowcast's feature vector, or ``None`` if it cannot be built yet.

    The arithmetic is :func:`app.features.compute`, and only that: this hands
    it the last :data:`app.features.HISTORY_DAYS` of readings and the learned
    pressure cycle. ``ml/train.py`` hands the same function years of the
    weather service's observations, so a feature cannot come to mean one
    thing in training and another in the browser.

    ``None`` on a young database — the pressure ranks need a week of history
    before they mean anything — and when the newest reading is more than half
    an hour old, because a nowcast of stale readings is a forecast for a
    moment that has already passed.
    """
    series, cycle = await asyncio.gather(
        database.get_recent_series(), database.get_pressure_cycle()
    )
    return features.compute(series, clock.now().replace(tzinfo=None), cycle)


def run_nowcast(
    vector: dict[str, float | None] | None, model: nowcast.Model | None = None
) -> dict | None:
    """Turn a feature vector into the payload the dashboard renders."""
    model = model if model is not None else NOWCAST_MODEL
    if model is None or vector is None:
        return None
    if any(name not in vector for name in model.features):
        return None

    probability = model.predict(vector)
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
        "trained_from": meta.get("trained_from"),
        "trained_through": meta.get("trained_through"),
        "base_rate": meta.get("base_rate"),
        "baselines": meta.get("baselines"),
        "trees": len(model.trees),
        # How many weather-service stations it learned from — a count, never
        # which ones: they are the nearest to the balcony, and naming them
        # would say where it is.
        "stations": meta.get("stations"),
        # Skill season by season, over the held-out year, and on this
        # balcony's own readings: the two tests a candidate must pass.
        "seasons": meta.get("seasons"),
        "holdout": meta.get("holdout"),
        "archive": meta.get("archive"),
        # What each feature is worth out of sample, labelled the way the
        # breakdown labels them so the two tables name the same things.
        "ablations": [
            {**row, "label": nowcast.describe_feature(row.get("feature", "")).label}
            for row in (meta.get("ablations") or [])
        ] or None,
        # The start of the breakdown: the log-odds before any signal has
        # moved them, which every effect in the table is measured from.
        "baseline": round(model.expected, 3),
        "logit": round(model.logit(vector), 3),
        "contributions": nowcast_breakdown(model, vector),
    }


def run_sky(
    vector: dict[str, float | None] | None, model: nowcast.Model | None = None
) -> dict | None:
    """Turn a feature vector into the sky half of the outlook.

    Deliberately thinner than :func:`run_nowcast`. The rain probability is a
    number the page shows in its own right, with a model card and a live
    breakdown beside it; the sky probability exists to decide a word on the
    banner, so it carries what the explainer needs to justify that word and
    nothing more.
    """
    model = model if model is not None else SKY_MODEL
    if model is None or vector is None:
        return None
    if any(name not in vector for name in model.features):
        return None

    probability = model.predict(vector)
    meta = model.metadata
    return {
        "probability": round(probability, 3),
        "band": weather.sky_band(probability),
        "label": nowcast.describe_sky(probability),
        "overcast_percent": meta.get("overcast_percent", nowcast.OVERCAST_PERCENT),
        "horizon_hours": nowcast.HORIZON_HOURS,
        **_model_card(model),
        "contributions": nowcast_breakdown(model, vector),
    }


def run_event(vector: dict[str, float | None] | None, model: nowcast.Model | None) -> dict | None:
    """Fog or thunder: a probability, the threshold it fires at, and its card.

    Thinner still than :func:`run_sky`. Each decides one rung of the outlook
    — fires or does not — so the page needs the probability, the threshold,
    and enough of the model card to say what it was fitted to and how well
    it did. No breakdown: the explainer prints the rain model's, which is
    the number shown in its own right.
    """
    if model is None or vector is None:
        return None
    if any(name not in vector for name in model.features):
        return None
    probability = model.predict(vector)
    return {
        "probability": round(probability, 3),
        "threshold": model.threshold,
        "fires": probability >= model.threshold,
        "horizon_hours": nowcast.HORIZON_HOURS,
        **_model_card(model),
    }


def _model_card(model: nowcast.Model) -> dict:
    """What the explainer prints about any of the models beside rain.

    Metadata the trainer wrote, passed through: a retrain updates the page
    without a code change, and anything it did not write comes back ``None``.
    """
    meta = model.metadata
    return {
        "trained_at": meta.get("trained_at"),
        "samples": meta.get("samples"),
        "trained_from": meta.get("trained_from"),
        "trained_through": meta.get("trained_through"),
        "stations": meta.get("stations"),
        "base_rate": meta.get("base_rate"),
        "skill": meta.get("skill"),
        "baselines": meta.get("baselines"),
        "archive": meta.get("archive"),
        "level": meta.get("level"),
    }


def nowcast_breakdown(model: nowcast.Model, values: dict[str, float | None]) -> list[dict]:
    """The per-feature decomposition, ready to print.

    The value is scaled and the decimals are chosen here rather than in the
    browser, for the same reason the numbers elsewhere are: the poller
    rewrites what the render produced, and a value that changes shape after
    sixty seconds reads as a bug. A feature that could not be measured — its
    lag fell in an outage — is sent as ``None`` and printed as a dash; the
    trees still routed it, so its effect is real and still in the sum.
    """
    rows = []
    for c in model.contributions(values):
        fmt = nowcast.describe_feature(c.name)
        rows.append({
            "name": c.name,
            "label": fmt.label,
            "unit": fmt.unit,
            "digits": fmt.digits,
            "sign": fmt.sign,
            "value": None if c.value is None else round(c.value * fmt.factor, 6),
            "effect": round(c.effect, 3),
        })
    # Biggest movers first: the point of the table is which signals are
    # driving this number, and sixteen rows in training order does not say.
    rows.sort(key=lambda r: abs(r["effect"]), reverse=True)
    return rows
