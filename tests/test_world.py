"""CP6.1 — natural recovery. SPEC §14.3, A77.

Without this path B0 recovers nothing, incremental equals gross, and §14.3's
subtraction protects nothing. It is also what gives `do_nothing` positive
expected value: if inaction never recovers anything then every action dominates
it, and the contact-restraint result is unreachable by construction.
"""

import collections

import pytest

from settle.recon.reconcile import reconcile, run_arm
from settle.schema.enums import IntentType
from settle.sim.generator import PARAMS, generate_batch
from settle.sim.streams import STREAM_NAMES, Streams
from settle.sim.world import natural_recovery, natural_recovery_at, natural_recovery_probability

SEED = 42
HORIZON_TICKS = 60 * 24


@pytest.fixture(scope="module")
def batch():
    return generate_batch(2_000, SEED)


def test_WLD_1_b0_recovers_a_non_zero_fraction(batch):
    """B0 does nothing and still recovers. That is the point of the arm."""
    streams = Streams(SEED)
    cured = [g for g in batch.cases if natural_recovery(g.observed, g.truth, HORIZON_TICKS, streams)]
    rate = len(cured) / len(batch.cases)
    assert rate > 0.05, "B0 recovers nothing, so incremental scoring protects nothing"
    assert 0.15 < rate < 0.32, rate


def test_WLD_1_the_realised_rate_matches_the_declared_parameter(batch):
    """Per intent, because a single global rate would make `intent_type`
    decorative in exactly the place it matters most."""
    streams = Streams(SEED)
    total = collections.Counter(g.truth.intent_type for g in batch.cases)
    cured = collections.Counter(
        g.truth.intent_type
        for g in batch.cases
        if natural_recovery(g.observed, g.truth, HORIZON_TICKS, streams)
    )
    for intent in IntentType:
        declared = natural_recovery_probability(intent)
        n = total[intent]
        if n < 100:
            continue
        realised = cured[intent] / n
        tolerance = max(3.5 * ((declared * (1 - declared) / n) ** 0.5), 0.02)
        assert abs(realised - declared) <= tolerance, (intent.value, realised, declared)


def test_WLD_1_recovery_is_ordered_by_intent(batch):
    """Someone willing and able notices and pays. Someone churned does not."""
    assert natural_recovery_probability(IntentType.WILLING_ABLE) > natural_recovery_probability(
        IntentType.WILLING_BROKE
    )
    assert natural_recovery_probability(IntentType.WILLING_BROKE) > natural_recovery_probability(
        IntentType.CHURNED
    )
    assert natural_recovery_probability(IntentType.CHURNED) < 0.05


def test_WLD_2_the_self_cure_is_identical_across_arms(batch):
    """It does not depend on what the arm did — it happened anyway.

    §14.3 subtracts whatever B0 recovers from every other arm. That subtraction
    is only meaningful if the self-cure is identifiably the *same event*.
    """
    first, second = Streams(SEED), Streams(SEED)
    for generated in batch.cases[:400]:
        for tick in (0, 240, HORIZON_TICKS):
            assert natural_recovery(
                generated.observed, generated.truth, tick, first
            ) == natural_recovery(generated.observed, generated.truth, tick, second)
        assert natural_recovery_at(
            generated.observed, generated.truth, first
        ) == natural_recovery_at(generated.observed, generated.truth, second)


def test_WLD_2_every_arm_reconciles_the_same_self_cures():
    """The end-to-end version: two different arms, the same cases self-curing."""
    cured_by_arm = {}
    for arm_key in ("b0", "b1"):
        entries, actuals, cases, name, _, truths, streams = run_arm(arm_key, 400, SEED)
        reconciled = reconcile(entries, actuals, cases, truths=truths, streams=streams)
        cured_by_arm[name] = {
            case_id
            for case_id in cases
            if natural_recovery_at(cases[case_id], truths[case_id], streams) is not None
        }
    b0, b1 = cured_by_arm["B0"], cured_by_arm["B1"]
    assert b0, "B0 cured nothing"
    assert b0 == b1, "the self-cure set diverged between arms — CRN is broken"


