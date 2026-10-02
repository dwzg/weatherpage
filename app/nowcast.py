"""The learned nowcasts: probabilities, trained offline, served locally.

This module holds the machinery for both fitted models and the constants
that say what they mean. One predicts rain, the other cloud; they share a
feature vector, a file format and every line of code below, and differ only
in the labels they were fitted against.

The rain model is the second of the two predictions on the dashboard, and
deliberately a different kind of thing from the one in :mod:`app.weather`.
That one is a handful of thresholds a person can read and argue with. This
one is an ensemble of gradient-boosted trees, and it answers a narrower
question with a number: how likely is measurable rain here in the next six
hours.

It is not fitted to this balcony's own archive, which is one summer long and
would teach any model that "cool" means "dry". It is fitted to years of
ten-minute observations from the weather service's stations nearest the
balcony — the same three measurements this sensor makes, each labelled by the
rain gauge standing beside it — and then scored against this balcony's own
readings before it is allowed to ship (see ``ml/train.py``).

Why trees rather than the logistic regression this used to be: measured on
the last two years at eight stations, the regression over the same signals
scored a Brier skill of 0.20 and the trees 0.25, because the signals matter
in combination. A sharp temperature drop at saturation means rain under any
barometer; a linear model can only add the two up.

Nothing here trains, fits or downloads. Training happens in CI, which writes
:data:`MODEL_PATH` — the trees as plain arrays in JSON — and serving them is a
walk down each tree and a sum, so the container needs no numpy, no
scikit-learn and no network.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the trained model lives inside the image. Written by ml/train.py.
MODEL_PATH = Path(__file__).parent / "model.json"

#: The other fitted models, one per claim the outlook makes besides rain: an
#: overcast sky, fog, thunder. Same shape, same features, same loader — only
#: the labels they were fitted against differ, so everything below serves
#: all four. They are optional in a way the rain model is not: until
#: ``ml/train.py`` has shipped one, its rung of the outlook falls back to the
#: hand-made thresholds in :mod:`app.weather`, which is what it always was.
SKY_MODEL_PATH = Path(__file__).parent / "sky_model.json"
FOG_MODEL_PATH = Path(__file__).parent / "fog_model.json"
THUNDER_MODEL_PATH = Path(__file__).parent / "thunder_model.json"

#: How the deployed model has actually done, written weekly by ml/train.py
#: from the prediction log. Absent until there are enough scored hours, and
#: absent is the honest state — a verification is a claim about a record, and
#: for the first months there is no record to make a claim about.
VERIFICATION_PATH = Path(__file__).parent / "verification.json"

#: How far ahead the model predicts, and what counts as rain. Both are baked
#: into the training labels; they are here so the UI can say what it means.
HORIZON_HOURS = 6
RAIN_MM = 0.2

#: Why the labels are rain gauges and not a reanalysis, measured rather than
#: assumed: the same trees and signals, trained on 2014 to September 2024 at
#: eight weather-service stations and scored on the two years after, ranked
#: wet hours above dry ones as well against each station's own gauge as
#: against the 25 km ERA5 reanalysis at the same point. An earlier finding
#: that point rain was out of reach (AUC 0.715 against 0.831) had asked it of
#: one summer, ten signals and a weighted sum, scored against a 2 km forecast
#: model rather than any gauge.
LABEL_CHOICE = {"auc_gauge": 0.842, "auc_reanalysis": 0.837}

#: What the sky model calls overcast: mean cloud cover over the next
#: :data:`HORIZON_HOURS` at or above this percentage. Baked into its labels.
OVERCAST_PERCENT = 80

#: What the fog model calls fog: visibility under this many metres in any
#: hour of the next :data:`HORIZON_HOURS`. Baked into its labels.
FOG_METRES = 1000

#: A station foggy for more than this share of its hours is a summit in the
#: cloud, and does not label fog. Baked into the fog model's labels; mirrors
#: ml/train.py, which the tests hold to it.
MAX_FOG_SHARE = 0.15


@dataclass(frozen=True)
class Contribution:
    """One feature's share of a single prediction, in log-odds.

    The model is a sum of trees, and each tree walks one path from its root
    to a leaf. Every step down that path is a split on one feature, and it
    moves the tree's expected output from the node's value to the child's:
    that move is credited to the feature that made the split. Summed over
    every step of every tree, the credits plus the model's expected output
    are exactly the logit the sigmoid squashes — the leaf values telescope —
    so the set of them is a decomposition of this prediction rather than an
    illustration of it, which is the only reason it is worth printing.

    There is no fitted coefficient to show beside it, as there was when the
    model was a logistic regression: a tree's response to a signal depends
    on the other signals, which is the whole point of using trees.
    """

    name: str
    value: float | None  #: the feature as it was measured; None if unavailable
    effect: float        #: log-odds this feature moved the prediction by, now


@dataclass(frozen=True)
class Tree:
    """One fitted tree, as parallel arrays indexed by node.

    ``feature`` is -1 at a leaf. A reading goes left when it is at or below
    the node's threshold, and a missing one goes the way training sent the
    missing values (``missing_left``). ``value`` is the tree's expected
    output below each node — the leaf value at a leaf, and the
    training-weighted mean of the leaves beneath an internal node — which is
    what :class:`Contribution` measures the steps of a path against.
    """

    feature: tuple[int, ...]
    threshold: tuple[float, ...]
    left: tuple[int, ...]
    right: tuple[int, ...]
    missing_left: tuple[bool, ...]
    value: tuple[float, ...]

    def path(self, row: Sequence[float | None]) -> list[int]:
        """The nodes a reading visits, root first, leaf last."""
        node, visited = 0, [0]
        while self.feature[node] >= 0:
            x = row[self.feature[node]]
            if x is None:
                node = self.left[node] if self.missing_left[node] else self.right[node]
            else:
                node = self.left[node] if x <= self.threshold[node] else self.right[node]
            visited.append(node)
        return visited

    def is_sound(self, width: int) -> bool:
        """Every array the same length, every split on a known feature, and
        every child after its parent — which is what guarantees a walk ends."""
        n = len(self.feature)
        if not n or any(
            len(a) != n
            for a in (self.threshold, self.left, self.right, self.missing_left, self.value)
        ):
            return False
        for node in range(n):
            f = self.feature[node]
            if f < 0:
                continue
            if f >= width or not (node < self.left[node] < n and node < self.right[node] < n):
                return False
        return True


@dataclass(frozen=True)
class Model:
    """A fitted ensemble of trees, as it comes out of training.

    Serving it is a walk down each tree and a sum: no numpy, no
    scikit-learn. ``ml/train.py`` checks on every run that this walk
    reproduces scikit-learn's own output for the model it is about to ship,
    and refuses to write one that does not.
    """

    features: tuple[str, ...]
    #: The raw score before any tree has spoken.
    base: float
    trees: tuple[Tree, ...]
    threshold: float
    metadata: dict
    #: The calibration to this balcony: the trees' log-odds are multiplied by
    #: ``slope`` and shifted by ``intercept``. The trees are fitted to
    #: weather-service screens, and on this sensor they rank hours well but
    #: run high, so ml/train.py fits these two numbers against the balcony's
    #: own record. Identity when the file carries none. A monotone rescaling:
    #: it changes how sure the model sounds, never which hour it thinks wetter.
    slope: float = 1.0
    intercept: float = 0.0

    @property
    def expected(self) -> float:
        """The starting point: the log-odds before any signal has moved it.

        The model's average output over its training data, calibrated, which
        is where the breakdown on the page begins and every contribution is
        measured from.
        """
        return self.slope * (self.base + sum(tree.value[0] for tree in self.trees)) + self.intercept

    def _row(self, values: dict[str, float | None]) -> list[float | None]:
        return [values.get(name) for name in self.features]

    def contributions(self, values: dict[str, float | None]) -> tuple[Contribution, ...]:
        """Break one prediction into what each feature added to the log-odds.

        Each step is scaled by the calibration's slope, as the sum it is part
        of is: the starting point plus these is then still exactly the
        calibrated logit.
        """
        row = self._row(values)
        effect = [0.0] * len(self.features)
        for tree in self.trees:
            path = tree.path(row)
            for parent, child in itertools.pairwise(path):
                effect[tree.feature[parent]] += tree.value[child] - tree.value[parent]
        return tuple(
            Contribution(name, row[i], self.slope * effect[i])
            for i, name in enumerate(self.features)
        )

    def logit(self, values: dict[str, float | None]) -> float:
        """The log-odds of rain: the starting point plus every feature's effect."""
        return self.expected + sum(c.effect for c in self.contributions(values))

    def raw(self, values: dict[str, float | None]) -> float:
        """The same log-odds summed leaf by leaf, as scikit-learn computes it.

        Equal to :meth:`logit` up to rounding. Kept separate because it is
        what the trainer's self-check compares against scikit-learn — the
        breakdown has to agree with *this*, and this with the library, once
        both are put through the same calibration.
        """
        row = self._row(values)
        trees = self.base + sum(tree.value[tree.path(row)[-1]] for tree in self.trees)
        return self.slope * trees + self.intercept

    def predict(self, values: dict[str, float | None]) -> float:
        """Probability of rain, from a feature dict.

        Routed through :meth:`contributions` on purpose: the breakdown the
        page shows is then the same arithmetic as the number beside it, so
        the two cannot drift into disagreeing.
        """
        z = self.logit(values)
        return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


