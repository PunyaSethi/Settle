"""CP11 — the sensitivity sweep. SPEC §15.

One parameter at a time, at 0.25x / 0.5x / 1x / 2x / 4x of its shipped value,
clamped to the range the parameter can legally take. OURS and B2 are re-run at
each point against the same B0 baseline, and the two claims the project rests on
are checked:

    headline   OURS incremental rate  >  B2 incremental rate
    restraint  OURS contacts per case <  B2 contacts per case

A conclusion that survives a 16x swing in every input is a strong claim. One
that flips at 2x on an unsourced number is a finding we disclose ourselves,
which is why the flip points are the primary output of this module and the
survival ranges are computed rather than eyeballed.

**The estimator is not retrained.** The model in `out/model_<sha>.pkl` was
fitted on EXPLORE logs drawn from the world at 1x, so every off-1x point is a
policy running a model that is now *wrong about the world*. That is the honest
question for a merchant — a prior is an estimate and the policy has to survive
being wrong about it — but it is not "what OURS would score if it were refitted
at this parameter value", and the two must not be read as the same number. A
sweep that quietly retrained at each point would be measuring an easier claim:
it would confound the policy's robustness with the trainer's.

`MAX_FLAT_DECISION_RATE` is the exception, and it is swept without retraining
for a reason rather than by omission. The artifact stores every candidate model
together with the resolution and uplift-ECE figures selection was made from
(A91, A92), so re-applying the floor at a different value re-runs the *selection*
exactly, and picks a model that was really fitted. Nothing is refitted, because
nothing needs to be.

    python -m settle.eval.sensitivity --cases 2000 --seed 42 \
        --out out/sensitivity.json

Runs OURS 57 times, so it is minutes rather than seconds; `--workers` fans the
configurations out across processes. Every task is a pure function of
`(overrides, arm, cases, seed)`, so the result does not depend on how many
workers ran it (SEN-1).
"""

from __future__ import annotations

import os

# Before sklearn loads. `predict_proba` on a fifteen-row grid spends more time
# in OpenMP's thread handshake than in the trees, so a 500-case OURS run costs
# 23s at the default thread count and 9s at one. The sweep runs OURS 57 times.
os.environ.setdefault("OMP_NUM_THREADS", "1")

import argparse
import contextlib
import json
import pickle
import tempfile
import time
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Final, Iterator, Sequence

from settle.agent.estimator import Estimator, latest_model_path
from settle.audit.chain import Ledger, read_entries
from settle.execute.executor import WorldHandle
from settle.policy.params import POLICY_PARAMS, action_cost_paise
from settle.recon.reconcile import ReconciledCase, reconcile
from settle.runner.arm import DoNothingArm
from settle.runner.arms.baselines import FixedLadderArm
from settle.runner.arms.ours import OursArm
from settle.runner.case_runner import run_case
from settle.schema.enums import ActionType, Channel, LedgerKind
from settle.sim.generator import PARAMS, generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams

MULTIPLES: Final[tuple[float, ...]] = (0.25, 0.5, 1.0, 2.0, 4.0)
BASE_MULTIPLE: Final[float] = 1.0

# §14.4's contact count. A retry is not a contact — nobody hears from us — so
# the restraint claim is about exactly these five verbs.
CONTACT_VERBS: Final[frozenset[str]] = frozenset({
    "send_message", "request_mandate_update", "serve_notice", "voice_call", "escalate_human",
})

WORLD, POLICY, MODEL = "world", "policy", "model"