def test_WLD_2_the_draws_come_from_shared_streams():
    assert "natural_recovery_draw" in STREAM_NAMES
    assert "natural_recovery_day" in STREAM_NAMES


def test_WLD_1_every_rate_carries_a_priors_row():
    """INV-10. These move B0's recovery, which is subtracted from every arm."""
    for intent in IntentType:
        assert f"natural_recovery.{intent.value}" in PARAMS
    assert "natural_recovery.max_day" in PARAMS


# --------------------------------------------------------------------------
# WLD-3 / WLD-4 / WLD-5 — mandate re-authorisation. SPEC §6, §9, A86.
# --------------------------------------------------------------------------
#
# Before A86, `request_mandate_update` was legal, selected, and structurally
# incapable of succeeding: it is contact-bearing, so `world.attempt()` produced
# no outcome for it, and nothing revived a dead mandate. §9 named it as the
# recovery path for `dead_instrument` while the simulator gave that path a hard
# zero — 17% of the batch unwinnable by construction, which inflated every
# arm's apparent restraint.

import tempfile
from pathlib import Path

from settle.audit.chain import Ledger, read_entries
from settle.diagnose.taxonomy import classify
from settle.execute.executor import WorldHandle
from settle.recon.reconcile import reconcile as reconcile_cases
from settle.runner.arm import DoNothingArm
from settle.runner.arms.baselines import FixedLadderArm
from settle.runner.case_runner import run_case
from settle.schema.enums import DeclineClass
from settle.sim.observability import ObservabilityConfig
from settle.sim.world import mandate_revival_probability, mandate_revives

WLD_N = 2_000


def _run(arm, batch):
    """One arm over the batch, reconciled. Same shape as a real run."""
    streams, config = Streams(SEED), ObservabilityConfig()
    actuals, cases, truths = {}, {}, {}
    path = Path(tempfile.mkdtemp()) / "a.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            world = WorldHandle(truth=generated.truth, streams=streams)
            run_case(generated.observed, arm, world, config, ledger)
            cid = generated.observed.case_id
            actuals[cid] = list(world.actuals)
            cases[cid] = generated.observed
            truths[cid] = generated.truth
    entries = read_entries(path)
    reconciled = reconcile_cases(entries, actuals, cases, truths=truths, streams=streams)
    settled = {c for c, r in reconciled.items() if r.actually_settled and not r.reversed}
    return entries, cases, settled


@pytest.fixture(scope="module")
def wld_batch():
    return generate_batch(WLD_N, SEED)


@pytest.fixture(scope="module")
def revival_run(wld_batch):
    """B2 asks for a mandate update; B0 never does. That is the contrast."""
    entries, cases, ladder = _run(FixedLadderArm(), wld_batch)
    b0_entries, _, nothing = _run(DoNothingArm(), wld_batch)
    dead = {
        cid for cid, case in cases.items()
        if classify(case.decline_code) is DeclineClass.DEAD_INSTRUMENT
    }
    return entries, b0_entries, cases, dead, ladder, nothing


def test_WLD_3_a_mandate_update_makes_dead_instrument_recoverable(revival_run, capsys):
    """The whole point of A86. `dead_instrument` recovery above B0 was exactly
    zero before it — not small, zero — because the class had no debit verb and
    nothing could revive the mandate."""
    entries, _, _, dead, ladder, nothing = revival_run
    asked = {e.case_id for e in entries if e.reason_code == "MANDATE_UPDATE_REQUESTED"}
    revived = {e.case_id for e in entries if e.reason_code == "MANDATE_REVIVED"}

    incremental = (ladder - nothing) & dead
    paid = {e.case_id for e in entries if e.reason_code == "CONTACT_PAID"}
    with capsys.disabled():
        print(f"\n  dead_instrument cases  {len(dead)}")
        print(f"  asked for an update    {len(asked & dead)}")
        print(f"  re-authorised          {len(revived & dead)}")
        print(f"  paid after a contact   {len(paid & dead)}")
        print(f"  incremental recovery   {len(incremental)}")
    assert revived, "no mandate ever came back — the path is still a hard zero"
    assert incremental, "dead_instrument still recovers nothing above B0"
    # Two routes out of this class now, not one. A86 revives the mandate so a
    # debit can run; A89 lets the customer pay of their own accord after being
    # contacted, which needs no mandate at all. Before CP9.1 the second did not
    # exist and this assertion read `incremental <= revived`.
    assert incremental <= (revived | paid), (
        "a dead_instrument case recovered with neither a revived mandate nor a "
        "customer-initiated payment"
    )


