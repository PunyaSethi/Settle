"""POST /policy/decide — price one case, live. SPEC §9, §10, §12.

A judge types a case and watches OURS price every option it is allowed to take.
Same policy, same gates, same estimator, same numbers as the batch: this module
builds an `ObservedCase` and a `CaseState`, hands them to `legal_actions` and
`policy.choose`, and formats the result. There is no decision logic here.

That is the whole design constraint. A live demo that ran a second, friendlier
policy would be worth less than no live demo — the point is not that the screen
produces a plausible answer, it is that the answer is the one the 10,000-case
run would have produced for the same case. DEC-1 asserts it against
`policy.choose` called directly.

Nothing was added to `choose()`
-------------------------------
Its signature already takes everything needed: `(case, state, legal, estimator,
arm_mode)`. `legal` comes from `legal_actions(case, state)`, exactly as
`case_runner` builds it. `p_settle(do_nothing)` — which the screen shows because
every EV is an uplift over it — is not returned by `choose`, but it does not
need to be: `PolicyDecision` carries `p_success` and `uplift`, and the baseline
is their difference. Deriving it beat widening the signature of the function the
batch depends on.

What the class excluded
-----------------------
The screen distinguishes two ways an action can be unavailable, because they
look identical in a results table and mean opposite things:

    excluded    §9 says this verb is not viable for this decline class. It was
                never a candidate. A retry on a revoked mandate is not a
                blocked retry, it is not a retry at all.
    blocked     a gate refused it. It was priced, it may have had the best EV,
                and a named rule stopped it.

`legal_actions` returns the first set already; this reports what it left out and
why, so "no retry option" reads as a diagnosis rather than a missing row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from settle.agent.policy import choose, total_cost_paise
from settle.diagnose.taxonomy import classify, viable_actions
from settle.policy.escalation import is_escalation_eligible
from settle.policy.gates import evaluate_gates
from settle.policy.legal import legal_actions
from settle.schema.enums import ActionType, ArmMode, Language, MandateState, Rail
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState, as_of

__all__ = ["DecideRequest", "decide", "router"]

router = APIRouter()

# The anchor a typed case is evaluated against. A constant, not a clock: gates
# derive the IST hour from `created_at + tick`, so a wall-clock anchor would make
# the same form produce different verdicts at different times of day and the
# screen would stop being reproducible.
#
# 04:30 UTC is 10:00 IST, which puts tick 0 inside G1's 08:00-19:00 window. A
# judge who hits Decide immediately should not meet a contact blocked by the
# clock as their first impression of the gates — that reads as the demo being
# broken. tick 16 lands at 02:00 IST, which is what the night-time preset uses.
DEFAULT_CREATED_AT: Final[datetime] = datetime(2026, 1, 15, 4, 30, tzinfo=timezone.utc)

# Every verb in the closed set, so "excluded" can be reported as a difference
# rather than as an absence.
ALL_VERBS: Final[tuple[ActionType, ...]] = tuple(ActionType)


class DecideRequest(BaseModel):
    """The form. `ObservedCase` fields, plus the `CaseState` overrides §5.7
    exposes to a demo — the ones that change a gate verdict."""

    model_config = ConfigDict(extra="forbid")

    # --- ObservedCase ------------------------------------------------------
    case_id: str = Field(default="typed_case", min_length=1, max_length=64)
    customer_id: str = Field(default="typed_customer", min_length=1, max_length=64)
    amount_paise: int = Field(default=249900, gt=0)
    rail: Rail = Rail.CARD
    decline_code: str = Field(default="insufficient_funds", min_length=1, max_length=64)
    decline_reason: str = Field(default="", max_length=256)
    attempt_number: int = Field(default=1, ge=1, le=20)
    mandate_state: MandateState = MandateState.ACTIVE
    mandate_cap_paise: int | None = Field(default=None, ge=0)
    tenure_months: int = Field(default=6, ge=0, le=600)
    prior_failures: int = Field(default=0, ge=0, le=100)
    prior_recoveries: int = Field(default=0, ge=0, le=100)
    plan_value_paise: int = Field(default=249900, gt=0)
    observed_credit_day: int | None = Field(default=None, ge=1, le=28)
    consent_whatsapp: bool = True
    dnd_flag: bool = False
    language: Language = Language.HINGLISH

    # --- CaseState overrides ----------------------------------------------
    tick: int = Field(default=0, ge=0, le=720)
    attempts_used: int = Field(default=0, ge=0, le=20)
    contacts_used: int = Field(default=0, ge=0, le=50)
    card_submissions_used: int = Field(default=0, ge=0, le=20)
    opted_out: bool = False
    disputed: bool = False
    promise_date: date | None = None
    notice_window_hours: int | None = Field(default=None, ge=0, le=720)
    last_attempt_tick: int | None = Field(default=None, ge=0, le=720)

    def observed(self) -> ObservedCase:
        return ObservedCase(
            case_id=self.case_id,
            created_at=DEFAULT_CREATED_AT,
            customer_id=self.customer_id,
            amount_paise=self.amount_paise,
            rail=self.rail,
            decline_code=self.decline_code,
            decline_reason=self.decline_reason or self.decline_code.replace("_", " "),
            attempt_number=self.attempt_number,
            mandate_state=self.mandate_state,
            mandate_cap_paise=self.mandate_cap_paise,
            tenure_months=self.tenure_months,
            prior_failures=self.prior_failures,
            prior_recoveries=self.prior_recoveries,
            plan_value_paise=self.plan_value_paise,
            observed_credit_day=self.observed_credit_day,
            consent_whatsapp=self.consent_whatsapp,
            dnd_flag=self.dnd_flag,
            language=self.language,
        )

    def state(self) -> CaseState:
        """A `CaseState` the gates will read exactly as they do in a run."""
        window = (
            DEFAULT_CREATED_AT + timedelta(hours=self.notice_window_hours)
            if self.notice_window_hours is not None
            else None
        )
        return CaseState(
            case_id=self.case_id,
            arm="OURS",
            arm_mode=ArmMode.ENFORCE,
            tick=self.tick,
            attempts_used=self.attempts_used,
            contacts_used=self.contacts_used,
            card_submissions_used=self.card_submissions_used,
            opted_out=self.opted_out,
            disputed=self.disputed,
            promise_date=self.promise_date,
            promise_logged_at=DEFAULT_CREATED_AT if self.promise_date else None,
            notice_window_until=window,
            last_attempt_tick=self.last_attempt_tick,
        )


def _action_label(action: Any) -> str:
    payload = action.model_dump(mode="json")
    bits = []
    if payload.get("at_hour_offset") is not None:
        bits.append(f"+{payload['at_hour_offset']}h")
    for key in ("rail", "to", "channel", "template_id"):
        if payload.get(key):
            bits.append(str(payload[key]))
    return f"{payload['type']} {' '.join(bits)}".strip()


def decide(request: DecideRequest, estimator) -> dict[str, Any]:
    """One case in, the whole priced option set out.

    Split from the route so DEC-1 can compare it against `policy.choose` called
    directly, through the same code the endpoint runs.
    """
    case = request.observed()
    state = request.state()

    decline_class = classify(case.decline_code)
    eligible = is_escalation_eligible(case)
    viable = viable_actions(decline_class, eligible)
    legal = legal_actions(case, state)

    result = choose(case, state, legal, estimator, ArmMode.ENFORCE)

    # Every EV on the screen is an uplift over doing nothing, so the screen
    # shows the term being subtracted. `choose` does not return it; it is
    # p_success minus uplift, which is exact rather than a second estimate.
    baseline = result.p_success - result.uplift

    chosen_label = _action_label(result.action)
    alternatives = []
    for alt in result.alternatives:
        label = _action_label(alt.action)
        alternatives.append({
            "action_label": label,
            "action_type": alt.action.type.value,
            "p_settle": alt.p_success,
            "p_settle_pct": f"{alt.p_success * 100:.1f}%",
            "uplift": alt.p_success - baseline,
            "uplift_pct": f"{(alt.p_success - baseline) * 100:+.2f}%",
            "ev_paise": alt.ev_paise,
            "ev_rupees": f"{alt.ev_paise / 100:,.2f}",
            "cost_rupees": f"{total_cost_paise(case, alt.action) / 100:,.2f}",
            "legal": alt.legal,
            "block_gate": alt.block_gate,
            "chosen": label == chosen_label,
        })
    alternatives.sort(key=lambda a: -a["ev_paise"])

    excluded = [
        {
            "action_type": verb.value,
            "why": (
                f"§9 does not make {verb.value} viable for {decline_class.value}"
                + ("" if eligible else "; this case is not escalation-eligible")
            ),
        }
        for verb in ALL_VERBS
        if verb not in viable
    ]

    # do_nothing is always legal and always gated, so it is the one row that
    # shows the gate verdict for an action the policy can always fall back to.
    do_nothing_verdict = evaluate_gates(
        case, state, next(a for a in legal if a.type is ActionType.DO_NOTHING),
        ArmMode.ENFORCE,
    ) if any(a.type is ActionType.DO_NOTHING for a in legal) else None

    return {
        "case": {
            "case_id": case.case_id,
            "amount_rupees": f"{case.amount_paise / 100:,.2f}",
            "rail": case.rail.value,
            "decline_code": case.decline_code,
            "attempt_number": case.attempt_number,
            "mandate_state": case.mandate_state.value,
            "consent_whatsapp": case.consent_whatsapp,
            "dnd_flag": case.dnd_flag,
            "created_at": case.created_at.isoformat(),
        },
        "state": {
            "tick": state.tick,
            "as_of": as_of(case.created_at, state).isoformat(),
            "ist_hour": (as_of(case.created_at, state) + timedelta(hours=5, minutes=30)).hour,
            "attempts_used": state.attempts_used,
            "contacts_used": state.contacts_used,
            "card_submissions_used": state.card_submissions_used,
            "opted_out": state.opted_out,
            "disputed": state.disputed,
            "promise_date": state.promise_date.isoformat() if state.promise_date else None,
            "notice_window_until": (
                state.notice_window_until.isoformat() if state.notice_window_until else None
            ),
        },
        "diagnosis": {
            "decline_class": decline_class.value,
            "escalation_eligible": eligible,
            "viable_verbs": sorted(v.value for v in viable),
            "excluded_verbs": excluded,
            "n_legal_actions": len(legal),
        },
        "decision": {
            "chosen_label": chosen_label,
            "chosen_type": result.action.type.value,
            "reason_code": result.reason_code,
            "economic_stop": result.economic_stop,
            "p_settle": result.p_success,
            "p_settle_pct": f"{result.p_success * 100:.1f}%",
            "p_settle_do_nothing": baseline,
            "p_settle_do_nothing_pct": f"{baseline * 100:.1f}%",
            "uplift": result.uplift,
            "uplift_pct": f"{result.uplift * 100:+.2f}%",
            "expected_value_paise": result.expected_value,
            "expected_value_rupees": f"{result.expected_value / 100:,.2f}",
            "n_alternatives": len(alternatives),
            "n_blocked": sum(1 for a in alternatives if not a["legal"]),
            "do_nothing_gate": (
                do_nothing_verdict.first_block if do_nothing_verdict
                and not do_nothing_verdict.allowed else None
            ),
        },
        "alternatives": alternatives,
    }


@router.post("/policy/decide")
async def policy_decide(payload: dict[str, Any]) -> JSONResponse:
    """Validate, decide, return. Nonsense is refused rather than priced."""
    try:
        request = DecideRequest.model_validate(payload)
    except ValidationError as error:
        # Field and message, not a pydantic dump. A judge who typed 0 into
        # `amount` should read "amount_paise: greater than 0", not a URL.
        return JSONResponse(
            {
                "reason_code": "INVALID_CASE",
                "detail": "the case was rejected rather than priced from nonsense",
                "errors": [
                    {
                        "field": ".".join(str(p) for p in e["loc"]) or "(body)",
                        "message": e["msg"],
                    }
                    for e in error.errors()
                ],
            },
            status_code=422,
        )

    from settle.agent.estimator import load_latest

    estimator = load_latest()
    if estimator is None:
        return JSONResponse(
            {
                "reason_code": "MODEL_UNAVAILABLE",
                "detail": (
                    "no model in out/. Train one, or run the batch report — the "
                    "screen prices options with the shipped estimator and will "
                    "not substitute a stand-in."
                ),
            },
            status_code=503,
        )

    return JSONResponse(decide(request, estimator), status_code=200)
