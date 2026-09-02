"""The committed metrics artefact. SPEC §14.4, §19.

    python -m settle.eval.report --cases 2000 --seed 42

Writes `out/charts/metrics.json`: every number the README quotes and every
number the charts draw, in one file, derived from run artefacts rather than
typed by hand.

Why the numbers live in a file
------------------------------
CHT-3 asserts that every figure in the README appears in a committed artefact.
That is not bookkeeping. This project's entire claim is that a recovery number
is worth exactly as much as the evidence behind it, and a README quoting a
recovery rate with nothing behind it would be making the mistake it was written
to criticise. So the README is generated against this file and tested against
it, and a number that cannot be traced here does not go in.

Cost
----
This runs five arms over the batch, reconciles each, then measures calibration
and retry-offset sensitivity on the held-out split. Roughly ten minutes at
10,000 cases. `charts.py` does not run it: charts render from the committed
JSON, so the fast path stays fast and CHT-1 can assert determinism without a
simulation in the loop.

Each arm is run exactly once. The per-class breakdown and the headline table are
two views of the same runs, and running them twice would have cost half the wall
clock to produce numbers that must agree by construction anyway.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pickle
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from settle.agent.calibration import MIN_CELL_OBSERVATIONS
from settle.agent.calibration import headline as calibration_headline
from settle.agent.calibration import reliability_table
from settle.agent.estimator import Estimator, build_matrix, latest_model_path, split_by_case
from settle.agent.train import cell_for, load_rows
from settle.audit.chain import Ledger, read_entries
from settle.diagnose.taxonomy import classify
from settle.eval.sensitivity import metrics as arm_metrics
from settle.eval.sensitivity import ArmResult, _action_cost
from settle.execute.executor import WorldHandle
from settle.recon.reconcile import ReconciledCase, reconcile
from settle.runner.arm import DoNothingArm
from settle.runner.arms.baselines import FixedLadderArm, MaxPressureArm, SingleRetryArm
from settle.runner.arms.ours import OursArm
from settle.runner.case_runner import run_case
from settle.schema.enums import LedgerKind, SilentFailureClass
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams

DEFAULT_OUT: Final[Path] = Path("out/metrics.json")
SENSITIVITY: Final[Path] = Path("out/sensitivity.json")

# See `calibration_block`. A reliability bucket drawing this share or more of
# its rows from thin cells is drawn as extrapolated.
EXTRAPOLATED_BUCKET_FRACTION: Final[float] = 0.05

CONTACT_VERBS: Final[frozenset[str]] = frozenset(
    {"send_message", "request_mandate_update", "serve_notice", "voice_call", "escalate_human"}
)


def _run(arm, batch, seed: int) -> tuple[ArmResult, dict[str, ReconciledCase]]:
    """One arm over the batch, reconciled. Returns both halves.

    `sensitivity.run_arm` discards the reconciled dict, and the silent-failure
    table is the one thing in §14.4 that cannot be recovered from what it keeps.
    Rather than reconcile twice, this returns both.
    """
    streams, config = Streams(seed), ObservabilityConfig()
    actuals: dict[str, list] = {}
    cases: dict[str, Any] = {}
    truths: dict[str, Any] = {}
    path = Path(tempfile.mkdtemp()) / "report.jsonl"
    try:
        with Ledger(path) as ledger:
            for generated in batch.cases:
                world = WorldHandle(truth=generated.truth, streams=streams)
                run_case(generated.observed, arm, world, config, ledger)
                actuals[generated.observed.case_id] = list(world.actuals)
                cases[generated.observed.case_id] = generated.observed
                truths[generated.observed.case_id] = generated.truth
        entries = read_entries(path)
    finally:
        with contextlib.suppress(OSError):
            path.unlink()
            path.parent.rmdir()

    reconciled = reconcile(entries, actuals, cases, truths=truths, streams=streams)
    dispatches = [e for e in entries if e.kind is LedgerKind.DISPATCH]
    result = ArmResult(
        recovered=frozenset(
            c for c, r in reconciled.items() if r.actually_settled and not r.reversed
        ),
        amounts={c: cases[c].amount_paise for c in cases},
        contacts=sum(1 for e in dispatches if e.payload["action"]["type"] in CONTACT_VERBS),
        dispatches=len(dispatches),
        spend_paise=sum(_action_cost(e.payload) for e in dispatches),
        cases=len(cases),
    )
    return result, reconciled


def _silent_failures(reconciled: dict[str, ReconciledCase]) -> dict[str, int]:
    counts = {cls.value: 0 for cls in SilentFailureClass}
    for record in reconciled.values():
        for cls in record.silent_failures:
            counts[cls.value] += 1
    return counts


def _censored(reconciled: dict[str, ReconciledCase]) -> float:
    if not reconciled:
        return 0.0
    return sum(1 for r in reconciled.values() if r.censored) / len(reconciled)


def run_arms(batch, seed: int, estimator: Estimator | None) -> dict[str, tuple]:
    """Every arm, once. The result feeds both the headline table and the classes."""
    specs: list[tuple[str, Any]] = [
        ("B0", DoNothingArm()),
        ("B1", SingleRetryArm()),
        ("B2", FixedLadderArm()),
        ("B3", MaxPressureArm()),
    ]
    if estimator is not None:
        specs.append(("OURS", OursArm(estimator)))

    results: dict[str, tuple[ArmResult, dict[str, ReconciledCase]]] = {}
    for name, arm in specs:
        started = time.perf_counter()
        results[name] = _run(arm, batch, seed)
        print(f"  {name:<5} {time.perf_counter() - started:6.1f}s")
    return results


def arms_block(results: dict[str, tuple]) -> dict[str, Any]:
    """Every arm's §14.4 row. Chart 1 reads `contacts_per_case` and rate."""
    baseline = results["B0"][0]
    block: dict[str, Any] = {}
    for name, (result, reconciled) in results.items():
        row = arm_metrics(result, baseline)
        failures = _silent_failures(reconciled)
        n = result.cases
        row.update(
            {
                "arm": name,
                "mode": "OBSERVE" if name == "B3" else "ENFORCE",
                "cases": n,
                "silent_failures": failures,
                # §7: SF-5 and SF-6 are compliance breaches, and for any arm in
                # ENFORCE they must be zero. Reported as its own number because
                # a non-zero value for OURS is a gate failure, not a finding.
                "compliance_violations": failures["SF-5"] + failures["SF-6"],
                "silent_failure_rate": (
                    sum(failures.values()) / n if n else 0.0
                ),
                "censored_fraction": _censored(reconciled),
                "believed_recovered": sum(
                    1 for r in reconciled.values() if r.ledger_says_recovered
                ),
                "actually_settled": sum(
                    1 for r in reconciled.values() if r.actually_settled
                ),
            }
        )
        # §14.4's "reported minus reconciled recovery": what the ledger claimed
        # against what the money did. The row no comparable submission prints.
        row["reported_minus_reconciled_cases"] = (
            row["believed_recovered"] - row["actually_settled"]
        )
        block[name] = row
    return block


