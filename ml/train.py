#!/usr/bin/env python3
"""Fit the nowcasts and, if they earn their place, write them into app/.

Run by .github/workflows/retrain.yml every week. It is the only part of this
project that reaches outside the box, and it does so in CI, never in the app.

Where everything comes from, and what each source is for:

  * **Training data** — ten-minute observations from the weather service's
    stations nearest the balcony (``ml/dwd.py``): temperature, humidity and
    pressure, the three things this sensor measures. Years of them, so every
    season is in the fit. This replaced fitting to the balcony's own archive,
    which was one summer long: a model that has only seen July learns that
    "cool" means "dry", and in October it said 11% with rain on the sensor.
  * **The labels** — what was actually observed at those same stations: the
    rain gauge beside the sensors, and the hourly record of cloud cover,
    visibility and present weather. Every model here is fitted to something
    that was measured or seen at the place its inputs came from.
  * **The balcony's own archive** (``/api/weather/export``) — never trained
    on. It is where a candidate is *scored* before it may ship, because a
    weather-service screen is not a sun-baked balcony and the only honest test
    of that gap is this sensor. Labelled by the nearest station that observes
    the thing in question.
  * **The prediction log** (``/api/weather/predictions``) — what the page
    actually said, scored against the nearest gauge (``app/verification.json``).

Four models come out of one set of samples, one per claim the outlook banner
makes: rain (``app/model.json``), an overcast sky (``app/sky_model.json``),
fog (``app/fog_model.json``) and thunder (``app/thunder_model.json``). They
share the feature vector and are gated separately, so any of them can ship
while the others are refused. Refusing to ship is a normal outcome, not a
failure: the script exits 0 and leaves the file alone.

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
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402

from app import features, nowcast, weather  # noqa: E402
from ml import dwd  # noqa: E402

DEFAULT_APP_URL = "https://weather.wtzg.de"

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
#: main() asserts the two agree rather than trusting that they do. Observers
#: and instruments report octas, so this is 6.4 of 8; a sky hidden by fog is
#: read as 8, because nothing above it is getting through.
OVERCAST_PERCENT = 80
#: Visibility under which an hour is foggy: the meteorological definition of
#: fog, and the one the observations are made against. Mirrors app.nowcast.
FOG_METRES = 1000
#: A station foggy for more than this share of its hours is not under the
#: fog, it is in the cloud: a hilltop whose "fog" is a low cloud base. Of the
#: five stations nearest the location this was tested at, four were foggy
#: 1.0-4.5% of the time and one, on a summit, 29.8% — and its labels taught
#: the model that saturated air means fog, which on a balcony that reads 100%
#: whenever it is wet is the one lesson it must not learn. Its rain, cloud and
#: thunder are still used; only its fog is not.
MAX_FOG_SHARE = 0.15
#: The present-weather codes that mean thunder: heard with no rain (17), in
#: the last hour (29), in the last hour with rain or snow now (91-94), and at
#: the time of the observation (95-99).
THUNDER_CODES = (17, 29, 91, 92, 93, 94, 95, 96, 97, 98, 99)
#: Thunder was reported by people, and the instruments that replaced them
#: report visibility and cloud but not thunder. The last observers went off
#: duty in 2022: at the eight stations first measured, 2014-2021 held 25-118
#: thunder reports a year each, and 2023 onwards held none at all. So thunder
#: is labelled only before this date — and, because many stations lost their
#: observers years earlier, each only up to its own last report: see
#: observed_thunder().
THUNDER_UNTIL = date(2022, 1, 1)

#: The model's inputs, all computed by app.features. All four targets are
#: fitted on this one tuple: a feature that meant different things to two
#: models would make their breakdowns incomparable.
#:
#: Chosen by measurement over 1.1 million station-hours, scored on the last
#: two years. Dropped because they added nothing once these were in: the
#: absolute temperature (a sun-baked balcony reads hot, and on screens it
#: carried no signal the dew point did not), the day of the year (which is
#: how a model learns the calendar instead of the sky), and the humidity
#: changes (the dew-point and temperature changes already carry them).
#:
#: The swings — how far temperature and humidity have ranged over the last
#: few hours to a day — were added for the sky, and are the sensor's view of
#: the sun: a clear day heats and a clear night cools, an overcast one does
#: neither. Measured over the same 1.1 million station-hours, adding them
#: moved Brier skill +0.025 for an overcast sky (0.265 to 0.290), +0.010 for
#: rain, +0.008 for thunder and nothing for fog, which the humidity signals
#: already carry.
FEATURES = (
    "rh", "rh_max1", "rh_max3", "sat3",
    "dT1", "dT3", "dT24", "td", "dtd3",
    "pct30", "pct7", "dp1", "dp3", "dp6", "dp12",
    "t_range3", "t_range6", "t_range12", "t_range24", "rh_range6", "rh_min24",
    "hour",
)

#: How many of the nearest weather-service stations each model learns from,
#: and from when. Measured: a model trained on seven other stations scored
#: the eighth as well as one trained on that station's own fourteen years, so
#: more is not better past a handful — it is only slower. Each model counts
#: its own: the nearest stations that observe its label, which for thunder,
#: reported only where people kept watch, can mean reaching further than the
#: others (of the five stations nearest the test location, one had).
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
#: The same, for the observations that label the archive for the other
#: models. A sky is shared over a wider area than a shower is; fog is as
#: local as rain, and lies in valleys a hill a few kilometres off is above.
MAX_CLOUD_KM = 50.0
MAX_VISIBILITY_KM = 25.0

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
#: The calibration to the balcony. The trees are fitted to weather-service
#: screens; on this sensor they rank hours well (AUC 0.807 on the first
#: archive) but run high (Brier skill -0.116), because a sun-baked, dewy
#: balcony is not a ventilated screen. Two numbers on the log-odds fix the
#: scale without touching the ranking. Fitted on the most recent
#: CALIBRATION_DAYS of the archive, so it follows the season as the record
#: grows instead of carrying a summer's sensor into the winter; needs
#: MIN_ARCHIVE_EVENTS hours of the thing happening to fit at all; and scored
#: out of fold, the archive cut into CALIBRATION_FOLDS stretches each judged
#: by a calibration fitted without it.
#:
#: MIN_ARCHIVE_EVENTS is also what it takes for the archive to be a test at
#: all. Fog is rare: a summer can hold a handful of foggy hours, and a Brier
#: score over five events is a coin toss, not a verdict.
CALIBRATION_DAYS = 120
MIN_ARCHIVE_EVENTS = 30
CALIBRATION_FOLDS = 4
#: Thunder has no label on the balcony at all — nothing near it reports
#: thunder any more — so its calibration can only be to the level: one shift
#: of the log-odds that makes the model's mean on the balcony's recent
#: readings equal how often the training stations had thunder in those same
#: months. A shift this large would mean the readings are not ones the model
#: understands, and the gate refuses it rather than papering over it.
LEVEL_BOUND = 2.0
#: Gates a candidate must clear before it replaces the shipped model.
#: BSS is skill over quoting the base rate; MAX_REGRESSION lets a model
#: wobble without thrashing the deployed one.
MIN_SKILL = 0.05
MAX_REGRESSION = 0.05

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
SEASONS = {"DJF": (12, 1, 2), "MAM": (3, 4, 5), "JJA": (6, 7, 8), "SON": (9, 10, 11)}


@dataclass(frozen=True)
class Target:
    """One of the four models, and everything that differs between them."""

    name: str
    file: str                #: written under app/
    product: str             #: the ml/dwd.py product its labels come from
    labels: str              #: what the model card says labelled it
    #: How near an observation of it must be to label the balcony's archive;
    #: None where nothing near the balcony observes it any more.
    archive_km: float | None
    #: "scale" fits a slope and an intercept against the archive's labels;
    #: "level" only the intercept, against the stations' climatology.
    calibration: str = "scale"
    #: Refuse to ship without passing on the balcony's own readings. For fog
    #: the sensor's best-known quirk — 100 % whenever it is wet — reads to a
    #: model fitted on screens as exactly the signal it is looking for.
    needs_archive: bool = False
    #: Measure what each feature is worth: one refit per feature, done for
    #: the model whose breakdown the page prints row by row.
    measure_worth: bool = False


TARGETS = {
    "rain": Target("rain", "model.json", "rain",
                   "rain gauges beside the training stations' sensors",
                   MAX_GAUGE_KM, measure_worth=True),
    "sky": Target("sky", "sky_model.json", "cloud",
                  "cloud cover observed at the training stations",
                  MAX_CLOUD_KM),
    "fog": Target("fog", "fog_model.json", "visibility",
                  "visibility observed at the training stations",
                  MAX_VISIBILITY_KM, needs_archive=True),
    "thunder": Target("thunder", "thunder_model.json", "weather",
                      "thunder reported by the training stations' observers",
                      None, calibration="level"),
}
#: The order of the label columns in Samples.
TARGET_NAMES = tuple(TARGETS)


# ── Inputs ─────────────────────────────────────────────────────────────────


def fetch_json(
    url: str, timeout: int = 120, attempts: int = 5, headers: dict | None = None
) -> dict | list:
    """GET some JSON from the app, retrying the failures a server hands out.

    A reverse proxy answers a restarting container with 5xx, and a rate
    limiter with 429. A weekly job that gave up on the first one would
    quietly stop retraining, so back off and try again rather than failing
    the run.
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


