"""Gates. SPEC §12.

Eleven gates, evaluated in order, pure. Each returns a verdict whether or not it
applies, because "G1 did not apply" and "G1 passed" are different facts and the
audit trail needs both.

Every gate runs in every arm. What varies is whether the verdict binds: ENFORCE
blocks, OBSERVE records the violation and permits it (SPEC §4, §13.2). There is
one implementation and one code path — a gate reimplemented per arm is a gate
that can disagree with itself.

No hour argument
----------------
The IST evaluation hour is derived from `case.created_at + state.tick`. A
separate `hour` parameter would let a caller pass a tick and an hour that
disagree, and nothing would catch it: G2 would measure its rolling window
against one instant while G1 judged the window against another. An inconsistent
pair must be unrepresentable, not merely undocumented (SPEC §12). A dispatch
proposed for a future hour is expressed by advancing the tick.

Which actions each gate applies to
----------------------------------
G1, G2, G7  contact-bearing only. A silent retry is a message to the bank, not
            to a person. Gating it on the contact window would suppress the one
            action §9 calls viable for `time_shiftable` at exactly the hour it
            works, and would count a machine retry against a human's patience.
G3, G4, G9  debit-bearing only.
G10         retries only. A rail switch is not a retry, and capping it under
            G10 would forbid the action §9 calls viable for `auth_abandoned`.
G11         `voice_call` only. DND covers unsolicited commercial calling.
G5, G6, G8  everything that is actually dispatched (G6 narrows to contacts, per
            INV-7's wording: "no *contact* between a logged promise and its
            promise date").

Numeric constants live in `settle.policy.params.POLICY_PARAMS` with rows in
PRIORS.md under Policy constants, and PAR-1 enforces the correspondence.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Final, NamedTuple

from settle.diagnose.taxonomy import classify
from settle.policy.legal import is_contact, is_debit
from settle.policy.params import POLICY_PARAMS, class_retry_cap
from settle.schema.action import Action, Retry, SwitchRail
from settle.schema.canonical import canonical_json
from settle.schema.enums import ActionType, ArmMode, MandateState, Rail
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState, as_of

IST: Final = timezone(timedelta(hours=5, minutes=30))

# G1 — SPEC §12, INV-2. Half-open: the window closes at 19:00, so 19:xx is out.
CONTACT_WINDOW_START_HOUR_IST: Final[int] = 8
CONTACT_WINDOW_END_HOUR_IST: Final[int] = 19

FREQUENCY_CAP_PER_WINDOW: Final[int] = int(POLICY_PARAMS["frequency_cap_per_window"])
FREQUENCY_WINDOW_HOURS: Final[int] = int(POLICY_PARAMS["frequency_window_hours"])
MIN_CONTACT_GAP_HOURS: Final[int] = int(POLICY_PARAMS["min_contact_gap_hours"])
CARD_NETWORK_RETRY_CAP: Final[int] = int(POLICY_PARAMS["card_network_retry_cap"])
NOTICE_WINDOW_DAYS: Final[int] = int(POLICY_PARAMS["notice_window_days"])


class GateVerdict(NamedTuple):
    """One gate's answer. `allowed` is the verdict, not the outcome."""

    allowed: bool
    gate: str
    reason_code: str


class GateResult(NamedTuple):
    """All eleven verdicts, plus what the arm mode does with them."""

    verdicts: tuple[GateVerdict, ...]
    allowed: bool
    blocked_by: tuple[str, ...]
    violations: tuple[str, ...]

    @property
    def first_block(self) -> str | None:
        """The gate `Alternative.block_gate` should name (SPEC §5.4)."""
        return self.blocked_by[0] if self.blocked_by else None


def ist_hour(at: datetime) -> int:
    """Hour of day in IST. The only timezone INV-2 recognises."""
    return at.astimezone(IST).hour


def evaluation_hour(case: ObservedCase, state: CaseState) -> int:
    """The IST hour a dispatch would land on. Derived, never supplied."""
    return ist_hour(as_of(case.created_at, state))


def target_rail(case: ObservedCase, action: Action) -> Rail:
    """The rail a debit would actually run on."""
    if isinstance(action, Retry):
        return action.rail
    if isinstance(action, SwitchRail):
        return action.to
    return case.rail


def idempotency_key(case: ObservedCase, state: CaseState, action: Action) -> str:
    """A dispatch's identity. INV-4.

    Pure and content-addressed: the same action for the same case at the same
    tick is the same dispatch, however many times the caller asks. Derived from
    canonical JSON so the key cannot shift with dict ordering or across
    processes. The hour is not a component because it is a function of the tick.
    """
    payload = canonical_json(
        {"case_id": case.case_id, "tick": state.tick, "action": action}
    )
    return hashlib.sha256(payload).hexdigest()[:32]


# ---------------------------------------------------------------------------
# G1 — contact window
# ---------------------------------------------------------------------------