# ---------------------------------------------------------------------------
# What gets swept
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    """One sweep member: a parameter, or a family scaled together.

    A family is scaled as a unit because the members of one are not independent
    claims. `contact_response.rate.*` is five numbers describing a single belief
    — how answerable a contacted customer is — and moving `willing_able` alone
    while holding `churned` fixed would sweep the *shape* of the intent
    conditioning rather than the level the PRIORS row is uncertain about.
    """

    name: str
    space: str
    keys: tuple[str, ...]
    why: str
    probability: bool = False       # clamp to [0, 1]
    integral: bool = False          # the consumer calls int(); record what it sees
    floor: float | None = None      # smallest value the consumer can use

    def applied(self, multiple: float, base: dict[str, float]) -> dict[str, float]:
        """The value each key takes at `multiple`, after clamping."""
        out: dict[str, float] = {}
        for key in self.keys:
            value = base[key] * multiple
            if self.floor is not None:
                value = max(self.floor, value)
            if self.probability:
                value = min(1.0, max(0.0, value))
            if self.integral:
                value = float(int(value))
            out[key] = value
        return out


def _keys(prefix: str, *suffixes: str) -> tuple[str, ...]:
    return tuple(f"{prefix}{suffix}" for suffix in suffixes)


_INTENTS: Final = ("willing_able", "willing_broke", "disputing", "churned", "adversarial")
_BEHAVIOURS: Final = (
    "promise_and_break", "dispute_stall", "go_silent",
    "opt_out_midway", "hedged_reply", "pay_then_complain",
)
_VERBS: Final = (
    "do_nothing", "retry", "switch_rail", "send_message",
    "request_mandate_update", "serve_notice", "escalate_human", "voice_call",
)
_OPT_OUT_KEYS: Final = (
    "p_opt_out.do_nothing", "p_opt_out.retry", "p_opt_out.switch_rail",
    "p_opt_out.send_message.sms", "p_opt_out.send_message.whatsapp",
    "p_opt_out.request_mandate_update.sms", "p_opt_out.request_mandate_update.whatsapp",
    "p_opt_out.serve_notice.sms", "p_opt_out.serve_notice.whatsapp",
    "p_opt_out.voice_call", "p_opt_out.escalate_human",
)

MEMBERS: Final[tuple[Member, ...]] = (
    Member(
        "mandate_update.success_rate.*", WORLD,
        _keys("mandate_update.success_rate.", *_INTENTS),
        "decides whether 17% of the batch — `dead_instrument` — is winnable at all (A86)",
        probability=True,
    ),
    Member(
        "mandate_update.response_delay_h_max", WORLD,
        ("mandate_update.response_delay_h_max",),
        "the wait that makes a mandate update a decision rather than a coin flip; it "
        "competes with the 30-day decision horizon",
        floor=1.0,
    ),
    Member(
        "contact_response.rate.*", WORLD,
        _keys("contact_response.rate.", *_INTENTS),
        "decides whether contacting anyone is viable at all, and therefore whether the "
        "contact-restraint result is a finding or an artefact (A89)",
        probability=True,
    ),
    Member(
        "contact_response.behaviour_multiplier.*", WORLD,
        _keys("contact_response.behaviour_multiplier.", *_BEHAVIOURS),
        "§8's debtors modulate A89's response rate, so this sets how much of the batch "
        "is reachable by a message at all",
    ),
    Member(
        "contact_response.delay_h_max", WORLD,
        ("contact_response.delay_h_max",),
        "how long a contact takes to turn into money; a response due past the decision "
        "horizon still lands (§6.1)",
        floor=1.0,
    ),
    Member(
        "natural_recovery.*", WORLD,
        _keys("natural_recovery.", *_INTENTS),
        "B0's recovery, subtracted from every arm (§14.3)",
        probability=True,
    ),
    Member(
        "natural_recovery.max_day", WORLD,
        ("natural_recovery.max_day",),
        "when a self-cure lands, which decides how much of B0's recovery falls inside "
        "the 60-day observation horizon",
        floor=1.0,
    ),
    Member(
        "action_lift.*", WORLD,
        _keys("action_lift.", *_VERBS),
        "whether a retry outperforms a message — upstream of every rupee in §14.4",
    ),
    Member(
        "world.liquidity_window_days", WORLD,
        ("world.liquidity_window_days",),
        "how often a time_shiftable retry lands inside the liquidity window; named a "
        "REQUIRED member since CP2.3 (A54)",
        floor=0.0,
    ),
    Member(
        "p_opt_out.*", POLICY,
        _OPT_OUT_KEYS,
        "98.6% of a contact's priced cost at the shipped LTV — the term that makes OURS "
        "decline a contact (§20, A26)",
        probability=True,
    ),
    Member(
        "ltv_months", POLICY, ("ltv_months",),
        "the other half of a contact's priced cost: opt_out_cost = P(opt_out) x "
        "plan_value x ltv_months",
        floor=0.0,
    ),
    Member(
        "economic_stop_multiple", POLICY, ("economic_stop_multiple",),
        "S7's hurdle rate. At CP9 it declined a mandate-update campaign returning 1.69x "
        "on the project's own priced cost, so it — not the estimator — decides the "
        "restraint result at the margin",
        floor=0.0,
    ),
    Member(
        "class_retry_cap.dead_instrument", POLICY, ("class_retry_cap.dead_instrument",),
        "G10's budget for a revived mandate, which bounds how much of the "
        "`dead_instrument` slice A86 made reachable",
        integral=True, floor=0.0,
    ),
    Member(
        "MAX_FLAT_DECISION_RATE", MODEL, ("MAX_FLAT_DECISION_RATE",),
        "the resolution floor a scorer must clear to be selectable — it decides which "
        "model ships (A92)",
        probability=True,
    ),
)