# ── Labels ─────────────────────────────────────────────────────────────────
#
# Each takes one station's observations and an instant, and answers for the
# HORIZON_HOURS after it: 1.0, 0.0, or None where the record has a gap in that
# window. A partial window is not a weaker answer, it is a different question,
# and an hour the instrument was down is not an hour nothing happened.


def rain_label(gauge: dict[datetime, float], moment: datetime) -> float | None:
    """Did at least :data:`RAIN_MM` fall in the :data:`HORIZON_HOURS` after ``moment``?

    The gauge's ten-minute totals are stamped with the end of their ten
    minutes, so the window is the stamps after ``moment`` up to and including
    six hours on.
    """
    total = 0.0
    for k in range(1, HORIZON_HOURS * 6 + 1):
        amount = gauge.get(moment + dwd.STEP * k)
        if amount is None:
            return None
        total += amount
    return float(total >= RAIN_MM - 1e-9)


def _hours_after(series: dict[datetime, float], moment: datetime) -> list[float] | None:
    """The hourly observations for the horizon after ``moment``, or None on a gap.

    An hourly value is stamped with the top of its hour, so the six after an
    on-the-hour ``moment`` are the stamps one to six hours on.
    """
    hours = [series.get(moment + timedelta(hours=k)) for k in range(1, HORIZON_HOURS + 1)]
    return None if any(v is None for v in hours) else hours


def sky_label(cloud: dict[datetime, float], moment: datetime) -> float | None:
    """Was the next :data:`HORIZON_HOURS` overcast, on average?

    The mean rather than any hour of it: the outlook is a claim about the
    period as a whole, and one cloudy hour inside a bright afternoon is not
    what "Cloudy" on the banner is meant to say. Octas, with a sky hidden by
    fog (-1) read as 8.
    """
    hours = _hours_after(cloud, moment)
    if hours is None:
        return None
    octas = [8.0 if v < 0 else v for v in hours]
    return float(sum(octas) / len(octas) >= 8 * OVERCAST_PERCENT / 100 - 1e-9)