def by_decline_class(batch, results: dict[str, tuple]) -> dict[str, Any]:
    """Chart 3. Incremental recovery per class, OURS against B2.

    Split out rather than folded into the headline because the headline hides
    the shape: a single incremental rate cannot say which classes we win and
    which we lose, and a chart that only showed the wins would be a limitations
    section displaced into a picture.

    Takes the runs rather than making its own. At 10,000 cases re-running three
    arms costs minutes to produce figures that must agree with the headline
    table by construction — and if they ever disagreed, the bug would be here.
    """
    b0 = results["B0"][0]
    b2 = results["B2"][0]
    ours = results["OURS"][0] if "OURS" in results else None

    class_of = {
        generated.observed.case_id: classify(generated.observed.decline_code).value
        for generated in batch.cases
    }
    amounts = b0.amounts

    out: dict[str, Any] = {}
    for case_id, name in class_of.items():
        out.setdefault(
            name,
            {"cases": 0, "b0_recovered": 0, "b2": {"cases": 0, "paise": 0},
             "ours": {"cases": 0, "paise": 0}},
        )
        out[name]["cases"] += 1

    for case_id in b0.recovered:
        out[class_of[case_id]]["b0_recovered"] += 1

    for key, result in (("b2", b2), ("ours", ours)):
        if result is None:
            continue
        for case_id in result.recovered - b0.recovered:
            bucket = out[class_of[case_id]][key]
            bucket["cases"] += 1
            bucket["paise"] += amounts[case_id]

    for name, row in out.items():
        n = row["cases"]
        for key in ("b2", "ours"):
            row[key]["rate"] = row[key]["cases"] / n if n else 0.0
        row["ours_minus_b2_rate"] = row["ours"]["rate"] - row["b2"]["rate"]
    return out


