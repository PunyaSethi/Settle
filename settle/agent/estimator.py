"""The estimator. SPEC §10.1.

`HistGradientBoostingClassifier` wrapped in `CalibratedClassifierCV` (isotonic),
predicting `P(settle | case, action, hour)`. Logistic regression on identical
features ships alongside as the interpretable baseline — and if LR wins on
calibration, LR is what ships and this report says so.

Splitting by case, not by row
-----------------------------
Rows from the same case share hidden truth: the same `true_recoverability`, the
same `payday_day`, the same debtor. Splitting by row puts sibling rows either
side of the boundary, and the model scores well on the test split by having
memorised the case rather than learned the world. Every metric comes out
optimistic and nothing warns you.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Sequence

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from settle.agent.features import FEATURE_NAMES, feature_vector
from settle.schema.action import Action
from settle.schema.observed import ObservedCase

TRAIN_FRACTION: Final[float] = 0.6
CALIBRATION_FRACTION: Final[float] = 0.2
RANDOM_STATE: Final[int] = 0


@dataclass(frozen=True)
class Split:
    """Row indices for one split, and the cases they came from."""

    name: str
    rows: list[int]
    case_ids: frozenset[str]


def split_by_case(case_ids: Sequence[str], seed: int = RANDOM_STATE) -> tuple[Split, Split, Split]:
    """Partition rows by case. EST-3 asserts no case crosses a boundary.

    Assignment is a hash of the case id, not a shuffle: the same case lands in
    the same split on every run and in every process, so a model artifact is
    reproducible without carrying a shuffle order around with it.
    """
    def bucket(case_id: str) -> float:
        digest = hashlib.blake2b(f"{seed}|{case_id}".encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") / (1 << 64)

    assignment: dict[str, str] = {}
    for case_id in set(case_ids):
        u = bucket(case_id)
        if u < TRAIN_FRACTION:
            assignment[case_id] = "train"
        elif u < TRAIN_FRACTION + CALIBRATION_FRACTION:
            assignment[case_id] = "calibration"
        else:
            assignment[case_id] = "test"

    rows: dict[str, list[int]] = {"train": [], "calibration": [], "test": []}
    for index, case_id in enumerate(case_ids):
        rows[assignment[case_id]].append(index)

    return tuple(
        Split(name, rows[name], frozenset(c for c, s in assignment.items() if s == name))
        for name in ("train", "calibration", "test")
    )


def build_matrix(rows: Sequence[tuple]) -> np.ndarray:
    return np.asarray([feature_vector(*row) for row in rows])


def fit_gbm(X: np.ndarray, y: np.ndarray, X_cal: np.ndarray, y_cal: np.ndarray):
    """Gradient boosting, isotonically calibrated on a held-out split."""
    base = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.08, max_depth=6, random_state=RANDOM_STATE
    )
    base.fit(X, y)
    # Fitted on `train`, calibrated on `calibration`. Frozen so the calibrator
    # cannot refit it on the calibration split and quietly train on the data it
    # is supposed to be held out from.
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
    calibrated.fit(X_cal, y_cal)
    return calibrated


def fit_logistic(X: np.ndarray, y: np.ndarray, X_cal: np.ndarray, y_cal: np.ndarray):
    """The interpretable baseline, on identical features."""
    base = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, random_state=RANDOM_STATE),
    )
    base.fit(X, y)
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
    calibrated.fit(X_cal, y_cal)
    return calibrated


class Estimator:
    """A fitted model, queried the way the policy will query it."""

    def __init__(self, model, name: str) -> None:
        self.model = model
        self.name = name

    def predict_proba(
        self, case: ObservedCase, action: Action, tick: int, last_attempt_tick: int | None = None
    ) -> float:
        """P(settle | case, action, hour). SPEC §10.1."""
        X = np.asarray([feature_vector(case, action, tick, last_attempt_tick)])
        return float(self.model.predict_proba(X)[0, 1])

    def predict_pairs(
        self, case: ObservedCase, actions: Sequence[Action], tick: int,
        last_attempt_tick: int | None = None,
    ) -> np.ndarray:
        """P(settle) for many actions on one case, in a single call."""
        X = np.asarray([feature_vector(case, a, tick, last_attempt_tick) for a in actions])
        return self.model.predict_proba(X)[:, 1]

    def predict_many(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(X)[:, 1]

    def artifact_hash(self) -> str:
        """A content hash of the fitted model. EST-4 asserts it is stable."""
        import pickle

        return hashlib.sha256(pickle.dumps(self.model)).hexdigest()


def constant_rate_baseline(y_train: np.ndarray) -> float:
    """Predict the training base rate for everything. EST-8's floor.

    A model that cannot beat this has learned nothing the base rate did not
    already say, and the problem is upstream of the model.
    """
    return float(y_train.mean())


assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES)), "duplicate feature name"


# ---------------------------------------------------------------------------
# Uplift calibration. SPEC §10.1 (A84).
# ---------------------------------------------------------------------------

def uplift_calibration(
    model, rows: Sequence[tuple], X: np.ndarray, y: np.ndarray,
    test_rows: Sequence[int], bins: int = 10, p0_bins: int = 20,
) -> dict:
    """Calibration of the *difference*, which is what §10.2 actually uses.

    Uplift is not observable per row — one case has one outcome, not two — so it
    is estimated the standard way: bin by predicted uplift, and compare the
    treated rate against a control rate matched on `p_settle(do_nothing)`.
    Matching on `p_0` matters because treated and control rows are not
    exchangeable: EXPLORE acts more often on cases it has more options for.

    This is an estimate with real assumptions, not a measurement. It is reported
    as one.
    """
    from settle.schema.action import DoNothing

    idx = np.asarray(test_rows)
    p_action = model.predict_many(X[idx])
    p0 = np.asarray(
        [model.predict_proba(rows[i][0], DoNothing(), rows[i][2], rows[i][3]) for i in idx]
    )
    uplift = p_action - p0
    outcomes = y[idx]
    treated = np.asarray([rows[i][1].type.value != "do_nothing" for i in idx])

    # Control rate per p_0 bucket, from the do_nothing rows only.
    control_rate: dict[int, float] = {}
    p0_bucket = np.minimum((p0 * p0_bins).astype(int), p0_bins - 1)
    for bucket in range(p0_bins):
        members = outcomes[(~treated) & (p0_bucket == bucket)]
        if len(members):
            control_rate[bucket] = float(members.mean())

    counterfactual = np.asarray(
        [control_rate.get(int(b), float(outcomes[~treated].mean())) for b in p0_bucket]
    )

    table, total, error = [], 0, 0.0
    t_uplift, t_outcome, t_counter = uplift[treated], outcomes[treated], counterfactual[treated]
    if len(t_uplift):
        edges = np.quantile(t_uplift, np.linspace(0, 1, bins + 1))
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (t_uplift >= lo) & (t_uplift <= hi if i == bins - 1 else t_uplift < hi)
            n = int(mask.sum())
            if not n:
                continue
            predicted = float(t_uplift[mask].mean())
            realised = float(t_outcome[mask].mean() - t_counter[mask].mean())
            table.append({"bin": i, "n": n, "predicted": predicted, "realised": realised})
            total += n
            error += n * abs(predicted - realised)

    return {
        "ece_uplift": error / total if total else 0.0,
        "brier_uplift": float(
            np.mean(((t_outcome - t_counter) - t_uplift) ** 2) if len(t_uplift) else 0.0
        ),
        "n_treated": int(treated.sum()),
        "table": table,
    }