def test_WLD_3_a_case_never_asked_never_revives(revival_run):
    """The other half: revival is caused by the action, not by the calendar.

    Stated over arms rather than over cases. A case can both self-cure under B0
    and have its mandate revived under B2 — those are different events and CRN
    keeps both available — so the assertion that carries weight is that the arm
    which never asks never gets one.
    """
    entries, b0_entries, _, dead, _, nothing = revival_run
    asked = {e.case_id for e in entries if e.reason_code == "MANDATE_UPDATE_REQUESTED"}
    revived = {e.case_id for e in entries if e.reason_code == "MANDATE_REVIVED"}
    assert revived <= asked, "a mandate revived on a case nobody asked"

    assert not [e for e in b0_entries if e.reason_code == "MANDATE_UPDATE_REQUESTED"]
    assert not [e for e in b0_entries if e.reason_code == "MANDATE_REVIVED"], (
        "B0 revived a mandate without dispatching anything"
    )
    # And B0 still recovers in this class, from the self-cure alone — which is
    # what the incremental subtraction removes.
    assert nothing & dead


def test_WLD_4_revival_is_identical_across_arms(wld_batch):
    """CRN. Whether a customer re-authorises is a fact about the customer, so
    two arms asking at the same tick must get the same answer — otherwise the
    §14.3 comparison is measuring luck rather than policy."""
    first, second = Streams(SEED), Streams(SEED)
    for generated in wld_batch.cases[:400]:
        for tick in (1, 24, 73, 300, 719):
            assert mandate_revives(
                generated.observed, generated.truth, tick, first
            ) == mandate_revives(generated.observed, generated.truth, tick, second)


def test_WLD_4_the_draw_comes_from_a_shared_stream():
    assert "mandate_revival_draw" in STREAM_NAMES
    assert "mandate_response_delay" in STREAM_NAMES


def test_WLD_5_a_churned_customer_does_not_re_authorise(wld_batch, capsys):
    """Conditioned on intent, and materially so. A single global rate would make
    `intent_type` decorative in the place it decides most: someone who has left
    does not go and enter a new card."""
    churned = mandate_revival_probability(IntentType.CHURNED)
    willing = mandate_revival_probability(IntentType.WILLING_ABLE)
    assert churned < willing / 10, (churned, willing)
    assert churned <= 0.02

    # And realised, not just declared.
    streams = Streams(SEED)
    realised = {}
    for intent in (IntentType.CHURNED, IntentType.WILLING_ABLE):
        members = [g for g in wld_batch.cases if g.truth.intent_type is intent]
        revived = [
            g for g in members
            if mandate_revives(g.observed, g.truth, 48, streams)
        ]
        realised[intent.value] = len(revived) / len(members)
    with capsys.disabled():
        print(f"\n  realised revival at tick 48  {realised}")
    assert realised["churned"] < realised["willing_able"] / 5


def test_WLD_5_every_revival_rate_carries_a_priors_row():
    """INV-10. These decide how much of the 17% dead_instrument slice is
    winnable at all, which makes them the highest-leverage unsourced numbers in
    the world model."""
    for intent in IntentType:
        assert f"mandate_update.success_rate.{intent.value}" in PARAMS
    assert "mandate_update.response_delay_h_max" in PARAMS


def test_WLD_5_the_response_delay_is_a_real_wait(wld_batch):
    """Not a coin flip at dispatch. The mandate is dead for the whole delay, so
    an arm that asks has to decide what to do while it waits."""
    from settle.sim.world import mandate_response_delay_h

    streams = Streams(SEED)
    delays = [
        mandate_response_delay_h(g.observed, 24, streams) for g in wld_batch.cases[:500]
    ]
    assert min(delays) >= 1
    assert max(delays) <= PARAMS["mandate_update.response_delay_h_max"]
    assert len(set(delays)) > 24, "the delay is nearly constant, so it is not a draw"