def calibration_block(model_path: Path) -> dict[str, Any]:
    """Chart 2, and the calibration trade the README states in its body.

    The fitted models are re-used from the committed artifact and the test split
    is re-derived — `split_by_case` is deterministic in the case ids — so this
    reproduces the shipped model's held-out predictions without refitting.
    Refitting here would risk reporting a reliability diagram for a model that
    is not the one in `out/model.latest`.
    """
    payload = pickle.loads(model_path.read_bytes())
    winner = payload["winner"]

    rows, y, case_ids = load_rows(
        Path("out/explore.decisions.jsonl"),
        Path("out/labels.jsonl"),
        Path("out/explore.cases.jsonl"),
    )
    _, _, test = split_by_case(case_ids)
    matrix = build_matrix(rows)
    estimator = Estimator(payload["models"][winner], winner)
    probabilities = estimator.predict_many(matrix[test.rows]).tolist()
    outcomes = y[test.rows].tolist()
    cells = [cell_for(*rows[i]) for i in test.rows]

    stats = calibration_headline(probabilities, outcomes, cells)
    thin = {tuple(cell) for cell in stats["extrapolated_cells"]}

    # Which reliability buckets are carried by thin cells. A bucket drawn from
    # cells the model has barely seen is an extrapolation, and the diagram says
    # so rather than drawing it identically to a bucket with 8,000 observations.
    thin_by_bucket: dict[str, int] = {}
    table = reliability_table(probabilities, outcomes)
    edges = [(index / 10, (index + 1) / 10) for index in range(10)]
    for (low, high), bucket in zip(edges, table):
        thin_by_bucket[bucket["bucket"]] = sum(
            1
            for cell, p in zip(cells, probabilities)
            if tuple(cell) in thin and (low <= p < high or (high == 1.0 and p == 1.0))
        )

    # A bucket is flagged EXTRAPOLATED when it is thin itself, or when a
    # material share of it comes from cells the model has barely seen. 5% is
    # low on purpose: the point of the flag is to stop a reader treating every
    # point on the diagram as equally earned, and a majority-thin threshold
    # would never fire on this data and so would say nothing at all.
    for bucket in table:
        bucket["n_extrapolated"] = thin_by_bucket.get(bucket["bucket"], 0)
        bucket["extrapolated_fraction"] = (
            bucket["n_extrapolated"] / bucket["n"] if bucket["n"] else 0.0
        )
        bucket["extrapolated"] = bucket["n"] > 0 and (
            bucket["n"] < MIN_CELL_OBSERVATIONS
            or bucket["extrapolated_fraction"] >= EXTRAPOLATED_BUCKET_FRACTION
        )

    selection = payload["selection"]
    return {
        "model": model_path.name,
        "shipped": winner,
        "reliability": table,
        "ece": stats["ece"],
        "brier": stats["brier"],
        "ece_all": stats["ece_all"],
        "brier_all": stats["brier_all"],
        "n_test_rows": stats["n_all"],
        "n_covered_rows": stats["n_covered"],
        "n_extrapolated_cells": len(stats["extrapolated_cells"]),
        "selection": selection,
        # The trade, as two numbers rather than a sentence. §10.2 consumes only
        # the difference between two probabilities, so the model was selected on
        # the calibration of that difference and pays for it in the level.
        "trade": {
            "shipped": winner,
            "rejected": selection["rejected_on_resolution"][0]
            if selection["rejected_on_resolution"]
            else None,
            "shipped_overall_ece": selection["overall"][winner],
            "rejected_overall_ece": (
                selection["overall"][selection["rejected_on_resolution"][0]]
                if selection["rejected_on_resolution"]
                else None
            ),
            "shipped_uplift_ece": selection["uplift"][winner],
            "rejected_uplift_ece": (
                selection["uplift"][selection["rejected_on_resolution"][0]]
                if selection["rejected_on_resolution"]
                else None
            ),
            "shipped_resolution_median": selection["resolution"][winner]["median"],
            "rejected_resolution_flat_rate": (
                selection["resolution"][selection["rejected_on_resolution"][0]]["flat_rate"]
                if selection["rejected_on_resolution"]
                else None
            ),
        },
    }