def fog_label(visibility: dict[datetime, float], moment: datetime) -> float | None:
    """Was visibility under :data:`FOG_METRES` in any hour of the horizon?

    Any rather than the mean: fog is an event, and a morning that clears by
    nine was a foggy morning.
    """
    hours = _hours_after(visibility, moment)
    return None if hours is None else float(min(hours) < FOG_METRES)


def thunder_label(weather: dict[datetime, float], moment: datetime) -> float | None:
    """Was thunder reported in any hour of the horizon?

    -1 is the service's "nothing reported", which an observer on duty uses
    for an hour without significant weather — and thunder is always
    significant. It is an observation, not a gap. The series is cut at
    :data:`THUNDER_UNTIL` by :func:`observed_thunder`, so a window reaching
    past it is a gap like any other.
    """
    hours = _hours_after(weather, moment)
    return None if hours is None else float(any(int(v) in THUNDER_CODES for v in hours))


def observed_thunder(weather: dict[datetime, float]) -> dict[datetime, float]:
    """The part of a weather record that was watching for thunder, or nothing.

    Each station lost its observers on its own date, and after it every hour
    reads "nothing reported" whatever the sky did. Of the five thunder
    stations nearest the test location, the last reports came in 2015, 2016,
    2018, 2019 and 2022; counted through 2021 regardless, the held-out year
    was mostly a silence mistaken for calm, its base rate fell from 3.0% to
    0.7%, and a model that still ranked hours at AUC 0.87 scored a Brier
    skill of -0.15 against it. So a record is used up to the end of the
    month of its last thunder report, and never past :data:`THUNDER_UNTIL`.
    A station with no report at all had nobody listening.
    """
    cutoff = datetime(THUNDER_UNTIL.year, THUNDER_UNTIL.month, THUNDER_UNTIL.day, tzinfo=UTC)
    reports = [t for t, v in weather.items() if t < cutoff and int(v) in THUNDER_CODES]
    if not reports:
        return {}
    last = max(reports)
    month_after = datetime(last.year + last.month // 12, last.month % 12 + 1, 1, tzinfo=UTC)
    end = min(month_after, cutoff)
    return {t: v for t, v in weather.items() if t < end}


def under_the_fog(visibility: dict[datetime, float]) -> dict[datetime, float]:
    """A visibility record, or nothing if the station is foggy too often to be
    under the fog rather than in the cloud (:data:`MAX_FOG_SHARE`)."""
    if not visibility:
        return {}
    share = sum(v < FOG_METRES for v in visibility.values()) / len(visibility)
    return visibility if share <= MAX_FOG_SHARE else {}


LABELS = {"rain": rain_label, "sky": sky_label, "fog": fog_label, "thunder": thunder_label}
#: What a station's record must pass before it may label a target at all.
SCREENS = {"fog": under_the_fog, "thunder": observed_thunder}


def labeller(target: str, observations: dict[datetime, float]):
    """One target's label function, bound to one station's observations, or
    None where the station cannot label it (see :data:`SCREENS`)."""
    if target in SCREENS:
        observations = SCREENS[target](observations)
    return partial(LABELS[target], observations) if observations else None


# ── Samples ────────────────────────────────────────────────────────────────


@dataclass
class Samples:
    """One row per hour: every feature app.features computes, and the labels.

    ``X`` holds all of :data:`app.features.NAMES` — the model reads
    :data:`FEATURES` out of it, the rules they are compared with their own
    few — with NaN where a feature was unavailable, which is how the trees are
    told. ``y`` holds one column per target in :data:`TARGET_NAMES`: 1, 0, or
    NaN where that station does not observe it or the window had a gap.
    """

    X: np.ndarray
    moments: np.ndarray   #: UTC, as datetime64[s]
    local: list[datetime]  #: the same instants on the local wall clock
    y: np.ndarray

    def __len__(self) -> int:
        return len(self.moments)

    def take(self, keep: np.ndarray) -> Samples:
        return Samples(
            self.X[keep], self.moments[keep],
            [m for m, k in zip(self.local, keep, strict=True) if k],
            self.y[keep],
        )

    @staticmethod
    def concat(parts: list[Samples]) -> Samples:
        return Samples(
            np.concatenate([p.X for p in parts]),
            np.concatenate([p.moments for p in parts]),
            [m for p in parts for m in p.local],
            np.concatenate([p.y for p in parts]),
        )

    def column(self, name: str) -> np.ndarray:
        return self.X[:, features.NAMES.index(name)]

    def matrix(self, names: tuple[str, ...] = FEATURES) -> np.ndarray:
        return self.X[:, [features.NAMES.index(n) for n in names]]

    def labels(self, target: str) -> np.ndarray:
        return self.y[:, TARGET_NAMES.index(target)]


def build_samples(
    air: list[tuple[datetime, float, float, float]],
    labellers: dict,
    every_hour: bool = False,
) -> Samples:
    """The hourly samples for one run of readings, as the app would have seen them.

    ``air`` is ``(UTC instant, temperature, humidity, pressure)``, from a
    weather-service station or from the balcony's own export alike. It is put
    into the app's conventions first — local wall-clock time, ordered as the
    database orders it — and then, at every hour, handed to
    :func:`app.features.compute` with the pressure cycle the app would have
    learned by that day: the 90 whole days before it.

    ``labellers`` maps a target to its label function (:func:`labeller`).
    An hour no target can label is dropped, unless ``every_hour``: the
    balcony's archive keeps them, because the thunder model's level is set
    from the readings alone.
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

    label_fns = [labellers.get(name) for name in TARGET_NAMES]
    rows, moments, wall, ys = [], [], [], []
    first, last = air[0][0], air[-1][0]
    moment = (first + timedelta(days=MIN_HISTORY_DAYS)).replace(minute=0, second=0)
    while moment <= last:
        now = moment.astimezone(tz).replace(tzinfo=None)
        vector = features.compute(series, now, cycle(now.date()))
        if vector is not None:
            y = [None if fn is None else fn(moment) for fn in label_fns]
            if every_hour or any(v is not None for v in y):
                rows.append([np.nan if vector[n] is None else vector[n] for n in features.NAMES])
                moments.append(moment.replace(tzinfo=None))
                wall.append(now)
                ys.append([np.nan if v is None else v for v in y])
        moment += timedelta(hours=1)

    return Samples(
        np.array(rows, dtype=float).reshape(-1, len(features.NAMES)),
        np.array(moments, dtype="datetime64[s]"),
        wall,
        np.array(ys, dtype=float).reshape(-1, len(TARGET_NAMES)),
    )


def station_samples(
    job: tuple[str, Path, date, tuple[str, ...]],
) -> tuple[Samples, float | None]:
    """Load one weather-service station and build its samples (a worker).

    ``targets`` are the ones still short of stations that this one might
    label. The hourly records are small, so they are read first: a station
    that turns out to label none of them — an automatic one, asked only for
    thunder — costs those and nothing else, and comes back with coverage
    ``None``. Then the barometer check, and only then the years of
    ten-minute readings.
    """
    try:
        return _station_samples(job)
    except urllib.error.URLError as error:
        # An HTTPError holds the open response, which cannot be pickled back
        # to the parent; the run would die on that instead of on this.
        raise RuntimeError(f"a weather-service download failed: {error}") from None


def _station_samples(job: tuple[str, Path, date, tuple[str, ...]]) -> tuple[Samples, float | None]:
    station_id, cache, since, targets = job
    labellers = {}
    for name in targets:
        if TARGETS[name].product != "rain" and (
            fn := labeller(name, dwd.load_hourly(TARGETS[name].product, station_id, cache, since))
        ) is not None:
            labellers[name] = fn
    if not labellers and "rain" not in targets:
        return _empty(), None
    if not dwd.has_barometer(station_id, cache, MIN_COVERAGE):
        return _empty(), 0.0
    observations = dwd.load(station_id, cache, since, rain="rain" in targets)
    expected = (datetime.now(UTC).date() - since).days * 144
    coverage = len(observations.air) / expected if expected else 0.0
    if coverage < MIN_COVERAGE:
        return _empty(), coverage
    if "rain" in targets and (fn := labeller("rain", observations.rain)) is not None:
        labellers["rain"] = fn
    return build_samples(observations.air, labellers), coverage


def _empty() -> Samples:
    return Samples(np.zeros((0, len(features.NAMES))), np.array([], dtype="datetime64[s]"),
                   [], np.zeros((0, len(TARGET_NAMES))))


def claim(samples: Samples, need: dict[str, int]) -> tuple[Samples, list[str]]:
    """Count one station towards every target still short of stations that it labels.

    Its labels for a target that already has its stations are dropped, so
    each model learns from its own nearest; hours left with no label at all
    are dropped with them. ``need`` is decremented in place.
    """
    labelled = []
    y = samples.y.copy()
    for name in TARGET_NAMES:
        column = TARGET_NAMES.index(name)
        if np.isnan(y[:, column]).all():
            continue
        if need.get(name, 0) > 0:
            need[name] -= 1
            labelled.append(name)
        else:
            y[:, column] = np.nan
    kept = Samples(samples.X, samples.moments, samples.local, y)
    return kept.take(~np.isnan(y).all(axis=1)), labelled


def training_samples(
    candidates: list[dwd.Station], cache: Path, want: int, targets: tuple[str, ...],
    workers: int,
) -> tuple[Samples, dict[str, int]]:
    """Samples from the ``want`` nearest stations that can label each target.

    Candidates are tried nearest first. A station without a barometer, or
    with long gaps, is passed over for the next one. One that labels a target
    which already has its stations has those labels dropped, so every model
    learns from its own nearest. Nothing about which stations were used or
    skipped is printed (see the module docstring); only counts.
    """
    # Which stations carry each hourly product, from the service's own lists:
    # asking for a file a station never had is a wasted request and a 404.
    carried = {
        TARGETS[name].product: {s.id for s in dwd.stations(TARGETS[name].product, cache)}
        for name in targets if TARGETS[name].product != "rain"
    }
    need = dict.fromkeys(targets, want)
    parts: list[Samples] = []
    queue = list(candidates)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        while queue and any(need.values()):
            size = max(need.values())
            batch, queue = queue[:size], queue[size:]
            jobs = [
                (station.id, cache, TRAINING_SINCE, tuple(
                    name for name in targets
                    if need[name] > 0 and (
                        TARGETS[name].product == "rain"
                        or station.id in carried[TARGETS[name].product]
                    )
                ))
                for station in batch
            ]
            for samples, coverage in pool.map(station_samples, [j for j in jobs if j[3]]):
                if coverage is None:
                    continue  # observes nothing a model is still short of
                if coverage < MIN_COVERAGE or not len(samples):
                    print(f"  a candidate station was passed over ({coverage:.0%} coverage)")
                    continue
                samples, labelled = claim(samples, need)
                if labelled:
                    parts.append(samples)
                    print(f"  station {len(parts)}: {len(samples)} hours "
                          f"(labels: {', '.join(labelled)})")
    if not parts:
        raise SystemExit("no weather-service station near enough measured all three things")
    used = {name: want - need[name] for name in targets}
    print("stations per model: " + ", ".join(f"{name} {n}" for name, n in used.items()))
    return Samples.concat(parts), used


# ── Fitting and exporting ──────────────────────────────────────────────────


def fit(X: np.ndarray, y: np.ndarray) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(**GBM).fit(X, y)


def export(
    model: HistGradientBoostingClassifier, names: tuple[str, ...], threshold: float,
    metadata: dict, calibration: tuple[float, float] | None = None,
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
    exported = {
        "format": nowcast.FORMAT,
        "features": list(names),
        "base": round(float(np.ravel(model._baseline_prediction)[0]), 6),
        "trees": trees,
        "threshold": round(threshold, 3),
        "metadata": metadata,
    }
    if calibration is not None:
        exported["calibration"] = {
            "slope": round(calibration[0], 6), "intercept": round(calibration[1], 6),
        }
    return exported


def predict(model: nowcast.Model, samples: Samples) -> np.ndarray:
    return sigmoid(raw_scores(model, samples.matrix(model.features)))


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
    return model.slope * out + model.intercept


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
    calibration = exported.get("calibration") or {}
    expected = (
        calibration.get("slope", 1.0) * fitted.decision_function(X)
        + calibration.get("intercept", 0.0)
    )
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
    """The cut that maximises CSI — chosen on training data only.

    The grid starts low because fog and thunder are rare: with thunder in a
    few percent of windows, the cut that catches the most of them for the
    fewest false alarms sits well under a coin toss.
    """
    grid = np.arange(0.05, 0.80, 0.025)
    return float(grid[int(np.argmax([csi(probabilities >= g, truth) for g in grid]))])


def csi(prediction: np.ndarray, truth: np.ndarray) -> float:
    hits = int((prediction & (truth == 1)).sum())
    misses = int((~prediction & (truth == 1)).sum())
    false_alarms = int((prediction & (truth == 0)).sum())
    total = hits + misses + false_alarms
    return hits / total if total else 0.0


def score(
    probabilities: np.ndarray, truth: np.ndarray, threshold: float,
    calls: np.ndarray | None = None,
) -> dict:
    """Probabilistic and yes/no skill. ``calls`` overrides the yes/no
    decisions, for a calibrated score whose calls are the uncalibrated
    model's — the same hours, since a calibration never reorders them."""
    probabilities = np.asarray(probabilities, dtype=float)
    brier = brier_score_loss(truth, probabilities)
    climatology = brier_score_loss(truth, np.full_like(probabilities, truth.mean()))
    prediction = probabilities >= threshold if calls is None else calls
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
    """What the page claims without the model, as a 0/1 call per hour.

    This is the thing each model is asking to improve on, so it is the
    baseline that decides whether shipping is an improvement or just a
    change. Each is the hand-made rung the model replaces, evaluated by
    :mod:`app.weather` itself rather than restated here:

    * rain — the ladder's "says rain" rungs;
    * sky — the humidity test behind "Overcast and humid";
    * fog — "Fog or drizzle possible": near saturation and still wetting;
    * thunder — "Thunderstorm possible": a warm, humid summer afternoon.
    """
    rh, pct, temp = samples.column("rh"), samples.column("pct30"), samples.column("temp")
    spread, drh3 = samples.column("spread"), samples.column("drh3")
    if target == "sky":
        return (rh > weather.HUMIDITY_MUGGY).astype(float)
    out = np.zeros(len(samples))
    for i in range(len(samples)):
        temperature = None if np.isnan(temp[i]) else float(temp[i])
        dew_point = (
            None if temperature is None or np.isnan(spread[i]) else temperature - spread[i]
        )
        trend = {"delta": 0.0 if np.isnan(drh3[i]) else float(drh3[i])}
        if target == "rain":
            phrase = weather.compute_forecast(
                None if np.isnan(pct[i]) else float(pct[i]), float(rh[i]),
                temperature, dew_point, trend, moment=samples.local[i],
            )
            out[i] = float("Rain" in phrase or "Thunder" in phrase)
            continue
        f = weather.ForecastInputs.build(
            None, float(rh[i]), temperature, dew_point, trend, samples.local[i]
        )
        out[i] = float(f.convective if target == "thunder" else
                       f.near_saturation and f.humidity_rising)
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


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60, 60)))


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return float(np.log(p / (1 - p)))


