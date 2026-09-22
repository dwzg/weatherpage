#!/usr/bin/env python3
"""Fit the nowcasts and, if they earn their place, write them into app/.

Run by .github/workflows/retrain.yml every week. It is the only part of this
project that reaches outside the box, and it does so in CI, never in the app:

  * the station's own readings, from its public history endpoint;
  * observed hourly weather for the station's location, from Open-Meteo's
    ERA5 archive, which supplies the labels, and its 2 km ICON series, which
    is scored as an independent check but never trained on.

Two models come out of one replay, because they are the same question asked
of the same feature vector against different labels:

  * rain -> app/model.json, labelled by observed precipitation;
  * sky  -> app/sky_model.json, labelled by observed cloud cover.

They are gated separately, so one can ship while the other is refused. The
sky model replaces the humidity-only guess behind "Fair and settled" and
"Overcast and humid" on the banner; the outlook's fog and thunderstorm rungs
stay hand-made, because the archive carries no label for either (zero fog
codes and zero thunderstorm codes over two years, and an empty CAPE field).

Features come from ``app.services.nowcast_features`` — the same function the
running app calls — replayed against a throwaway database with the clock
pinned to each historical hour. Training cannot see a feature the browser
would not, and cannot compute one differently.

A new model replaces the shipped one only if it beats it out of sample, and
only if it beats the baselines that need no model at all. Refusing to ship is
a normal outcome, not a failure: the script exits 0 and leaves the file alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

DEFAULT_APP_URL = "https://weather.wtzg.de"

# Labels come from the 25 km reanalysis, not the 2 km model, and that was
# measured rather than assumed. Trained and judged on the 2 km series the
# same features score AUC 0.715 / CSI 0.220, against 0.831 / 0.473 on the
# reanalysis: point rain is 8% of hours and turns on convective detail a
# barometer cannot see, while "did it rain around here" is the synoptic
# question this station's sensors actually answer. The 2 km series is still
# fetched and scored, as a check that the two have not drifted apart.
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HIGH_RESOLUTION_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

#: Where the station stands, used only to ask Open-Meteo for the rainfall that
#: labels the data. It is supplied by the environment rather than written down
#: here: the coordinates are the one thing in this repository that says where
#: somebody lives, and this file is public. In CI they come from the
#: STATION_LATITUDE and STATION_LONGITUDE secrets.
#:
#: There is deliberately no fallback. A default would be either wrong — which
#: would label the data with some other place's weather and quietly poison the
#: model — or the real location, which is the thing being kept out of the file.
LOCATION_VARS = ("STATION_LATITUDE", "STATION_LONGITUDE")

#: The rainfall hours must line up with the station's local timestamps, which
#: app.clock reads from the same variable.
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Berlin")

HORIZON_HOURS = 6
RAIN_MM = 0.2
#: Mean cloud cover over the horizon at or above which the sky model's label
#: is "overcast". Mirrors app.nowcast.OVERCAST_PERCENT, which the page prints;
#: main() asserts the two agree rather than trusting that they do.
OVERCAST_PERCENT = 80
#: Both models are fitted on the same vector. Adding a feature means adding it
#: to app.services.nowcast_features() and to this tuple, never to one target's
#: fit alone — a feature that means two things is the bug this guards against.
FEATURES = ("pct30", "pct7", "rh", "rh_max6", "drh3", "drh6", "spread", "dp6", "dp12", "temp")
REGULARISATION = 1.0

#: Validation settings. Weekly refits mirror how this job itself runs.
MIN_TRAIN_DAYS = 21
FOLD_DAYS = 7
MIN_TRAIN_SAMPLES = 200
#: Below this many labelled hours the sample is too thin to conclude anything.
MIN_SAMPLES = 500
#: Gates a candidate must clear before it replaces the shipped model.
#: BSS is skill over quoting the long-run average; MAX_REGRESSION lets a
#: model wobble with the season without thrashing the deployed one.
MIN_SKILL = 0.05
MAX_REGRESSION = 0.05

#: History a sample needs behind it before its pressure ranks mean what they
#: say. The app will rank against a week if that is all it has; a model
#: trained on those would be learning from a different feature.
MIN_HISTORY_DAYS = 31

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


# ── Inputs ─────────────────────────────────────────────────────────────────


def fetch_json(
    url: str, timeout: int = 120, attempts: int = 5, headers: dict | None = None
) -> dict | list:
    """GET some JSON, retrying the failures a free weather API actually hands out.

    Open-Meteo rate-limits with 429 and occasionally 5xx. A weekly job that
    gave up on the first one would quietly stop retraining, so back off and
    try again rather than failing the run.
    """
    delay = 10
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == attempts:
                raise
            print(f"  {error.code} from the API, retrying in {delay}s "
                  f"({attempt}/{attempts - 1})")
        except urllib.error.URLError:
            if attempt == attempts:
                raise
            print(f"  network error, retrying in {delay}s ({attempt}/{attempts - 1})")
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def fetch_readings(app_url: str, api_key: str | None = None) -> list[dict]:
    """Every raw reading the station has, walked page by page.

    Deliberately NOT /api/weather/history. That endpoint downsamples above
    TARGET_CHART_POINTS, so on any archive longer than a few days it returns
    bucket averages — and it did, for months: a 92-day archive of ~26,500
    readings came back as 734 three-hourly points. The features replayed
    below are 30-minute medians and 6 and 12 hour deltas over a 5-minute
    grid, and none of them can be completed from 3-hourly means, so every
    run produced zero samples, printed "too few samples" and exited 0. A
    green tick and nothing learned.

    /export never downsamples. It needs the API key, and pages with a
    (timestamp, utc_offset) cursor because a timestamp alone is not unique
    across the repeated hour of the autumn DST fallback.
    """
    base = f"{app_url.rstrip('/')}/api/weather/export"
    headers = {"X-API-Key": api_key} if api_key else {}
    rows: list[dict] = []
    cursor: dict | None = None

    while True:
        query = urllib.parse.urlencode(cursor) if cursor else ""
        payload = fetch_json(f"{base}?{query}" if query else base, headers=headers)
        page = payload["readings"]
        rows.extend(page)
        cursor = payload.get("next")
        if not cursor:
            break
        print(f"  {len(rows)} readings so far")

    for row in rows:
        row["dt"] = datetime.strptime(row["timestamp"], TS_FORMAT)
    # The server already returns them oldest first; sorting is belt and
    # braces for a replay whose whole correctness rests on the order.
    rows.sort(key=lambda r: (r["dt"], -row_offset(r)))
    return rows


def fetch_predictions(app_url: str, api_key: str | None = None) -> list[dict]:
    """Every prediction the page has logged, walked page by page.

    Same cursor and the same key as the readings export, for the same
    reasons. Returns an empty list rather than failing if the endpoint is
    not there: an older container has no log to hand over, and a run that
    cannot verify should still be able to train.
    """
    base = f"{app_url.rstrip('/')}/api/weather/predictions"
    headers = {"X-API-Key": api_key} if api_key else {}
    rows: list[dict] = []
    cursor: dict | None = None

    while True:
        query = urllib.parse.urlencode(cursor) if cursor else ""
        try:
            payload = fetch_json(f"{base}?{query}" if query else base, headers=headers)
        except Exception as error:
            print(f"  (no prediction log available: {error})")
            return []
        rows.extend(payload["predictions"])
        cursor = payload.get("next")
        if not cursor:
            break

    for row in rows:
        row["dt"] = datetime.strptime(row["timestamp"], TS_FORMAT)
    rows.sort(key=lambda r: (r["dt"], -row_offset(r)))
    return rows


def row_offset(row: dict) -> int:
    """A reading's UTC offset, defaulting for a row that predates the column."""
    value = row.get("utc_offset")
    return int(value) if value is not None else 0


