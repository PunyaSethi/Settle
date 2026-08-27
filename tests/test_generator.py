"""CP2 — the batch generator. SPEC §5.1, §5.2, §6, §8, §9.

GEN-5 is the load-bearing one. GEN-1 says the batch is reproducible today;
GEN-5 is what keeps that true next week, because a single `datetime.now()` or
unseeded `random` call anywhere under `settle/sim/` would break reproducibility
silently and nobody would notice until two runs stopped agreeing.
"""

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from settle.schema.canonical import canonical_json
from settle.schema.enums import DeclineClass, MandateState
from settle.sim.generator import (
    PARAMS,
    UNMAPPED_CODES,
    generate_batch,
    generate_case,
    is_escalation_eligible,
    summarise,
)
from settle.sim.observability import (
    OBSERVABILITY_DEFAULTS,
    REPORTING_PARAMETERS,
    ObservabilityConfig,
    perfect_observability,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = REPO_ROOT / "settle" / "sim"
AGENT_FACING_DIRS = ("settle/agent", "settle/policy", "settle/schema")

N = 10_000
SEED = 42


@pytest.fixture(scope="module")
def batch():
    return generate_batch(N, SEED)


@pytest.fixture(scope="module")
def realised(batch):
    return summarise(batch)


def batch_digest(n: int, seed: int) -> str:
    return hashlib.sha256(
        b"".join(canonical_json(g) for g in generate_batch(n, seed).cases)
    ).hexdigest()


# --------------------------------------------------------------------------
# GEN-1
# --------------------------------------------------------------------------

def test_GEN_1_same_seed_gives_the_same_batch():
    assert batch_digest(500, SEED) == batch_digest(500, SEED)
    assert batch_digest(500, SEED) != batch_digest(500, SEED + 1)


def test_GEN_1_identical_across_two_separate_processes():
    """Two processes, two different hash seeds, one digest.

    This is what catches someone reaching for `hash()` or an unseeded PRNG.
    """
    script = (
        "import hashlib;"
        "from settle.schema.canonical import canonical_json;"
        "from settle.sim.generator import generate_batch;"
        "print(hashlib.sha256(b''.join(canonical_json(g) for g in "
        "generate_batch(1000, 42).cases)).hexdigest())"
    )
    digests = {
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
        ).stdout.strip()
        for hash_seed in ("0", "1", "random")
    }
    assert len(digests) == 1, f"batch differed across processes: {digests}"
    assert digests.pop() == batch_digest(1000, 42)


def test_GEN_1_a_case_does_not_depend_on_the_batch_around_it():
    """Order independence. Regenerating case 4000 alone must give case 4000."""
    alone = generate_case(SEED, 4000)
    in_batch = generate_batch(4200, SEED).cases[4000]
    assert canonical_json(alone) == canonical_json(in_batch)


# --------------------------------------------------------------------------
# GEN-2 — truth leakage
# --------------------------------------------------------------------------

def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                list(package_parts[: len(package_parts) - (node.level - 1)]) if node.level else []
            )
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _leaks(path: Path) -> set[str]:
    return {
        name
        for name in _imported_modules(path)
        if name == "settle.sim" or name.startswith("settle.sim.")
    }


def test_GEN_2_no_agent_facing_module_imports_hidden_truth():
    scanned, missing, offenders = 0, [], {}
    for rel in AGENT_FACING_DIRS:
        directory = REPO_ROOT / rel
        if not directory.exists():
            missing.append(rel)
            continue
        for module_path in sorted(directory.rglob("*.py")):
            scanned += 1
            leaked = _leaks(module_path)
            if leaked:
                offenders[str(module_path.relative_to(REPO_ROOT))] = sorted(leaked)

    assert scanned > 0, f"GEN-2 scanned nothing — all of {AGENT_FACING_DIRS} absent"
    assert not offenders, f"INV-8 breach: {offenders}"
    # Not a failure yet; recorded so the test cannot quietly become vacuous when
    # settle/agent/ and settle/policy/ arrive.
    assert set(missing) <= {"settle/agent", "settle/policy"}