def platt(z: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """The slope and intercept that best rescale log-odds ``z`` to labels ``y``."""
    fitted = LogisticRegression(C=1e6, max_iter=1000).fit(z.reshape(-1, 1), y)
    return float(fitted.coef_[0][0]), float(fitted.intercept_[0])


def can_calibrate(truth: np.ndarray) -> bool:
    """Whether a run of labelled balcony hours is enough to fit to, or to judge by."""
    return len(truth) >= MIN_ARCHIVE_HOURS and truth.sum() >= MIN_ARCHIVE_EVENTS


def out_of_fold(z: np.ndarray, truth: np.ndarray, folds: int = CALIBRATION_FOLDS) -> np.ndarray:
    """Calibrated probabilities for every hour, each from a fit that never saw it.

    The hours are in time order, and the folds are contiguous stretches of
    them: rain comes in spells, and a calibration fitted on the hours either
    side of a wet one would be scored on its own spell.
    """
    edges = np.linspace(0, len(z), folds + 1).astype(int)
    out = sigmoid(z)
    for k in range(folds):
        held = np.zeros(len(z), dtype=bool)
        held[edges[k]:edges[k + 1]] = True
        if held.all() or len(set(truth[~held].tolist())) < 2:
            continue  # nothing to fit on: that stretch stays uncalibrated
        slope, intercept = platt(z[~held], truth[~held])
        out[held] = sigmoid(slope * z[held] + intercept)
    return out


def match_level(z: np.ndarray, rate: float) -> float:
    """The shift of the log-odds ``z`` that makes their mean probability ``rate``.

    The mean probability rises with the shift, so a bisection finds it. It
    is bounded by :data:`LEVEL_BOUND`, and a result at the bound means the
    shift wanted to be larger — which :func:`gates` refuses.
    """
    low, high = -LEVEL_BOUND, LEVEL_BOUND
    for _ in range(50):
        middle = (low + high) / 2
        if float(sigmoid(z + middle).mean()) < rate:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def monthly_rates(samples: Samples, target: str) -> dict[int, float]:
    """How often each calendar month's windows held the event, over ``samples``."""
    truth = samples.labels(target)
    months = np.array([m.month for m in samples.local])
    return {m: float(truth[months == m].mean()) for m in range(1, 13) if (months == m).any()}


def recent(samples: Samples, days: int) -> Samples:
    return samples.take(samples.moments >= samples.moments.max() - np.timedelta64(days, "D"))


def describe_period(samples: Samples) -> dict:
    """The span ``samples`` cover, by their earliest and latest hour.

    Not their first and last rows: samples from several stations are
    stacked station after station, so the last row is the end of whichever
    station came last — for thunder, whose stations lost their observers in
    different years, that read as a model labelled to 2018 when it ran to 2021.
    """
    return {
        "from": samples.local[int(np.argmin(samples.moments))].strftime("%Y-%m-%d"),
        "to": samples.local[int(np.argmax(samples.moments))].strftime("%Y-%m-%d"),
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
    shipped: nowcast.Model | None = None,
) -> dict | None:
    """Score a candidate out of sample, then fit the one that would ship.

    Two tests, both on hours the evaluation fit never saw:

    * the last :data:`HOLDOUT_DAYS` of the target's labels at the training
      stations — a full year, so every season is in the score;
    * the balcony's own archive, labelled by the nearest station observing
      the same thing — the only test of whether a model fitted to
      weather-service screens survives this sensor. The evaluation fit stops
      before the archive begins, so the same weather cannot be learned at a
      neighbour and scored here.

    The model that ships is then refitted on everything, and calibrated to
    the balcony: to the archive's labels where there are enough of them, and
    for thunder, which nothing near the balcony reports any more, to how
    often the stations had it in the same months.
    """
    spec = TARGETS[target]
    print(f"\n── {target} " + "─" * (66 - len(target)))
    usable = samples.take(~np.isnan(samples.labels(target)))
    if len(usable) < 10 * MIN_ARCHIVE_HOURS:
        print(f"{len(usable)} labelled hours; leaving the shipped model alone")
        return None

    holdout_start = usable.moments.max() - np.timedelta64(HOLDOUT_DAYS, "D")
    split = holdout_start
    scored_archive = unscored = None
    if archive is not None:
        labelled = archive.take(~np.isnan(archive.labels(target)))
        if len(labelled) and len(set(labelled.labels(target).tolist())) > 1:
            scored_archive = labelled
            split = min(split, labelled.moments.min())
        elif len(labelled):
            # Labelled, and one-sided: a summer without fog. Nothing can be
            # scored on it, but how much was looked at is still worth saying.
            truth = labelled.labels(target)
            unscored = {**describe_period(labelled), "events": int(truth.sum()),
                        "base_rate": round(float(truth.mean()), 3)}

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
        z = candidate.decision_function(scored_archive.matrix())
        straight = score(sigmoid(z), truth, threshold)
        on_archive = {
            **describe_period(scored_archive),
            "events": int(truth.sum()),
            "base_rate": round(float(truth.mean()), 3),
            "skill": straight,
            "rules": score(incumbent(scored_archive, target), truth, 0.5),
        }
        rows = {target: straight, "rules": on_archive["rules"]}
        if can_calibrate(truth):
            # What ships is calibrated, so what is gated is too — scored out
            # of fold, with the yes/no calls the threshold makes either way.
            on_archive["uncalibrated"] = straight
            on_archive["skill"] = score(
                out_of_fold(z, truth), truth, threshold, calls=sigmoid(z) >= threshold
            )
            rows = {f"{target} (calibrated)": on_archive["skill"],
                    f"{target} (straight)": straight, "rules": on_archive["rules"]}
        # What is live now, on the same hours. Only to know whether the
        # candidate is an improvement on it — see gates() — and it is judged
        # with whatever calibration it shipped with.
        if shipped is not None and set(shipped.features) <= set(features.NAMES):
            on_archive["shipped"] = score(predict(shipped, scored_archive), truth, shipped.threshold)
            rows["shipped"] = on_archive["shipped"]
        print_scores(
            f"the balcony's own archive: {len(scored_archive)} hours, "
            f"{on_archive['events']} with {target} (base rate {truth.mean() * 100:.1f}%)",
            rows,
        )
    elif unscored is not None:
        print(f"the balcony's archive: {unscored['hours']} labelled hours, "
              f"{unscored['events']} with {target}; nothing to score against yet")
        on_archive = unscored
    elif spec.archive_km is not None:
        print(f"the balcony's archive has no {target} labels to be scored against")

    worth = ablations(train, test, target, skill) if measure_worth else None

    final = fit(usable.matrix(), usable.labels(target))
    final_threshold = best_threshold(
        final.predict_proba(usable.matrix())[:, 1], usable.labels(target)
    )
    calibration, level = None, None
    if on_archive is not None and "uncalibrated" in on_archive:
        window = recent(scored_archive, CALIBRATION_DAYS)
        if can_calibrate(window.labels(target)):
            calibration = platt(final.decision_function(window.matrix()), window.labels(target))
            on_archive["calibrated_on"] = describe_period(window)
            print(f"  calibration to the balcony: log-odds x {calibration[0]:.3f} "
                  f"{calibration[1]:+.3f}, fitted on {len(window)} hours")
    elif spec.calibration == "level" and archive is not None and len(archive):
        # No label, so no slope: only where the model's level sits on these
        # readings against how often the event happened in the same months
        # at the stations. That corrects a sensor that reads every summer
        # afternoon as hotter than a screen would, and it cannot reorder hours.
        window = recent(archive, CALIBRATION_DAYS)
        climate = monthly_rates(usable, target)
        expected = np.array([climate.get(m.month, np.nan) for m in window.local])
        known = ~np.isnan(expected)
        if known.sum() >= MIN_ARCHIVE_HOURS and expected[known].sum() >= MIN_ARCHIVE_EVENTS:
            z = final.decision_function(window.matrix()[known])
            shift = match_level(z, float(expected[known].mean()))
            calibration = (1.0, shift)
            level = {
                **describe_period(window),
                "said": round(float(sigmoid(z).mean()), 4),
                "climate": round(float(expected[known].mean()), 4),
                "shift": round(shift, 3),
            }
            print(f"  level on the balcony's last {len(window)} hours: the model said "
                  f"{level['said'] * 100:.1f}% on average where the stations' "
                  f"{target} rate for those months is {level['climate'] * 100:.1f}%; "
                  f"log-odds shifted {shift:+.3f}")
        else:
            print(f"  too little {target} expected in the balcony's recent months to set "
                  "the level by; it ships as fitted")
    if calibration is not None:
        # The same decision on the new scale: a monotone rescaling moves the
        # threshold with the probabilities, so the same hours fire.
        final_threshold = float(sigmoid(np.array(
            calibration[0] * logit(final_threshold) + calibration[1]
        )))
    return {
        "target": target,
        "usable": usable,
        "fitted": final,
        "threshold": final_threshold,
        "calibration": calibration,
        "skill": skill,
        "baselines": reference,
        "seasons": seasons,
        "holdout": describe_period(test),
        "archive": on_archive,
        "level": level,
        "ablations": worth,
    }


# ── Shipping ───────────────────────────────────────────────────────────────


def gates(evaluation: dict, target: str, shipped: dict | None) -> list[str]:
    """Why this candidate may not ship, or an empty list if it may.

    What a model is *for* decides what it has to beat, and each of these
    exists to give a calibrated probability, which the hand-made rung it
    replaces cannot. So the gates are probabilistic — better than quoting the base
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
    spec = TARGETS[target]
    recorded = (shipped or {}).get("metadata", {})
    reasons = []
    tests = [("held-out year", evaluation["skill"], evaluation["baselines"]["rules"],
              (recorded.get("skill") or {}).get("bss"), None)]
    archive = evaluation["archive"]
    # The archive is a test only when it holds enough of the event to judge
    # by: a Brier score over a handful of foggy hours is a coin toss.
    if (archive and "skill" in archive and archive["hours"] >= MIN_ARCHIVE_HOURS
            and archive["events"] >= MIN_ARCHIVE_EVENTS):
        tests.append(("balcony archive", archive["skill"], archive["rules"],
                      ((recorded.get("archive") or {}).get("skill") or {}).get("bss"),
                      archive.get("shipped")))
    elif spec.needs_archive:
        have = "no labelled hours" if not archive else (
            f"{archive['hours']} labelled hours, {archive['events']} with {target}"
        )
        reasons.append(
            f"not yet tested on the balcony's own readings, which a {target} model must be "
            f"(needs {MIN_ARCHIVE_HOURS} labelled hours with {MIN_ARCHIVE_EVENTS} of {target}; "
            f"has {have})"
        )
    for where, skill, rules, before, live in tests:
        # A candidate short of the bar may still replace a live model that is
        # further short of it on the same hours, provided it has some skill
        # of its own: better than what is showing, and better than nothing.
        # Without this, a model that was never tested on the balcony — the
        # first one was trained before any archive could be scored — would
        # stay up behind a gate it could not pass either.
        improves_on_live = (
            live is not None and skill["bss"] > max(live["bss"], 0.0)
        )
        if skill["bss"] < MIN_SKILL and not improves_on_live:
            reasons.append(f"not enough skill over the base rate on the {where} "
                           f"(BSS {skill['bss']:+.3f}, need {MIN_SKILL:+.2f})")
        if skill["auc"] is None or (rules["auc"] is not None and skill["auc"] < rules["auc"]):
            reasons.append(f"ranks hours no better than the rules on the {where} "
                           f"(AUC {skill['auc']} vs {rules['auc']})")
        if before is not None and skill["bss"] < before - MAX_REGRESSION:
            reasons.append(f"a clear step down from the shipped {target} model on the {where} "
                           f"(BSS {skill['bss']:+.3f} vs {before:+.3f} when it was fitted)")
    calibration = evaluation.get("calibration")
    if calibration is not None and calibration[0] <= 0:
        reasons.append("the balcony's record ranks this model's hours backwards "
                       f"(calibration slope {calibration[0]:+.3f})")
    level = evaluation.get("level")
    if level is not None and abs(level["shift"]) >= LEVEL_BOUND - 1e-3:
        reasons.append("on the balcony's readings this model's level is further from the "
                       f"stations' {target} climatology than a shift should correct "
                       f"(said {level['said'] * 100:.1f}% against {level['climate'] * 100:.1f}%)")
    return reasons


def ship(evaluation: dict, out: Path, extra: dict, force: bool) -> bool:
    """Gate one evaluated candidate and write it out if it passes."""
    target = evaluation["target"]
    spec = TARGETS[target]
    shipped = json.loads(out.read_text()) if out.exists() else None
    if shipped is not None and (
        shipped.get("format") != nowcast.FORMAT
        or shipped.get("metadata", {}).get("labels") != spec.labels
    ):
        # A model of another kind, or fitted to other labels, recorded its
        # skill on another test: there is nothing to regress from.
        shipped = None
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
            "trained_from": describe_period(usable)["from"],
            "trained_through": describe_period(usable)["to"],
            "base_rate": round(float(y.mean()), 3),
            "horizon_hours": HORIZON_HOURS,
            "labels": spec.labels,
            "trees": len(evaluation["fitted"]._predictors),
            "skill": evaluation["skill"],
            "baselines": evaluation["baselines"],
            "seasons": evaluation["seasons"],
            "holdout": evaluation["holdout"],
            "archive": evaluation["archive"],
            "level": evaluation["level"],
            "ablations": evaluation["ablations"],
            **extra,
        },
        evaluation["calibration"],
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
    targets: tuple[str, ...],
) -> tuple[Samples | None, dict[datetime, float] | None]:
    """The balcony's own readings as samples, each target labelled by its nearest station.

    Returns the rain gauge too, because the prediction log is scored against
    the same one. Either can be ``None`` — no gauge near enough, or an archive
    the endpoint would not hand over — and the run carries on without it.
    Nothing about the stations chosen is printed: only which targets found
    one within reach.
    """
    today = datetime.now(UTC).date()
    try:
        rows = fetch_readings(app_url, api_key)
    except Exception as error:
        print(f"(the balcony's archive is unavailable: {error})")
        rows = []
    since = min((instant(rows[0]).date() if rows else today) - timedelta(days=1),
                today - timedelta(days=400))

    observed: dict[str, dict[datetime, float]] = {}
    gauge = None
    for name in dict.fromkeys(("rain", *targets)):
        spec = TARGETS[name]
        if spec.archive_km is None:
            continue
        near = [
            station for station in dwd.nearest(
                dwd.stations(spec.product, cache), latitude, longitude,
                since=today - timedelta(days=30), current=today - timedelta(days=3))
            if station.km_from(latitude, longitude) <= spec.archive_km
        ]
        # The nearest one that can label it: a summit in the cloud is near,
        # and its visibility is no record of fog down here.
        for station in near:
            record = (
                dwd.load_rain(station.id, cache, since) if spec.product == "rain"
                else dwd.load_hourly(spec.product, station.id, cache, since)
            )
            if labeller(name, record) is not None:
                observed[name] = record
                break
        else:
            print(f"no weather-service station observing {spec.product} within "
                  f"{spec.archive_km:.0f} km; the {name} model is not scored on the balcony")
            continue
        if name == "rain":
            gauge = observed[name]
    if not rows:
        return None, gauge

    air = sorted(
        (instant(r), float(r["temperature"]), float(r["humidity"]), float(r["pressure"]))
        for r in rows
    )
    labellers = {
        name: fn for name, record in observed.items()
        if name in targets and (fn := labeller(name, record)) is not None
    }
    samples = build_samples(air, labellers, every_hour=True)
    counts = ", ".join(
        f"{name} {int((~np.isnan(samples.labels(name))).sum())}" for name in labellers
    )
    print(f"the balcony's archive: {len(rows)} readings, {len(samples)} hours "
          f"(labelled: {counts or 'none'})")
    return (samples if len(samples) else None), gauge


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default=os.environ.get("APP_URL", DEFAULT_APP_URL))
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"),
                        help="key for /api/weather/export; defaults to $API_KEY")
    parser.add_argument("--out-dir", type=Path, default=REPO / "app",
                        help="where the models are written, each under its own name")
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
    parser.add_argument("--target", action="append", choices=TARGET_NAMES,
                        help="fit only this model (repeatable); the others are left untouched")
    parser.add_argument("--no-ablations", action="store_true",
                        help="skip the leave-one-out refits (faster, for experiments)")
    parser.add_argument("--force", action="store_true",
                        help="write the models even if they do not clear their gates")
    args = parser.parse_args()
    targets = tuple(args.target or TARGET_NAMES)

    # The page prints what each label means, reading it from app.nowcast; the
    # labels here decide what it actually means. A mismatch would be invisible
    # on the page and wrong in the model, so it fails the run instead.
    for name, here, there in (
        ("OVERCAST_PERCENT", OVERCAST_PERCENT, nowcast.OVERCAST_PERCENT),
        ("FOG_METRES", FOG_METRES, nowcast.FOG_METRES),
    ):
        if here != there:
            raise SystemExit(f"{name} disagrees: ml/train.py says {here}, "
                             f"app/nowcast.py says {there}")

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
    samples, used = training_samples(candidates, args.cache, want, targets, args.workers)
    print(f"{len(samples)} hours in all")

    archive, gauge = (
        balcony_archive(args.app_url, args.api_key, latitude, longitude, args.cache, targets)
        if located else (None, None)
    )

    wrote = False
    years = [TRAINING_SINCE.year, today.year]
    extras = {
        "rain": {"rain_mm": RAIN_MM},
        "sky": {"overcast_percent": OVERCAST_PERCENT},
        "fog": {"fog_metres": FOG_METRES},
        "thunder": {"labelled_until": THUNDER_UNTIL.isoformat()},
    }
    for name in targets:
        out = args.out_dir / TARGETS[name].file
        evaluation = evaluate(
            samples, name, archive,
            measure_worth=TARGETS[name].measure_worth and not args.no_ablations,
            shipped=nowcast.load(out),
        )
        if evaluation is not None:
            wrote |= ship(evaluation, out,
                          {"stations": used[name], "years": years, **extras[name]}, args.force)

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
