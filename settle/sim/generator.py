"""Batch generator. SPEC §5.1, §5.2, §9.

Produces N `ObservedCase` and N `HiddenTruth`, seed-stable and
order-independent. Every case is derived by hashing `(seed, case_id, field)`,
so adding a field to case 5 cannot shift case 6, and regenerating case 4000
alone gives the same case it had in a full batch.

Every number that shapes a distribution lives in `PARAMS` and nowhere else.
INV-10 requires each of them to trace to a cited source or be marked ASSERTED,
and GEN-4 enforces the correspondence with PRIORS.md in both directions — a
parameter with no row fails the build, and so does a row with no parameter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Final

from pydantic import BaseModel, ConfigDict

from settle.schema.enums import (
    DebtorBehaviour,
    DeclineClass,
    IntentType,
    Language,
    MandateState,
    Rail,
)
from settle.policy.escalation import (
    ESCALATION_MIN_AMOUNT_PAISE,
    ESCALATION_MIN_ATTEMPT_NUMBER,
    is_escalation_eligible,
)
from settle.schema.observed import ObservedCase
from settle.sim.streams import derive_unit_float
from settle.sim.truth import GeneratedCase, HiddenTruth

# ---------------------------------------------------------------------------
# PARAMS — every number that shapes a distribution. All ASSERTED pending D4.
# ---------------------------------------------------------------------------

PARAMS: Final[dict[str, float]] = {
    # --- rail mix (§5.1) ---
    "rail_mix.card": 0.42,
    "rail_mix.upi_autopay": 0.45,
    "rail_mix.enach": 0.13,
    # --- decline class mix (§9) ---
    "decline_class_mix.time_shiftable": 0.46,
    "decline_class_mix.transient": 0.13,
    "decline_class_mix.dead_instrument": 0.17,
    "decline_class_mix.auth_abandoned": 0.11,
    "decline_class_mix.ambiguous": 0.10,
    "decline_class_mix.terminal": 0.03,
    # §9 caps the unmapped-code rate at 5%. Generating a non-zero rate below it
    # is what proves the ambiguous fallback path is exercised rather than dead.
    "unmapped_code_rate": 0.02,
    # --- amount (§5.1), lognormal on paise ---
    "amount.median_paise": 49900.0,
    "amount.log_sigma": 0.80,
    "amount.min_paise": 4900.0,
    "amount.max_paise": 999900.0,
    # A prorated debit is smaller than the plan it belongs to, which is the only
    # way amount_paise and plan_value_paise come apart.
    "plan_value.prorated_rate": 0.08,
    "plan_value.prorated_fraction": 0.5,
    # --- tenure and history (§5.1) ---
    "tenure.mean_months": 9.0,
    "tenure.max_months": 60.0,
    "attempt_number.decay": 0.45,
    "attempt_number.max": 4.0,
    "prior_failures.mean": 0.9,
    "prior_failures.max": 8.0,
    "prior_recoveries.mean": 0.6,
    "prior_recoveries.max": 8.0,
    # --- consent and contactability (§5.1) ---
    "consent_whatsapp_rate": 0.71,
    "dnd_flag_rate": 0.09,
    # --- language mix (§5.1) ---
    "language_mix.en": 0.27,
    "language_mix.hi": 0.21,
    "language_mix.hinglish": 0.52,
    # --- mandate state (§5.1) ---
    # Conditioned on the decline class. A case declined `mandate_revoked` while
    # reporting `mandate_state=active` is incoherent, and it would make G3
    # untestable against its own input.
    "mandate_state_base.active": 0.90,
    "mandate_state_base.expired": 0.05,
    "mandate_state_base.revoked": 0.03,
    "mandate_state_base.none": 0.02,
    "mandate_state_dead.active": 0.00,
    "mandate_state_dead.expired": 0.34,
    "mandate_state_dead.revoked": 0.58,
    "mandate_state_dead.none": 0.08,
    "mandate_cap.known_rate": 0.64,
    "mandate_cap.multiple": 3.0,
    # --- observed credit day (§5.1) — a noisy estimate of payday_day ---
    "observed_credit_day.known_rate": 0.31,
    "observed_credit_day.exact_rate": 0.72,
    "observed_credit_day.max_error_days": 3.0,
    # --- escalation-eligible slice (§2) ---
    # "High value, retries exhausted" — both conditions are observable, and
    # that is deliberate. Eligibility must be a pure function of ObservedCase
    # so a policy can recompute it at run time instead of being handed it from
    # the sim side, which would make it a channel (A46, GEN-6). An earlier
    # version drew a coin within the high-value population; that draw was not
    # recoverable from observables and had to go.
    "escalation.target_overall_rate": 0.15,
    "escalation.min_amount_paise": float(ESCALATION_MIN_AMOUNT_PAISE),
    "escalation.min_attempt_number": float(ESCALATION_MIN_ATTEMPT_NUMBER),
    # --- intent mix (§5.2) ---
    "intent_mix.willing_able": 0.33,
    "intent_mix.willing_broke": 0.38,
    "intent_mix.disputing": 0.07,
    "intent_mix.churned": 0.16,
    "intent_mix.adversarial": 0.06,
    # --- debtor behaviour mix (§8) ---
    "behaviour_mix.promise_and_break": 0.21,
    "behaviour_mix.dispute_stall": 0.08,
    "behaviour_mix.go_silent": 0.33,
    "behaviour_mix.opt_out_midway": 0.12,
    "behaviour_mix.hedged_reply": 0.20,
    "behaviour_mix.pay_then_complain": 0.06,
    # --- true recoverability by intent (§5.2) ---
    "recoverability_mean.willing_able": 0.78,
    "recoverability_mean.willing_broke": 0.42,
    "recoverability_mean.disputing": 0.15,
    "recoverability_mean.churned": 0.06,
    "recoverability_mean.adversarial": 0.11,
    "recoverability.spread": 0.12,
    # --- patience and payday (§5.2) ---
    "patience.mean": 4.0,
    "patience.min": 1.0,
    "patience.max": 9.0,
    "payday.first_of_month_rate": 0.46,
    "payday.seventh_rate": 0.18,
    # --- settlement and reversal (§5.2, §6) ---
    "will_settle_rate": 0.962,
    # A96. Was 38.0, which was faster than the settlement cycle this project
    # would cite for it: Razorpay settles T+2 *working* days from capture, so
    # 48h is the floor and 96h the weekend-spanning maximum. 56 sits between
    # them. Found by the CP11 sourcing pass, which is the pass working — a
    # number is not merely unsourced when it contradicts its own nearest source.
    "settlement_lag_h.mean": 56.0,
    "settlement_lag_h_max": 96.0,
    "will_reverse_rate": 0.011,
    "reversal_delay_days_max": 21.0,
    # --- response function (§5.2) ---
    "response.base_mean": 0.22,
    "response.payday_lift": 0.38,
    "response.hour_lift": 0.15,
    # --- authorisation without settlement (§6, world side) ---
    # Deliberately NOT an observability parameter. Whether an authorised
    # payment settles is a fact about the bank, not about our reporting, and
    # putting it in the reporting layer would let --perfect-observability
    # abolish SF-1 by making authorisation equivalent to settlement.
    "auth_no_settle_rate": 0.018,
    # --- action lift (§14.4, via world.p_authorise) ---
    # How much each verb moves P(authorise) relative to the case's own
    # recoverability. These decide whether a retry outperforms a message and
    # are therefore upstream of every rupee in §14.4 — "structural, not fitted"
    # is exactly the reasoning that lets an unsourced number reach a headline.
    "action_lift.do_nothing": 0.0,
    "action_lift.retry": 1.0,
    "action_lift.switch_rail": 1.05,
    "action_lift.send_message": 0.35,
    "action_lift.request_mandate_update": 0.45,
    "action_lift.serve_notice": 0.0,
    "action_lift.escalate_human": 0.9,
    "action_lift.voice_call": 0.7,
    # --- reply mix by debtor behaviour (§8) ---
    # P(reply kind | behaviour). Drives promise-kept rate and opt-outs induced,
    # both of which are headline metrics.
    "reply_mix.promise_and_break.promise": 0.70,
    "reply_mix.promise_and_break.hedged": 0.20,
    "reply_mix.promise_and_break.silence": 0.10,
    "reply_mix.dispute_stall.dispute": 0.65,
    "reply_mix.dispute_stall.hedged": 0.25,
    "reply_mix.dispute_stall.silence": 0.10,
    "reply_mix.go_silent.hedged": 0.15,
    "reply_mix.go_silent.silence": 0.85,
    "reply_mix.opt_out_midway.hedged": 0.45,
    "reply_mix.opt_out_midway.opt_out": 0.40,
    "reply_mix.opt_out_midway.silence": 0.15,
    "reply_mix.hedged_reply.hedged": 0.80,
    "reply_mix.hedged_reply.silence": 0.20,
    "reply_mix.pay_then_complain.complaint": 0.55,
    "reply_mix.pay_then_complain.hedged": 0.25,
    "reply_mix.pay_then_complain.silence": 0.20,
    # --- debtor disengagement and patience cost (§8) ---
    # Both move the opt-outs-induced count, a §14.4 headline.
    "debtor.disengage_after_contacts": 2.0,
    "patience.complaint_cost": 2.0,
    # --- p_authorise shape (§5.2, via world.p_authorise) ---
    # The multipliers and the day window that decide whether a retry beats a
    # message at 03:00. Changing any of them moves incremental recovery, so
    # INV-10 applies (SPEC §15) — "structural, not fitted" is not a defence.
    "p_authorise.base_floor": 0.5,
    "p_authorise.switch_rail_same_rail_penalty": 0.5,
    "p_authorise.retry_cross_rail_penalty": 0.9,
    "p_authorise.dnd_contact_penalty": 0.6,
    "p_authorise.day_window_start_hour": 9.0,
    "p_authorise.day_window_end_hour": 20.0,
    # --- natural recovery (§14.3, A77) ---
    # P(the case cures itself within the observation window, no arm involved).
    # Conditioned on intent: someone willing and able notices the failed debit
    # and pays; someone churned does not. Without this B0 recovers zero,
    # incremental equals gross, and `do_nothing` has no positive expected value
    # for any case — which makes contact restraint unreachable by construction.
    "natural_recovery.willing_able": 0.45,
    "natural_recovery.willing_broke": 0.18,
    "natural_recovery.disputing": 0.05,
    "natural_recovery.churned": 0.01,
    "natural_recovery.adversarial": 0.03,
    # A self-cure lands somewhere in the first N days; the draw picks when.
    "natural_recovery.max_day": 45.0,
    # --- mandate re-authorisation (§6, §9, A86) ---
    # P(a dispatched `request_mandate_update` is acted on and the mandate comes
    # back ACTIVE), conditioned on intent. Before A86 nothing revived a dead
    # mandate, so `dead_instrument` — 17% of the batch — was unwinnable by
    # construction and every arm's apparent restraint was inflated by it.
    #
    # This is now the highest-leverage unsourced number in the model: it decides
    # how much of that 17% is winnable at all. Set conservatively. Public
    # card-updater and dunning benchmarks cluster well above these figures, and
    # they measure a whole campaign rather than one request; these are per
    # request, and the batch-weighted rate lands near 0.18.
    #
    # Conditioned on intent because a churned customer does not re-authorise —
    # the whole point of asking is that only some customers still want the
    # service.
    "mandate_update.success_rate.willing_able": 0.35,
    "mandate_update.success_rate.willing_broke": 0.15,
    "mandate_update.success_rate.disputing": 0.03,
    "mandate_update.success_rate.churned": 0.01,
    "mandate_update.success_rate.adversarial": 0.02,
    # How long the customer takes to act on the link, in hours. Uniform on
    # [1, max]. The delay is the reason this is not a coin flip at dispatch: the
    # mandate is still dead while it runs, and the arm has to decide what to do
    # in the meantime.
    "mandate_update.response_delay_h_max": 72.0,
    # --- contact response (§6, A89) ---
    # P(a contacted customer goes and pays of their own accord), before the
    # verb's own lift and the debtor behaviour multiplier below:
    #
    #     p = contact_response.rate[intent]
    #         x action_lift[verb]
    #         x contact_response.behaviour_multiplier[behaviour]
    #         x (dnd penalty, for the verbs p_authorise already applies it to)
    #
    # Before A89 this path did not exist. `world.attempt()` ran for debits only,
    # so `action_lift.send_message`, `.voice_call` and `.escalate_human` were
    # unreachable code carrying PRIORS rows, and no contact could recover money
    # for any class. Every contact-heavy against contact-light comparison the
    # project had made was measuring the absence of a mechanism.
    #
    # Conditioned on intent, because a message to someone who has left is not a
    # message that gets paid. Set conservatively: these are per single contact,
    # not per campaign, and natural recovery (0.45 for willing_able) already
    # carries the customers who would have paid unprompted. What is left for a
    # message to earn is the incremental slice on top of that.
    #
    # willing_able x send_message x a neutral behaviour lands near 4%, and
    # x voice_call near 14%. Those are the numbers to argue with.
    "contact_response.rate.willing_able": 0.20,
    "contact_response.rate.willing_broke": 0.08,
    "contact_response.rate.disputing": 0.02,
    "contact_response.rate.churned": 0.005,
    "contact_response.rate.adversarial": 0.02,
    # Debtor behaviour modulates it (§8). `go_silent` is near zero by
    # definition; `promise_and_break` commits and then mostly does not pay;
    # `pay_then_complain` is the one behaviour that reliably pays, which is what
    # makes it the pair to SF-2.
    "contact_response.behaviour_multiplier.promise_and_break": 0.5,
    "contact_response.behaviour_multiplier.dispute_stall": 0.2,
    "contact_response.behaviour_multiplier.go_silent": 0.05,
    "contact_response.behaviour_multiplier.opt_out_midway": 0.4,
    "contact_response.behaviour_multiplier.hedged_reply": 0.6,
    "contact_response.behaviour_multiplier.pay_then_complain": 1.3,
    # How long the customer takes to act, in hours. Uniform on [1, max]. Same
    # role as A86's delay: it is what stops this being a coin flip at dispatch,
    # and it means an arm that contacts has to decide what to do while it waits.
    "contact_response.delay_h_max": 96.0,
    # --- liquidity window (§9 time_shiftable) ---
    # How many days before payday still counts as "money is about to be there".
    # The highest-leverage number in the world model: it decides how often a
    # time_shiftable retry lands inside the window, which is the mechanism the
    # whole retry-timing result rests on. Required member of the D4 sweep.
    "world.liquidity_window_days": 1.0,
}

# Numbers that appear in reported output but that nothing reads to make a
# decision. They are checked against a realised distribution by a test rather
# than sampled from, and PRIORS.md records them under their own heading so the
# distinction survives review (A50).
ASSERTED_TARGETS: Final[frozenset[str]] = frozenset({"escalation.target_overall_rate"})

SAMPLED_PARAMS: Final[dict[str, float]] = {
    key: value for key, value in PARAMS.items() if key not in ASSERTED_TARGETS
}

# ---------------------------------------------------------------------------
# Structure, not priors. These are taxonomy from SPEC §9, so they carry no
# numbers and need no PRIORS row.
# ---------------------------------------------------------------------------

_CARD_LIKE: Final = (Rail.CARD,)

DECLINE_CODES: Final[dict[DeclineClass, dict[str, tuple[Rail, ...]]]] = {
    DeclineClass.TIME_SHIFTABLE: {"insufficient_funds": tuple(Rail)},
    DeclineClass.TRANSIENT: {"gateway_timeout": tuple(Rail), "issuer_down": tuple(Rail)},
    DeclineClass.DEAD_INSTRUMENT: {
        "card_expired": _CARD_LIKE,
        "card_stolen": _CARD_LIKE,
        "mandate_revoked": tuple(Rail),
    },
    DeclineClass.AUTH_ABANDONED: {"authentication_failed": tuple(Rail)},
    DeclineClass.AMBIGUOUS: {"do_not_honour": tuple(Rail)},
    DeclineClass.TERMINAL: {"fraud_flagged": tuple(Rail)},
}

DECLINE_REASONS: Final[dict[str, str]] = {
    "insufficient_funds": "Insufficient funds in the customer account",
    "gateway_timeout": "Gateway did not respond within the timeout",
    "issuer_down": "Issuer unavailable",
    "card_expired": "Card has expired",
    "card_stolen": "Card reported lost or stolen",
    "mandate_revoked": "Mandate revoked by the customer",
    "authentication_failed": "Customer did not complete authentication",
    "do_not_honour": "Do not honour",
    "fraud_flagged": "Transaction flagged by risk",
}

# Codes §9's table does not map. They fall through to `ambiguous` and are
# counted; the batch carries a few so the fallback is exercised.
UNMAPPED_CODES: Final[tuple[str, ...]] = ("issuer_error_91", "npci_rc_u69", "acquirer_declined")

BATCH_ANCHOR: Final = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

_NORMAL = NormalDist()


# ---------------------------------------------------------------------------
# Draws — addressed, never consumed
# ---------------------------------------------------------------------------

def draw(seed: int, case_id: str, field: str, k: int = 0) -> float:
    """A uniform in [0, 1) at the address `(seed, case_id, field, k)`.

    Separate from `settle.sim.streams.value`, which addresses the seven named
    §14.2 streams the arms read during a run. Generation draws are their own
    address space so that adding a generated field can never shift a stream an
    arm is reading.
    """
    return derive_unit_float(seed, "gen", case_id, field, k)


def pick_from_mix(prefix: str, u: float) -> str:
    """Choose a key from the `prefix.*` mix in PARAMS by inverse CDF."""
    items = [(k[len(prefix) + 1 :], v) for k, v in PARAMS.items() if k.startswith(prefix + ".")]
    total = sum(weight for _, weight in items)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"{prefix} mix sums to {total}, not 1.0")
    cumulative = 0.0
    for name, weight in items:
        cumulative += weight
        if u < cumulative:
            return name
    return items[-1][0]


def _lognormal_paise(u: float, median: float, sigma: float, lo: float, hi: float) -> int:
    """Lognormal on paise, clipped. Money is int; rounding happens once, here."""
    z = _NORMAL.inv_cdf(min(max(u, 1e-12), 1 - 1e-12))
    value = median * pow(2.718281828459045, sigma * z)
    return int(round(min(max(value, lo), hi)))


def _poisson(u: float, mean: float, cap: int) -> int:
    """Inverse-transform Poisson. Means here are small, so the loop is short."""
    p = pow(2.718281828459045, -mean)
    cumulative = p
    k = 0
    while u >= cumulative and k < cap:
        k += 1
        p *= mean / k
        cumulative += p
    return k


def _geometric(u: float, decay: float, cap: int) -> int:
    """1-based geometric, clipped at `cap`."""
    k = 1
    remaining = 1.0 - decay
    threshold = remaining
    while u >= threshold and k < cap:
        k += 1
        remaining *= decay
        threshold += remaining
    return k


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def case_id_for(index: int) -> str:
    """Stable, sortable, and independent of batch size."""
    return f"case_{index:06d}"


def _decline_for(rail: Rail, decline_class: DeclineClass, u: float) -> str:
    viable = [code for code, rails in DECLINE_CODES[decline_class].items() if rail in rails]
    return viable[int(u * len(viable)) % len(viable)]


def generate_case(seed: int, index: int) -> GeneratedCase:
    """One case, derived entirely from its own address space."""
    cid = case_id_for(index)
    d = lambda field, k=0: draw(seed, cid, field, k)  # noqa: E731 — local shorthand, read-only

    rail = Rail(pick_from_mix("rail_mix", d("rail")))
    decline_class = DeclineClass(pick_from_mix("decline_class_mix", d("decline_class")))

    if d("unmapped") < PARAMS["unmapped_code_rate"]:
        code = UNMAPPED_CODES[int(d("unmapped_pick") * len(UNMAPPED_CODES)) % len(UNMAPPED_CODES)]
        reason = "Declined by issuer"
        # §9 maps anything it does not know to `ambiguous`, and counts it.
        decline_class = DeclineClass.AMBIGUOUS
    else:
        code = _decline_for(rail, decline_class, d("decline_code"))
        reason = DECLINE_REASONS[code]

    amount = _lognormal_paise(
        d("amount"),
        PARAMS["amount.median_paise"],
        PARAMS["amount.log_sigma"],
        PARAMS["amount.min_paise"],
        PARAMS["amount.max_paise"],
    )
    if d("prorated") < PARAMS["plan_value.prorated_rate"]:
        plan_value = int(round(amount / PARAMS["plan_value.prorated_fraction"]))
    else:
        plan_value = amount

    mix = "mandate_state_dead" if decline_class is DeclineClass.DEAD_INSTRUMENT else "mandate_state_base"
    mandate_state = MandateState(pick_from_mix(mix, d("mandate_state")))

    mandate_cap = None
    if d("mandate_cap") < PARAMS["mandate_cap.known_rate"]:
        mandate_cap = int(round(amount * PARAMS["mandate_cap.multiple"]))

    intent = IntentType(pick_from_mix("intent_mix", d("intent")))
    behaviour = DebtorBehaviour(pick_from_mix("behaviour_mix", d("behaviour")))

    payday = _payday(d("payday"), d("payday_uniform"))
    credit_day = None
    if d("credit_day_known") < PARAMS["observed_credit_day.known_rate"]:
        credit_day = _noisy_credit_day(payday, d("credit_day_exact"), d("credit_day_error"))

    attempt_number = _geometric(
        d("attempt_number"), PARAMS["attempt_number.decay"], int(PARAMS["attempt_number.max"])
    )

    observed = ObservedCase(
        case_id=cid,
        created_at=BATCH_ANCHOR + timedelta(hours=int(d("created_at") * 24 * 28)),
        customer_id=f"cust_{index:06d}",
        amount_paise=amount,
        rail=rail,
        decline_code=code,
        decline_reason=reason,
        attempt_number=attempt_number,
        mandate_state=mandate_state,
        mandate_cap_paise=mandate_cap,
        tenure_months=_poisson(
            d("tenure"), PARAMS["tenure.mean_months"], int(PARAMS["tenure.max_months"])
        ),
        prior_failures=_poisson(
            d("prior_failures"), PARAMS["prior_failures.mean"], int(PARAMS["prior_failures.max"])
        ),
        prior_recoveries=_poisson(
            d("prior_recoveries"),
            PARAMS["prior_recoveries.mean"],
            int(PARAMS["prior_recoveries.max"]),
        ),
        plan_value_paise=plan_value,
        observed_credit_day=credit_day,
        consent_whatsapp=d("consent") < PARAMS["consent_whatsapp_rate"],
        dnd_flag=d("dnd") < PARAMS["dnd_flag_rate"],
        language=Language(pick_from_mix("language_mix", d("language"))),
    )

    spread = PARAMS["recoverability.spread"]
    mean = PARAMS[f"recoverability_mean.{intent.value}"]
    truth = HiddenTruth(
        case_id=cid,
        true_recoverability=min(max(mean + (d("recoverability") - 0.5) * 2 * spread, 0.0), 1.0),
        intent_type=intent,
        patience_budget=_clamp_int(
            _poisson(d("patience"), PARAMS["patience.mean"], int(PARAMS["patience.max"])),
            int(PARAMS["patience.min"]),
            int(PARAMS["patience.max"]),
        ),
        payday_day=payday,
        response_fn_params={
            "base": PARAMS["response.base_mean"] * (0.5 + d("response_base")),
            "payday_lift": PARAMS["response.payday_lift"],
            "hour_lift": PARAMS["response.hour_lift"],
        },
        will_settle=d("will_settle") < PARAMS["will_settle_rate"],
        settlement_lag_h=_clamp_int(
            _poisson(
                d("settlement_lag"),
                PARAMS["settlement_lag_h.mean"],
                int(PARAMS["settlement_lag_h_max"]),
            ),
            0,
            int(PARAMS["settlement_lag_h_max"]),
        ),
        will_reverse=d("will_reverse") < PARAMS["will_reverse_rate"],
    )

    # SPEC §2.1 — the rule lives in settle/policy/, and the dependency runs
    # sim -> policy. The reverse would be an INV-8 breach.
    escalation_eligible = is_escalation_eligible(observed)

    return GeneratedCase(
        observed=observed,
        truth=truth,
        behaviour=behaviour,
        decline_class=decline_class,
        escalation_eligible=escalation_eligible,
    )


def behaviour_for(seed: int, case_id: str) -> DebtorBehaviour:
    """The debtor behaviour this case was generated with. SPEC §8.

    A pure function of `(seed, case_id)`, drawn at the same address the
    generator used. Exposed so the executor can obtain it without being handed
    the whole `GeneratedCase` — behaviour drives replies, and a reply is
    something the world produces, not something the agent may look up.
    """
    return DebtorBehaviour(pick_from_mix("behaviour_mix", draw(seed, case_id, "behaviour")))


def _payday(u_kind: float, u_uniform: float) -> int:
    first = PARAMS["payday.first_of_month_rate"]
    seventh = PARAMS["payday.seventh_rate"]
    if u_kind < first:
        return 1
    if u_kind < first + seventh:
        return 7
    return 1 + int(u_uniform * 28) % 28


def _noisy_credit_day(payday: int, u_exact: float, u_error: float) -> int:
    if u_exact < PARAMS["observed_credit_day.exact_rate"]:
        return payday
    span = int(PARAMS["observed_credit_day.max_error_days"])
    offset = int(u_error * (2 * span + 1)) - span
    return _clamp_int(payday + offset, 1, 28)


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class Batch(BaseModel):
    """A generated batch and the seed that produced it."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    seed: int
    cases: list[GeneratedCase]