def station_location(latitude: float | None, longitude: float | None) -> tuple[float, float]:
    """The coordinates to label against, from the command line or the environment.

    Raises rather than guessing: see :data:`LOCATION_VARS`.
    """
    values = []
    for given, name in zip((latitude, longitude), LOCATION_VARS, strict=True):
        if given is not None:
            values.append(given)
            continue
        raw = os.environ.get(name)
        if not raw:
            raise SystemExit(
                f"{name} is not set. The station's coordinates are not stored in this "
                f"repository; set {' and '.join(LOCATION_VARS)} (they are GitHub secrets "
                "in CI), or pass --latitude and --longitude."
            )
        try:
            values.append(float(raw))
        except ValueError as error:
            raise SystemExit(f"{name} is not a number: {raw!r}") from error
    return values[0], values[1]


def fetch_observations(
    start: datetime, end: datetime, latitude: float, longitude: float,
    variables: tuple[str, ...] = ("precipitation",), high_resolution: bool = False,
) -> dict[str, dict[datetime, float]]:
    """Observed hourly weather at the station, one series per variable.

    Both targets are labelled from the same call: precipitation for the rain
    model, cloud cover for the sky model. The reanalysis supplies the labels;
    the high-resolution series is only ever scored against, never trained on
    (see the note by ARCHIVE_URL).
    """
    base = HIGH_RESOLUTION_URL if high_resolution else ARCHIVE_URL
    url = (
        f"{base}?latitude={latitude}&longitude={longitude}"
        f"&start_date={start:%Y-%m-%d}&end_date={end:%Y-%m-%d}"
        f"&hourly={','.join(variables)}"
        f"&timezone={urllib.parse.quote(TIMEZONE, safe='')}"
        + ("&models=icon_seamless" if high_resolution else "")
    )
    hourly = fetch_json(url)["hourly"]
    moments = [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in hourly["time"]]
    return {
        name: {m: v for m, v in zip(moments, hourly[name], strict=True) if v is not None}
        for name in variables
        if name in hourly
    }


