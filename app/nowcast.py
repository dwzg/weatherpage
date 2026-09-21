"""The learned nowcasts: probabilities, trained offline, served locally.

This module holds the machinery for both fitted models and the constants
that say what they mean. One predicts rain, the other cloud; they share a
feature vector, a file format and every line of code below, and differ only
in the labels they were fitted against.

The rain model is the second of the two predictions on the dashboard, and
deliberately a different kind of thing from the one in :mod:`app.weather`.
That one is a handful of thresholds a person can read and argue with. This
one is a logistic regression fitted to the station's own history against
observed rainfall, and it answers a narrower question with a number: how
likely is measurable rain in the next six hours.

The sky model answers the other half of the outlook. The threshold ladder
asserts "Fair and settled" or "Overcast and humid" from humidity alone, with
nothing measured behind it; fitted against observed cloud cover, that claim
becomes one the station has evidence for. Where no label exists — fog, and
the convective afternoon — the ladder's hand-made rungs stay exactly as they
were, because no amount of fitting invents ground truth.

Nothing here trains, fits or downloads. Training happens in CI (``ml/train.py``),
which writes :data:`MODEL_PATH` — a small JSON file of feature names, scaling
and coefficients that ships inside the image. Serving a logistic regression is
a dot product and a sigmoid, so the container needs no numpy, no scikit-learn
and no network.

Why a probability rather than another phrase: scored walk-forward over the
station's history, the model matched a persistence baseline on the yes/no
call (CSI 0.438 against 0.446) while beating it badly as a probability
(Brier 0.135 against 0.171, AUC 0.829 against 0.754). Persistence needs to
know whether it actually rained in the last six hours, which a station with
no rain gauge cannot; matching it from pressure and humidity alone is the
point. The value it adds over the rules is calibration, so it is shown as a
percentage and not collapsed back into a phrase.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the trained model lives inside the image. Written by ml/train.py.
MODEL_PATH = Path(__file__).parent / "model.json"

#: The second fitted model, predicting cloud rather than rain. Same shape,
#: same features, same loader — only the labels it was fitted against differ,
#: so everything below serves both. It is optional in a way the rain model is
#: not: until ``ml/train.py`` has shipped one, the outlook falls back to the
#: threshold ladder in :mod:`app.weather`, which is what it always was.
SKY_MODEL_PATH = Path(__file__).parent / "sky_model.json"

#: How far ahead the model predicts, and what counts as rain. Both are baked
#: into the training labels; they are here so the UI can say what it means.
HORIZON_HOURS = 6
RAIN_MM = 0.2

#: What the sky model calls overcast: mean cloud cover over the next
#: :data:`HORIZON_HOURS` at or above this percentage. Baked into its labels.
OVERCAST_PERCENT = 80


@dataclass(frozen=True)
class Contribution:
    """One feature's share of a single prediction.

    ``weight`` is in log-odds, which is the unit the model actually adds in:
    the intercept plus every weight is the logit the sigmoid squashes. That
    makes the set of them an exact decomposition of one prediction rather
    than an illustration of it, which is the only reason it is worth showing
    on the page.
    """

    name: str
    value: float        #: the feature as it was measured
    standardised: float #: standard deviations from the training mean
    weight: float       #: log-odds this feature contributed


@dataclass(frozen=True)
class Model:
    """A fitted logistic regression, as it comes out of training."""

    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coef: tuple[float, ...]
    intercept: float
    threshold: float
    metadata: dict

    def contributions(self, values: dict[str, float]) -> tuple[Contribution, ...]:
        """Break one prediction into what each feature added to the log-odds."""
        out = []
        for name, mean, scale, coef in zip(
            self.features, self.mean, self.scale, self.coef, strict=True
        ):
            z = (values[name] - mean) / (scale or 1.0)
            out.append(Contribution(name, values[name], z, coef * z))
        return tuple(out)

    def logit(self, values: dict[str, float]) -> float:
        """The log-odds of rain: the intercept plus every feature's weight."""
        return self.intercept + sum(c.weight for c in self.contributions(values))

    def predict(self, values: dict[str, float]) -> float:
        """Probability of rain, from a feature dict. Standardise, dot, squash.

        Routed through :meth:`contributions` on purpose: the breakdown the
        page shows is then the same arithmetic as the number beside it, so
        the two cannot drift into disagreeing.
        """
        z = self.logit(values)
        return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


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

    try:
        model = Model(
            features=tuple(raw["features"]),
            mean=tuple(float(v) for v in raw["mean"]),
            scale=tuple(float(v) for v in raw["scale"]),
            coef=tuple(float(v) for v in raw["coef"]),
            intercept=float(raw["intercept"]),
            threshold=float(raw.get("threshold", 0.5)),
            metadata=dict(raw.get("metadata", {})),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("nowcast model at %s is malformed; skipping", path, exc_info=True)
        return None

    widths = {len(model.features), len(model.mean), len(model.scale), len(model.coef)}
    if len(widths) != 1 or not model.features:
        log.warning("nowcast model at %s has mismatched feature arrays; skipping", path)
        return None
    return model


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


#: Keyed by the feature names ml/train.py writes. The labels are English
#: because the English string is the message id (see app.i18n).
FEATURE_FORMATS: dict[str, FeatureFormat] = {
    "pct30": FeatureFormat("Pressure rank, 30 days", "%", 0, factor=100.0),
    "pct7": FeatureFormat("Pressure rank, 7 days", "%", 0, factor=100.0),
    "rh": FeatureFormat("Humidity", "%", 0),
    "rh_max6": FeatureFormat("Peak humidity, 6 h", "%", 0),
    "drh3": FeatureFormat("Humidity change, 3 h", "pp", 1, sign=True),
    "drh6": FeatureFormat("Humidity change, 6 h", "pp", 1, sign=True),
    "spread": FeatureFormat("Dew-point spread", "°C", 1),
    "dp6": FeatureFormat("Pressure change, 6 h", "hPa", 1, sign=True),
    "dp12": FeatureFormat("Pressure change, 12 h", "hPa", 1, sign=True),
    "temp": FeatureFormat("Temperature", "°C", 1),
}

def describe_feature(name: str) -> FeatureFormat:
    """How to print one feature.

    A name this code does not know — a feature a newer model was trained with
    — prints as itself with a couple of decimals, rather than dropping the row
    and making the breakdown stop adding up.
    """
    known = FEATURE_FORMATS.get(name)
    return known if known is not None else FeatureFormat(name, digits=2)
