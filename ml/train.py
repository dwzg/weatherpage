#!/usr/bin/env python3
"""Fit the nowcasts and, if they earn their place, write them into app/.

Run by .github/workflows/retrain.yml every week. It is the only part of this
project that reaches outside the box, and it does so in CI, never in the app.

Where everything comes from, and what each source is for:

  * **Training data** — ten-minute observations from the weather service's
    stations nearest the balcony (``ml/dwd.py``): temperature, humidity and
    pressure, the three things this sensor measures, and the rain gauge beside
    them for the label. Years of them, so every season is in the fit. This
    replaced fitting to the balcony's own archive, which was one summer long:
    a model that has only seen July learns that "cool" means "dry", and in
    October it said 11% with rain on the sensor.
  * **The balcony's own archive** (``/api/weather/export``) — never trained
    on. It is where a candidate is *scored* before it may ship, because a
    weather-service screen is not a sun-baked balcony and the only honest test
    of that gap is this sensor. Labelled by the nearest weather-service gauge.
  * **The prediction log** (``/api/weather/predictions``) — what the page
    actually said, scored against that same gauge (``app/verification.json``).
  * **Cloud cover** for the sky model's labels — Open-Meteo's ERA5 archive at
    each training station, the one thing the stations do not observe.

Two models come out of one set of samples: rain -> ``app/model.json`` and
sky -> ``app/sky_model.json``. They share the feature vector and are gated
separately, so one can ship while the other is refused. Refusing to ship is a
normal outcome, not a failure: the script exits 0 and leaves the file alone.

Features come from :mod:`app.features` — the same function the running app
calls — fed the weather service's readings in the app's own conventions
(local wall-clock time, a de-tided pressure). There is no second copy of the
arithmetic for training to drift from.

The stations used are never printed or written anywhere: they are the ones
nearest the balcony, and their names and distances would put the location the
coordinate secrets protect into a public log. Counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402

from app import features, nowcast, weather  # noqa: E402
from ml import dwd  # noqa: E402

DEFAULT_APP_URL = "https://weather.wtzg.de"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

#: Where the station stands. Supplied by the environment rather than written
#: down here: the coordinates are the one thing in this repository that says
#: where somebody lives, and this file is public. In CI they come from the
#: STATION_LATITUDE and STATION_LONGITUDE secrets. There is deliberately no
#: fallback — a default would either be wrong or be the real location.
LOCATION_VARS = ("STATION_LATITUDE", "STATION_LONGITUDE")

#: The readings are local wall-clock times, which app.clock reads from the
#: same variable; the weather service's are UTC and are converted to match.
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Berlin")

HORIZON_HOURS = 6
RAIN_MM = 0.2
#: Mean cloud cover over the horizon at or above which the sky model's label
#: is "overcast". Mirrors app.nowcast.OVERCAST_PERCENT, which the page prints;
#: main() asserts the two agree rather than trusting that they do.
OVERCAST_PERCENT = 80

#: The model's inputs, all computed by app.features. Both targets are fitted
#: on this one tuple: a feature that meant two things to the two models would
#: make their breakdowns incomparable.
#:
#: Chosen by measurement over 1.1 million station-hours, scored on the last
#: two years. Dropped because they added nothing once these were in: the
#: absolute temperature (a sun-baked balcony reads hot, and on screens it
#: carried no signal the dew point did not), the day of the year (which is
#: how a model learns the calendar instead of the sky), and the humidity
#: changes (the dew-point and temperature changes already carry them).
FEATURES = (
    "rh", "rh_max1", "rh_max3", "sat3",
    "dT1", "dT3", "dT24", "td", "dtd3",
    "pct30", "pct7", "dp1", "dp3", "dp6", "dp12",
    "hour",
)

#: How many of the nearest weather-service stations to learn from, and from
#: when. Measured: a model trained on seven other stations scored the eighth
#: as well as one trained on that station's own fourteen years, so more is
#: not better past a handful — it is only slower.
TRAINING_STATIONS = 5
TRAINING_SINCE = date(2014, 1, 1)
#: A training station must have measured all three of temperature, humidity
#: and pressure for this share of its ten-minute slots. Plenty of the
#: service's stations carry no barometer at all.
MIN_COVERAGE = 0.8
#: Beyond this the nearest stations are not "near" any more, and the
#: coordinates are probably not in Germany, whose service this is.
MAX_STATION_KM = 250.0
#: The gauge that labels the balcony's own archive and the prediction log
#: must be at least this close, or "rain here" is a claim about elsewhere.
MAX_GAUGE_KM = 25.0

#: History a sample needs behind it before its pressure ranks mean what they
#: say. The app will rank against a week if that is all it has; a model
#: trained on those would be learning from a different feature.
MIN_HISTORY_DAYS = 31

#: The gradient-boosted trees. Sized by measurement: 31 leaves instead of 15
#: bought +0.009 Brier skill for twice the file, and four times the trees
#: +0.012 for four times — neither worth a model.json that every weekly
#: retrain rewrites in full.
GBM = {
    "max_iter": 300,
    "learning_rate": 0.07,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 200,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": 0,
}

#: The most recent year is held out of the evaluation fit and scored, so the
#: page's numbers cover every season once.
HOLDOUT_DAYS = 365
#: Below this many labelled hours the balcony's archive is too short to gate
#: on, and only the weather-service hold-out decides.
MIN_ARCHIVE_HOURS = 500
#: Gates a candidate must clear before it replaces the shipped model.
#: BSS is skill over quoting the base rate; MAX_REGRESSION lets a model
#: wobble without thrashing the deployed one.
MIN_SKILL = 0.05
MAX_REGRESSION = 0.05

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
SEASONS = {"DJF": (12, 1, 2), "MAM": (3, 4, 5), "JJA": (6, 7, 8), "SON": (9, 10, 11)}


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
    """Every raw reading the balcony has, walked page by page.

    Deliberately NOT /api/weather/history. That endpoint downsamples above
    TARGET_CHART_POINTS, so on any archive longer than a few days it returns
    bucket averages — and it did, for months: a 92-day archive of ~26,500
    readings came back as 734 three-hourly points, none of the features could
    be completed from them, and every run printed "too few samples" and
    exited 0. A green tick and nothing learned.

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
        rows.extend(payload["readings"])
        cursor = payload.get("next")
        if not cursor:
            break
        print(f"  {len(rows)} readings so far")
    return rows