# --------------------------------------------------------------------------
# WLD-6 / WLD-7 / WLD-8 / WLD-9 — contact response. SPEC §6, A89.
# --------------------------------------------------------------------------
#
# Before A89, no contact verb could produce a settlement. `world.attempt()` ran
# for debits only, so a message, a voice call and a human escalation were
# dispatched, priced, gated and logged while being structurally incapable of
# recovering money. "Same recovery, 145x fewer contacts" is trivially true when
# contacts cannot recover anything, and every contact-heavy against
# contact-light comparison made before this point was measuring the absence of a
# mechanism rather than a policy difference.

from settle.schema.enums import ActionType, DebtorBehaviour
from settle.sim.world import (
    ACTION_LIFT,
    contact_payment,
    contact_response_delay_h,
    contact_response_probability,
    contact_responds,
)

CONTACT_VERBS = (
    ActionType.SEND_MESSAGE,
    ActionType.REQUEST_MANDATE_UPDATE,
    ActionType.VOICE_CALL,
    ActionType.ESCALATE_HUMAN,
    ActionType.SERVE_NOTICE,
)
# The two verbs §14.4 prices at zero lift, and why each is deliberate.
ZERO_LIFT_VERBS = {
    ActionType.DO_NOTHING: "inaction is not an action; §14.3's self-cure carries it",
    ActionType.SERVE_NOTICE: "a regulatory notice is not a persuasion; it opens G9's window",
}


def test_WLD_6_every_contact_verb_can_produce_a_settlement(wld_batch, capsys):
    """Each verb with a non-zero lift must have a non-zero settle rate for at
    least one intent. A verb that cannot ever work is not a policy option, it is
    a priced no-op — which is what all four of these were."""
    streams = Streams(SEED)
    rates: dict[str, dict[str, float]] = {}
    for verb in CONTACT_VERBS:
        rates[verb.value] = {}
        for intent in IntentType:
            members = [g for g in wld_batch.cases if g.truth.intent_type is intent][:400]
            if not members:
                continue
            paid = sum(
                1
                for g in members
                if contact_responds(
                    g.observed, g.truth, DebtorBehaviour.HEDGED_REPLY, verb, 48, streams
                )
            )
            rates[verb.value][intent.value] = paid / len(members)

    with capsys.disabled():
        print("\n  realised payment rate per contact, hedged_reply, tick 48")
        for verb in sorted(rates):
            row = "  ".join(f"{k}={v:.3f}" for k, v in sorted(rates[verb].items()))
            print(f"    {verb:<24}{row}")

    for verb in CONTACT_VERBS:
        realised = rates[verb.value]
        if verb in ZERO_LIFT_VERBS:
            assert max(realised.values()) == 0.0, (verb, ZERO_LIFT_VERBS[verb])
            continue
        assert max(realised.values()) > 0.0, f"{verb.value} can never recover money"
        assert realised["churned"] < realised["willing_able"] / 10, (
            f"{verb.value}: a churned customer responds nearly as often as a willing one"
        )
        assert realised["churned"] <= 0.02, verb.value


def test_WLD_6_the_verb_lift_orders_the_response(wld_batch):
    """A voice call outranks an SMS here for the same declared reason it does
    for a debit: `action_lift` is reused, not duplicated."""
    case = wld_batch.cases[0].observed
    p = {
        verb: contact_response_probability(
            case, IntentType.WILLING_ABLE, DebtorBehaviour.HEDGED_REPLY, verb
        )
        for verb in CONTACT_VERBS
    }
    assert p[ActionType.ESCALATE_HUMAN] > p[ActionType.VOICE_CALL]
    assert p[ActionType.VOICE_CALL] > p[ActionType.REQUEST_MANDATE_UPDATE]
    assert p[ActionType.REQUEST_MANDATE_UPDATE] > p[ActionType.SEND_MESSAGE]
    assert p[ActionType.SERVE_NOTICE] == 0.0


