"""CP8 — the OURS policy. SPEC §10.2, §13 S7.

POL-2 is the thesis. If `do_nothing` cannot win on a case where the uplift is
small and the cost is real, contact restraint is rhetoric and the §14.4 column
for "cases deliberately not contacted" would be zero by construction.
"""

import ast
import pickle
from pathlib import Path

import pytest

from settle.agent.estimator import Estimator
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
MODEL = REPO_ROOT / "out" / "model.pkl"

pytestmark = pytest.mark.skipif(not MODEL.exists(), reason="no trained model; run CP7 training")


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