def base_values() -> dict[str, float]:
    """The shipped value of every swept key, read from its source of truth."""
    from settle.agent.estimator import MAX_FLAT_DECISION_RATE

    base: dict[str, float] = {}
    for member in MEMBERS:
        for key in member.keys:
            if member.space == WORLD:
                base[key] = float(PARAMS[key])
            elif member.space == POLICY:
                base[key] = float(POLICY_PARAMS[key])
            else:
                base[key] = float(MAX_FLAT_DECISION_RATE)
    return base


# ---------------------------------------------------------------------------
# Applying an override
# ---------------------------------------------------------------------------
#
# Most consumers read `PARAMS[...]` and `POLICY_PARAMS[...]` at call time, so
# patching the dict is enough. Three do not: they cache a derived value at
# import. A sweep that patched the dict and left those alone would report that a
# parameter has no effect — the most dangerous wrong answer this module can
# give, because it looks exactly like a robust result.
#
# Listed by the prefix that owns them rather than discovered, and REB-1 in the
# test file asserts the list is complete by walking the modules for cached
# constants derived from a swept key.
#
# Rebinding `world.ACTION_LIFT` means importing `settle.sim.world`, which EXE-1
# otherwise reserves for the executor and the reconciler. This module is its
# third named exception (F6, CP11.1), recorded in EXE-1's own list with the
# reason: it reads the world's constants and never dispatches. The exception is
# named rather than evaded — reaching the module through `sys.modules` would
# have passed EXE-1 while breaking exactly what EXE-1 protects.

def _rebind(keys: Sequence[str]) -> list[tuple[Any, str, Any]]:
    """`(module, attribute, new_value)` for every cached constant a key invalidates."""
    from settle.agent import policy as agent_policy
    from settle.sim import world as sim_world

    out: list[tuple[Any, str, Any]] = []
    if any(key.startswith("action_lift.") for key in keys):
        out.append((
            sim_world, "ACTION_LIFT",
            {t: PARAMS[f"action_lift.{t.value}"] for t in ActionType},
        ))
    if "economic_stop_multiple" in keys:
        out.append((
            agent_policy, "ECONOMIC_STOP_MULTIPLE",
            float(POLICY_PARAMS["economic_stop_multiple"]),
        ))
    return out