def test_WLD_6_debtor_behaviour_modulates_it(wld_batch):
    """SPEC §8. `go_silent` at near zero, `pay_then_complain` the one that pays."""
    case = wld_batch.cases[0].observed
    p = {
        behaviour: contact_response_probability(
            case, IntentType.WILLING_ABLE, behaviour, ActionType.SEND_MESSAGE
        )
        for behaviour in DebtorBehaviour
    }
    assert p[DebtorBehaviour.GO_SILENT] < p[DebtorBehaviour.HEDGED_REPLY] / 5
    assert p[DebtorBehaviour.PAY_THEN_COMPLAIN] == max(p.values())
    assert p[DebtorBehaviour.PROMISE_AND_BREAK] < p[DebtorBehaviour.PAY_THEN_COMPLAIN]


def test_WLD_7_a_contact_payment_is_still_a_payment(wld_batch, capsys):
    """It runs through `settle()` like any other, so `auth_no_settle_rate`,
    `settlement_lag_h` and `will_reverse` all apply. Routing it around the
    two-step that carries INV-1 would make messaging the one channel where money
    is certain — the opposite of the failure this project exists to model."""
    streams = Streams(SEED)
    outcomes = []
    for generated in wld_batch.cases:
        actual = contact_payment(
            generated.observed,
            generated.truth,
            DebtorBehaviour.PAY_THEN_COMPLAIN,
            ActionType.ESCALATE_HUMAN,
            generated.observed.created_at,
            48,
            streams,
        )
        if actual is not None:
            outcomes.append((generated, actual))

    assert outcomes, "nobody paid, so there is nothing to check"
    authorised_never_settled = [a for _, a in outcomes if not a.settled]
    settled = [a for _, a in outcomes if a.settled]
    lagged = [a for a in settled if a.settled_at > a.at]
    reversed_ = [a for a in settled if a.reversed]

    with capsys.disabled():
        print(f"\n  contact payments        {len(outcomes)}")
        print(f"  authorised, never settled {len(authorised_never_settled)}  (SF-1 material)")
        print(f"  settled with a lag        {len(lagged)}/{len(settled)}")
        print(f"  settled then reversed     {len(reversed_)}  (SF-7 material)")

    assert authorised_never_settled, (
        "every contact payment settled — auth_no_settle_rate is not being applied"
    )
    assert lagged, "no settlement lag on a contact payment"
    assert reversed_, "no contact payment ever reverses"


def test_WLD_8_the_response_is_identical_across_arms(wld_batch):
    """CRN. Whether a contacted customer pays is a fact about the customer, so
    two arms contacting at the same tick must get the same answer — otherwise
    "B2 contacts more and recovers more" is partly about which arm drew the
    luckier numbers."""
    first, second = Streams(SEED), Streams(SEED)
    for generated in wld_batch.cases[:400]:
        for tick in (1, 24, 97, 400, 900):
            for verb in (ActionType.SEND_MESSAGE, ActionType.VOICE_CALL):
                assert contact_responds(
                    generated.observed, generated.truth,
                    DebtorBehaviour.HEDGED_REPLY, verb, tick, first,
                ) == contact_responds(
                    generated.observed, generated.truth,
                    DebtorBehaviour.HEDGED_REPLY, verb, tick, second,
                )


def test_WLD_8_the_draw_is_addressed_at_the_due_tick_not_the_noticing_tick():
    """An arm that stops at the horizon and one that runs on must resolve the
    same pending response the same way. The address is the due tick, so
    resolving late is bit-identical to resolving on time."""
    assert "contact_response_draw" in STREAM_NAMES
    assert "contact_response_delay" in STREAM_NAMES

    import inspect

    from settle.runner import case_runner

    source = inspect.getsource(case_runner.run_case)
    assert "due_tick = current.contact_response_due_tick" in source
    assert "contact_response_outcome(case, world, verb, due_tick, observability)" in source


