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
from pathlib import Path
from typing import Final, Sequence

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from settle.agent.features import FEATURE_NAMES, feature_vector
from settle.policy.params import POLICY_PARAMS
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


def calibrate(base, X_cal: np.ndarray, y_cal: np.ndarray, method: str = "isotonic"):
    """Post-hoc calibration on a held-out split.

    Frozen, so the calibrator cannot refit the base on the calibration split and
    quietly train on the data it is supposed to be held out from.
    """
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method=method)
    calibrated.fit(X_cal, y_cal)
    return calibrated


def fit_gbm(
    X: np.ndarray, y: np.ndarray, X_cal: np.ndarray, y_cal: np.ndarray, calibrated: bool = True
):
    """Gradient boosting, isotonically calibrated on a held-out split by default.

    `calibrated=False` returns the base fit. That is a real option rather than a
    debugging convenience — see `uplift_resolution` and A92.
    """
    base = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.08, max_depth=6, random_state=RANDOM_STATE
    )
    base.fit(X, y)
    return calibrate(base, X_cal, y_cal) if calibrated else base


def fit_logistic(
    X: np.ndarray, y: np.ndarray, X_cal: np.ndarray, y_cal: np.ndarray, calibrated: bool = True
):
    """The interpretable baseline, on identical features."""
    base = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, random_state=RANDOM_STATE),
    )
    base.fit(X, y)
    return calibrate(base, X_cal, y_cal) if calibrated else base


# --- speed (OQ-49) ---------------------------------------------------------
#
# `HistGradientBoostingClassifier.predict_proba` costs ~3.5 ms whether it is
# handed one row or fifteen: the time goes on walking 200 boosting iterations in
# Python, not on the rows. OURS asks ~29 times per case, so a 10,000-case run
# spent ~25 minutes almost entirely inside that fixed overhead — unusable for
# the D4 sweep and impossible in a demo.
#
# So the fix is fewer calls, not fewer rows. Two things get us there:
#
#   1. a memo, so a row is never scored twice; and
#   2. warming — on a miss, score the same actions at every future decision tick
#      as well, in the one call we are already paying for. The runner's cadence
#      is a day, so an untouched case's whole remaining horizon costs one call.
#
# The memo is keyed on `(action, tick, last_attempt_tick)` within one case,
# which is exactly what determines the feature row: `feature_row` reads the case,
# the action, the tick and the last attempt, and nothing else. Keying on a hash
# of the row itself would be equivalent but slower, because it would mean
# building the row — the expensive part — before discovering we already had it.
#
# This is an optimisation and nothing more. EST-10 asserts a warmed estimator
# returns bit-identical probabilities to a cold one.
DECISION_CADENCE_HOURS: Final[int] = int(POLICY_PARAMS["decision_cadence_hours"])
DECISION_HORIZON_H: Final[int] = int(POLICY_PARAMS["action_grid.max_horizon_h"])


class Estimator:
    """A fitted model, queried the way the policy will query it."""

    def __init__(self, model, name: str, warm: bool = True) -> None:
        self.model = model
        self.name = name
        self.warm = warm
        # Scoped to one case and dropped when the caller moves to the next, so
        # a 10,000-case run holds one case's grid rather than the whole batch.
        self._cache_case: ObservedCase | None = None
        self._cache: dict[tuple, float] = {}
        # Reported per run: a cache that never hits is a cache that is lying
        # about why the run got faster.
        self.calls = 0
        self.rows_scored = 0
        self.hits = 0

    # -- the memo ----------------------------------------------------------

    def _bind(self, case: ObservedCase) -> None:
        """Drop the memo whenever the case is not the one it was built for.

        Compared by value, not by `case_id`. A86 lets a case change under the
        runner — a revived mandate advances `mandate_state` — and although no
        feature reads that field today, a memo that keyed on the id alone would
        start returning stale probabilities the moment one did. The identity
        check short-circuits the common path; the equality check is what makes
        it safe.
        """
        if self._cache_case is not case and self._cache_case != case:
            self._cache_case = case
            self._cache = {}

    def _warm_ticks(self, tick: int) -> tuple[int, ...]:
        """Every future tick the runner could next decide at, on the cadence.

        A case nobody acts on is reconsidered once a day until the horizon, so
        one call covers the rest of its life. A case that is acted on falls off
        the cadence and warms again from wherever it lands — still far fewer
        calls than one per decision.
        """
        if not self.warm:
            return (tick,)
        return tuple(range(tick, DECISION_HORIZON_H, DECISION_CADENCE_HOURS)) or (tick,)

    def _score(
        self,
        case: ObservedCase,
        actions: Sequence[Action],
        ticks: Sequence[int],
        last_attempt_tick: int | None,
    ) -> None:
        """Fill the memo for every (action, tick) not already in it. One call."""
        wanted: dict[tuple, list[float]] = {}
        for tick in ticks:
            for action in actions:
                key = (action, tick, last_attempt_tick)
                if key not in self._cache and key not in wanted:
                    wanted[key] = feature_vector(case, action, tick, last_attempt_tick)
        if not wanted:
            return
        keys = list(wanted)
        probabilities = self.model.predict_proba(np.asarray([wanted[k] for k in keys]))[:, 1]
        self.calls += 1
        self.rows_scored += len(keys)
        for key, p in zip(keys, probabilities):
            self._cache[key] = float(p)

    # -- the query the policy makes ---------------------------------------

    def predict_proba(
        self, case: ObservedCase, action: Action, tick: int, last_attempt_tick: int | None = None
    ) -> float:
        """P(settle | case, action, hour). SPEC §10.1."""
        self._bind(case)
        key = (action, tick, last_attempt_tick)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]
        self._score(case, [action], self._warm_ticks(tick), last_attempt_tick)
        return self._cache[key]

    def predict_pairs(
        self, case: ObservedCase, actions: Sequence[Action], tick: int,
        last_attempt_tick: int | None = None,
    ) -> np.ndarray:
        """P(settle) for many actions on one case, in a single call."""
        self._bind(case)
        keys = [(a, tick, last_attempt_tick) for a in actions]
        missing = [k for k in keys if k not in self._cache]
        self.hits += len(keys) - len(missing)
        if missing:
            self._score(case, actions, self._warm_ticks(tick), last_attempt_tick)
        return np.asarray([self._cache[k] for k in keys])

    def predict_many(self, X: np.ndarray) -> np.ndarray:
        """A raw matrix, uncached: training and calibration hand whole splits
        over at once, where the fixed overhead is already amortised."""
        return self.model.predict_proba(X)[:, 1]

    def cache_report(self) -> dict:
        """What the speed came from. OQ-49."""
        asked = self.hits + self.rows_scored
        return {
            "predict_calls": self.calls,
            "rows_scored": self.rows_scored,
            "rows_served_from_cache": self.hits,
            "hit_rate": self.hits / asked if asked else 0.0,
        }

    def artifact_hash(self) -> str:
        """A content hash of the fitted model. EST-4 asserts it is stable."""
        import pickle

        return hashlib.sha256(pickle.dumps(self.model)).hexdigest()