def window(series: dict[datetime, float], moment: datetime) -> list[float] | None:
    """The horizon's hourly values after ``moment``, or ``None`` if incomplete.

    A partial window is not a weaker label, it is a different question, so
    every target refuses one rather than averaging over the hours it has.
    """
    hours = [
        moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=k)
        for k in range(1, HORIZON_HOURS + 1)
    ]
    observed = [series[h] for h in hours if h in series]
    return observed if len(observed) == len(hours) else None


def label_rain(rain: dict[datetime, float], moment: datetime) -> float | None:
    """Did at least :data:`RAIN_MM` fall in the next :data:`HORIZON_HOURS`?"""
    observed = window(rain, moment)
    return None if observed is None else float(sum(observed) >= RAIN_MM)


def label_sky(cloud: dict[datetime, float], moment: datetime) -> float | None:
    """Was the next :data:`HORIZON_HOURS` overcast, on average?

    The mean rather than any hour of it: the outlook is a claim about the
    period as a whole, and one cloudy hour inside a bright afternoon is not
    what "Cloudy" on the banner is meant to say.
    """
    observed = window(cloud, moment)
    return None if observed is None else float(
        sum(observed) / len(observed) >= OVERCAST_PERCENT
    )


# ── Replaying the app's own feature code over history ──────────────────────


async def replay(
    readings: list[dict],
    rain: dict[datetime, float],
    cloud: dict[datetime, float] | None = None,
) -> list[dict]:
    """Walk the history hour by hour, asking the app what it would have seen.

    Readings are inserted as the clock reaches them rather than all at once.
    The app's "latest reading" queries are ``ORDER BY timestamp DESC LIMIT n``
    with no upper bound — correct in production, where nothing is newer than
    now, but against a fully populated table they would hand every historical
    hour the values from the end of the series. Feeding the database forward
    keeps each step honest without the app needing to know it is being
    replayed.
    """
    from app import cache, clock, database, services

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        from app.config import get_settings

        get_settings.cache_clear()

        await database.connect()
        real_now = clock.now
        try:
            samples: list[dict] = []
            start = readings[0]["dt"].replace(minute=0, second=0, microsecond=0)
            usable_from = start + timedelta(days=MIN_HISTORY_DAYS)
            moment, last, cursor, day = start, readings[-1]["dt"], 0, None

            while moment <= last:
                # Everything the station had measured by this hour, and nothing after.
                batch = []
                while cursor < len(readings) and readings[cursor]["dt"] <= moment:
                    row = readings[cursor]
                    # /export carries the offset each reading was stored with,
                    # so the repeated autumn hour replays as the two distinct
                    # hours it was. Only a local --readings file lacks it, and
                    # then the wall clock is all there is to go on — the same
                    # first-pass reading the migration takes.
                    offset = row.get("utc_offset")
                    batch.append((row["temperature"], row["humidity"], row["pressure"],
                                  row["timestamp"],
                                  int(offset) if offset is not None
                                  else clock.resolve_offset(row["timestamp"])))
                    cursor += 1
                if batch:
                    async with database.acquire() as db:
                        await db.executemany(
                            "INSERT OR REPLACE INTO weather_readings "
                            "(temperature, humidity, pressure, timestamp, utc_offset) "
                            "VALUES (?, ?, ?, ?, ?)",
                            batch,
                        )
                        await db.commit()
                    cache.invalidate()

                # One replay, both targets. The feature vector is the expensive
                # part and it is identical for the two, so fitting them from
                # separate passes would cost twice as much to get the same
                # numbers — and risk them being built against different hours.
                labels = {
                    "rain": label_rain(rain, moment),
                    "sky": label_sky(cloud, moment) if cloud else None,
                }
                if any(v is not None for v in labels.values()) and moment >= usable_from:
                    clock.now = lambda m=moment: m
                    if moment.date() != day:
                        cache.invalidate()  # the ranking windows have moved on
                        day = moment.date()
                    features = await services.nowcast_features()
                    if features is not None:
                        samples.append({"dt": moment, "y": labels, "features": features})
                moment += timedelta(hours=1)
            return samples
        finally:
            clock.now = real_now
            await database.disconnect()


