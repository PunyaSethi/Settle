"""Stops. SPEC §13, §13.2.

A stop is terminal. Post-stop events are recorded and do nothing.

Two classes, and the split is load-bearing (§13.2):

  COMPLIANCE      S4 opt-out, S5 dispute.
                  Relaxed in OBSERVE so B3 can actually breach INV-3. Without
                  the relaxation S4 would fire before G7 was ever consulted,
                  G7 and G8 would be shadowed, and the unguarded baseline would
                  report structurally zero opt-out violations while appearing
                  to test them.

  TERMINAL_STATE  S1 recovered, S2 dead instrument with no customer path,
                  S3 budget exhausted, S6 decision horizon.
                  Binding in every arm without exception.

S7, the economic stop, is deliberately NOT here. It compares expected recovery
against action cost and nuisance, which means it needs an EV, which means it
needs the estimator. It belongs in the policy at D3. Putting it here would drag
a model dependency into a module whose whole value is being a pure function of
its arguments.

Budget constants are ASSERTED and carry no PRIORS row yet — `PRIORS.md` was not
in this checkpoint's allowlist. S3's budgets bound B3's violation count, which
is a reported number, so INV-10 applies. See the CP3 report.
"""

from typing import Final, NamedTuple

from settle.schema.enums import ArmMode, DeclineClass, StopClass
from settle.schema.observed import ObservedCase
from settle.policy.params import POLICY_PARAMS
from settle.schema.state import CaseState, CaseStatus
from settle.diagnose.taxonomy import classify

# S3 — SPEC §13 gives no numbers. These live in POLICY_PARAMS with rows in
# PRIORS.md under Policy constants, because they bound B3's violation count and
# a number that bounds a reported metric is INV-10's business (SPEC §15).
ATTEMPT_BUDGET: Final[int] = int(POLICY_PARAMS["attempt_budget"])
CONTACT_BUDGET: Final[int] = int(POLICY_PARAMS["contact_budget"])

# S6 — SPEC §13.1. The agent stops acting at 30 days; the world runs to 60.
DECISION_HORIZON_DAYS: Final[int] = 30
DECISION_HORIZON_HOURS: Final[int] = DECISION_HORIZON_DAYS * 24


class StopVerdict(NamedTuple):
    """Which stop fired, and whether it binds in this arm."""

    stop: str
    stop_class: StopClass
    reason_code: str


def _terminal(stop: str, reason_code: str) -> StopVerdict:
    return StopVerdict(stop, StopClass.TERMINAL_STATE, reason_code)


def _compliance(stop: str, reason_code: str) -> StopVerdict:
    return StopVerdict(stop, StopClass.COMPLIANCE, reason_code)


def check_stops(
    case: ObservedCase,
    state: CaseState,
    arm_mode: ArmMode,
) -> StopVerdict | None:
    """The first stop that applies, or None.

    S1 reads `state.settled`, a recorded field (§5.7, A60). It was briefly a
    keyword argument; that was inference by another name, and §5.7's rule is
    that state transitions are recorded. The flag arrives from reconciliation,
    which is the only thing entitled to say a settlement happened (INV-1).
    """
    if state.status is CaseStatus.STOPPED:
        return StopVerdict(
            state.stop_reason or "UNKNOWN",
            state.stop_class or StopClass.TERMINAL_STATE,
            "ALREADY_STOPPED",
        )

    # --- TERMINAL_STATE: binding in every arm, OBSERVE included -------------

    # S1. INV-1: a settlement record, never an authorisation.
    if state.settled:
        return _terminal("S1", "S1_RECOVERED_SETTLED")

    # S2. A dead instrument is only terminal when there is no way left to ask
    # for a new one. Opted out and dead is the end of the road; dead alone is
    # still a `request_mandate_update` away from recovery.
    if classify(case.decline_code) is DeclineClass.DEAD_INSTRUMENT and state.opted_out:
        return _terminal("S2", "S2_DEAD_INSTRUMENT_NO_PATH")

    # S3.
    if state.attempts_used >= ATTEMPT_BUDGET:
        return _terminal("S3", "S3_ATTEMPT_BUDGET_EXHAUSTED")
    if state.contacts_used >= CONTACT_BUDGET:
        return _terminal("S3", "S3_CONTACT_BUDGET_EXHAUSTED")

    # S6.
    if state.tick >= DECISION_HORIZON_HOURS:
        return _terminal("S6", "S6_DECISION_HORIZON")

    # --- COMPLIANCE: relaxed in OBSERVE (§13.2) -----------------------------

    if arm_mode is ArmMode.OBSERVE:
        return None

    if state.opted_out:
        return _compliance("S4", "S4_OPT_OUT")

    if state.disputed:
        return _compliance("S5", "S5_DISPUTE_RAISED")

    return None


def accepts_dispatch(state: CaseState) -> bool:
    """Whether a dispatch may proceed at all. SPEC §13.

    Terminal in every mode, OBSERVE included: OBSERVE relaxes which *stops fire*,
    not whether an already-stopped case can be acted on. Test ADV-1 fires events
    at stopped cases and asserts zero dispatches.
    """
    return state.status is CaseStatus.OPEN