def timing_block(model_path: Path) -> dict[str, Any]:
    """The withdrawn retry-timing claim, as an artefact rather than a log line.

    §10.1's A83 records that retry timing was hypothesised as a differentiator,
    tested, and withdrawn. The two numbers that support the withdrawal — the
    median spread across the eight offsets, and where the timing features rank
    by permutation importance — were printed by `train.py` and nowhere else, so
    the README was quoting a training log a reader could not check.

    Recomputed here with the same parameters `train.py` uses, so the figures are
    the same ones and CHT-3 can verify them.
    """
    from sklearn.inspection import permutation_importance

    from settle.agent.features import FEATURE_NAMES
    from settle.policy.params import hour_offsets
    from settle.schema.action import Retry

    payload = pickle.loads(model_path.read_bytes())
    winner = payload["winner"]
    estimator = Estimator(payload["models"][winner], winner)

    rows, y, case_ids = load_rows(
        Path("out/explore.decisions.jsonl"),
        Path("out/labels.jsonl"),
        Path("out/explore.cases.jsonl"),
    )
    _, _, test = split_by_case(case_ids)
    matrix = build_matrix(rows)

    # Permutation importance, same call as train.py's `_feature_importance`:
    # 3,000 rows, 5 repeats, random_state 0, neg-Brier. Model-agnostic, so it
    # answers "which features did it use" rather than "which has a coefficient".
    sample = min(3_000, len(test.rows))
    importance = permutation_importance(
        estimator.model,
        matrix[test.rows][:sample],
        y[test.rows][:sample],
        n_repeats=5,
        random_state=0,
        scoring="neg_brier_score",
    )
    ranked = sorted(
        zip(FEATURE_NAMES, importance.importances_mean), key=lambda kv: -kv[1]
    )
    order = [name for name, _ in ranked]
    # `train.py` groups all four as "timing features". They are not one thing.
    # A83's withdrawn claim was specifically about reaching a customer's
    # liquidity window — the first three. `days_since_last_attempt` measures
    # recency, which is a different hypothesis and, as it turns out, a much
    # better feature. Reporting them together would understate one and overstate
    # the other, so they are separated here.
    liquidity_features = (
        "day_of_month_at_dispatch",
        "days_to_month_start",
        "in_liquidity_window",
    )
    recency_features = ("days_since_last_attempt",)
    lookup = dict(ranked)

    def rank_block(names):
        return {
            name: {"rank": order.index(name) + 1, "importance": lookup[name]}
            for name in names
            if name in order
        }

    liquidity = rank_block(liquidity_features)
    recency = rank_block(recency_features)
    ranks = {**liquidity, **recency}

    # EST-9's spread. If the probability does not move with the offset, the
    # model has learned nothing about timing and §9's liquidity claim is
    # unsupported. Same 4,000-row cap as train.py.
    offsets = hour_offsets()
    spreads: list[float] = []
    for index in test.rows[:4_000]:
        case, action, tick, last_attempt = rows[index]
        if not isinstance(action, Retry):
            continue
        probabilities = [
            estimator.predict_proba(
                case, Retry(at_hour_offset=offset, rail=action.rail), tick, last_attempt
            )
            for offset in offsets
        ]
        spreads.append(max(probabilities) - min(probabilities))
    spreads.sort()

    return {
        "claim": "withdrawn — SPEC §10.1, A83",
        "n_features": len(order),
        "n_offsets": len(offsets),
        "offsets": list(offsets),
        "timing_feature_ranks": ranks,
        "liquidity_feature_ranks": liquidity,
        "recency_feature_ranks": recency,
        "liquidity_rank_best": min(r["rank"] for r in liquidity.values()) if liquidity else None,
        "liquidity_rank_worst": max(r["rank"] for r in liquidity.values()) if liquidity else None,
        "timing_rank_best": min(r["rank"] for r in ranks.values()) if ranks else None,
        "timing_rank_worst": max(r["rank"] for r in ranks.values()) if ranks else None,
        "spread": {
            "n_retry_rows": len(spreads),
            "median": spreads[len(spreads) // 2] if spreads else None,
            "p90": spreads[int(len(spreads) * 0.9)] if spreads else None,
            "max": spreads[-1] if spreads else None,
        },
    }


def sensitivity_block(path: Path = SENSITIVITY) -> dict[str, Any]:
    """Chart 4. The sweep, distilled to what the chart draws.

    Read rather than recomputed: the sweep is a 21-minute job and its output is
    already an artefact. Distilled rather than copied whole so the committed
    metrics file stays readable.
    """
    if not path.exists():
        return {"available": False, "reason": f"{path} not found — run settle.eval.sensitivity"}

    raw = json.loads(path.read_text(encoding="utf-8"))
    members = []
    for member in raw["members"]:
        points = []
        for point in member["points"]:
            ours = point.get("ours") or {}
            points.append(
                {
                    "multiple": point["multiple"],
                    "ours_incremental_rate": ours.get("incremental_rate"),
                    "b2_incremental_rate": point["b2"]["incremental_rate"],
                    "margin": (
                        ours.get("incremental_rate", 0.0) - point["b2"]["incremental_rate"]
                        if ours
                        else None
                    ),
                    "headline_holds": point["headline_holds"],
                    "restraint_holds": point["restraint_holds"],
                }
            )
        members.append(
            {
                "name": member["name"],
                "space": member["space"],
                "why": member["why"],
                "points": points,
                "holds_everywhere": all(p["headline_holds"] for p in points),
                "flips_at": [p["multiple"] for p in points if not p["headline_holds"]],
            }
        )
    return {
        "available": True,
        "meta": raw["meta"],
        "base": raw["base"],
        "members": members,
        "n_members": len(members),
        "n_holding_everywhere": sum(1 for m in members if m["holds_everywhere"]),
        "flipping": [m["name"] for m in members if not m["holds_everywhere"]],
    }


def headline_rows(block: dict[str, Any]) -> dict[str, Any]:
    """The subset of an arms block a size-comparison needs."""
    keep = (
        "incremental_rate", "incremental_cases", "incremental_paise", "recovered",
        "contacts", "contacts_per_case", "dispatches", "cost_per_100",
        "silent_failure_rate", "compliance_violations", "believed_recovered",
        "actually_settled", "reported_minus_reconciled_cases",
    )
    return {arm: {k: row[k] for k in keep} for arm, row in block.items()}


def build(cases: int, seed: int, model_path: Path, compare_cases: int | None = None) -> dict[str, Any]:
    print(f"report: {cases:,} cases, seed {seed}, model {model_path.name}")
    payload = pickle.loads(model_path.read_bytes())
    estimator = Estimator(payload["models"][payload["winner"]], payload["winner"])

    batch = generate_batch(cases, seed)
    print("arms:")
    results = run_arms(batch, seed, estimator)
    arms = arms_block(results)
    print("decline classes:")
    classes = by_decline_class(batch, results)
    print("calibration:")
    calibration = calibration_block(model_path)
    print("retry timing:")
    timing = timing_block(model_path)
    print("sensitivity:")
    sensitivity = sensitivity_block()

    comparison: dict[str, Any] | None = None
    if compare_cases and compare_cases != cases:
        # The headline moved between batch sizes at CP13.1 and a reader who has
        # seen both should be able to see the divergence rather than wonder
        # which one is current. Run once, keep only the headline rows.
        print(f"comparison at {compare_cases:,} cases:")
        small_batch = generate_batch(compare_cases, seed)
        small_results = run_arms(small_batch, seed, estimator)
        comparison = {
            "cases": compare_cases,
            "seed": seed,
            "arms": headline_rows(arms_block(small_results)),
            "by_decline_class": by_decline_class(small_batch, small_results),
            "note": (
                "Reported so the divergence between batch sizes is visible. The "
                "headline figures above are the 10,000-case run; SPEC §3 "
                "specifies 10,000 and this is it."
            ),
        }

    return {
        "meta": {
            "checkpoint": "CP13.1",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "cases": cases,
            "seed": seed,
            "model": model_path.name,
            "regenerate": (
                f"python -m settle.eval.report --cases {cases} --seed {seed}"
            ),
            "note": (
                "Every number the README quotes comes from this file (CHT-3). "
                "Derived from the run artefacts and out/sensitivity.json, never "
                "typed by hand."
            ),
        },
        "arms": arms,
        "by_decline_class": classes,
        "calibration": calibration,
        "retry_timing": timing,
        "sensitivity": sensitivity,
        "comparison": comparison,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settle.eval.report", description=__doc__)
    parser.add_argument("--cases", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument(
        "--compare-cases", type=int, default=None,
        help="also run this batch size and record its headline rows, so a "
             "change of scale is visible rather than silent",
    )
    args = parser.parse_args(argv)

    model_path = args.model or Path(latest_model_path())
    report = build(args.cases, args.seed, model_path, args.compare_cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