@contextlib.contextmanager
def override(values: dict[str, float]) -> Iterator[None]:
    """Apply `values` to whichever dict owns each key, then put it all back."""
    from settle.agent import estimator as agent_estimator

    saved: list[tuple[dict[str, float] | Any, str, Any]] = []
    for key, value in values.items():
        if key in PARAMS:
            saved.append((PARAMS, key, PARAMS[key]))
            PARAMS[key] = value
        elif key in POLICY_PARAMS:
            saved.append((POLICY_PARAMS, key, POLICY_PARAMS[key]))
            POLICY_PARAMS[key] = value
        elif key == "MAX_FLAT_DECISION_RATE":
            saved.append((agent_estimator, key, agent_estimator.MAX_FLAT_DECISION_RATE))
            agent_estimator.MAX_FLAT_DECISION_RATE = value
        else:  # pragma: no cover - a typo in MEMBERS, caught by SEN-2
            raise KeyError(f"{key} is in no parameter table")

    rebound = _rebind(list(values))
    for module, attribute, new in rebound:
        saved.append((module, attribute, getattr(module, attribute)))
        setattr(module, attribute, new)

    try:
        yield
    finally:
        for holder, key, old in reversed(saved):
            if isinstance(holder, dict):
                holder[key] = old
            else:
                setattr(holder, key, old)


# ---------------------------------------------------------------------------
# Model selection under a different resolution floor
# ---------------------------------------------------------------------------

def select_winner(payload: dict) -> str | None:
    """Re-apply A92's rule at the floor currently in force. Returns what ships.

    This is `train.py`'s selection rather than an approximation of it: the
    artifact records every candidate's `flat_rate` and `ece_uplift` from the run
    that fitted them, so the rule replays exactly with nothing refitted, and the
    floor is read through `has_usable_resolution` so the two cannot drift apart.
    `None` means no candidate clears the floor — which `train.py` treats as a
    hard stop rather than as a model, and the sweep records rather than raises.
    """
    from settle.agent.estimator import has_usable_resolution

    selection = payload["selection"]
    usable = [
        name for name, resolution in selection["resolution"].items()
        if has_usable_resolution(resolution)
    ]
    if not usable:
        return None
    return min(usable, key=lambda name: selection["uplift"][name])


# ---------------------------------------------------------------------------
# One run, and what it is worth
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArmResult:
    recovered: frozenset[str]
    amounts: dict[str, int]
    contacts: int
    dispatches: int
    spend_paise: int
    cases: int


def _action_cost(payload: dict) -> int:
    action = payload["action"]
    raw_channel = action.get("channel")
    channel = Channel(raw_channel) if raw_channel else None
    return action_cost_paise(ActionType(action["type"]), channel)


def run_arm(arm, batch, seed: int) -> ArmResult:
    """One arm over the batch under common random numbers, then reconciled.

    Reconciliation, not the ledger, decides what recovered: the ledger records
    what the agent was told, and INV-1 exists because those differ.
    """
    streams, config = Streams(seed), ObservabilityConfig()
    actuals, cases, truths = {}, {}, {}
    path = Path(tempfile.mkdtemp()) / "sweep.jsonl"
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

    reconciled: dict[str, ReconciledCase] = reconcile(
        entries, actuals, cases, truths=truths, streams=streams
    )
    dispatches = [e for e in entries if e.kind is LedgerKind.DISPATCH]
    return ArmResult(
        recovered=frozenset(
            c for c, r in reconciled.items() if r.actually_settled and not r.reversed
        ),
        amounts={c: cases[c].amount_paise for c in cases},
        contacts=sum(1 for e in dispatches if e.payload["action"]["type"] in CONTACT_VERBS),
        dispatches=len(dispatches),
        spend_paise=sum(_action_cost(e.payload) for e in dispatches),
        cases=len(cases),
    )