def fetch_predictions(app_url: str, api_key: str | None = None) -> list[dict]:
    """Every prediction the page has logged, walked page by page.

    Same cursor and the same key as the readings export. Returns an empty
    list rather than failing if the endpoint is not there: a run that cannot
    verify should still be able to train.
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
    return rows


def instant(row: dict) -> datetime:
    """The UTC instant of an app row, from its local timestamp and offset.

    The offset is what tells the two passes through the repeated autumn hour
    apart; a row that predates the column is read as the first pass, as the
    migration reads it.
    """
    local = datetime.strptime(row["timestamp"], TS_FORMAT)
    offset = row.get("utc_offset")
    if offset is None:
        offset = int(local.replace(tzinfo=ZoneInfo(TIMEZONE)).utcoffset().total_seconds() // 60)
    return (local - timedelta(minutes=int(offset))).replace(tzinfo=UTC)


def station_location(latitude: float | None, longitude: float | None) -> tuple[float, float]:
    """The balcony's coordinates, from the command line or the environment.

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


def fetch_cloud(latitude: float, longitude: float, start: date, end: date) -> dict[datetime, float]:
    """Hourly ERA5 cloud cover at one point, keyed by UTC hour."""
    url = (
        f"{ARCHIVE_URL}?latitude={latitude}&longitude={longitude}"
        f"&start_date={start:%Y-%m-%d}&end_date={end:%Y-%m-%d}"
        "&hourly=cloud_cover&timezone=GMT"
    )
    hourly = fetch_json(url, timeout=300)["hourly"]
    return {
        datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC): v
        for t, v in zip(hourly["time"], hourly["cloud_cover"], strict=True)
        if v is not None
    }


# ── Labels ─────────────────────────────────────────────────────────────────


def rain_label(gauge: dict[datetime, float], moment: datetime) -> float | None:
    """Did at least :data:`RAIN_MM` fall in the :data:`HORIZON_HOURS` after ``moment``?

    The gauge's ten-minute totals are stamped with the end of their ten
    minutes, so the window is the stamps after ``moment`` up to and including
    six hours on. A window with any ten minutes missing is no label at all:
    a partial window is not a weaker answer, it is a different question.
    """
    total = 0.0
    for k in range(1, HORIZON_HOURS * 6 + 1):
        amount = gauge.get(moment + dwd.STEP * k)
        if amount is None:
            return None
        total += amount
    return float(total >= RAIN_MM - 1e-9)


def sky_label(cloud: dict[datetime, float], moment: datetime) -> float | None:
    """Was the next :data:`HORIZON_HOURS` overcast, on average?

    The mean rather than any hour of it: the outlook is a claim about the
    period as a whole, and one cloudy hour inside a bright afternoon is not
    what "Cloudy" on the banner is meant to say.
    """
    hours = [cloud.get(moment + timedelta(hours=k)) for k in range(1, HORIZON_HOURS + 1)]
    if any(v is None for v in hours):
        return None
    return float(sum(hours) / len(hours) >= OVERCAST_PERCENT)


# ── Samples ────────────────────────────────────────────────────────────────


