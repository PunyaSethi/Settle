"""CP8 — the OURS policy. SPEC §10.2, §13 S7.

POL-2 is the thesis. If `do_nothing` cannot win on a case where the uplift is
small and the cost is real, contact restraint is rhetoric and the §14.4 column
for "cases deliberately not contacted" would be zero by construction.
"""

import ast
import pickle
from pathlib import Path

import pytest

from settle.agent.estimator import Estimator, latest_model_path
from settle.agent.policy import (
    ECONOMIC_STOP_MULTIPLE,
    choose,
    expected_value,
    total_cost_paise,
)
from settle.policy.params import POLICY_PARAMS, action_cost_paise, opt_out_cost_paise, p_opt_out
from settle.schema.action import DoNothing, Retry, SendMessage, VoiceCall
from settle.schema.enums import ActionType, ArmMode, Channel, Rail
from settle.schema.state import CaseState
from settle.sim.generator import generate_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = latest_model_path(REPO_ROOT / "out")

pytestmark = pytest.mark.skipif(MODEL is None, reason="no trained model; run CP7 training")


@pytest.fixture(scope="module")
def estimator():
    payload = pickle.loads(MODEL.read_bytes())
    return Estimator(payload["models"][payload["winner"]], payload["winner"])


@pytest.fixture(scope="module")
def batch():
    return generate_batch(300, 42)


def state_for(case, **kw):
    return CaseState(case_id=case.case_id, arm="OURS", arm_mode=ArmMode.ENFORCE, **kw)


class _Flat:
    """An estimator that returns a fixed probability for everything.

    Uplift is exactly zero for every action, so any action selected must have
    been selected on something other than its effect. Lets POL-1 and POL-2 test
    the formula rather than the model.
    """

    def __init__(self, value=0.5):
        self.value = value

    def predict_pairs(self, case, actions, tick, last_attempt_tick=None):
        import numpy as np

        return np.full(len(actions), self.value)

    def predict_proba(self, case, action, tick, last_attempt_tick=None):
        return self.value


class _RewardRetry:
    """Uplift only for retries. Everything else is worth nothing."""

    def __init__(self, uplift=0.30):
        self.uplift = uplift

    def predict_pairs(self, case, actions, tick, last_attempt_tick=None):
        import numpy as np

        return np.asarray(
            [0.4 + (self.uplift if a.type is ActionType.RETRY else 0.0) for a in actions]
        )

    def predict_proba(self, case, action, tick, last_attempt_tick=None):
        return 0.4 + (self.uplift if action.type is ActionType.RETRY else 0.0)


# --------------------------------------------------------------------------
# POL-1
# --------------------------------------------------------------------------

def test_POL_1_ev_uses_uplift_not_raw_probability(batch):
    """A82. A raw probability says acting on a `willing_able` case succeeds 99%
    of the time — true, and useless, because it would have settled anyway."""
    case = batch.cases[0].observed
    flat = _Flat(0.99)
    decision = choose(case, state_for(case), [DoNothing()], flat)

    # Every action has probability 0.99 and therefore uplift 0.
    assert decision.action.type is ActionType.DO_NOTHING
    for alternative in decision.alternatives:
        assert alternative.p_success == pytest.approx(0.99)
        # EV must be negative for anything with a cost, because uplift is zero.
        if alternative.action.type is not ActionType.DO_NOTHING:
            assert alternative.ev_paise <= 0, alternative.action.type


def test_POL_1_the_formula_is_uplift_times_amount_minus_costs(batch):
    case = batch.cases[0].observed
    action = SendMessage(channel=Channel.WHATSAPP, template_id="t")
    uplift = 0.1
    expected = uplift * case.amount_paise - (
        action_cost_paise(ActionType.SEND_MESSAGE, Channel.WHATSAPP)
        + opt_out_cost_paise(ActionType.SEND_MESSAGE, case.plan_value_paise, Channel.WHATSAPP)
    )
    assert expected_value(case, action, uplift) == pytest.approx(expected)


def test_POL_1_the_source_subtracts_the_baseline():
    source = (REPO_ROOT / "settle" / "agent" / "policy.py").read_text(encoding="utf-8")
    assert "uplift = p - baseline" in source
    assert "baseline = probabilities[0]" in source