# ── Fitting and scoring ────────────────────────────────────────────────────


def fit(X: np.ndarray, y: np.ndarray):
    model = make_pipeline(
        StandardScaler(), LogisticRegression(C=REGULARISATION, max_iter=3000)
    )
    model.fit(X, y)
    return model


def best_threshold(probabilities: np.ndarray, truth: np.ndarray) -> float:
    """The cut that maximises CSI — chosen on training data only."""
    grid = np.arange(0.10, 0.80, 0.025)
    return float(grid[int(np.argmax([csi(probabilities >= g, truth) for g in grid]))])


def csi(prediction: np.ndarray, truth: np.ndarray) -> float:
    hits = int((prediction & (truth == 1)).sum())
    misses = int((~prediction & (truth == 1)).sum())
    false_alarms = int((prediction & (truth == 0)).sum())
    total = hits + misses + false_alarms
    return hits / total if total else 0.0


def score(probabilities: np.ndarray, truth: np.ndarray, threshold: float) -> dict:
    probabilities = np.asarray(probabilities, dtype=float)
    brier = brier_score_loss(truth, probabilities)
    climatology = brier_score_loss(truth, np.full_like(probabilities, truth.mean()))
    prediction = probabilities >= threshold
    hits = int((prediction & (truth == 1)).sum())
    misses = int((~prediction & (truth == 1)).sum())
    false_alarms = int((prediction & (truth == 0)).sum())
    correct = int((~prediction & (truth == 0)).sum())
    pod = hits / (hits + misses) if hits + misses else 0.0
    pofd = false_alarms / (false_alarms + correct) if false_alarms + correct else 0.0
    return {
        "brier": round(brier, 4),
        "bss": round(1 - brier / climatology, 3) if climatology else 0.0,
        "auc": round(roc_auc_score(truth, probabilities), 3)
        if len(set(probabilities.tolist())) > 1
        else None,
        "csi": round(csi(prediction, truth), 3),
        "kss": round(pod - pofd, 3),
        "pod": round(pod, 3),
        "far": round(false_alarms / (hits + false_alarms), 3) if hits + false_alarms else 0.0,
        "fires": round(float(prediction.mean()), 3),
    }


def labelled(samples: list[dict], target: str) -> list[dict]:
    """The samples carrying a label for one target, in time order."""
    return [s for s in samples if s["y"].get(target) is not None]


