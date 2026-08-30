"""CP7 — features. SPEC §10.1.

EST-1 and EST-2 are the pair that matter. A feature the merchant cannot compute
at decision time is a feature that will not exist in production, and a feature
derived from `HiddenTruth` is the model being handed its own answer.
"""

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from settle.agent.features import (
    FEATURE_NAMES,
    action_offset,
    dispatch_moment,
    feature_row,
    feature_vector,
    target_rail,
)
from settle.schema.action import DoNothing, Retry, SendMessage, SwitchRail, VoiceCall
from settle.schema.enums import Channel, Rail
from settle.sim.generator import generate_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "settle" / "agent"


@pytest.fixture(scope="module")
def case():
    return generate_batch(1, 90_000).cases[0].observed


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(parts[: len(parts) - (node.level - 1)]) if node.level else []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{a.name}" for a in node.names)
    return found


# --------------------------------------------------------------------------
# EST-1
# --------------------------------------------------------------------------

def test_EST_1_the_agent_package_imports_nothing_from_settle_sim():
    """INV-8. A model that could reach hidden truth would be predicting an
    answer it had been handed."""
    offenders = {}
    for module_path in sorted(AGENT_DIR.rglob("*.py")):
        leaked = {n for n in _imports(module_path) if n == "settle.sim" or n.startswith("settle.sim.")}
        if leaked:
            offenders[str(module_path.relative_to(REPO_ROOT))] = sorted(leaked)
    assert not offenders, f"INV-8 breach in settle/agent/: {offenders}"


def test_EST_1_detects_a_planted_violation():
    planted = AGENT_DIR / "_leak_probe.py"
    planted.write_text("from settle.sim.truth import HiddenTruth\n", encoding="utf-8")
    try:
        assert {n for n in _imports(planted) if n.startswith("settle.sim")}
    finally:
        planted.unlink()


def test_EST_1_no_feature_name_mentions_hidden_truth():
    """A crude check, and a useful one: `payday_day` or `true_recoverability`
    appearing here would mean the leak got in by another route."""
    banned = ("payday", "recoverability", "patience", "intent", "will_settle", "will_reverse")
    for name in FEATURE_NAMES:
        assert not any(token in name for token in banned), name


# --------------------------------------------------------------------------
# EST-2
# --------------------------------------------------------------------------

def test_EST_2_a_row_is_computable_from_case_action_and_tick_alone(case):
    row = feature_row(case, Retry(at_hour_offset=48, rail=case.rail), 24)
    assert set(row) == set(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in row.values())
    assert len(feature_vector(case, DoNothing(), 0)) == len(FEATURE_NAMES)


@pytest.mark.parametrize(
    "action",
    [
        DoNothing(),
        Retry(at_hour_offset=0, rail=Rail.CARD),
        Retry(at_hour_offset=168, rail=Rail.ENACH),
        SwitchRail(to=Rail.UPI_AUTOPAY),
        SendMessage(channel=Channel.WHATSAPP, template_id="t"),
        VoiceCall(),
    ],
)
def test_EST_2_every_verb_produces_a_complete_row(case, action):
    row = feature_row(case, action, 30)
    assert set(row) == set(FEATURE_NAMES)


def test_EST_2_features_are_deterministic(case):
    first = feature_vector(case, Retry(at_hour_offset=18, rail=case.rail), 12)
    for _ in range(20):
        assert feature_vector(case, Retry(at_hour_offset=18, rail=case.rail), 12) == first


def test_EST_2_the_dispatch_moment_is_tick_plus_offset_never_a_clock(case):
    """A feature derived from wall time makes the model unreproducible and the
    ledger unreplayable."""
    assert dispatch_moment(case, Retry(at_hour_offset=48, rail=case.rail), 24) == (
        case.created_at + timedelta(hours=72)
    )
    assert dispatch_moment(case, DoNothing(), 24) == case.created_at + timedelta(hours=24)

    source = (AGENT_DIR / "features.py").read_text(encoding="utf-8")
    for banned in ("datetime.now", "utcnow", "date.today", "time.time"):
        assert banned not in source