def test_GEN_2_detects_a_planted_violation():
    """A leak detector that has never fired is not evidence of anything."""
    planted = REPO_ROOT / "settle" / "schema" / "_leak_probe.py"
    planted.write_text(
        "from settle.sim.truth import HiddenTruth\nimport settle.sim.debtors\n", encoding="utf-8"
    )
    try:
        assert _leaks(planted) == {
            "settle.sim.truth",
            "settle.sim.truth.HiddenTruth",
            "settle.sim.debtors",
        }
    finally:
        planted.unlink()


def test_GEN_2_relative_imports_are_resolved_not_missed():
    planted = REPO_ROOT / "settle" / "schema" / "_leak_probe_rel.py"
    planted.write_text("from ..sim import truth\n", encoding="utf-8")
    try:
        assert _leaks(planted) == {"settle.sim", "settle.sim.truth"}
    finally:
        planted.unlink()


# --------------------------------------------------------------------------
# GEN-3 — distribution sanity
# --------------------------------------------------------------------------

def _binomial_tolerance(p: float, n: int = N, sigmas: float = 3.5) -> float:
    return max(sigmas * ((p * (1 - p) / n) ** 0.5), 0.004)


def _effective_class_rate(name: str) -> float:
    """§9 folds unmapped codes into `ambiguous`, so declared != realised there.

    The unmapped draw is independent of the class draw: a case is unmapped with
    probability u, and otherwise takes its drawn class. So every class shrinks
    by (1 - u), and `ambiguous` gets all of u back.
    """
    u = PARAMS["unmapped_code_rate"]
    declared = PARAMS[f"decline_class_mix.{name}"]
    return declared * (1 - u) + (u if name == DeclineClass.AMBIGUOUS.value else 0.0)


@pytest.mark.parametrize(
    "group",
    ["rail_mix", "language_mix", "intent_mix", "behaviour_mix"],
)
def test_GEN_3_categorical_mixes_match_their_params(realised, group):
    for key, got in realised[group].items():
        want = PARAMS[f"{group}.{key}"]
        assert abs(got - want) <= _binomial_tolerance(want), f"{group}.{key}: {got} vs {want}"


def test_GEN_3_decline_class_mix_matches_after_the_unmapped_fold():
    got = summarise(generate_batch(N, SEED))["decline_class_mix"]
    for name, observed_rate in got.items():
        want = _effective_class_rate(name)
        assert abs(observed_rate - want) <= _binomial_tolerance(want), f"{name}: {observed_rate} vs {want}"


def test_GEN_3_unmapped_codes_appear_and_stay_under_the_five_percent_gate(realised):
    rate = realised["flags"]["unmapped_code_rate"]
    assert rate > 0.0, "the §9 ambiguous fallback is never exercised"
    assert rate < 0.05, "§9 fails a run whose unmapped-code rate exceeds 5%"
    assert abs(rate - PARAMS["unmapped_code_rate"]) <= _binomial_tolerance(
        PARAMS["unmapped_code_rate"]
    )


@pytest.mark.parametrize(
    "key",
    [
        "consent_whatsapp_rate",
        "dnd_flag_rate",
        "mandate_cap.known_rate",
        "observed_credit_day.known_rate",
        "will_settle_rate",
        "will_reverse_rate",
        "escalation.target_overall_rate",
    ],
)
def test_GEN_3_binary_rates_match_their_params(realised, key):
    got, want = realised["flags"][key], PARAMS[key]
    assert abs(got - want) <= _binomial_tolerance(want), f"{key}: {got} vs {want}"


