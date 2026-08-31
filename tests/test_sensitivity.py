"""CP11 — the sensitivity sweep. SPEC §15.

SEN-1  the sweep is reproducible: same seed, same results, and the same results
       however many worker processes ran it.
SEN-2  every REQUIRED member is actually swept. A required member missing from
       the sweep is the same class of bug as a PRIORS row nothing reads — the
       document claims a check that no code performs.
REB-1  the override rebinds every cached constant a swept key invalidates. A
       sweep that patched `PARAMS` and left a module-level `Final` derived from
       it would report that the parameter has no effect, which is the most
       dangerous wrong answer this module can give: it is indistinguishable
       from a robust result.
"""

import ast
import re
from pathlib import Path

import pytest

from settle.agent.estimator import latest_model_path
from settle.eval.sensitivity import (
    MEMBERS,
    MODEL,
    MULTIPLES,
    POLICY,
    WORLD,
    Member,
    base_values,
    override,
    select_winner,
    survival_range,
    sweep,
)
from settle.policy.params import POLICY_PARAMS
from settle.schema.enums import ActionType
from settle.sim.generator import PARAMS

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = latest_model_path(REPO_ROOT / "out")

SWEPT_KEYS = frozenset(key for member in MEMBERS for key in member.keys)

# The members CP11 names in the checkpoint prompt. Kept literal rather than
# derived from MEMBERS: a test that reads its expectations out of the thing it
# is testing asserts nothing.
CP11_REQUIRED = frozenset({
    "mandate_update.success_rate.*",
    "contact_response.rate.*",
    "p_opt_out.*",
    "ltv_months",
    "economic_stop_multiple",
    "world.liquidity_window_days",
    "MAX_FLAT_DECISION_RATE",
    "action_lift.*",
    "natural_recovery.*",
})


def _priors_required_keys() -> set[str]:
    """Every PRIORS row whose sensitivity cell says it is a required member."""
    text = (REPO_ROOT / "PRIORS.md").read_text(encoding="utf-8")
    required = set()
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[4].startswith("REQUIRED"):
            required.add(cells[0])
    return required


# --------------------------------------------------------------------------
# SEN-2 — every required member is swept
# --------------------------------------------------------------------------

def test_SEN_2_every_priors_required_row_is_swept():
    """PRIORS marks rows REQUIRED in the D4 sweep. This is that sweep."""
    required = _priors_required_keys()
    assert required, "no PRIORS row is marked REQUIRED — the parser has drifted"
    missing = sorted(required - SWEPT_KEYS)
    assert not missing, f"marked REQUIRED in PRIORS.md and not swept: {missing}"


def test_SEN_2_every_member_cp11_names_is_swept():
    names = {member.name for member in MEMBERS}
    missing = sorted(CP11_REQUIRED - names)
    assert not missing, f"named REQUIRED by CP11 and not swept: {missing}"


def test_SEN_2_every_swept_key_exists_in_its_table():
    """A typo in a key would silently sweep nothing, and `override` would raise
    only when that member's point ran — an hour into a sweep."""
    for member in MEMBERS:
        for key in member.keys:
            if member.space == WORLD:
                assert key in PARAMS, f"{key} is not a generator parameter"
            elif member.space == POLICY:
                assert key in POLICY_PARAMS, f"{key} is not a policy parameter"
            else:
                assert member.space == MODEL and key == "MAX_FLAT_DECISION_RATE"


def test_SEN_2_base_values_reads_the_shipped_value():
    base = base_values()
    assert set(base) == SWEPT_KEYS
    assert base["economic_stop_multiple"] == float(POLICY_PARAMS["economic_stop_multiple"])
    assert base["world.liquidity_window_days"] == float(PARAMS["world.liquidity_window_days"])


def test_SEN_2_multiples_span_a_sixteenfold_range_around_the_shipped_value():
    assert MULTIPLES == (0.25, 0.5, 1.0, 2.0, 4.0)
    assert max(MULTIPLES) / min(MULTIPLES) == 16.0


# --------------------------------------------------------------------------
# REB-1 — the override reaches every cached constant
# --------------------------------------------------------------------------