def generate_batch(n_cases: int, seed: int) -> Batch:
    """N cases. Order-independent: case k depends on nothing but `(seed, k)`."""
    if n_cases < 1:
        raise ValueError("a batch needs at least one case")
    return Batch(seed=seed, cases=[generate_case(seed, i) for i in range(n_cases)])


# ---------------------------------------------------------------------------
# Realised mix — what the batch actually contains, not what PARAMS asked for
# ---------------------------------------------------------------------------

def summarise(batch: Batch) -> dict[str, dict[str, float]]:
    """Realised rates, grouped, for the CLI table and for GEN-3."""
    n = len(batch.cases)
    counts: dict[str, dict[str, float]] = {}

    def tally(group: str, key: str, weight: float = 1.0) -> None:
        counts.setdefault(group, {}).setdefault(key, 0.0)
        counts[group][key] += weight

    for g in batch.cases:
        o, t = g.observed, g.truth
        tally("rail_mix", o.rail.value)
        tally("decline_class_mix", g.decline_class.value)
        tally("language_mix", o.language.value)
        tally("mandate_state", o.mandate_state.value)
        tally("intent_mix", t.intent_type.value)
        tally("behaviour_mix", g.behaviour.value)
        tally("flags", "consent_whatsapp_rate", float(o.consent_whatsapp))
        tally("flags", "dnd_flag_rate", float(o.dnd_flag))
        tally("flags", "escalation.target_overall_rate", float(g.escalation_eligible))
        tally("flags", "mandate_cap.known_rate", float(o.mandate_cap_paise is not None))
        tally("flags", "observed_credit_day.known_rate", float(o.observed_credit_day is not None))
        tally("flags", "will_settle_rate", float(t.will_settle))
        tally("flags", "will_reverse_rate", float(t.will_reverse))
        tally("flags", "unmapped_code_rate", float(o.decline_code in UNMAPPED_CODES))
        tally("means", "amount.median_paise", o.amount_paise / n)
        tally("means", "tenure.mean_months", o.tenure_months / n)
        tally("means", "prior_failures.mean", o.prior_failures / n)
        tally("means", "prior_recoveries.mean", o.prior_recoveries / n)
        tally("means", "patience.mean", t.patience_budget / n)
        tally("means", "settlement_lag_h.mean", t.settlement_lag_h / n)

    for group, entries in counts.items():
        if group != "means":
            for key in entries:
                entries[key] /= n

    medians = sorted(g.observed.amount_paise for g in batch.cases)
    counts["means"]["amount.median_paise"] = float(medians[n // 2])
    return counts


def format_summary(batch: Batch) -> str:
    """The table the CLI prints. Realised beside declared, so drift is visible."""
    realised = summarise(batch)
    lines = [
        f"batch: {len(batch.cases)} cases, seed {batch.seed}",
        "",
        f"{'group':<20} {'value':<26} {'realised':>10} {'declared':>10} {'delta':>9}",
        "-" * 79,
    ]
    for group in ("rail_mix", "decline_class_mix", "language_mix", "intent_mix", "behaviour_mix"):
        for key, got in sorted(realised[group].items()):
            want = PARAMS.get(f"{group}.{key}")
            lines.append(_row(group, key, got, want))
        lines.append("")
    for key, got in sorted(realised["mandate_state"].items()):
        lines.append(_row("mandate_state", key, got, None))
    lines.append("")
    for key, got in sorted(realised["flags"].items()):
        lines.append(_row("flags", key, got, PARAMS.get(key)))
    lines.append("")
    for key, got in sorted(realised["means"].items()):
        lines.append(_row("means", key, got, PARAMS.get(key), fmt="{:>10.1f}"))
    return "\n".join(lines)


def _row(group: str, key: str, got: float, want: float | None, fmt: str = "{:>10.4f}") -> str:
    got_s = fmt.format(got)
    if want is None:
        return f"{group:<20} {key:<26} {got_s} {'—':>10} {'—':>9}"
    want_s = fmt.format(want)
    delta = (got - want) / want if want else 0.0
    return f"{group:<20} {key:<26} {got_s} {want_s} {delta:>+8.1%}"


def main(argv: list[str] | None = None) -> int:
    """CLI. Writes what the agent may see and what it may not to separate files."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="settle.sim.generator", description=__doc__)
    parser.add_argument("--cases", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("out/batch.jsonl"))
    args = parser.parse_args(argv)

    batch = generate_batch(args.cases, args.seed)

    # Observed and hidden go to separate files. One file holding both would be
    # an INV-8 breach waiting for the first person who greps it.
    truth_path = args.out.with_suffix(".truth" + args.out.suffix)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as observed_f, truth_path.open(
        "w", encoding="utf-8"
    ) as truth_f:
        for g in batch.cases:
            observed_f.write(g.observed.model_dump_json() + "\n")
            truth_f.write(
                json.dumps(
                    {
                        "truth": json.loads(g.truth.model_dump_json()),
                        "behaviour": g.behaviour.value,
                        "decline_class": g.decline_class.value,
                        "escalation_eligible": g.escalation_eligible,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    print(format_summary(batch))
    print()
    print(f"observed -> {args.out}")
    print(f"hidden   -> {truth_path}   (never read by settle/agent/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