def gate_g1(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """Contact window 08:00–19:00 IST. INV-2."""
    if not is_contact(action):
        return GateVerdict(True, "G1", "G1_NOT_A_CONTACT")
    hour = evaluation_hour(case, state)
    if CONTACT_WINDOW_START_HOUR_IST <= hour < CONTACT_WINDOW_END_HOUR_IST:
        return GateVerdict(True, "G1", "G1_INSIDE_WINDOW")
    return GateVerdict(False, "G1", "G1_OUTSIDE_CONTACT_WINDOW")


# ---------------------------------------------------------------------------
# G2 — frequency cap
# ---------------------------------------------------------------------------

def gate_g2(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """3 contacts per rolling 168h, and 20h minimum between any two."""
    if not is_contact(action):
        return GateVerdict(True, "G2", "G2_NOT_A_CONTACT")

    now = as_of(case.created_at, state)
    window_start = now - timedelta(hours=FREQUENCY_WINDOW_HOURS)
    recent = [at for at in state.contact_history if at > window_start]
    if len(recent) >= FREQUENCY_CAP_PER_WINDOW:
        return GateVerdict(False, "G2", "G2_FREQUENCY_CAP")

    if state.last_contact_at is not None:
        gap_hours = (now - state.last_contact_at).total_seconds() / 3600.0
        if gap_hours < MIN_CONTACT_GAP_HOURS:
            return GateVerdict(False, "G2", "G2_MINIMUM_GAP")

    return GateVerdict(True, "G2", "G2_WITHIN_CAP")


# ---------------------------------------------------------------------------
# G3 — mandate validity
# ---------------------------------------------------------------------------

def gate_g3(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """No debit without a live mandate."""
    if not is_debit(action):
        return GateVerdict(True, "G3", "G3_NOT_A_DEBIT")
    if case.mandate_state is MandateState.ACTIVE:
        return GateVerdict(True, "G3", "G3_MANDATE_ACTIVE")
    return GateVerdict(False, "G3", f"G3_MANDATE_{case.mandate_state.value.upper()}")


# ---------------------------------------------------------------------------
# G4 — card-network retry cap
# ---------------------------------------------------------------------------

def gate_g4(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """Card networks cap retries on a declined credential."""
    if not is_debit(action):
        return GateVerdict(True, "G4", "G4_NOT_A_DEBIT")
    if target_rail(case, action) is not Rail.CARD:
        return GateVerdict(True, "G4", "G4_NOT_A_CARD_RAIL")
    if state.attempts_used >= CARD_NETWORK_RETRY_CAP:
        return GateVerdict(False, "G4", "G4_CARD_RETRY_CAP")
    return GateVerdict(True, "G4", "G4_WITHIN_CARD_CAP")


# ---------------------------------------------------------------------------
# G5 — idempotency
# ---------------------------------------------------------------------------

def gate_g5(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """No two dispatches share a key. INV-4."""
    if action.type is ActionType.DO_NOTHING:
        return GateVerdict(True, "G5", "G5_NOT_DISPATCHED")
    if idempotency_key(case, state, action) in state.dispatched_keys:
        return GateVerdict(False, "G5", "G5_DUPLICATE_KEY")
    return GateVerdict(True, "G5", "G5_KEY_UNSEEN")


# ---------------------------------------------------------------------------
# G6 — promise suppression
# ---------------------------------------------------------------------------

def gate_g6(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """No contact between a logged promise and its date. INV-7.

    Wrongly logging a brush-off as a promise suppresses contact for weeks, which
    is why §11 refuses to treat a hedged reply as one. This gate is the second
    half of that bargain: a promise that IS logged is honoured to the day.
    """
    if not is_contact(action):
        return GateVerdict(True, "G6", "G6_NOT_A_CONTACT")
    if state.promise_date is None:
        return GateVerdict(True, "G6", "G6_NO_PROMISE")
    if as_of(case.created_at, state).date() < state.promise_date:
        return GateVerdict(False, "G6", "G6_PROMISE_SUPPRESSION")
    return GateVerdict(True, "G6", "G6_PROMISE_ELAPSED")


# ---------------------------------------------------------------------------
# G7 — opt-out
# ---------------------------------------------------------------------------

def gate_g7(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """Opt-out honoured on every channel. INV-3.

    Channel-blind by construction: it tests the action's contact-bearing nature,
    never its channel, so adding a channel to §5.3 cannot open a hole here.
    """
    if not is_contact(action):
        return GateVerdict(True, "G7", "G7_NOT_A_CONTACT")
    if state.opted_out:
        return GateVerdict(False, "G7", "G7_OPTED_OUT")
    return GateVerdict(True, "G7", "G7_NOT_OPTED_OUT")


# ---------------------------------------------------------------------------
# G8 — dispute freeze
# ---------------------------------------------------------------------------

def gate_g8(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """A raised dispute freezes collection.

    `escalate_human` is exempt: it is the path by which a dispute gets resolved,
    and freezing it would make the freeze permanent.
    """
    if not state.disputed:
        return GateVerdict(True, "G8", "G8_NO_DISPUTE")
    if action.type is ActionType.ESCALATE_HUMAN:
        return GateVerdict(True, "G8", "G8_ESCALATION_EXEMPT")
    if is_contact(action) or is_debit(action):
        return GateVerdict(False, "G8", "G8_DISPUTE_FREEZE")
    return GateVerdict(True, "G8", "G8_NOT_COLLECTION")


# ---------------------------------------------------------------------------
# G9 — e-mandate pre-debit notice
# ---------------------------------------------------------------------------

def gate_g9(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """A debit outside an active notice window is blocked. SPEC §12.

    This sequences the plan rather than ticking a box: on `enach` the agent must
    spend a contact on `serve_notice` first, and that contact costs G2 budget and
    patience. The cost is the point — an executor that served notices implicitly
    would hide it from the decision.
    """
    if not is_debit(action):
        return GateVerdict(True, "G9", "G9_NOT_A_DEBIT")
    if target_rail(case, action) is not Rail.ENACH:
        return GateVerdict(True, "G9", "G9_NOT_ENACH")
    if state.notice_window_until is None:
        return GateVerdict(False, "G9", "G9_NO_NOTICE_SERVED")
    if as_of(case.created_at, state) > state.notice_window_until:
        return GateVerdict(False, "G9", "G9_NOTICE_WINDOW_EXPIRED")
    return GateVerdict(True, "G9", "G9_INSIDE_NOTICE_WINDOW")


# ---------------------------------------------------------------------------
# G10 — class retry budget
# ---------------------------------------------------------------------------

def gate_g10(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """Per-class cap on retries, distinct from G4's card-network cap.

    `ambiguous` is 1: §9 permits one retry at a different hour, then a message.
    That is a sentence about the class, not about the card network, and without
    this gate it was unenforced — `ambiguous` retries ran until G4 or S3 fired,
    which is a different rule than the one §9 states.

    Retries only. A rail switch is not a retry, and capping it here would forbid
    the action §9 calls viable for `auth_abandoned`.
    """
    if action.type is not ActionType.RETRY:
        return GateVerdict(True, "G10", "G10_NOT_A_RETRY")
    cap = class_retry_cap(classify(case.decline_code))
    if state.attempts_used >= cap:
        return GateVerdict(False, "G10", "G10_CLASS_RETRY_CAP")
    return GateVerdict(True, "G10", "G10_WITHIN_CLASS_CAP")


# ---------------------------------------------------------------------------
# G11 — TRAI DND registry
# ---------------------------------------------------------------------------

def gate_g11(case: ObservedCase, state: CaseState, action: Action) -> GateVerdict:
    """Blocks `voice_call` when `dnd_flag` is set.

    Not SMS or WhatsApp. DND covers unsolicited commercial contact, and a
    transactional message to an existing customer about a failed payment on
    their own subscription is exempt. That exemption is ASSERTED and recorded in
    Known Limitations — if it turns out to be wrong, this gate widens to every
    contact-bearing action and the compliance numbers move with it.
    """
    if action.type is not ActionType.VOICE_CALL:
        return GateVerdict(True, "G11", "G11_NOT_A_VOICE_CALL")
    if case.dnd_flag:
        return GateVerdict(False, "G11", "G11_DND_REGISTERED")
    return GateVerdict(True, "G11", "G11_NOT_ON_DND")


GATES: Final[tuple] = (
    gate_g1,
    gate_g2,
    gate_g3,
    gate_g4,
    gate_g5,
    gate_g6,
    gate_g7,
    gate_g8,
    gate_g9,
    gate_g10,
    gate_g11,
)


def evaluate_gates(
    case: ObservedCase,
    state: CaseState,
    action: Action,
    arm_mode: ArmMode,
) -> GateResult:
    """Run all eleven, in order, and return every verdict.

    Not short-circuited on the first block. The audit trail needs the whole
    picture, and a rejected alternative has to name the gate that stopped it
    (§5.4) — which is unknowable if evaluation stopped early.
    """
    verdicts = tuple(gate(case, state, action) for gate in GATES)
    blocked_by = tuple(v.gate for v in verdicts if not v.allowed)

    if arm_mode is ArmMode.OBSERVE:
        # B3 only (INV-11). The block is recorded as a violation and permitted.
        return GateResult(verdicts, True, blocked_by, blocked_by)

    return GateResult(verdicts, not blocked_by, blocked_by, ())


def after_serve_notice(case: ObservedCase, state: CaseState) -> CaseState:
    """The state transition `serve_notice` performs. SPEC §12 G9.

    Recorded, never inferred (§5.7). The window runs `NOTICE_WINDOW_DAYS` from
    the moment the notice is served; retries inside it inherit the notice, and
    retries outside need a fresh one.
    """
    served_at = as_of(case.created_at, state)
    return state.model_copy(
        update={"notice_window_until": served_at + timedelta(days=NOTICE_WINDOW_DAYS)}
    )
