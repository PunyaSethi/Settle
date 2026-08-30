"""The OURS policy. SPEC §10.2, §13 S7.

Expected value over the gate-passing action grid, with the `do_nothing` term
subtracted from every action:

    EV(a) = [p_settle(a) − p_settle(do_nothing)] × amount_recoverable
            − action_cost(a) − opt_out_cost(a)

**Uplift, not raw probability** (A82). 21.8% of cases self-cure regardless of
any action, so a raw probability tells the policy that acting on a
`willing_able` case succeeds 99% of the time — true, and useless, because it
would have settled anyway. Subtracting `p_settle(do_nothing)` cancels the
self-cure component and leaves the action's causal contribution. It is also what
gives `do_nothing` a real expected value: its uplift is zero by construction, so
any action whose uplift does not pay for its cost loses to doing nothing, and
the contact-restraint result becomes reachable rather than rhetorical.

Every option considered is recorded as an `Alternative`, including the ones
gates blocked. A decision log that only shows what was chosen is a number dump;
one that shows what was declined, and why, is auditable.

S7, the economic stop, lives here rather than in `settle/policy/stops.py`. It
compares expected recovery against cost, which needs an EV, which needs the
estimator. A pure function of `CaseState` cannot ask that question.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from settle.agent.features import action_channel
from settle.policy.gates import evaluate_gates
from settle.policy.params import POLICY_PARAMS, action_cost_paise, opt_out_cost_paise
from settle.schema.action import Action, DoNothing
from settle.schema.decision import Alternative
from settle.schema.enums import ActionType, ArmMode
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState

ECONOMIC_STOP_MULTIPLE: Final[float] = float(POLICY_PARAMS["economic_stop_multiple"])


@dataclass(frozen=True)
class PolicyDecision:
    """What the policy chose, what it declined, and why."""

    action: Action
    p_success: float
    uplift: float
    expected_value: int
    alternatives: list[Alternative]
    reason_code: str
    economic_stop: bool = False


def total_cost_paise(case: ObservedCase, action: Action) -> float:
    """Action cost plus the opt-out cost it risks. §20, A26."""
    channel = action_channel(action)
    return action_cost_paise(action.type, channel) + opt_out_cost_paise(
        action.type, case.plan_value_paise, channel
    )


def expected_value(case: ObservedCase, action: Action, uplift: float) -> float:
    """SPEC §10.2, verbatim."""
    return uplift * case.amount_paise - total_cost_paise(case, action)


def choose(
    case: ObservedCase,
    state: CaseState,
    legal: list[Action],
    estimator,
    arm_mode: ArmMode = ArmMode.ENFORCE,
) -> PolicyDecision:
    """Pick the highest-EV gate-passing action, or stop."""
    from settle.runner.arms.explore import candidate_pairs

    # A71's binding constraint: OURS searches the same grid EXPLORE sampled.
    # An estimator trained on one grid and queried on another has zero coverage
    # exactly where it is asked to predict.
    pairs = candidate_pairs(case, state) if legal else []

    # One batched call rather than one per candidate. A policy that queried the
    # model per option would spend most of a run inside sklearn's per-call
    # overhead, and the grid is the same shape every time.
    probabilities = estimator.predict_pairs(case, [DoNothing(), *pairs], state.tick)
    baseline = probabilities[0]

    alternatives: list[Alternative] = []
    best: tuple[float, float, Action] | None = None

    for action, p in zip(pairs, probabilities[1:]):
        verdict = evaluate_gates(case, state, action, ArmMode.ENFORCE)
        uplift = p - baseline
        ev = expected_value(case, action, uplift)
        alternatives.append(
            Alternative(
                action=action,
                p_success=max(0.0, min(1.0, p)),
                ev_paise=int(round(ev)),
                legal=verdict.allowed,
                block_gate=None if verdict.allowed else verdict.first_block,
            )
        )
        if not verdict.allowed:
            continue
        # Ties break toward the cheaper action, then toward do_nothing — which
        # is free, so it wins any tie it is in.
        key = (ev, -total_cost_paise(case, action))
        if best is None or key > (best[0], -total_cost_paise(case, best[2])):
            best = (ev, uplift, action)

    if best is None:
        return PolicyDecision(
            action=DoNothing(), p_success=baseline, uplift=0.0, expected_value=0,
            alternatives=alternatives, reason_code="NO_LEGAL_ACTION",
        )

    ev, uplift, action = best
    if action.type is ActionType.DO_NOTHING:
        return PolicyDecision(
            action=action, p_success=baseline, uplift=0.0, expected_value=0,
            alternatives=alternatives, reason_code="DO_NOTHING_DOMINATES",
        )

    # S7. §13: expected recovery below a multiple of cost is not worth taking.
    # Evaluated on the *best* option, so the stop means "nothing here pays",
    # not "this one action does not".
    gross = uplift * case.amount_paise
    if gross < ECONOMIC_STOP_MULTIPLE * total_cost_paise(case, action):
        return PolicyDecision(
            action=DoNothing(), p_success=baseline, uplift=uplift,
            expected_value=int(round(ev)), alternatives=alternatives,
            reason_code="S7_ECONOMIC_STOP", economic_stop=True,
        )

    return PolicyDecision(
        action=action,
        p_success=max(0.0, min(1.0, baseline + uplift)),
        uplift=uplift,
        expected_value=int(round(ev)),
        alternatives=alternatives,
        reason_code="EV_ARGMAX",
    )