# --------------------------------------------------------------------------
# POL-2 — the annoyance-budget thesis
# --------------------------------------------------------------------------

def test_POL_2_do_nothing_wins_when_uplift_is_small_and_cost_is_real(batch):
    """The thesis, demonstrated rather than asserted. Without this the
    "cases deliberately not contacted" column is zero by construction."""
    case = batch.cases[0].observed
    decision = choose(case, state_for(case), [DoNothing()], _Flat(0.5))
    assert decision.action.type is ActionType.DO_NOTHING
    assert decision.reason_code in ("DO_NOTHING_DOMINATES", "S7_ECONOMIC_STOP")


def test_POL_2_a_real_uplift_does_beat_doing_nothing(batch):
    """And the restraint is not blanket refusal: given a payoff, it acts."""
    case = next(g.observed for g in batch.cases if g.observed.decline_code == "insufficient_funds")
    decision = choose(case, state_for(case), [DoNothing()], _RewardRetry(0.30))
    assert decision.action.type is ActionType.RETRY
    assert decision.reason_code == "EV_ARGMAX"
    assert decision.uplift > 0


def test_POL_2_an_expensive_channel_loses_to_a_cheap_one_at_equal_uplift(batch):
    """`voice_call` costs 400 paise and carries 31x the opt-out risk of an SMS.
    At equal uplift it must lose, or the cost model is decorative."""
    case = batch.cases[0].observed
    assert action_cost_paise(ActionType.VOICE_CALL) > action_cost_paise(
        ActionType.SEND_MESSAGE, Channel.SMS
    )
    assert p_opt_out(ActionType.VOICE_CALL) > p_opt_out(ActionType.SEND_MESSAGE, Channel.SMS)
    assert total_cost_paise(case, VoiceCall()) > total_cost_paise(
        case, SendMessage(channel=Channel.SMS, template_id="t")
    )


# --------------------------------------------------------------------------
# POL-3
# --------------------------------------------------------------------------

def test_POL_3_every_considered_option_is_recorded_with_its_ev(batch, estimator):
    """This is what makes the case-trace screen worth building: a log that shows
    only what was chosen is a number dump."""
    from settle.runner.arms.explore import candidate_pairs

    case = batch.cases[3].observed
    state = state_for(case)
    decision = choose(case, state, [DoNothing()], estimator)

    assert len(decision.alternatives) == len(candidate_pairs(case, state))
    assert decision.alternatives
    for alternative in decision.alternatives:
        assert isinstance(alternative.ev_paise, int)
        assert 0.0 <= alternative.p_success <= 1.0
        # A41's pairing: an illegal alternative names the gate that blocked it.
        assert (alternative.block_gate is None) == alternative.legal


def test_POL_3_blocked_options_are_recorded_not_dropped(batch, estimator):
    case = batch.cases[0].observed
    hostile = state_for(case, opted_out=False, disputed=True)
    decision = choose(case, hostile, [DoNothing()], estimator)
    blocked = [a for a in decision.alternatives if not a.legal]
    assert blocked, "a disputed case blocked nothing"
    assert all(a.block_gate for a in blocked)


# --------------------------------------------------------------------------
# POL-4 — S7
# --------------------------------------------------------------------------

def test_POL_4_s7_fires_when_the_best_ev_is_below_the_threshold(batch):
    """§13: expected recovery below a multiple of cost is not worth taking.
    Evaluated on the *best* option, so it means "nothing here pays"."""
    case = next(g.observed for g in batch.cases if g.observed.decline_code == "insufficient_funds")
    # Uplift just large enough to be positive but not 3x the cost.
    tiny = action_cost_paise(ActionType.RETRY) * 2.0 / case.amount_paise
    decision = choose(case, state_for(case), [DoNothing()], _RewardRetry(tiny))
    assert decision.economic_stop or decision.action.type is ActionType.DO_NOTHING
    if decision.economic_stop:
        assert decision.reason_code == "S7_ECONOMIC_STOP"


