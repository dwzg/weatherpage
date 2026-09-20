"""The learned rain nowcast: a probability, trained offline, served locally.

This is the second of the two predictions on the dashboard, and deliberately
a different kind of thing from the one in :mod:`app.weather`. That one is a
handful of thresholds a person can read and argue with. This one is a
logistic regression fitted to the station's own history against observed
rainfall, and it answers a narrower question with a number: how likely is
measurable rain in the next six hours.

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

#: How far ahead the model predicts, and what counts as rain. Both are baked
#: into the training labels; they are here so the UI can say what it means.
HORIZON_HOURS = 6
RAIN_MM = 0.2


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

    def predict(self, values: dict[str, float]) -> float:
        """Probability of rain, from a feature dict. Standardise, dot, squash."""
        z = self.intercept
        for name, mean, scale, coef in zip(
            self.features, self.mean, self.scale, self.coef, strict=True
        ):
            z += coef * (values[name] - mean) / (scale or 1.0)
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