#: The only model format this code serves. A file in any other — the
#: logistic regressions shipped before the trees — is skipped like any other
#: unreadable model, and the page carries on without a nowcast.
FORMAT = "trees"


def load(path: Path | None = None) -> Model | None:
    """Load the shipped model, or ``None`` if there isn't a usable one.

    A missing or malformed model is not fatal: the dashboard simply does not
    show a nowcast, and the rule-based forecast carries on. A model file is
    the one part of this app that arrives by automation, so it is treated as
    data that might be wrong rather than as code that must be right.
    """
    path = path or MODEL_PATH
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("nowcast model at %s could not be read; skipping", path, exc_info=True)
        return None

    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        log.warning("nowcast model at %s is not in the %r format; skipping", path, FORMAT)
        return None
    try:
        model = Model(
            features=tuple(str(name) for name in raw["features"]),
            base=float(raw["base"]),
            trees=tuple(
                Tree(
                    feature=tuple(int(v) for v in tree["feature"]),
                    threshold=tuple(float(v) for v in tree["threshold"]),
                    left=tuple(int(v) for v in tree["left"]),
                    right=tuple(int(v) for v in tree["right"]),
                    missing_left=tuple(bool(v) for v in tree["missing_left"]),
                    value=tuple(float(v) for v in tree["value"]),
                )
                for tree in raw["trees"]
            ),
            threshold=float(raw.get("threshold", 0.5)),
            metadata=dict(raw.get("metadata", {})),
            slope=float((raw.get("calibration") or {}).get("slope", 1.0)),
            intercept=float((raw.get("calibration") or {}).get("intercept", 0.0)),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        log.warning("nowcast model at %s is malformed; skipping", path, exc_info=True)
        return None

    if not model.features or not model.trees or not all(
        tree.is_sound(len(model.features)) for tree in model.trees
    ):
        log.warning("nowcast model at %s has an inconsistent tree; skipping", path)
        return None
    # A slope at or below zero would turn the ranking upside down, which no
    # calibration should do: the file is wrong, whatever wrote it.
    if not (model.slope > 0 and math.isfinite(model.slope) and math.isfinite(model.intercept)):
        log.warning("nowcast model at %s has an impossible calibration; skipping", path)
        return None
    return model


#: Width of a reliability bin. Ten over [0, 1]: fine enough to show a curve
#: bending, coarse enough that each holds a countable number of hours.
RELIABILITY_BIN = 0.1

#: A bin thinner than this is shown with its count rather than hidden — the
#: reader can see how little is behind it — but nothing is inferred from it.
RELIABILITY_MIN_BIN = 5


def reliability_bins(pairs: Sequence[tuple[float, float]]) -> list[dict]:
    """Predicted probability against observed frequency, in bins.

    ``pairs`` is ``(probability, outcome)`` per scored hour, outcome being 1
    or 0. This is the whole question a probability makes of itself: when it
    said 40%, did it rain about 40% of the time?

    Lives here rather than in ``ml/train.py``, which is what writes it, for
    two reasons. It is the shape the page renders, so the page's own package
    should define it; and it is pure arithmetic, so it is testable in the
    ordinary suite without numpy or scikit-learn, which the image
    deliberately does not carry.

    Empty bins are dropped rather than reported as zeroes, which would draw
    a curve through hours that never happened.
    """
    bins = []
    count = round(1.0 / RELIABILITY_BIN)
    for index in range(count):
        low = index * RELIABILITY_BIN
        high = low + RELIABILITY_BIN
        inside = [
            (p, y) for p, y in pairs
            # The top bin takes 1.0 itself, so a certain forecast is counted.
            if p >= low and (p < high if index < count - 1 else p <= high)
        ]
        if not inside:
            continue
        bins.append({
            "from": round(low, 2),
            "to": round(high, 2),
            "hours": len(inside),
            "predicted": round(sum(p for p, _ in inside) / len(inside), 3),
            "observed": round(sum(y for _, y in inside) / len(inside), 3),
            "thin": len(inside) < RELIABILITY_MIN_BIN,
        })
    return bins


def load_verification(path: Path | None = None) -> dict | None:
    """The live verification, or ``None`` if there isn't a usable one.

    Treated exactly like the model file: written by automation, so read as
    data that might be wrong or missing rather than as something that must be
    there. A malformed one costs the page a section, not the page.
    """
    path = path or VERIFICATION_PATH
    try:
        loaded = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("verification at %s could not be read; skipping", path, exc_info=True)
        return None
    if not isinstance(loaded, dict) or not loaded.get("hours"):
        return None
    return loaded


def describe(probability: float, threshold: float) -> str:
    """A short word for a probability, for the label beside the percentage."""
    if probability >= max(threshold, 0.6):
        return "likely"
    if probability >= threshold:
        return "possible"
    if probability >= threshold / 2:
        return "unlikely"
    return "not expected"


def describe_sky(probability: float) -> str:
    """A short word for the sky probability.

    Deliberately not :func:`describe`: that one grades how likely an *event*
    is, and cloud is not an event. The bands are the ones
    :func:`app.weather.sky_band` composes the outlook from, so the word beside
    the percentage and the phrase on the banner cannot disagree.
    """
    from . import weather

    return {
        weather.SKY_OVERCAST: "mostly cloudy",
        weather.SKY_MIXED: "some cloud",
        weather.SKY_CLEAR: "mostly clear",
    }[weather.sky_band(probability)]


# ── Describing the features to a reader ────────────────────────────────────
#
# The feature *names* come out of model.json, which is data written by CI, so
# nothing here may assume a particular set of them: an unknown name falls back
# to printing itself. What each one means is fixed by
# ``services.nowcast_features()``, which is the only place the vector is built.


@dataclass(frozen=True)
class FeatureFormat:
    """How one feature is written on the page.

    ``factor`` exists for the pressure ranks, which the model carries as a
    fraction and the page shows as a percentage. The server applies it before
    sending the value, so the browser and the render format the same number.
    """

    label: str
    unit: str = ""
    digits: int = 1
    factor: float = 1.0
    sign: bool = False


#: Keyed by the feature names :mod:`app.features` computes. The labels are
#: English because the English string is the message id (see app.i18n).
FEATURE_FORMATS: dict[str, FeatureFormat] = {
    "rh": FeatureFormat("Humidity", "%", 0),
    "rh_max1": FeatureFormat("Peak humidity, 1 h", "%", 0),
    "rh_max3": FeatureFormat("Peak humidity, 3 h", "%", 0),
    "sat3": FeatureFormat("Time saturated, 3 h", "%", 0, factor=100.0),
    "temp": FeatureFormat("Temperature", "°C", 1),
    "td": FeatureFormat("Dew point", "°C", 1),
    "spread": FeatureFormat("Dew-point spread", "°C", 1),
    "dT1": FeatureFormat("Temperature change, 1 h", "°C", 1, sign=True),
    "dT3": FeatureFormat("Temperature change, 3 h", "°C", 1, sign=True),
    "dT24": FeatureFormat("Temperature change, 24 h", "°C", 1, sign=True),
    "dtd3": FeatureFormat("Dew-point change, 3 h", "°C", 1, sign=True),
    "drh3": FeatureFormat("Humidity change, 3 h", "pp", 1, sign=True),
    "pct30": FeatureFormat("Pressure rank, 30 days", "%", 0, factor=100.0),
    "pct7": FeatureFormat("Pressure rank, 7 days", "%", 0, factor=100.0),
    "dp1": FeatureFormat("Pressure change, 1 h", "hPa", 1, sign=True),
    "dp3": FeatureFormat("Pressure change, 3 h", "hPa", 1, sign=True),
    "dp6": FeatureFormat("Pressure change, 6 h", "hPa", 1, sign=True),
    "dp12": FeatureFormat("Pressure change, 12 h", "hPa", 1, sign=True),
    "t_range3": FeatureFormat("Temperature swing, 3 h", "°C", 1),
    "t_range6": FeatureFormat("Temperature swing, 6 h", "°C", 1),
    "t_range12": FeatureFormat("Temperature swing, 12 h", "°C", 1),
    "t_range24": FeatureFormat("Temperature swing, 24 h", "°C", 1),
    "rh_range6": FeatureFormat("Humidity swing, 6 h", "pp", 0),
    "rh_min24": FeatureFormat("Lowest humidity, 24 h", "%", 0),
    "hour": FeatureFormat("Hour of the day", "h", 0),
}


def describe_feature(name: str) -> FeatureFormat:
    """How to print one feature.

    A name this code does not know — a feature a newer model was trained with
    — prints as itself with a couple of decimals, rather than dropping the row
    and making the breakdown stop adding up.
    """
    known = FEATURE_FORMATS.get(name)
    return known if known is not None else FeatureFormat(name, digits=2)