def test_POL_4_s7_lives_in_the_policy_because_it_needs_an_ev():
    """Recorded in §13. A pure function of CaseState cannot ask this question."""
    stops = (REPO_ROOT / "settle" / "policy" / "stops.py").read_text(encoding="utf-8")
    assert "S7" in stops and "deliberately NOT here" in stops
    policy = (REPO_ROOT / "settle" / "agent" / "policy.py").read_text(encoding="utf-8")
    assert "S7_ECONOMIC_STOP" in policy
    assert ECONOMIC_STOP_MULTIPLE == POLICY_PARAMS["economic_stop_multiple"]


# --------------------------------------------------------------------------
# POL-5 / POL-6 / POL-7
# --------------------------------------------------------------------------

def test_POL_5_the_policy_never_selects_an_action_gates_would_block(batch, estimator):
    from settle.policy.gates import evaluate_gates

    for generated in batch.cases[:60]:
        case = generated.observed
        for tick in (0, 11, 30, 100):
            state = state_for(case, tick=tick)
            action = choose(case, state, [DoNothing()], estimator).action
            assert evaluate_gates(case, state, action, ArmMode.ENFORCE).allowed, (
                case.case_id, tick, action.type
            )


def test_POL_6_decisions_are_identical_across_repeated_calls(batch, estimator):
    case = batch.cases[7].observed
    state = state_for(case, tick=14)
    first = choose(case, state, [DoNothing()], estimator)
    for _ in range(10):
        again = choose(case, state, [DoNothing()], estimator)
        assert again.action == first.action
        assert again.expected_value == first.expected_value
        assert [a.ev_paise for a in again.alternatives] == [a.ev_paise for a in first.alternatives]