def walk_forward(
    samples: list[dict], target: str
) -> tuple[np.ndarray, np.ndarray, float, list[dict]]:
    """Out-of-sample probabilities: refit weekly, never look ahead.

    Returns the samples that were actually tested alongside the scores, so a
    baseline can be lined up against exactly the same hours instead of
    assuming they are the tail of the input.
    """
    X = np.array([[s["features"][f] for f in FEATURES] for s in samples])
    y = np.array([s["y"][target] for s in samples])
    moments = np.array([s["dt"] for s in samples])

    probabilities, truth, thresholds, tested = [], [], [], []
    cursor = moments[0] + timedelta(days=MIN_TRAIN_DAYS)
    while cursor < moments[-1]:
        train = moments < cursor
        test = (moments >= cursor) & (moments < cursor + timedelta(days=FOLD_DAYS))
        if train.sum() >= MIN_TRAIN_SAMPLES and test.sum() and len(set(y[train])) > 1:
            model = fit(X[train], y[train])
            in_sample = model.predict_proba(X[train])[:, 1]
            threshold = best_threshold(in_sample, y[train])
            probabilities.append(model.predict_proba(X[test])[:, 1])
            truth.append(y[test])
            thresholds.append(threshold)
            tested.extend(s for s, keep in zip(samples, test, strict=True) if keep)
        cursor += timedelta(days=FOLD_DAYS)

    if not probabilities:
        return np.array([]), np.array([]), 0.5, []
    return (
        np.concatenate(probabilities),
        np.concatenate(truth),
        float(np.median(thresholds)),
        tested,
    )


def incumbent(samples: list[dict], target: str) -> np.ndarray:
    """What the threshold ladder already claims, as a 0/1 call per hour.

    This is the thing each model is asking to replace, so it is the baseline
    that decides whether shipping is an improvement or just a change. For
    rain it is the ladder's own "says rain" rungs. For cloud it is the
    humidity test behind "Overcast and humid" — the claim the sky model
    exists to put evidence under.
    """
    from app import weather

    if target == "sky":
        return np.array([
            float(s["features"]["rh"] > weather.HUMIDITY_MUGGY) for s in samples
        ])
    return np.array([
        float(
            any(
                word in weather.compute_forecast(
                    s["features"]["pct30"],
                    s["features"]["rh"],
                    s["features"]["temp"],
                    s["features"]["temp"] - s["features"]["spread"],
                    {"delta": s["features"]["drh3"]},
                    moment=s["dt"],
                )
                for word in ("Rain", "Thunder")
            )
        )
        for s in samples
    ])


def baselines(tested: list[dict], truth: np.ndarray, target: str) -> dict:
    """What you get without a model: climatology, and the incumbent rules."""
    return {
        "climatology": score(np.full_like(truth, truth.mean(), dtype=float), truth, 0.5),
        "rules": score(incumbent(tested, target), truth, 0.5),
    }


# ── Entry point ────────────────────────────────────────────────────────────


def gates(candidate: dict, reference: dict, shipped: dict | None, target: str) -> list[str]:
    """Why this candidate may not ship, or an empty list if it may.

    What a model is *for* decides what it has to beat, and both of these
    exist to give a calibrated probability, which the ladder they sit beside
    cannot: it emits a phrase. So the gates are probabilistic — better than
    quoting the long-run average, and ranking hours at least as well as the
    rule it replaces. The yes/no hit rate is reported but deliberately not a
    gate: trading calibration for a better CSI would lose the thing the model
    adds.
    """
    previous = (shipped or {}).get("metadata", {}).get("skill", {})
    reasons = []
    if candidate["bss"] < MIN_SKILL:
        reasons.append(
            f"not enough skill over climatology (BSS {candidate['bss']:+.3f}, "
            f"need {MIN_SKILL:+.2f})"
        )
    if candidate["auc"] is None or (
        reference["rules"]["auc"] is not None and candidate["auc"] < reference["rules"]["auc"]
    ):
        reasons.append(
            f"ranks hours no better than the rules (AUC {candidate['auc']} "
            f"vs {reference['rules']['auc']})"
        )
    if previous and candidate["bss"] < previous.get("bss", -1) - MAX_REGRESSION:
        reasons.append(
            f"a clear step down from the shipped {target} model "
            f"(BSS {candidate['bss']:+.3f} vs {previous.get('bss'):+.3f})"
        )
    return reasons


