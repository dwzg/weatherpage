"""The parts of the retraining job that can be checked without a network.

The trainer runs weekly in CI with numpy and scikit-learn installed from
ml/requirements.txt; the ordinary test environment has neither, so these
skip there. They run for anyone working on the model, which is when they
are the ones that matter.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("sklearn")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml import train


def samples(count: int = 1500, seed: int = 7) -> list[dict]:
    """Hours where one feature decides the label and the rest are noise.

    ``rh`` carries the answer, ``temp`` is pure noise. A leave-one-out that
    cannot tell those two apart is not measuring anything.
    """
    import random

    rng = random.Random(seed)
    start = datetime(2026, 1, 1)
    rows = []
    for i in range(count):
        features = {f: rng.gauss(0, 1) for f in train.FEATURES}
        wet = float(features["rh"] > 0.4)
        rows.append({
            "dt": start + timedelta(hours=i),
            "features": features,
            "y": {"rain": wet},
        })
    return rows


class TestLeaveOneOut:
    def setup_method(self):
        self.rows = samples()
        probabilities, truth, threshold, _ = train.walk_forward(self.rows, "rain")
        self.skill = train.score(probabilities, truth, threshold)
        self.worth = train.ablations(self.rows, "rain", self.skill)

    def test_every_feature_gets_a_row(self):
        assert {row["feature"] for row in self.worth} == set(train.FEATURES)

    def test_the_feature_carrying_the_label_is_worth_the_most(self):
        assert self.worth[0]["feature"] == "rh"
        assert self.worth[0]["brier_cost"] > 0.05, self.worth[0]

    def test_a_feature_that_is_only_noise_costs_nothing_to_drop(self):
        """The question the whole measurement exists for: a coefficient on a
        column that carries nothing should be visible as carrying nothing."""
        noise = next(row for row in self.worth if row["feature"] == "temp")
        assert abs(noise["brier_cost"]) < 0.01, noise

    def test_rows_are_ordered_by_what_they_cost(self):
        costs = [row["brier_cost"] for row in self.worth]
        assert costs == sorted(costs, reverse=True)

    def test_dropping_a_feature_refits_without_it(self):
        """Not merely zeroing it: the remaining coefficients have to move."""
        kept = tuple(f for f in train.FEATURES if f != "rh")
        probabilities, _, _, _ = train.walk_forward(self.rows, "rain", kept)
        full, _, _, _ = train.walk_forward(self.rows, "rain")
        assert len(probabilities) == len(full)
        assert not (probabilities == full).all()