@dataclass
class Samples:
    """One row per hour: every feature app.features computes, and the labels.

    ``X`` holds all of :data:`app.features.NAMES` — the model reads
    :data:`FEATURES` out of it, the threshold ladder its own few — with NaN
    where a feature was unavailable, which is how the trees are told.
    """

    X: np.ndarray
    moments: np.ndarray   #: UTC, as datetime64[s]
    local: list[datetime]  #: the same instants on the local wall clock
    rain: np.ndarray      #: 1, 0, or NaN where the window was incomplete
    sky: np.ndarray

    def __len__(self) -> int:
        return len(self.moments)

    def take(self, keep: np.ndarray) -> Samples:
        return Samples(
            self.X[keep], self.moments[keep],
            [m for m, k in zip(self.local, keep, strict=True) if k],
            self.rain[keep], self.sky[keep],
        )

    @staticmethod
    def concat(parts: list[Samples]) -> Samples:
        return Samples(
            np.concatenate([p.X for p in parts]),
            np.concatenate([p.moments for p in parts]),
            [m for p in parts for m in p.local],
            np.concatenate([p.rain for p in parts]),
            np.concatenate([p.sky for p in parts]),
        )

    def column(self, name: str) -> np.ndarray:
        return self.X[:, features.NAMES.index(name)]

    def matrix(self, names: tuple[str, ...] = FEATURES) -> np.ndarray:
        return self.X[:, [features.NAMES.index(n) for n in names]]

    def labels(self, target: str) -> np.ndarray:
        return self.rain if target == "rain" else self.sky


def build_samples(
    air: list[tuple[datetime, float, float, float]],
    gauge: dict[datetime, float] | None,
    cloud: dict[datetime, float] | None,
) -> Samples:
    """The hourly samples for one run of readings, as the app would have seen them.

    ``air`` is ``(UTC instant, temperature, humidity, pressure)``, from a
    weather-service station or from the balcony's own export alike. It is put
    into the app's conventions first — local wall-clock time, ordered as the
    database orders it — and then, at every hour, handed to
    :func:`app.features.compute` with the pressure cycle the app would have
    learned by that day: the 90 whole days before it.
    """
    tz = ZoneInfo(TIMEZONE)
    local = sorted(
        ((u.astimezone(tz).replace(tzinfo=None), u, t, h, p) for u, t, h, p in air),
        key=lambda r: (r[0], r[1]),
    )
    series = features.Series.from_rows((r[0], r[2], r[3], r[4]) for r in local)
    days = features.summarise_days(
        (r[0].date(), features.slot_of(r[0]), r[4]) for r in local
    )

    cycles: dict[date, dict[int, float]] = {}

    def cycle(day: date) -> dict[int, float]:
        if day not in cycles:
            window = (day - timedelta(days=k) for k in range(1, features.CYCLE_LEARN_DAYS + 1))
            cycles[day] = features.cycle_from_days(days[d] for d in window if d in days)
        return cycles[day]

    rows, moments, wall, rain, sky = [], [], [], [], []
    first, last = air[0][0], air[-1][0]
    moment = (first + timedelta(days=MIN_HISTORY_DAYS)).replace(minute=0, second=0)
    while moment <= last:
        now = moment.astimezone(tz).replace(tzinfo=None)
        vector = features.compute(series, now, cycle(now.date()))
        if vector is not None:
            y_rain = rain_label(gauge, moment) if gauge else None
            y_sky = sky_label(cloud, moment) if cloud else None
            if y_rain is not None or y_sky is not None:
                rows.append([np.nan if vector[n] is None else vector[n] for n in features.NAMES])
                moments.append(moment.replace(tzinfo=None))
                wall.append(now)
                rain.append(np.nan if y_rain is None else y_rain)
                sky.append(np.nan if y_sky is None else y_sky)
        moment += timedelta(hours=1)

    width = len(features.NAMES)
    return Samples(
        np.array(rows, dtype=float).reshape(-1, width),
        np.array(moments, dtype="datetime64[s]"),
        wall,
        np.array(rain, dtype=float),
        np.array(sky, dtype=float),
    )


def station_samples(job: tuple[str, Path, date, dict | None]) -> tuple[Samples, float]:
    """Load one weather-service station and build its samples (a worker)."""
    station_id, cache, since, cloud = job
    observations = dwd.load(station_id, cache, since)
    expected = (datetime.now(UTC).date() - since).days * 144
    coverage = len(observations.air) / expected if expected else 0.0
    if coverage < MIN_COVERAGE:
        return _empty(), coverage
    return build_samples(observations.air, observations.rain, cloud), coverage


def _empty() -> Samples:
    return Samples(np.zeros((0, len(features.NAMES))), np.array([], dtype="datetime64[s]"),
                   [], np.array([]), np.array([]))


def training_samples(
    candidates: list[dwd.Station], cache: Path, want: int, with_cloud: bool, workers: int,
) -> tuple[Samples, int]:
    """Samples from the first ``want`` candidates that measure all three things.

    Candidates are tried nearest first. A station without a barometer, or
    with long gaps, is passed over for the next one; nothing about which
    stations were used or skipped is printed (see the module docstring).
    """
    today = datetime.now(UTC).date()
    parts: list[Samples] = []
    queue = list(candidates)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        while len(parts) < want and queue:
            batch, queue = queue[: want - len(parts)], queue[want - len(parts):]
            jobs = []
            for station in batch:
                cloud = (
                    fetch_cloud(station.latitude, station.longitude, TRAINING_SINCE,
                                today - timedelta(days=1))
                    if with_cloud else None
                )
                jobs.append((station.id, cache, TRAINING_SINCE, cloud))
            for samples, coverage in pool.map(station_samples, jobs):
                if coverage >= MIN_COVERAGE and len(samples):
                    parts.append(samples)
                    print(f"  station {len(parts)} of {want}: {len(samples)} hours")
                else:
                    print(f"  a candidate station was passed over ({coverage:.0%} coverage)")
    if not parts:
        raise SystemExit("no weather-service station near enough measured all three things")
    return Samples.concat(parts), len(parts)