def evaluate(samples: list[dict], target: str) -> dict | None:
    """Walk one target forward and score it, or ``None`` if it cannot be.

    Both targets go through this identically — they differ only in their
    labels, which is the whole claim being made by fitting them on one shared
    feature vector.
    """
    print(f"\n── {target} " + "─" * (66 - len(target)))
    usable = labelled(samples, target)
    if len(usable) < MIN_SAMPLES:
        print(f"{len(usable)} labelled hours, need {MIN_SAMPLES}; "
              "leaving the shipped model alone")
        return None

    probabilities, truth, threshold, tested = walk_forward(usable, target)
    if not len(probabilities):
        print("not enough history for a walk-forward fold; leaving the shipped model alone")
        return None

    candidate = score(probabilities, truth, threshold)
    reference = baselines(tested, truth, target)
    print(f"walk-forward over {len(truth)} out-of-sample hours "
          f"(base rate {truth.mean()*100:.1f}%), operating threshold {threshold:.3f}")
    for name, s in ((target, candidate), *reference.items()):
        print(f"  {name:<12} Brier {s['brier']:.4f}  BSS {s['bss']:+.3f}  "
              f"AUC {s['auc']}  CSI {s['csi']:.3f}  KSS {s['kss']:+.3f}")
    return {
        "target": target,
        "samples": usable,
        "tested": tested,
        "probabilities": probabilities,
        "truth": truth,
        "threshold": threshold,
        "skill": candidate,
        "baselines": reference,
    }