# `ast.unparse` normalises string quotes, so both styles have to match.
_CACHE_PATTERN = re.compile(r"""(?:PARAMS|POLICY_PARAMS)\[['"]([^'"]+)['"]\]""")


def _module_level_caches() -> dict[str, set[str]]:
    """`module -> swept keys it caches in a module-level constant`.

    Literal subscripts only. `world.ACTION_LIFT` builds its keys with an
    f-string and cannot be found this way, which is why the test asserts that
    one by hand below — a scanner that silently missed it would be worse than
    no scanner.
    """
    found: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module level only
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            keys = set(_CACHE_PATTERN.findall(ast.unparse(node.value))) & SWEPT_KEYS
            if keys:
                found.setdefault(str(path.relative_to(REPO_ROOT)), set()).update(keys)
    return found


def test_REB_1_every_cached_constant_derived_from_a_swept_key_is_rebound():
    """The scan is over the whole package, so a future constant caching a swept
    key fails here rather than producing a flat sweep row nobody questions."""
    caches = _module_level_caches()
    # Everything the scan can find is accounted for by `override`, checked
    # behaviourally: patch the key, and the cached constant must have moved.
    assert caches, "the scanner found nothing — the pattern has drifted"
    assert set(caches) == {"settle/agent/policy.py"}, (
        f"a module now caches a swept parameter and may not be rebound: {caches}"
    )


def test_REB_1_economic_stop_multiple_rebinds():
    from settle.agent import policy as agent_policy

    before = agent_policy.ECONOMIC_STOP_MULTIPLE
    with override({"economic_stop_multiple": before * 4}):
        assert agent_policy.ECONOMIC_STOP_MULTIPLE == before * 4
        assert POLICY_PARAMS["economic_stop_multiple"] == before * 4
    assert agent_policy.ECONOMIC_STOP_MULTIPLE == before
    assert POLICY_PARAMS["economic_stop_multiple"] == before


def test_REB_1_action_lift_rebinds():
    """`world.ACTION_LIFT` is built once at import, with f-string keys the AST
    scan above cannot see. If this stops holding, `action_lift.*` sweeps as a
    flat line and reads as robustness."""
    from settle.sim import world as sim_world

    before = sim_world.ACTION_LIFT[ActionType.RETRY]
    with override({"action_lift.retry": before * 4}):
        assert sim_world.ACTION_LIFT[ActionType.RETRY] == before * 4
    assert sim_world.ACTION_LIFT[ActionType.RETRY] == before


def test_REB_1_override_restores_on_exception():
    before = dict(PARAMS)
    with pytest.raises(RuntimeError):
        with override({"world.liquidity_window_days": 99.0}):
            raise RuntimeError("boom")
    assert PARAMS == before


def test_REB_1_an_unknown_key_is_refused():
    with pytest.raises(KeyError):
        with override({"not.a.parameter": 1.0}):
            pass


# --------------------------------------------------------------------------
# Clamping and survival arithmetic — pure, so they need no model
# --------------------------------------------------------------------------

def test_probabilities_clamp_at_one():
    member = next(m for m in MEMBERS if m.name == "natural_recovery.*")
    applied = member.applied(4.0, base_values())
    assert applied["natural_recovery.willing_able"] == 1.0   # 0.45 x 4, clamped
    assert applied["natural_recovery.churned"] == 0.04


def test_integral_members_record_what_the_consumer_sees():
    """`class_retry_cap` is read through `int()`, so 0.25 x 2 is a cap of zero
    and the report must say zero rather than 0.5."""
    member = next(m for m in MEMBERS if m.name == "class_retry_cap.dead_instrument")
    base = base_values()
    assert member.applied(0.25, base)["class_retry_cap.dead_instrument"] == 0.0
    assert member.applied(0.5, base)["class_retry_cap.dead_instrument"] == 1.0
    assert member.applied(4.0, base)["class_retry_cap.dead_instrument"] == 8.0


