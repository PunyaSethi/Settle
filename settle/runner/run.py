"""Run one arm over a batch. SPEC §14.1.

    python -m settle.runner.run --arm b0 --cases 10000 --seed 42 \
        --out out/run_b0.jsonl --audit out/audit_b0.jsonl

This is the only module under `settle/runner/` that knows a simulator exists.
It builds the batch and hands each case's `WorldHandle` to the runner as an
opaque token; `case_runner.py` never opens it, which is what keeps the loop
honest about seeing only what it is told (RUN-9).
"""

import argparse
import json
import time
from pathlib import Path

from settle.audit.chain import Ledger
from settle.execute.executor import WorldHandle
from settle.runner.arm import ARMS, assert_enforce_only
from settle.runner.arms.explore import EXPLORE_SEED_RANGE, is_explore_seed
from settle.runner.case_runner import run_case
from settle.schema.enums import ArmMode
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig, perfect_observability
from settle.sim.streams import Streams


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settle.runner.run", description=__doc__)
    parser.add_argument("--arm", default="b0", choices=sorted(ARMS))
    parser.add_argument("--cases", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("out/run.jsonl"))
    parser.add_argument("--audit", type=Path, default=Path("out/audit.jsonl"))
    parser.add_argument("--mode", default="ENFORCE", choices=[m.value for m in ArmMode])
    parser.add_argument(
        "--perfect-observability",
        action="store_true",
        help="zero the five reporting parameters (SPEC §6). Measures what unreliable "
        "reporting costs; it does NOT make the world perfect.",
    )
    args = parser.parse_args(argv)

    # EXP-5, checked before anything is constructed. A run that trains on the
    # evaluation seeds is not a held-out set, it is a memorisation test.
    if args.arm == "explore" and not is_explore_seed(args.seed):
        parser.error(
            f"--arm explore requires a seed in {EXPLORE_SEED_RANGE.start}.."
            f"{EXPLORE_SEED_RANGE.stop - 1}; got {args.seed}"
        )
    if args.arm != "explore" and is_explore_seed(args.seed):
        parser.error(
            f"seed {args.seed} belongs to the EXPLORE range and must not be used "
            "for an evaluation arm"
        )

    arm_class = ARMS[args.arm]
    if args.arm == "explore":
        arm = arm_class(args.seed)
    elif args.arm == "first_legal":
        arm = arm_class(ArmMode(args.mode))
    else:
        arm = arm_class()
    assert_enforce_only(arm.name, arm.mode)
    observability = perfect_observability() if args.perfect_observability else ObservabilityConfig()
    streams = Streams(args.seed)

    batch = generate_batch(args.cases, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.audit.exists():
        args.audit.unlink()

    started = time.perf_counter()
    stops: dict[str, int] = {}
    with Ledger(args.audit) as ledger, args.out.open("w", encoding="utf-8") as out:
        for generated in batch.cases:
            world = WorldHandle(truth=generated.truth, streams=streams)
            final = run_case(generated.observed, arm, world, observability, ledger)
            stops[final.stop_reason or "NONE"] = stops.get(final.stop_reason or "NONE", 0) + 1
            out.write(final.model_dump_json() + "\n")
        entries = ledger.seq
    elapsed = time.perf_counter() - started

    print(f"arm {arm.name} ({arm.mode.value})  {args.cases} cases  seed {args.seed}")
    print(f"  wall time     {elapsed:.2f}s  ({elapsed / args.cases * 1000:.3f} ms/case)")
    print(f"  ledger        {entries} entries -> {args.audit}")
    print(f"  final states  -> {args.out}")

    decisions = getattr(arm, "decisions", None)
    if decisions:
        decisions_path = args.out.with_suffix(".decisions" + args.out.suffix)
        with decisions_path.open("w", encoding="utf-8") as handle:
            for decision in decisions:
                handle.write(decision.model_dump_json() + "\n")
        print(f"  decisions     {len(decisions)} -> {decisions_path}")
    print("  stops:")
    for reason, count in sorted(stops.items(), key=lambda kv: -kv[1]):
        print(f"    {reason:<34} {count:>6}  {count / args.cases:>6.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