# ── Fitting and exporting ──────────────────────────────────────────────────


def fit(X: np.ndarray, y: np.ndarray) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(**GBM).fit(X, y)


def export(
    model: HistGradientBoostingClassifier, names: tuple[str, ...], threshold: float,
    metadata: dict,
) -> dict:
    """The fitted trees as the plain arrays :func:`app.nowcast.load` reads.

    Every internal node also gets the expected output of the leaves beneath
    it, weighted by how many training hours reached each. That is what the
    page's breakdown measures each step of a path against, and scikit-learn
    does not store it in this form.

    Thresholds are written to one decimal more than :data:`app.features.DECIMALS`
    gives the feature. Features live on that grid in training and in the app
    alike, and a split falls between grid values, so the extra decimal keeps
    every split exactly where it was without seventeen digits of float.
    """
    trees = []
    for (predictor,) in model._predictors:
        nodes = predictor.nodes
        n = len(nodes)
        value, weight = [0.0] * n, [0.0] * n
        # Children always follow their parent in this array, so one pass from
        # the end has every child's expectation before its parent needs it.
        for i in reversed(range(n)):
            if nodes["is_leaf"][i]:
                value[i], weight[i] = float(nodes["value"][i]), float(nodes["count"][i])
            else:
                left, right = int(nodes["left"][i]), int(nodes["right"][i])
                weight[i] = weight[left] + weight[right]
                value[i] = (
                    (value[left] * weight[left] + value[right] * weight[right]) / weight[i]
                    if weight[i] else 0.0
                )
        leaf = [bool(v) for v in nodes["is_leaf"]]
        trees.append({
            "feature": [-1 if leaf[i] else int(nodes["feature_idx"][i]) for i in range(n)],
            "threshold": [
                0 if leaf[i] else round(
                    float(nodes["num_threshold"][i]),
                    features.DECIMALS[names[int(nodes["feature_idx"][i])]] + 1,
                )
                for i in range(n)
            ],
            "left": [0 if leaf[i] else int(nodes["left"][i]) for i in range(n)],
            "right": [0 if leaf[i] else int(nodes["right"][i]) for i in range(n)],
            "missing_left": [0 if leaf[i] else int(nodes["missing_go_to_left"][i])
                             for i in range(n)],
            "value": [round(v, 6) for v in value],
        })
    return {
        "format": nowcast.FORMAT,
        "features": list(names),
        "base": round(float(np.ravel(model._baseline_prediction)[0]), 6),
        "trees": trees,
        "threshold": round(threshold, 3),
        "metadata": metadata,
    }