def metrics(arm: ArmResult, baseline: ArmResult) -> dict:
    """§14.3 and §14.4 for one arm, against B0's self-cure set.

    `cost_per_100` is priced spend over incremental recovery, in rupees per ₹100
    recovered. Spend is the §20 action cost of every dispatch, retries included:
    a retry is cheap, not free, and a cost figure that counted only contacts
    would flatter whichever arm retries most. The opt-out cost is deliberately
    NOT added — it is a decision-time risk price, not money spent, and adding it
    would double-count the opt-outs that actually happened.
    """
    incremental = arm.recovered - baseline.recovered
    incremental_paise = sum(arm.amounts[c] for c in incremental)
    n = arm.cases
    return {
        "recovered": len(arm.recovered),
        "incremental_cases": len(incremental),
        "incremental_rate": len(incremental) / n if n else 0.0,
        "incremental_paise": incremental_paise,
        "contacts": arm.contacts,
        "contacts_per_case": arm.contacts / n if n else 0.0,
        "dispatches": arm.dispatches,
        "spend_paise": arm.spend_paise,
        "cost_per_100": (
            100.0 * arm.spend_paise / incremental_paise if incremental_paise else None
        ),
    }


# ---------------------------------------------------------------------------
# One configuration: B0, B2 and OURS at one point
# ---------------------------------------------------------------------------

_PAYLOAD: dict | None = None
_MODEL_PATH: Path | None = None


def _load_payload(model_path: str) -> dict:
    global _PAYLOAD, _MODEL_PATH
    if _PAYLOAD is None or _MODEL_PATH != Path(model_path):
        _MODEL_PATH = Path(model_path)
        _PAYLOAD = pickle.loads(_MODEL_PATH.read_bytes())
    return _PAYLOAD


def run_point(
    values: dict[str, float], cases: int, seed: int, model_path: str
) -> dict:
    """Everything one sweep point needs, as a pure function of its arguments.

    Pure is not decoration here: it is what lets the sweep be fanned across
    processes and still be reproducible (SEN-1). Nothing is read from a previous
    point, and the parameter tables are restored before returning.
    """
    payload = _load_payload(model_path)
    started = time.perf_counter()

    with override(values):
        winner = select_winner(payload)
        batch = generate_batch(cases, seed)
        b0 = run_arm(DoNothingArm(), batch, seed)
        b2 = run_arm(FixedLadderArm(), batch, seed)
        if winner is None:
            # train.py raises here rather than shipping a constant. The sweep
            # records it instead of crashing: "no model is selectable" is a
            # result about the floor, not a failure of the sweep.
            ours = None
        else:
            estimator = Estimator(payload["models"][winner], winner)
            ours = run_arm(OursArm(estimator), batch, seed)

    point = {
        "winner": winner,
        "b0": metrics(b0, b0),
        "b2": metrics(b2, b0),
        "ours": metrics(ours, b0) if ours is not None else None,
        "seconds": round(time.perf_counter() - started, 2),
    }
    point["headline_holds"] = (
        ours is not None and point["ours"]["incremental_rate"] > point["b2"]["incremental_rate"]
    )
    point["restraint_holds"] = (
        ours is not None and point["ours"]["contacts_per_case"] < point["b2"]["contacts_per_case"]
    )
    return point


def _task(args: tuple) -> tuple[str, float, dict]:
    member_name, multiple, values, cases, seed, model_path = args
    return member_name, multiple, run_point(values, cases, seed, model_path)


# ---------------------------------------------------------------------------
# Survival ranges
# ---------------------------------------------------------------------------

def survival_range(points: dict[float, bool]) -> dict:
    """The widest run of multiples around 1x over which a conclusion holds.

    Reported as an interval rather than a set because that is the claim a reader
    wants: "this survives from here to here". A conclusion that fails at 1x
    itself, or that holds away from 1x but not at it, is reported as such rather
    than smoothed into an interval — a non-contiguous survival set is a finding,
    not a formatting problem.
    """
    ordered = sorted(points)
    if not points.get(BASE_MULTIPLE, False):
        return {
            "holds_at_base": False,
            "low": None, "high": None, "label": "fails at 1x",
            "holds_at": [m for m in ordered if points[m]],
            "contiguous": True,
        }
    base = ordered.index(BASE_MULTIPLE)
    low = high = base
    while low > 0 and points[ordered[low - 1]]:
        low -= 1
    while high < len(ordered) - 1 and points[ordered[high + 1]]:
        high += 1
    holds_at = [m for m in ordered if points[m]]
    inside = ordered[low:high + 1]
    return {
        "holds_at_base": True,
        "low": ordered[low],
        "high": ordered[high],
        "label": f"{ordered[low]:g}x–{ordered[high]:g}x",
        "holds_at": holds_at,
        "contiguous": holds_at == inside,
    }