def test_EST_2_unknown_credit_day_is_flagged_not_imputed(case):
    """Zero and "we don't know" must be distinguishable, or the model reads a
    missing salary day as the zeroth of the month."""
    known = case.model_copy(update={"observed_credit_day": 1})
    unknown = case.model_copy(update={"observed_credit_day": None})
    a = feature_row(known, DoNothing(), 0)
    b = feature_row(unknown, DoNothing(), 0)
    assert a["observed_credit_day_known"] == 1.0 and b["observed_credit_day_known"] == 0.0
    assert b["observed_credit_day"] == 0.0


def test_EST_2_day_of_month_moves_with_the_offset(case):
    """The feature that carries the liquidity question. If it were constant the
    model could not learn timing from it even in principle."""
    days = {
        feature_row(case, Retry(at_hour_offset=o, rail=case.rail), 0)["day_of_month_at_dispatch"]
        for o in (0, 48, 168)
    }
    assert len(days) > 1


def test_EST_2_the_action_dimensions_are_encoded(case):
    row = feature_row(case, SwitchRail(to=Rail.ENACH), 0)
    assert row["action_switch_rail"] == 1.0
    assert row["target_rail_enach"] == 1.0
    assert row["channel_none"] == 1.0
    assert target_rail(case, SwitchRail(to=Rail.ENACH)) is Rail.ENACH
    assert action_offset(Retry(at_hour_offset=72, rail=Rail.CARD)) == 72
    assert action_offset(DoNothing()) == 0


# --------------------------------------------------------------------------
# EST-11 — the timing features
# --------------------------------------------------------------------------

def test_EST_11_days_to_month_start_is_correct_including_february():
    """Zero on the 1st and on the last day: both are adjacent to a salary
    credit, and treating the 31st as maximally far from payday would be
    describing the calendar rather than the customer."""
    from settle.agent.features import days_to_month_start

    assert days_to_month_start(1, 3) == 0
    assert days_to_month_start(2, 3) == 1
    assert days_to_month_start(31, 1) == 1
    assert days_to_month_start(30, 4) == 1
    # February is 28 because `payday_day` is bounded to 1..28 (§5.2), so no
    # salary lands on the 29th and a leap year cannot move a liquidity window.
    assert days_to_month_start(28, 2) == 1
    assert days_to_month_start(27, 2) == 2
    assert days_to_month_start(16, 3) == 15, "capped at 15"
    for month in range(1, 13):
        for day in range(1, 29):
            assert 0 <= days_to_month_start(day, month) <= 15


def test_EST_11_the_timing_features_come_from_created_at_plus_tick(case):
    """Never a clock. A feature read from wall time changes on a replay."""
    from settle.agent.features import LIQUIDITY_WINDOW_DAYS

    # Walk a whole month of ticks and confirm the feature tracks the calendar.
    seen = set()
    for hours in range(0, 24 * 40, 24):
        row = feature_row(case, DoNothing(), hours)
        seen.add(row["days_to_month_start"])
        assert row["in_liquidity_window"] == float(
            row["days_to_month_start"] <= LIQUIDITY_WINDOW_DAYS
        )
    assert 0.0 in seen and max(seen) > 5, "the feature never moved across 40 days"

    source = (AGENT_DIR / "features.py").read_text(encoding="utf-8")
    for banned in ("datetime.now", "utcnow", "date.today"):
        assert banned not in source


def test_EST_11_the_liquidity_window_is_the_agents_belief_not_the_worlds():
    """`world.liquidity_window_days` is the simulator's parameter and the agent
    may not read it. This is a modelling assumption, and it would be wrong for
    it to be exactly right by construction."""
    from settle.agent.features import LIQUIDITY_WINDOW_DAYS

    assert isinstance(LIQUIDITY_WINDOW_DAYS, int)
    # Checked against imports, not raw text: the module docstring explains the
    # rule and a text match would read the explanation as a breach.
    assert not [n for n in _imports(AGENT_DIR / "features.py") if n.startswith("settle.sim")]


def test_EST_11_days_since_last_attempt_is_optional_and_flagged(case):
    first = feature_row(case, DoNothing(), 48, None)
    later = feature_row(case, DoNothing(), 48, 24)
    assert first["has_prior_attempt"] == 0.0 and first["days_since_last_attempt"] == 0.0
    assert later["has_prior_attempt"] == 1.0 and later["days_since_last_attempt"] == 1.0