# ---------------------------------------------------------------------------
# The model artifact. SPEC §10.1, CP9.1 D2.
# ---------------------------------------------------------------------------
#
# Content-addressed, and never overwritten. The CP8-to-CP9 comparison is
# unrecoverable because retraining replaced `out/model.pkl` in place: the world
# had changed and the model had changed, and with the old artifact gone the two
# could not be told apart afterwards. A pointer file names the current one, so
# "the latest model" stays a single lookup without any file being a mutable
# target.

LATEST_POINTER: Final[str] = "model.latest"


def latest_model_path(out_dir: Path | str = "out") -> Path | None:
    """The artifact the last training run shipped, or None if there is none.

    Falls back to a bare `model.pkl` when no pointer exists, so a tree carrying
    a pre-CP9.1 artifact still loads rather than failing obscurely.
    """
    directory = Path(out_dir)
    pointer = directory / LATEST_POINTER
    if pointer.exists():
        named = directory / pointer.read_text(encoding="utf-8").strip()
        if named.exists():
            return named
    legacy = directory / "model.pkl"
    return legacy if legacy.exists() else None


def load_latest(out_dir: Path | str = "out") -> "Estimator | None":
    """The shipped model, wrapped the way the policy queries it."""
    import pickle

    path = latest_model_path(out_dir)
    if path is None:
        return None
    payload = pickle.loads(path.read_bytes())
    return Estimator(payload["models"][payload["winner"]], payload["winner"])


def constant_rate_baseline(y_train: np.ndarray) -> float:
    """Predict the training base rate for everything. EST-8's floor.

    A model that cannot beat this has learned nothing the base rate did not
    already say, and the problem is upstream of the model.
    """
    return float(y_train.mean())


assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES)), "duplicate feature name"


# ---------------------------------------------------------------------------
# Uplift resolution. SPEC §10.1 (A92), CP10.
# ---------------------------------------------------------------------------
#
# A84 selects on uplift *calibration*. Uplift ECE bins by predicted uplift and
# compares against a matched control rate, and a model that returns the same
# number for every candidate still gets binned — so the metric is nearly blind
# to a model that has stopped discriminating at all. Measured at CP10: isotonic
# calibration took the median within-decision uplift spread from 6.2 points to
# 1.7 and made 15.1% of multi-option decisions completely flat, while moving
# uplift ECE by 0.0003.
#
# Resolution is therefore reported and enforced alongside calibration. A retry
# costs 5 paise against a median debit of ~499 rupees and carries no opt-out
# risk, so S7 clears it at roughly 0.03% uplift. A scorer whose output steps in
# 3-point increments cannot express that decision: it is two orders of magnitude
# coarser than the threshold the policy needs.

MAX_FLAT_DECISION_RATE: Final[float] = 0.05


def uplift_resolution(estimator, decisions: Sequence[tuple]) -> dict:
    """How far apart the estimator can hold two candidates in one decision.

    `decisions` are `(case, tick, last_attempt_tick, actions)`. The spread is
    over the *uplift*, which is what §10.2 ranks on — a scorer that shifts every
    candidate by the same amount has resolved nothing.
    """
    from settle.schema.action import DoNothing

    spreads = []
    for case, tick, last_attempt_tick, actions in decisions:
        p = estimator.predict_pairs(case, [DoNothing(), *actions], tick, last_attempt_tick)
        uplift = p[1:] - p[0]
        spreads.append(float(uplift.max() - uplift.min()))
    if not spreads:
        return {"median": 0.0, "p90": 0.0, "flat_rate": 1.0, "n": 0}
    spreads.sort()
    return {
        "median": spreads[len(spreads) // 2],
        "p90": spreads[int(len(spreads) * 0.9)],
        "flat_rate": sum(1 for s in spreads if s == 0.0) / len(spreads),
        "n": len(spreads),
    }


def has_usable_resolution(resolution: dict) -> bool:
    """Whether a model can be selected at all. A92.

    A model that returns one number for every option in more than
    `MAX_FLAT_DECISION_RATE` of decisions is not a policy input, whatever its
    calibration says. It is a constant with a confidence interval.
    """
    return resolution["flat_rate"] <= MAX_FLAT_DECISION_RATE


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