@pytest.mark.parametrize(
    ("key", "relative_tolerance"),
    [
        ("amount.median_paise", 0.03),
        ("tenure.mean_months", 0.05),
        ("prior_failures.mean", 0.06),
        ("prior_recoveries.mean", 0.07),
        ("settlement_lag_h.mean", 0.05),
        ("patience.mean", 0.06),
    ],
)
def test_GEN_3_central_tendencies_match_their_params(realised, key, relative_tolerance):
    got, want = realised["means"][key], PARAMS[key]
    assert abs(got - want) / want <= relative_tolerance, f"{key}: {got} vs {want}"


def test_GEN_3_mandate_state_follows_the_conditional_mix(batch, realised):
    """A `mandate_revoked` decline with `mandate_state=active` is incoherent.

    The mix is conditioned on the decline class for exactly that reason, so the
    realised marginal has to be the mixture, not either component.
    """
    dead = realised["decline_class_mix"][DeclineClass.DEAD_INSTRUMENT.value]
    for state in MandateState:
        want = (
            dead * PARAMS[f"mandate_state_dead.{state.value}"]
            + (1 - dead) * PARAMS[f"mandate_state_base.{state.value}"]
        )
        got = realised["mandate_state"].get(state.value, 0.0)
        assert abs(got - want) <= _binomial_tolerance(want), f"{state.value}: {got} vs {want}"

    dead_cases = [g for g in batch.cases if g.decline_class is DeclineClass.DEAD_INSTRUMENT]
    assert dead_cases
    assert not [
        g
        for g in dead_cases
        if g.observed.decline_code not in UNMAPPED_CODES
        and g.observed.mandate_state is MandateState.ACTIVE
    ], "a dead-instrument decline reported an active mandate"


def test_GEN_3_every_field_stays_inside_its_declared_domain(batch):
    for g in batch.cases:
        o, t = g.observed, g.truth
        assert PARAMS["amount.min_paise"] <= o.amount_paise <= PARAMS["amount.max_paise"]
        assert 1 <= o.attempt_number <= PARAMS["attempt_number.max"]
        assert o.plan_value_paise >= o.amount_paise
        assert o.observed_credit_day is None or 1 <= o.observed_credit_day <= 28
        assert 1 <= t.payday_day <= 28
        assert PARAMS["patience.min"] <= t.patience_budget <= PARAMS["patience.max"]
        assert 0 <= t.settlement_lag_h <= PARAMS["settlement_lag_h_max"]
        assert 0.0 <= t.true_recoverability <= 1.0


def test_GEN_3_escalation_eligible_requires_real_value_on_the_line(batch):
    eligible = [g for g in batch.cases if g.escalation_eligible]
    assert eligible
    assert all(
        g.observed.amount_paise >= PARAMS["escalation.min_amount_paise"] for g in eligible
    ), "a low-value case was flagged escalation-eligible"


def test_GEN_3_reporting_defaults_are_all_non_zero_and_zeroable():
    config = ObservabilityConfig()
    assert not config.is_perfect
    assert len(OBSERVABILITY_DEFAULTS) == 5
    assert all(value > 0.0 for value in OBSERVABILITY_DEFAULTS.values())
    assert perfect_observability().is_perfect


# --------------------------------------------------------------------------
# GEN-4 — PARAMS <-> PRIORS.md
# --------------------------------------------------------------------------

def _priors_section(title: str) -> dict[str, str]:
    text = (REPO_ROOT / "PRIORS.md").read_text(encoding="utf-8")
    body = text.split(f"## {title}", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] not in ("parameter", "---") and not cells[0].startswith("-"):
            rows[cells[0]] = cells[1]
    return rows


def test_GEN_4_every_param_has_a_priors_row():
    rows = _priors_section("Generator and world parameters")
    missing = sorted(set(PARAMS) - set(rows))
    assert not missing, f"PARAMS with no PRIORS.md row — INV-10 breach: {missing}"


def test_GEN_4_every_priors_row_has_a_param():
    rows = _priors_section("Generator and world parameters")
    orphans = sorted(set(rows) - set(PARAMS))
    assert not orphans, f"PRIORS.md rows with no PARAMS entry: {orphans}"