def ship(evaluation: dict, out: Path, extra: dict, force: bool) -> bool:
    """Gate one evaluated candidate and write it out if it passes."""
    target, threshold = evaluation["target"], evaluation["threshold"]
    shipped = json.loads(out.read_text()) if out.exists() else None
    reasons = gates(evaluation["skill"], evaluation["baselines"], shipped, target)
    if reasons and not force:
        print(f"\nNOT shipping this {target} model:")
        for reason in reasons:
            print(f"  - {reason}")
        print("The shipped model is left exactly as it is.")
        return False

    samples = evaluation["samples"]
    X = np.array([[s["features"][f] for f in FEATURES] for s in samples])
    y = np.array([s["y"][target] for s in samples])
    final = fit(X, y)
    scaler = final.named_steps["standardscaler"]
    logistic = final.named_steps["logisticregression"]

    out.write_text(
        json.dumps(
            {
                "features": list(FEATURES),
                "mean": [round(float(v), 6) for v in scaler.mean_],
                "scale": [round(float(v), 6) for v in scaler.scale_],
                "coef": [round(float(v), 6) for v in logistic.coef_[0]],
                "intercept": round(float(logistic.intercept_[0]), 6),
                "threshold": round(threshold, 3),
                "metadata": {
                    "trained_at": datetime.now().strftime("%Y-%m-%d"),
                    "samples": len(samples),
                    "trained_through": samples[-1]["dt"].strftime("%Y-%m-%d"),
                    "base_rate": round(float(y.mean()), 3),
                    "horizon_hours": HORIZON_HOURS,
                    "skill": evaluation["skill"],
                    "baselines": evaluation["baselines"],
                    **extra,
                },
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nwrote {out}")
    return True


# ── Verifying what was actually shown ──────────────────────────────────────
#
# Everything above scores a candidate walk-forward against a held-out past.
# That is the right way to decide whether to ship, and it says nothing about
# whether the model *already deployed* has been right about this station's
# weather since it was deployed.
#
# This scores the prediction log — what the page actually said, hour by hour,
# written at the time — against the same observations that label the training
# data. It cannot be reconstructed by replay: the log spans however many
# weekly models were deployed across the window, and a replay would attribute
# every hour of it to today's.

#: The bins and their shape live in app/nowcast.py: it is the page that
#: renders them, and keeping the definition there makes it pure arithmetic
#: the ordinary test suite can check without numpy.
from app.nowcast import reliability_bins  # noqa: E402


def verify(predictions: list[dict], rain: dict[datetime, float]) -> dict | None:
    """Score the logged predictions against what the weather actually did."""
    scored = [
        (row, label_rain(rain, row["dt"]))
        for row in predictions
        if row["dt"] in rain or window(rain, row["dt"]) is not None
    ]
    scored = [(row, label) for row, label in scored if label is not None]
    if not scored:
        print("\nNothing in the prediction log can be scored yet.")
        return None

    rows = [row for row, _ in scored]
    truth = np.array([label for _, label in scored], dtype=float)
    result: dict = {
        "scored_at": datetime.now().strftime("%Y-%m-%d"),
        "from": rows[0]["dt"].strftime("%Y-%m-%d"),
        "to": rows[-1]["dt"].strftime("%Y-%m-%d"),
        "hours": len(rows),
        "base_rate": round(float(truth.mean()), 3),
        # However many weekly models the window spans. A replay could not
        # produce this, and it is the reason the log exists.
        "models": sorted({row["model_trained_at"] for row in rows
                          if row.get("model_trained_at")}),
        "horizon_hours": HORIZON_HOURS,
        "rain_mm": RAIN_MM,
    }

    have_model = np.array([row.get("rain_probability") is not None for row in rows])
    if have_model.any() and len(set(truth[have_model].tolist())) > 1:
        probabilities = np.array(
            [row["rain_probability"] for row, keep in zip(rows, have_model, strict=True) if keep],
            dtype=float,
        )
        model_truth = truth[have_model]
        # The threshold the shipped model fires at, so the yes/no figures
        # describe the call the page was actually making.
        shipped = REPO / "app" / "model.json"
        threshold = (
            json.loads(shipped.read_text()).get("threshold", 0.5)
            if shipped.exists() else 0.5
        )
        result["model"] = score(probabilities, model_truth, threshold)
        result["model"]["hours"] = int(have_model.sum())
        result["model"]["base_rate"] = round(float(model_truth.mean()), 3)
        result["reliability"] = reliability_bins(
            list(zip(probabilities.tolist(), model_truth.tolist(), strict=True))
        )

    # The ladder is scored over every logged hour, including the ones before
    # the model had enough history to say anything, because the phrase was on
    # the banner for all of them.
    says_rain = np.array([
        float(any(word in (row.get("forecast") or "") for word in ("Rain", "Thunder")))
        for row in rows
    ])
    result["rules"] = score(says_rain, truth, 0.5)
    result["rules"]["hours"] = len(rows)

    print(f"\nlive verification over {result['hours']} logged hours "
          f"({result['from']} .. {result['to']}, base rate "
          f"{result['base_rate'] * 100:.1f}%)")
    if "model" in result:
        m = result["model"]
        print(f"  model  Brier {m['brier']}  BSS {m['bss']:+.3f}  AUC {m['auc']}  "
              f"CSI {m['csi']:.3f}  over {m['hours']} hours")
        for row in result.get("reliability", []):
            print(f"    said {row['from']:.0%}-{row['to']:.0%}: "
                  f"observed {row['observed']:.0%} over {row['hours']} hours")
    r = result["rules"]
    print(f"  rules  CSI {r['csi']:.3f}  KSS {r['kss']:+.3f}  over {r['hours']} hours")
    return result


def cross_check(evaluation: dict, fine: dict[datetime, float]) -> dict | None:
    """Score the rain model's own probabilities against the 2 km series.

    An independent read on the same hours. Its base rate is far lower, so its
    Brier skill is not comparable with the reanalysis figures; what it is good
    for is AUC — whether the model still ranks wet hours above dry ones when a
    different source decides which were wet.
    """
    truth = np.array([label_rain(fine, s["dt"]) for s in evaluation["tested"]], dtype=object)
    have = np.array([v is not None for v in truth])
    if have.sum() <= MIN_TRAIN_SAMPLES or len(set(truth[have].tolist())) < 2:
        return None
    scored = score(
        evaluation["probabilities"][have], truth[have].astype(float), evaluation["threshold"]
    )
    scored["wet_rate"] = round(float(truth[have].astype(float).mean()), 3)
    print(f"  {'(2 km check)':<12} AUC {scored['auc']}  CSI {scored['csi']:.3f}  "
          f"on point rain, which fell in {scored['wet_rate']*100:.1f}% of these hours")
    return scored


# ── Entry point ────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default=os.environ.get("APP_URL", DEFAULT_APP_URL))
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"),
                        help="key for /api/weather/export; defaults to $API_KEY")
    parser.add_argument("--out", type=Path, default=REPO / "app" / "model.json")
    parser.add_argument("--sky-out", type=Path, default=REPO / "app" / "sky_model.json")
    parser.add_argument("--verification-out", type=Path,
                        default=REPO / "app" / "verification.json",
                        help="where the live verification of the deployed model goes")
    parser.add_argument("--readings", type=Path, help="a local readings JSON, instead of fetching")
    parser.add_argument("--rainfall", type=Path,
                        help="a local Open-Meteo archive JSON, instead of fetching")
    parser.add_argument("--latitude", type=float,
                        help="station latitude; defaults to $STATION_LATITUDE")
    parser.add_argument("--longitude", type=float,
                        help="station longitude; defaults to $STATION_LONGITUDE")
    parser.add_argument("--target", choices=("rain", "sky", "both"), default="both",
                        help="which model to fit; the other is left untouched")
    parser.add_argument("--force", action="store_true",
                        help="write the model even if it does not beat the shipped one")
    args = parser.parse_args()

    # The page prints what "overcast" means, reading it from app.nowcast; the
    # labels here decide what it actually means. A mismatch would be invisible
    # on the page and wrong in the model, so it fails the run instead.
    from app import nowcast as app_nowcast

    if app_nowcast.OVERCAST_PERCENT != OVERCAST_PERCENT:
        raise SystemExit(
            f"OVERCAST_PERCENT disagrees: ml/train.py says {OVERCAST_PERCENT}, "
            f"app/nowcast.py says {app_nowcast.OVERCAST_PERCENT}"
        )

    readings = (
        json.loads(args.readings.read_text())
        if args.readings
        else fetch_readings(args.app_url, args.api_key)
    )
    if args.readings:
        for row in readings:
            row["dt"] = datetime.strptime(row["timestamp"], TS_FORMAT)
        readings.sort(key=lambda r: r["dt"])
    print(f"{len(readings)} readings, {readings[0]['dt']} .. {readings[-1]['dt']}")

    latitude = longitude = None
    if args.rainfall:
        hourly = json.loads(args.rainfall.read_text())["hourly"]
        moments = [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in hourly["time"]]
        observed = {
            name: {m: v for m, v in zip(moments, hourly[name], strict=True) if v is not None}
            for name in ("precipitation", "cloud_cover")
            if name in hourly
        }
    else:
        latitude, longitude = station_location(args.latitude, args.longitude)
        observed = fetch_observations(
            readings[0]["dt"], readings[-1]["dt"], latitude, longitude,
            ("precipitation", "cloud_cover"),
        )
    rain, cloud = observed.get("precipitation", {}), observed.get("cloud_cover", {})
    print(f"{len(rain)} hours of rainfall and {len(cloud)} of cloud cover for labels")

    samples = asyncio.run(replay(readings, rain, cloud))
    print(f"{len(samples)} labelled hours with a complete feature vector")

    wrote = False

    if args.target in ("rain", "both"):
        evaluation = evaluate(samples, "rain")
        if evaluation is not None:
            fine = None
            if not args.rainfall:
                try:
                    fine = fetch_observations(
                        readings[0]["dt"], readings[-1]["dt"], latitude, longitude,
                        ("precipitation",), high_resolution=True,
                    )["precipitation"]
                except Exception as error:
                    # A check, never a gate: if the second source is down the
                    # run carries on and simply records nothing for it.
                    print(f"  (high-resolution cross-check unavailable: {error})")
            wrote |= ship(
                evaluation, args.out,
                {
                    "rain_mm": RAIN_MM,
                    "cross_check_2km": cross_check(evaluation, fine) if fine else None,
                    "labels": "ERA5 reanalysis, ~25 km: rain in the area, not on the balcony",
                },
                args.force,
            )

    if args.target in ("sky", "both"):
        evaluation = evaluate(samples, "sky")
        if evaluation is not None:
            wrote |= ship(
                evaluation, args.sky_out,
                {
                    "overcast_percent": OVERCAST_PERCENT,
                    "labels": "ERA5 reanalysis, ~25 km: mean cloud cover over the horizon",
                },
                args.force,
            )

    # Deliberately outside the shipping decision, and written every run.
    # This does not describe the candidate; it describes the model that has
    # been deployed over the scoring window, which is a different question
    # and the one the page could not answer before. Refusing to ship is a
    # normal outcome, and the verification must not go stale behind it.
    if not args.readings:
        verification = verify(fetch_predictions(args.app_url, args.api_key), rain)
        if verification is not None:
            args.verification_out.write_text(
                json.dumps(verification, indent=2) + "\n"
            )
            print(f"wrote {args.verification_out}")

    if not wrote:
        print("\nNothing shipped; every model on disk is left exactly as it is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