def flips(points: dict[float, dict]) -> list[dict]:
    """Every multiple at which a conclusion that holds at 1x stops holding."""
    out = []
    for multiple in sorted(points):
        point = points[multiple]
        if multiple == BASE_MULTIPLE:
            continue
        lost = [
            claim for claim in ("headline", "restraint")
            if points[BASE_MULTIPLE][f"{claim}_holds"] and not point[f"{claim}_holds"]
        ]
        if lost:
            out.append({"multiple": multiple, "lost": lost})
    return out


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def sweep(
    cases: int = 2000,
    seed: int = 42,
    model_path: str | Path | None = None,
    members: Sequence[Member] = MEMBERS,
    workers: int = 1,
    progress=None,
) -> dict:
    """Run every member at every multiple and assemble the report."""
    if model_path is None:
        found = latest_model_path("out")
        if found is None:
            raise SystemExit("no trained model in out/; run settle.agent.train first")
        model_path = found
    model_path = Path(model_path)
    payload = _load_payload(str(model_path))
    base = base_values()

    # 1x is the same configuration for every member, so it is run once. Every
    # off-1x point is distinct, and none of them share work.
    tasks = [
        (member.name, multiple, member.applied(multiple, base), cases, seed, str(model_path))
        for member in members
        for multiple in MULTIPLES
        if multiple != BASE_MULTIPLE
    ]

    started = time.perf_counter()
    base_point = run_point({}, cases, seed, str(model_path))
    if progress:
        progress("base", BASE_MULTIPLE, base_point)

    results: dict[tuple[str, float], dict] = {}
    if workers > 1:
        context = get_context("spawn")
        with context.Pool(workers) as pool:
            for name, multiple, point in pool.imap_unordered(_task, tasks):
                results[(name, multiple)] = point
                if progress:
                    progress(name, multiple, point)
    else:
        for task in tasks:
            name, multiple, point = _task(task)
            results[(name, multiple)] = point
            if progress:
                progress(name, multiple, point)

    report_members = []
    for member in members:
        points = {BASE_MULTIPLE: base_point}
        for multiple in MULTIPLES:
            if multiple != BASE_MULTIPLE:
                points[multiple] = results[(member.name, multiple)]
        report_members.append({
            "name": member.name,
            "space": member.space,
            "keys": list(member.keys),
            "why": member.why,
            "shipped": {key: base[key] for key in member.keys},
            "points": [
                {
                    "multiple": multiple,
                    "applied": member.applied(multiple, base),
                    **points[multiple],
                }
                for multiple in MULTIPLES
            ],
            "headline_survival": survival_range(
                {m: points[m]["headline_holds"] for m in MULTIPLES}
            ),
            "restraint_survival": survival_range(
                {m: points[m]["restraint_holds"] for m in MULTIPLES}
            ),
            "flips": flips(points),
        })

    return {
        "meta": {
            "cases": cases,
            "seed": seed,
            "multiples": list(MULTIPLES),
            "model": model_path.name,
            "shipped_winner": payload["winner"],
            "estimator_retrained": False,
            "headline": "OURS incremental rate > B2 incremental rate",
            "restraint": "OURS contacts per case < B2 contacts per case",
            "cost_per_100": "priced spend (§20) per ₹100 of incremental recovery",
            "wall_seconds": round(time.perf_counter() - started, 1),
        },
        "base": base_point,
        "members": report_members,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(report: dict) -> None:
    meta = report["meta"]
    base = report["base"]
    print(f"\nsensitivity sweep — {meta['cases']} cases, seed {meta['seed']}, "
          f"model {meta['model']} ({meta['shipped_winner']})")
    print(f"  headline   {meta['headline']}")
    print(f"  restraint  {meta['restraint']}")
    print(f"  1x         OURS {base['ours']['incremental_rate']:.2%}  "
          f"B2 {base['b2']['incremental_rate']:.2%}  "
          f"OURS contacts/case {base['ours']['contacts_per_case']:.3f}  "
          f"B2 {base['b2']['contacts_per_case']:.3f}")

    header = (f"\n  {'parameter':<38}{'x':>6}{'OURS':>8}{'B2':>8}"
              f"{'win':>5}{'c/case':>9}{'B2 c/c':>8}{'≤':>4}"
              f"{'₹/100 OURS':>12}{'₹/100 B2':>11}")
    print(header)
    for member in report["members"]:
        print()
        for point in member["points"]:
            ours, b2 = point["ours"], point["b2"]
            label = member["name"] if point["multiple"] == MULTIPLES[0] else ""
            if ours is None:
                print(f"  {label:<38}{point['multiple']:>5g}x{'—':>8}"
                      f"{b2['incremental_rate']:>8.2%}   no model selectable")
                continue
            print(
                f"  {label:<38}{point['multiple']:>5g}x"
                f"{ours['incremental_rate']:>8.2%}{b2['incremental_rate']:>8.2%}"
                f"{('Y' if point['headline_holds'] else 'N'):>5}"
                f"{ours['contacts_per_case']:>9.3f}{b2['contacts_per_case']:>8.3f}"
                f"{('Y' if point['restraint_holds'] else 'N'):>4}"
                f"{_money(ours['cost_per_100']):>12}{_money(b2['cost_per_100']):>11}"
            )

    print(f"\n  {'parameter':<38}{'headline survives':<22}{'restraint survives':<22}")
    for member in report["members"]:
        print(f"  {member['name']:<38}"
              f"{member['headline_survival']['label']:<22}"
              f"{member['restraint_survival']['label']:<22}")

    flipped = [(m["name"], f) for m in report["members"] for f in m["flips"]]
    print()
    if not flipped:
        print("  no conclusion flips anywhere in the swept range.")
    else:
        print("  conclusions that flip — the output of this checkpoint:")
        for name, flip in flipped:
            print(f"    {name:<38} at {flip['multiple']:g}x: "
                  f"{', '.join(flip['lost'])} no longer holds")


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settle.eval.sensitivity", description=__doc__)
    parser.add_argument("--cases", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("out/sensitivity.json"))
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--only", action="append", default=None,
        help="sweep only these members, by name. Repeatable. Debugging aid: a partial "
             "sweep is not a sensitivity result and SEN-2 will not accept one.",
    )
    args = parser.parse_args(argv)

    members = MEMBERS
    if args.only:
        wanted = set(args.only)
        members = tuple(m for m in MEMBERS if m.name in wanted)
        missing = wanted - {m.name for m in members}
        if missing:
            parser.error(f"unknown member(s): {', '.join(sorted(missing))}")

    done = [0]
    total = 1 + len(members) * (len(MULTIPLES) - 1)

    def progress(name, multiple, point):
        done[0] += 1
        ours = point["ours"]
        rate = "—" if ours is None else f"{ours['incremental_rate']:.2%}"
        print(f"  [{done[0]:>2}/{total}] {name:<40} {multiple:>4g}x  "
              f"OURS {rate:>7}  B2 {point['b2']['incremental_rate']:.2%}  "
              f"({point['seconds']:.1f}s)", flush=True)

    report = sweep(
        cases=args.cases, seed=args.seed, model_path=args.model,
        members=members, workers=args.workers, progress=progress,
    )
    _print_report(report)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"\n  written -> {args.out}  ({report['meta']['wall_seconds']:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