def test_GEN_4_recorded_values_match_the_code():
    rows = _priors_section("Generator and world parameters")
    drifted = {k: (PARAMS[k], rows[k]) for k in PARAMS if float(rows[k]) != PARAMS[k]}
    assert not drifted, f"PRIORS.md disagrees with PARAMS: {drifted}"


def test_GEN_4_every_prior_is_sourced_or_marked_asserted():
    text = (REPO_ROOT / "PRIORS.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] in PARAMS:
            assert cells[2], f"{cells[0]} has an empty source column (INV-10)"


def test_GEN_4_observability_defaults_are_recorded_too():
    rows = _priors_section("Observability parameters")
    assert set(rows) == set(OBSERVABILITY_DEFAULTS)
    for key, value in OBSERVABILITY_DEFAULTS.items():
        assert float(rows[key]) == value


# --------------------------------------------------------------------------
# GEN-5 — no wall clock, no unseeded randomness
# --------------------------------------------------------------------------

BANNED_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("os", "urandom"),
    ("uuid", "uuid4"),
}
BANNED_MODULES = {"random", "secrets"}


def _clock_or_randomness_uses(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in BANNED_CALLS:
                found.add(f"{owner.id}.{node.func.attr}()")
            elif isinstance(owner, ast.Attribute) and (owner.attr, node.func.attr) in BANNED_CALLS:
                found.add(f"{owner.attr}.{node.func.attr}()")
    for name in _imported_modules(path):
        root = name.split(".")[0]
        if root in BANNED_MODULES:
            found.add(f"import {name}")
    return found


def test_GEN_5_the_simulator_never_reads_a_clock_or_unseeded_randomness():
    """`created_at` is the as_of anchor (SPEC §5.1). Nothing else is.

    `random` is banned outright rather than merely required to be seeded:
    seeded or not, a sequential PRNG under `settle/sim/` would break STR-3 the
    moment two arms drew a different number of times. Addressed hashing is the
    mechanism; there is no legitimate second one.
    """
    modules = sorted(SIM_DIR.rglob("*.py"))
    assert modules, "GEN-5 scanned nothing under settle/sim/"
    offenders = {}
    for module_path in modules:
        uses = _clock_or_randomness_uses(module_path)
        if uses:
            offenders[str(module_path.relative_to(REPO_ROOT))] = sorted(uses)
    assert not offenders, f"wall clock or unseeded randomness under settle/sim/: {offenders}"


def test_GEN_5_detects_planted_violations():
    planted = SIM_DIR / "_clock_probe.py"
    planted.write_text(
        "import random\n"
        "from datetime import datetime\n"
        "import time\n"
        "def f():\n"
        "    return datetime.now(), time.time(), random.random()\n",
        encoding="utf-8",
    )
    try:
        found = _clock_or_randomness_uses(planted)
        assert "datetime.now()" in found
        assert "time.time()" in found
        assert "import random" in found
    finally:
        planted.unlink()


def test_GEN_5_the_batch_anchor_is_a_literal_not_a_clock():
    source = (SIM_DIR / "generator.py").read_text(encoding="utf-8")
    anchor = re.search(r"BATCH_ANCHOR:.*=\s*(datetime\([^)]*\))", source)
    assert anchor, "BATCH_ANCHOR is not a literal datetime"
    assert "now" not in anchor.group(1)


# --------------------------------------------------------------------------
# GEN-6 — escalation_eligible is derivable, not a channel
# --------------------------------------------------------------------------

def test_GEN_6_escalation_eligible_recomputes_from_observables_alone(batch):
    """SPEC §2's slice, A46's rule.

    The generator records the flag so the realised mix can be reported. A policy
    must recompute it. If these ever disagree, the recorded flag has become a
    channel from the hidden side into the agent, and INV-8 is one careless
    import away from breaking.
    """
    mismatches = [
        g.observed.case_id
        for g in batch.cases
        if is_escalation_eligible(g.observed) != g.escalation_eligible
    ]
    assert not mismatches, f"{len(mismatches)} cases disagree, first: {mismatches[:5]}"


def test_GEN_6_recomputation_survives_a_round_trip_through_observed_json(batch):
    """No hidden state: the rule works on an ObservedCase rebuilt from its JSON."""
    from settle.schema.observed import ObservedCase

    for g in batch.cases[:500]:
        rebuilt = ObservedCase.model_validate_json(g.observed.model_dump_json())
        assert is_escalation_eligible(rebuilt) == g.escalation_eligible


def test_GEN_6_the_rule_reads_only_high_value_and_retries_exhausted(batch):
    eligible = [g for g in batch.cases if g.escalation_eligible]
    assert eligible
    for g in eligible:
        assert g.observed.amount_paise >= PARAMS["escalation.min_amount_paise"]
        assert g.observed.attempt_number >= PARAMS["escalation.min_attempt_number"]
    # And the rule is total: every case that meets both conditions is flagged.
    assert not [
        g
        for g in batch.cases
        if not g.escalation_eligible
        and g.observed.amount_paise >= PARAMS["escalation.min_amount_paise"]
        and g.observed.attempt_number >= PARAMS["escalation.min_attempt_number"]
    ]


# --------------------------------------------------------------------------
# GEN-7 — --perfect-observability zeroes reporting, not the world
# --------------------------------------------------------------------------

WORLD_PARAMS_OUT_OF_REACH = ("auth_no_settle_rate", "settlement_lag_h.mean", "will_reverse_rate")


def test_GEN_7_perfect_observability_zeroes_exactly_the_five_reporting_parameters():
    perfect = perfect_observability()
    assert set(REPORTING_PARAMETERS) == set(ObservabilityConfig.model_fields)
    assert len(REPORTING_PARAMETERS) == 5
    for name in REPORTING_PARAMETERS:
        assert getattr(perfect, name) == 0.0, name
    assert perfect.is_perfect


def test_GEN_7_world_parameters_are_not_reachable_from_the_flag():
    """The three that decide whether money actually moves live in PARAMS."""
    for name in WORLD_PARAMS_OUT_OF_REACH:
        assert name in PARAMS, name
        assert PARAMS[name] > 0.0, name
        assert name not in ObservabilityConfig.model_fields
        assert f"observability.{name}" not in OBSERVABILITY_DEFAULTS


def test_GEN_7_world_module_does_not_import_the_reporting_layer():
    """Structural, not a convention. This is what makes the seam hold."""
    leaked = {
        name
        for name in _imported_modules(SIM_DIR / "world.py")
        if "observability" in name
    }
    assert not leaked, f"settle/sim/world.py imports the reporting layer: {leaked}"


def test_GEN_7_SF1_is_still_producible_under_perfect_observability(batch):
    """Marked recovered, never settled — with reporting perfect.

    If zeroing the flag made authorisation equivalent to settlement, SF-1 would
    become unproducible and the auditor would report zero for it. A detector
    that always reports zero is indistinguishable from a broken detector
    (SPEC §7).
    """
    from settle.schema.action import Retry
    from settle.sim.streams import Streams
    from settle.sim.world import attempt

    streams = Streams(SEED)
    perfect = perfect_observability()
    assert perfect.is_perfect

    authorised = 0
    authorised_but_unsettled = 0
    for g in batch.cases[:4000]:
        result = attempt(
            g.observed,
            g.truth,
            Retry(at_hour_offset=0, rail=g.observed.rail),
            g.observed.created_at,
            12,
            streams,
        )
        if result.authorised:
            authorised += 1
            if result.actual is not None and not result.actual.settled:
                authorised_but_unsettled += 1

    assert authorised > 0, "no authorisations to test against"
    assert authorised_but_unsettled > 0, (
        "SF-1 is unproducible: every authorisation settled, so the reporting "
        "flag has reached into the world"
    )