def test_WLD_9_every_action_lift_is_reachable(capsys):
    """A parameter with a PRIORS row that nothing reads is the bug CP9 and CP9.1
    were both spent on. `action_lift.send_message`, `.voice_call` and
    `.escalate_human` each carried a row for eight checkpoints while sitting in a
    branch only debits could reach.

    Stated as a routing-totality check rather than a grep: every verb is either
    debit-bearing and reaches `attempt()`, or contact-bearing and reaches
    `contact_payment()`, or declared zero-lift with a stated reason. A verb that
    falls through all three is unreachable and this fails.
    """
    from settle.policy.legal import is_contact, is_debit
    from settle.schema.action import (
        DoNothing, EscalateHuman, RequestMandateUpdate, Retry, SendMessage,
        ServeNotice, SwitchRail, VoiceCall,
    )
    from settle.schema.enums import Channel, Rail

    samples = {
        ActionType.DO_NOTHING: DoNothing(),
        ActionType.RETRY: Retry(at_hour_offset=0, rail=Rail.CARD),
        ActionType.SWITCH_RAIL: SwitchRail(to=Rail.UPI_AUTOPAY),
        ActionType.SEND_MESSAGE: SendMessage(channel=Channel.SMS, template_id="t"),
        ActionType.REQUEST_MANDATE_UPDATE: RequestMandateUpdate(channel=Channel.SMS),
        ActionType.SERVE_NOTICE: ServeNotice(channel=Channel.SMS),
        ActionType.ESCALATE_HUMAN: EscalateHuman(),
        ActionType.VOICE_CALL: VoiceCall(),
    }
    assert set(samples) == set(ActionType), "a verb was added and this test did not notice"

    routes: dict[str, str] = {}
    for verb, action in samples.items():
        assert f"action_lift.{verb.value}" in PARAMS, verb
        if ACTION_LIFT[verb] == 0.0:
            assert verb in ZERO_LIFT_VERBS, (
                f"{verb.value} has zero lift and no stated reason — either it is dead "
                "code or the reason belongs in ZERO_LIFT_VERBS"
            )
            routes[verb.value] = f"zero lift: {ZERO_LIFT_VERBS[verb]}"
            continue
        if is_debit(action):
            routes[verb.value] = "attempt() — submitted to a rail"
        elif is_contact(action):
            routes[verb.value] = "contact_payment() — customer-initiated"
        else:
            raise AssertionError(
                f"{verb.value} carries lift {ACTION_LIFT[verb]} and reaches neither "
                "attempt() nor contact_payment(). It is a priced no-op."
            )

    with capsys.disabled():
        print("\n  action_lift routing")
        for verb in sorted(routes):
            print(f"    {verb:<24}{ACTION_LIFT[ActionType(verb)]:>5}  {routes[verb]}")


def test_WLD_9_the_dnd_penalty_is_no_longer_dead(wld_batch):
    """`p_authorise.dnd_contact_penalty` sat in a branch testing for
    `SendMessage`/`VoiceCall` inside a function only debits ever reached. A89
    gives it the code path its PRIORS row always implied."""
    on = next(g.observed for g in wld_batch.cases if g.observed.dnd_flag)
    off = on.model_copy(update={"dnd_flag": False})
    args = (IntentType.WILLING_ABLE, DebtorBehaviour.HEDGED_REPLY, ActionType.SEND_MESSAGE)
    assert contact_response_probability(on, *args) < contact_response_probability(off, *args)
    # And not applied where `p_authorise` never applied it.
    quiet = (IntentType.WILLING_ABLE, DebtorBehaviour.HEDGED_REPLY, ActionType.ESCALATE_HUMAN)
    assert contact_response_probability(on, *quiet) == contact_response_probability(off, *quiet)


def test_WLD_9_the_response_delay_is_a_real_wait(wld_batch):
    """Not a coin flip at dispatch. The arm has to decide what to do while it
    waits, exactly as it does for a mandate update."""
    streams = Streams(SEED)
    delays = [contact_response_delay_h(g.observed, 24, streams) for g in wld_batch.cases[:500]]
    assert min(delays) >= 1
    assert max(delays) <= PARAMS["contact_response.delay_h_max"]
    assert len(set(delays)) > 24, "the delay is nearly constant, so it is not a draw"