def as_model(exported: dict) -> nowcast.Model:
    """Read an exported model back through the app's own loader."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.json"
        path.write_text(json.dumps(exported))
        model = nowcast.load(path)
    if model is None:
        raise SystemExit("the exported model does not load in the app")
    return model


def raw_scores(model: nowcast.Model, X: np.ndarray) -> np.ndarray:
    """The model's log-odds for many rows at once, columns in ``model.features``.

    The same walk as :meth:`app.nowcast.Tree.path`, vectorised, so the
    self-check can hold the export to scikit-learn on every row it has
    rather than on a sample, and the app's own walk to both.
    """
    rows = np.arange(len(X))
    out = np.full(len(X), model.base)
    for tree in model.trees:
        feature = np.array(tree.feature)
        threshold = np.array(tree.threshold)
        left, right = np.array(tree.left), np.array(tree.right)
        missing_left = np.array(tree.missing_left)
        node = np.zeros(len(X), dtype=int)
        while True:
            active = feature[node] >= 0
            if not active.any():
                break
            x = X[rows, np.maximum(feature[node], 0)]
            go_left = np.where(np.isnan(x), missing_left[node], x <= threshold[node])
            node = np.where(active, np.where(go_left, left[node], right[node]), node)
        out += np.array(tree.value)[node]
    return out


def self_check(
    exported: dict, fitted: HistGradientBoostingClassifier, X: np.ndarray, count: int = 2000,
) -> None:
    """Refuse to ship a file that does not reproduce the fitted model.

    The export reads scikit-learn's private tree arrays, which a release
    could change under it. So every run checks the exported trees against
    scikit-learn's own log-odds on every training row, and on a sample of
    them walks the app's pure-Python evaluator too, along with its
    breakdown, which must sum to the same number. A mismatch fails the run
    rather than putting a model on the page that says something else.
    """
    model = as_model(exported)
    expected = fitted.decision_function(X)
    worst = float(np.max(np.abs(raw_scores(model, X) - expected)))
    if worst > 1e-3:
        raise SystemExit(f"exported trees disagree with scikit-learn by up to {worst:.4f}")
    rng = np.random.default_rng(0)
    for index in rng.choice(len(X), size=min(count, len(X)), replace=False):
        values = {
            name: None if np.isnan(v) else float(v)
            for name, v in zip(model.features, X[index], strict=True)
        }
        walked, summed = model.raw(values), model.logit(values)
        if abs(walked - expected[index]) > 1e-3 or abs(summed - walked) > 1e-6:
            raise SystemExit(
                f"the app's evaluator disagrees with scikit-learn: {walked} vs {expected[index]}"
            )
    print(f"  self-check: the exported trees reproduce scikit-learn on {len(X)} hours")


# ── Scoring ────────────────────────────────────────────────────────────────


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
        if len(set(probabilities.tolist())) > 1 and len(set(truth.tolist())) > 1
        else None,
        "csi": round(csi(prediction, truth), 3),
        "kss": round(pod - pofd, 3),
        "pod": round(pod, 3),
        "far": round(false_alarms / (hits + false_alarms), 3) if hits + false_alarms else 0.0,
        "fires": round(float(prediction.mean()), 3),
    }


def incumbent(samples: Samples, target: str) -> np.ndarray:
    """What the threshold ladder already claims, as a 0/1 call per hour.

    This is the thing each model is asking to improve on, so it is the
    baseline that decides whether shipping is an improvement or just a
    change. For rain it is the ladder's own "says rain" rungs. For cloud it
    is the humidity test behind "Overcast and humid".
    """
    rh = samples.column("rh")
    if target == "sky":
        return (rh > weather.HUMIDITY_MUGGY).astype(float)
    pct, temp = samples.column("pct30"), samples.column("temp")
    spread, drh3 = samples.column("spread"), samples.column("drh3")
    out = np.zeros(len(samples))
    for i in range(len(samples)):
        phrase = weather.compute_forecast(
            pct[i], rh[i], temp[i], temp[i] - spread[i],
            {"delta": 0.0 if np.isnan(drh3[i]) else drh3[i]},
            moment=samples.local[i],
        )
        out[i] = float("Rain" in phrase or "Thunder" in phrase)
    return out


def baselines(samples: Samples, truth: np.ndarray, target: str) -> dict:
    """What you get without a model: the base rate, and the incumbent rules."""
    return {
        "climatology": score(np.full_like(truth, truth.mean(), dtype=float), truth, 0.5),
        "rules": score(incumbent(samples, target), truth, 0.5),
    }


def by_season(samples: Samples, probabilities: np.ndarray, truth: np.ndarray) -> dict:
    """Brier skill in each meteorological season, which a single summer never had."""
    months = np.array([m.month for m in samples.local])
    out = {}
    for name, members in SEASONS.items():
        keep = np.isin(months, members)
        if keep.sum() >= 100 and len(set(truth[keep].tolist())) > 1:
            out[name] = score(probabilities[keep], truth[keep], 0.5)["bss"]
    return out


def ablations(
    train: Samples, test: Samples, target: str, reference: dict,
) -> list[dict]:
    """What each feature is worth: the same fit and the same test without it.

    The question this answers is whether a feature carries weather. Refit
    from scratch each time, because dropping a column from a fitted model
    only ever looks bad: the point is generalisation, not fit.
    """
    y_train, y_test = train.labels(target), test.labels(target)
    worth = []
    for dropped in FEATURES:
        kept = tuple(f for f in FEATURES if f != dropped)
        model = fit(train.matrix(kept), y_train)
        without = score(model.predict_proba(test.matrix(kept))[:, 1], y_test, 0.5)
        worth.append({
            "feature": dropped,
            "brier": without["brier"],
            "auc": without["auc"],
            "bss": without["bss"],
            # Positive means the model is worse without it, which is what
            # "worth something" means here.
            "brier_cost": round(without["brier"] - reference["brier"], 4),
            "auc_cost": (
                round(reference["auc"] - without["auc"], 3)
                if reference["auc"] is not None and without["auc"] is not None
                else None
            ),
        })
    worth.sort(key=lambda row: row["brier_cost"], reverse=True)
    print("\nleave-one-out (positive = the model is worse without it)")
    for row in worth:
        auc = "" if row["auc_cost"] is None else f"  AUC {row['auc']} ({row['auc_cost']:+.3f})"
        print(f"  without {row['feature']:<8} "
              f"Brier {row['brier']:.4f} ({row['brier_cost']:+.4f}){auc}")
    return worth


def describe_period(samples: Samples) -> dict:
    return {
        "from": samples.local[0].strftime("%Y-%m-%d"),
        "to": samples.local[-1].strftime("%Y-%m-%d"),
        "hours": len(samples),
    }


def print_scores(label: str, rows: dict) -> None:
    print(label)
    for name, s in rows.items():
        auc = "—" if s["auc"] is None else s["auc"]
        print(f"  {name:<12} Brier {s['brier']:.4f}  BSS {s['bss']:+.3f}  "
              f"AUC {auc}  CSI {s['csi']:.3f}  KSS {s['kss']:+.3f}")


def evaluate(
    samples: Samples, target: str, archive: Samples | None, measure_worth: bool,
) -> dict | None:
    """Score a candidate out of sample, then fit the one that would ship.

    Two tests, both on hours the evaluation fit never saw:

    * the last :data:`HOLDOUT_DAYS` at the training stations — a full year,
      so every season is in the score;
    * the balcony's own archive, labelled by the nearest gauge — the only
      test of whether a model fitted to weather-service screens survives
      this sensor. The evaluation fit stops before the archive begins, so
      the same storms cannot be learned at a neighbour and scored here.

    The model that ships is then refitted on everything.
    """
    print(f"\n── {target} " + "─" * (66 - len(target)))
    usable = samples.take(~np.isnan(samples.labels(target)))
    if len(usable) < 10 * MIN_ARCHIVE_HOURS:
        print(f"{len(usable)} labelled hours; leaving the shipped model alone")
        return None

    holdout_start = usable.moments.max() - np.timedelta64(HOLDOUT_DAYS, "D")
    split = holdout_start
    scored_archive = None
    if archive is not None:
        scored_archive = archive.take(~np.isnan(archive.labels(target)))
        if len(scored_archive) and len(set(scored_archive.labels(target).tolist())) > 1:
            split = min(split, scored_archive.moments.min())
        else:
            scored_archive = None

    train = usable.take(usable.moments < split)
    test = usable.take(usable.moments >= holdout_start)
    y_train, y_test = train.labels(target), test.labels(target)
    candidate = fit(train.matrix(), y_train)
    threshold = best_threshold(candidate.predict_proba(train.matrix())[:, 1], y_train)
    p_test = candidate.predict_proba(test.matrix())[:, 1]

    skill = score(p_test, y_test, threshold)
    reference = baselines(test, y_test, target)
    seasons = by_season(test, p_test, y_test)
    rows = {target: skill, **reference}
    print_scores(
        f"held-out year at the training stations: {len(test)} hours "
        f"(base rate {y_test.mean() * 100:.1f}%), operating threshold {threshold:.3f}",
        rows,
    )
    print("  skill by season: " + ", ".join(f"{k} {v:+.3f}" for k, v in seasons.items()))

    on_archive = None
    if scored_archive is not None:
        truth = scored_archive.labels(target)
        p = candidate.predict_proba(scored_archive.matrix())[:, 1]
        on_archive = {
            **describe_period(scored_archive),
            "base_rate": round(float(truth.mean()), 3),
            "skill": score(p, truth, threshold),
            "rules": score(incumbent(scored_archive, target), truth, 0.5),
        }
        print_scores(
            f"the balcony's own archive: {len(scored_archive)} hours "
            f"(base rate {truth.mean() * 100:.1f}%)",
            {target: on_archive["skill"], "rules": on_archive["rules"]},
        )

    worth = ablations(train, test, target, skill) if measure_worth else None

    final = fit(usable.matrix(), usable.labels(target))
    final_threshold = best_threshold(
        final.predict_proba(usable.matrix())[:, 1], usable.labels(target)
    )
    return {
        "target": target,
        "usable": usable,
        "fitted": final,
        "threshold": final_threshold,
        "skill": skill,
        "baselines": reference,
        "seasons": seasons,
        "holdout": describe_period(test),
        "archive": on_archive,
        "ablations": worth,
    }


# ── Shipping ───────────────────────────────────────────────────────────────


def gates(evaluation: dict, target: str, shipped: dict | None) -> list[str]:
    """Why this candidate may not ship, or an empty list if it may.

    What a model is *for* decides what it has to beat, and both of these
    exist to give a calibrated probability, which the ladder they sit beside
    cannot. So the gates are probabilistic — better than quoting the base
    rate, and ranking hours at least as well as the rules — and they are
    applied to the balcony's own archive as well as to the held-out year,
    because a model that is good on weather-service screens and bad on this
    sensor is bad here. The yes/no hit rate is reported but deliberately not
    a gate: trading calibration for a better CSI would lose the thing the
    model adds.

    The regression gate compares the candidate's out-of-sample skill with the
    out-of-sample skill the shipped model *recorded* when it was fitted, not
    with the shipped model re-scored now. Re-scored, it would be judged on
    hours it was trained on, and that in-sample advantage was measured at
    +0.043 Brier skill — nearly the whole tolerance, enough to refuse every
    honest retrain from then on.
    """
    recorded = (shipped or {}).get("metadata", {})
    reasons = []
    tests = [("held-out year", evaluation["skill"], evaluation["baselines"]["rules"],
              (recorded.get("skill") or {}).get("bss"))]
    archive = evaluation["archive"]
    if archive and archive["hours"] >= MIN_ARCHIVE_HOURS:
        tests.append(("balcony archive", archive["skill"], archive["rules"],
                      ((recorded.get("archive") or {}).get("skill") or {}).get("bss")))
    for where, skill, rules, before in tests:
        if skill["bss"] < MIN_SKILL:
            reasons.append(f"not enough skill over the base rate on the {where} "
                           f"(BSS {skill['bss']:+.3f}, need {MIN_SKILL:+.2f})")
        if skill["auc"] is None or (rules["auc"] is not None and skill["auc"] < rules["auc"]):
            reasons.append(f"ranks hours no better than the rules on the {where} "
                           f"(AUC {skill['auc']} vs {rules['auc']})")
        if before is not None and skill["bss"] < before - MAX_REGRESSION:
            reasons.append(f"a clear step down from the shipped {target} model on the {where} "
                           f"(BSS {skill['bss']:+.3f} vs {before:+.3f} when it was fitted)")
    return reasons


def ship(evaluation: dict, out: Path, extra: dict, force: bool) -> bool:
    """Gate one evaluated candidate and write it out if it passes."""
    target = evaluation["target"]
    shipped = json.loads(out.read_text()) if out.exists() else None
    if shipped is not None and shipped.get("format") != nowcast.FORMAT:
        shipped = None  # a model of another kind recorded skill on another test
    reasons = gates(evaluation, target, shipped)
    if reasons and not force:
        print(f"\nNOT shipping this {target} model:")
        for reason in reasons:
            print(f"  - {reason}")
        print("The shipped model is left exactly as it is.")
        return False

    usable = evaluation["usable"]
    y = usable.labels(target)
    exported = export(
        evaluation["fitted"], FEATURES, evaluation["threshold"],
        {
            "trained_at": datetime.now().strftime("%Y-%m-%d"),
            "samples": len(usable),
            "trained_from": usable.local[0].strftime("%Y-%m-%d"),
            "trained_through": usable.local[-1].strftime("%Y-%m-%d"),
            "base_rate": round(float(y.mean()), 3),
            "horizon_hours": HORIZON_HOURS,
            "trees": len(evaluation["fitted"]._predictors),
            "skill": evaluation["skill"],
            "baselines": evaluation["baselines"],
            "seasons": evaluation["seasons"],
            "holdout": evaluation["holdout"],
            "archive": evaluation["archive"],
            "ablations": evaluation["ablations"],
            **extra,
        },
    )
    self_check(exported, evaluation["fitted"], usable.matrix())
    out.write_text(json.dumps(exported, separators=(",", ":")) + "\n")
    print(f"\nwrote {out} ({out.stat().st_size // 1024} KB)")
    return True


# ── Verifying what was actually shown ──────────────────────────────────────
#
# Everything above scores a candidate against a held-out past. That is the
# right way to decide whether to ship, and it says nothing about whether the
# model *already deployed* has been right about this balcony's weather.
#
# This scores the prediction log — what the page actually said, hour by hour,
# written at the time — against the gauge nearest the balcony. It cannot be
# reconstructed by replay: the log spans however many weekly models were
# deployed across the window, and a replay would credit every hour to today's.

#: The bins and their shape live in app/nowcast.py: it is the page that
#: renders them, and keeping the definition there makes it pure arithmetic
#: the ordinary test suite can check without numpy.
from app.nowcast import reliability_bins  # noqa: E402


def verify(predictions: list[dict], gauge: dict[datetime, float]) -> dict | None:
    """Score the logged predictions against what the gauge then recorded."""
    scored = [(row, rain_label(gauge, instant(row))) for row in predictions]
    scored = [(row, label) for row, label in scored if label is not None]
    if not scored:
        print("\nNothing in the prediction log can be scored yet.")
        return None

    rows = [row for row, _ in scored]
    truth = np.array([label for _, label in scored], dtype=float)
    result: dict = {
        "scored_at": datetime.now().strftime("%Y-%m-%d"),
        "from": rows[0]["timestamp"][:10],
        "to": rows[-1]["timestamp"][:10],
        "hours": len(rows),
        "base_rate": round(float(truth.mean()), 3),
        # However many weekly models the window spans. A replay could not
        # produce this, and it is the reason the log exists.
        "models": sorted({row["model_trained_at"] for row in rows
                          if row.get("model_trained_at")}),
        "horizon_hours": HORIZON_HOURS,
        "rain_mm": RAIN_MM,
        "labels": "the nearest weather-service rain gauge",
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
        shipped = nowcast.load()
        result["model"] = score(probabilities, model_truth,
                                shipped.threshold if shipped else 0.5)
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


# ── Entry point ────────────────────────────────────────────────────────────


def balcony_archive(
    app_url: str, api_key: str | None, latitude: float, longitude: float, cache: Path,
    with_cloud: bool,
) -> tuple[Samples | None, dict[datetime, float] | None]:
    """The balcony's own readings as samples, labelled by the nearest gauge.

    Returns the gauge too, because the prediction log is scored against the
    same one. Either can be ``None``: no gauge near enough, or an archive
    the endpoint would not hand over, and the run carries on without it.
    """
    today = datetime.now(UTC).date()
    gauges = dwd.nearest(dwd.stations("rain", cache), latitude, longitude,
                         since=today - timedelta(days=30), current=today - timedelta(days=3))
    if not gauges or gauges[0].km_from(latitude, longitude) > MAX_GAUGE_KM:
        print(f"no weather-service rain gauge within {MAX_GAUGE_KM:.0f} km; "
              "the archive and the log cannot be scored")
        return None, None
    try:
        rows = fetch_readings(app_url, api_key)
    except Exception as error:
        print(f"(the balcony's archive is unavailable: {error})")
        rows = []
    since = (instant(rows[0]).date() if rows else today) - timedelta(days=1)
    gauge = dwd.load_rain(gauges[0].id, cache, min(since, today - timedelta(days=400)))
    if not rows:
        return None, gauge

    air = sorted(
        (instant(r), float(r["temperature"]), float(r["humidity"]), float(r["pressure"]))
        for r in rows
    )
    cloud = (
        fetch_cloud(latitude, longitude, air[0][0].date(), today - timedelta(days=1))
        if with_cloud else None
    )
    samples = build_samples(air, gauge, cloud)
    print(f"the balcony's archive: {len(rows)} readings, {len(samples)} labelled hours")
    return (samples if len(samples) else None), gauge


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
    parser.add_argument("--latitude", type=float,
                        help="balcony latitude; defaults to $STATION_LATITUDE")
    parser.add_argument("--longitude", type=float,
                        help="balcony longitude; defaults to $STATION_LONGITUDE")
    parser.add_argument("--stations",
                        help="comma-separated weather-service station ids to train on, "
                             "instead of the ones nearest the balcony. Without a location "
                             "this skips the archive and the verification")
    parser.add_argument("--cache", type=Path, default=REPO / "ml" / ".cache",
                        help="where downloaded weather-service files are kept")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--target", choices=("rain", "sky", "both"), default="both",
                        help="which model to fit; the other is left untouched")
    parser.add_argument("--no-ablations", action="store_true",
                        help="skip the leave-one-out refits (faster, for experiments)")
    parser.add_argument("--force", action="store_true",
                        help="write the model even if it does not clear its gates")
    args = parser.parse_args()

    # The page prints what "overcast" means, reading it from app.nowcast; the
    # labels here decide what it actually means. A mismatch would be invisible
    # on the page and wrong in the model, so it fails the run instead.
    if nowcast.OVERCAST_PERCENT != OVERCAST_PERCENT:
        raise SystemExit(
            f"OVERCAST_PERCENT disagrees: ml/train.py says {OVERCAST_PERCENT}, "
            f"app/nowcast.py says {nowcast.OVERCAST_PERCENT}"
        )

    with_sky = args.target in ("sky", "both")
    located = not args.stations or (args.latitude is not None or os.environ.get(LOCATION_VARS[0]))
    latitude = longitude = None
    if located:
        latitude, longitude = station_location(args.latitude, args.longitude)

    today = datetime.now(UTC).date()
    if args.stations:
        listed = {s.id: s for s in dwd.stations("air", args.cache)}
        candidates = [listed[i.strip().zfill(5)] for i in args.stations.split(",")]
        want = len(candidates)
    else:
        candidates = dwd.nearest(dwd.stations("air", args.cache), latitude, longitude,
                                 since=TRAINING_SINCE, current=today - timedelta(days=3))
        candidates = [s for s in candidates if s.km_from(latitude, longitude) <= MAX_STATION_KM]
        want = TRAINING_STATIONS
    print(f"training on up to {want} weather-service stations, {TRAINING_SINCE.year} onwards")
    samples, used = training_samples(candidates, args.cache, want, with_sky, args.workers)
    print(f"{len(samples)} labelled hours from {used} stations")

    archive, gauge = (
        balcony_archive(args.app_url, args.api_key, latitude, longitude, args.cache, with_sky)
        if located else (None, None)
    )

    wrote = False
    provenance = {"stations": used, "years": [TRAINING_SINCE.year, today.year]}

    if args.target in ("rain", "both"):
        evaluation = evaluate(samples, "rain", archive, measure_worth=not args.no_ablations)
        if evaluation is not None:
            wrote |= ship(
                evaluation, args.out,
                {
                    **provenance,
                    "rain_mm": RAIN_MM,
                    "labels": "rain gauges beside the training stations' sensors",
                },
                args.force,
            )

    if with_sky:
        evaluation = evaluate(samples, "sky", archive, measure_worth=False)
        if evaluation is not None:
            wrote |= ship(
                evaluation, args.sky_out,
                {
                    **provenance,
                    "overcast_percent": OVERCAST_PERCENT,
                    "labels": "ERA5 reanalysis, ~25 km: mean cloud cover over the horizon",
                },
                args.force,
            )

    # Deliberately outside the shipping decision, and written every run.
    # This does not describe the candidate; it describes the model that has
    # been deployed over the scoring window, which is a different question.
    # Refusing to ship is a normal outcome, and the verification must not go
    # stale behind it.
    if gauge is not None:
        verification = verify(fetch_predictions(args.app_url, args.api_key), gauge)
        if verification is not None:
            args.verification_out.write_text(json.dumps(verification, indent=2) + "\n")
            print(f"wrote {args.verification_out}")

    if not wrote:
        print("\nNothing shipped; every model on disk is left exactly as it is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