def test_survival_range_is_the_interval_around_the_shipped_value():
    holds = {0.25: True, 0.5: True, 1.0: True, 2.0: True, 4.0: False}
    result = survival_range(holds)
    assert (result["low"], result["high"]) == (0.25, 2.0)
    assert result["label"] == "0.25x–2x"
    assert result["contiguous"]


def test_survival_range_reports_a_gap_rather_than_smoothing_it():
    """A conclusion that holds at 0.25x, fails at 0.5x and holds again at 1x is
    a finding. Reporting it as `0.25x–4x` would hide the hole."""
    holds = {0.25: True, 0.5: False, 1.0: True, 2.0: True, 4.0: True}
    result = survival_range(holds)
    assert (result["low"], result["high"]) == (1.0, 4.0)
    assert not result["contiguous"]
    assert result["holds_at"] == [0.25, 1.0, 2.0, 4.0]


def test_survival_range_says_so_when_the_conclusion_fails_at_the_shipped_value():
    result = survival_range({m: False for m in MULTIPLES})
    assert result["label"] == "fails at 1x"
    assert not result["holds_at_base"]


# --------------------------------------------------------------------------
# Model selection under a different floor
# --------------------------------------------------------------------------

@pytest.mark.skipif(MODEL_PATH is None, reason="no trained model; run CP7 training")
def test_selection_replays_the_shipped_choice_at_the_shipped_floor():
    import pickle

    payload = pickle.loads(MODEL_PATH.read_bytes())
    assert select_winner(payload) == payload["winner"]


@pytest.mark.skipif(MODEL_PATH is None, reason="no trained model; run CP7 training")
def test_selection_refuses_every_candidate_at_a_floor_nothing_clears():
    import pickle

    payload = pickle.loads(MODEL_PATH.read_bytes())
    with override({"MAX_FLAT_DECISION_RATE": -1.0}):
        assert select_winner(payload) is None


# --------------------------------------------------------------------------
# SEN-1 — reproducibility
# --------------------------------------------------------------------------

_SEN1_MEMBER: Member = next(m for m in MEMBERS if m.name == "world.liquidity_window_days")


def _comparable(report: dict) -> dict:
    """The report with wall-clock fields dropped. Timings differ between runs
    and are not a result."""
    stripped = {"base": {k: v for k, v in report["base"].items() if k != "seconds"}}
    for member in report["members"]:
        stripped[member["name"]] = [
            {k: v for k, v in point.items() if k != "seconds"} for point in member["points"]
        ]
    return stripped


@pytest.mark.skipif(MODEL_PATH is None, reason="no trained model; run CP7 training")
def test_SEN_1_same_seed_same_results():
    first = sweep(cases=30, seed=11, model_path=MODEL_PATH, members=(_SEN1_MEMBER,))
    second = sweep(cases=30, seed=11, model_path=MODEL_PATH, members=(_SEN1_MEMBER,))
    assert _comparable(first) == _comparable(second)


@pytest.mark.skipif(MODEL_PATH is None, reason="no trained model; run CP7 training")
def test_SEN_1_a_different_seed_is_a_different_batch():
    """The reproducibility check above is worth nothing unless the sweep is
    capable of returning something else."""
    first = sweep(cases=30, seed=11, model_path=MODEL_PATH, members=(_SEN1_MEMBER,))
    other = sweep(cases=30, seed=12, model_path=MODEL_PATH, members=(_SEN1_MEMBER,))
    assert _comparable(first) != _comparable(other)


@pytest.mark.slow
@pytest.mark.skipif(MODEL_PATH is None, reason="no trained model; run CP7 training")
def test_SEN_1_the_worker_count_does_not_change_the_answer():
    """Every point is a pure function of its overrides, so fanning the sweep
    across processes must not move a number. Marked slow only because spawning
    workers costs more than the 30-case sweep it is checking."""
    serial = sweep(cases=30, seed=11, model_path=MODEL_PATH, members=(_SEN1_MEMBER,))
    parallel = sweep(cases=30, seed=11, model_path=MODEL_PATH, members=(_SEN1_MEMBER,), workers=2)
    assert _comparable(serial) == _comparable(parallel)