def test_POL_7_the_policy_reads_only_observed_case_derived_features():
    """INV-8. A policy that could see `true_recoverability` would be reading the
    answer, and its numbers would mean nothing."""
    tree = ast.parse((REPO_ROOT / "settle" / "agent" / "policy.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [n for n in imported if n.startswith("settle.sim.truth")]
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("true_recoverability", "payday_day", "patience_budget", "intent_type", "truth"):
        assert banned not in attrs, banned


# --------------------------------------------------------------------------
# POL-8 — the dependency runs one direction only. OQ-50.
# --------------------------------------------------------------------------

def test_POL_8_the_agent_package_never_imports_the_runner():
    """OQ-50. The agent is the thing being evaluated; the runner is the harness
    that evaluates it. A dependency from agent to runner makes the policy
    unusable without the experiment that measured it.

    It is the same error class as CP3.1's escalation rule, where the policy
    could not recompute eligibility because the rule lived inside `settle/sim/`
    (§2.1, A62). Both are fixed the same way: the shared rule moves into
    `settle/policy/`, and this test keeps it there.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "settle" / "agent").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "settle.runner" or name.startswith("settle.runner."):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} -> {name}")
    assert not offenders, offenders


def test_POL_8_the_grid_has_one_definition():
    """A71's binding constraint is only checkable if there is one function to
    check. EXPLORE re-exports it; it does not redefine it."""
    from settle.policy import grid
    from settle.runner.arms import explore

    for name in ("candidate_pairs", "expand_grid", "gate_passing_pairs"):
        assert getattr(explore, name) is getattr(grid, name), name


def test_POL_8_the_policy_searches_the_grid_it_imports(batch, estimator):
    """And it is the same set, not merely the same name."""
    from settle.policy.grid import candidate_pairs

    case = batch.cases[3].observed
    state = state_for(case, tick=48)
    decision = choose(case, state, [DoNothing()], estimator)
    assert len(decision.alternatives) == len(candidate_pairs(case, state))


# --------------------------------------------------------------------------
# POL-9 — the estimator memo changes speed and nothing else. OQ-49.
# --------------------------------------------------------------------------

def test_POL_9_warming_returns_bit_identical_probabilities(batch):
    """A cache that changes an answer is not a cache, it is a second model.

    The memo is keyed on `(action, tick, last_attempt_tick)` within one case,
    which is exactly what `feature_row` reads. If that ever stops being true
    this test fails before any metric does.
    """
    import numpy as np

    payload = pickle.loads(MODEL.read_bytes())
    model = payload["models"][payload["winner"]]
    cold = Estimator(model, payload["winner"], warm=False)
    warm = Estimator(model, payload["winner"], warm=True)

    from settle.policy.grid import candidate_pairs

    for generated in batch.cases[:25]:
        case = generated.observed
        for tick in (0, 24, 96, 240):
            actions = [DoNothing(), *candidate_pairs(case, state_for(case, tick=tick))]
            assert np.array_equal(
                cold.predict_pairs(case, actions, tick),
                warm.predict_pairs(case, actions, tick),
            ), (case.case_id, tick)

    assert warm.calls < cold.calls, "warming did not reduce the number of calls"


def test_POL_9_the_memo_is_dropped_when_the_case_changes(batch):
    """A86 lets a case change under the runner — a revived mandate advances
    `mandate_state`. The memo is bound by value, so it cannot answer for a case
    it was not built for."""
    from settle.schema.enums import MandateState

    payload = pickle.loads(MODEL.read_bytes())
    estimator = Estimator(payload["models"][payload["winner"]], payload["winner"])
    case = batch.cases[0].observed

    estimator.predict_pairs(case, [DoNothing()], 0)
    assert estimator._cache

    revived = case.model_copy(update={"mandate_state": MandateState.ACTIVE})
    estimator.predict_pairs(revived, [DoNothing()], 0)
    assert estimator._cache_case == revived


# --------------------------------------------------------------------------
# POL-10 — the policy can tell its options apart. SPEC §10.1 (A92), CP10.
# --------------------------------------------------------------------------
#
# EST-13 guards the estimator. This is the same property seen from the policy's
# side: if every alternative comes back with the same EV, `argmax` is picking
# arbitrarily and the ties break toward `do_nothing`, which is free. That is
# what produced CP9.1's result — OURS declining retries costing 5 paise at zero
# opt-out risk, because the model scored them identically to inaction.

def test_POL_10_alternatives_are_not_all_scored_alike(batch, estimator, capsys):
    """Across real decisions, the recorded alternatives must actually differ."""
    from settle.policy.grid import candidate_pairs

    flat = varied = skipped = 0
    for generated in batch.cases[:120]:
        case = generated.observed
        for tick, last in ((0, None), (24, 0), (120, 24)):
            state = state_for(case, tick=tick, last_attempt_tick=last)
            if len(candidate_pairs(case, state)) < 2:
                skipped += 1
                continue
            decision = choose(case, state, [DoNothing()], estimator)
            evs = {a.ev_paise for a in decision.alternatives}
            if len(evs) == 1:
                flat += 1
            else:
                varied += 1

    total = flat + varied
    with capsys.disabled():
        print(f"\n  multi-option decisions {total}  varied {varied}  flat {flat}"
              f"  ({flat / total:.1%})")
    assert total, "no multi-option decisions to test"
    assert flat / total <= 0.10, (
        f"{flat / total:.1%} of decisions score every option identically — the "
        "argmax is choosing arbitrarily and ties break toward do_nothing"
    )


def test_POL_10_a_near_free_action_is_taken_when_its_uplift_is_real(batch):
    """The economics CP10 turned on. A retry costs 5 paise and risks no opt-out,
    so S7 clears it at roughly 0.03% uplift — three hundred times below what a
    message needs. A policy that declines a retry at 6 points of uplift is not
    being restrained, it is failing to see the difference."""
    from settle.policy.params import action_cost_paise, opt_out_cost_paise
    from settle.schema.enums import ActionType, Channel

    case = next(
        g.observed for g in batch.cases
        if g.observed.decline_code == "insufficient_funds"
    )
    retry_need = ECONOMIC_STOP_MULTIPLE * (
        action_cost_paise(ActionType.RETRY)
        + opt_out_cost_paise(ActionType.RETRY, case.plan_value_paise)
    ) / case.amount_paise
    message_need = ECONOMIC_STOP_MULTIPLE * (
        action_cost_paise(ActionType.SEND_MESSAGE, Channel.SMS)
        + opt_out_cost_paise(ActionType.SEND_MESSAGE, case.plan_value_paise, Channel.SMS)
    ) / case.amount_paise

    assert retry_need < 0.001, retry_need
    assert message_need > 0.05, message_need
    assert message_need / retry_need > 100, "the two thresholds are no longer worlds apart"

    # And the policy acts on it: a modest uplift on retries must beat inaction.
    decision = choose(case, state_for(case), [DoNothing()], _RewardRetry(uplift=0.02))
    assert decision.action.type is ActionType.RETRY, decision.reason_code
