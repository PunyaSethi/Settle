"""CP13 — the charts and the README. SPEC §14.4, §19.

CHT-3 is the one that matters. A README number with no artefact behind it is
the exact failure this project is about: we spend seven sections arguing that a
recovery figure is worth what its evidence is worth, and an unbacked figure in
our own headline would refute the argument more effectively than any critic.

So this file reads the committed README, pulls every number out of it, and
requires each one to appear in a committed artefact. It is deliberately
awkward to satisfy. Making a claim in the README means producing the number
first.
"""

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHARTS_DIR = REPO_ROOT / "out" / "charts"
METRICS = REPO_ROOT / "out" / "metrics.json"
README = REPO_ROOT / "README.md"
MODEL_REPORT = REPO_ROOT / "out" / "model_report.json"

CHART_FILES = (
    "recovery_vs_contacts.png",
    "reliability.png",
    "by_decline_class.png",
    "sensitivity.png",
)

needs_metrics = pytest.mark.skipif(
    not METRICS.exists(),
    reason="out/charts/metrics.json not generated — run settle.eval.report",
)


def load_metrics() -> dict:
    return json.loads(METRICS.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# CHT-1 — every chart regenerates deterministically from committed data
# --------------------------------------------------------------------------

@needs_metrics
def test_CHT_1_charts_regenerate_deterministically_from_committed_data(tmp_path: Path) -> None:
    """Render twice into scratch directories and compare bytes.

    Byte-identical, not merely similar. A chart that drifts between renders
    cannot be reviewed in a diff, and the committed PNG stops being evidence of
    anything a reader can reproduce.
    """
    from settle.eval.charts import render_all

    data = load_metrics()
    first = tmp_path / "a"
    second = tmp_path / "b"
    render_all(data, first)
    render_all(data, second)

    for name in CHART_FILES:
        left, right = first / name, second / name
        assert left.exists(), f"{name} was not rendered"
        assert left.read_bytes() == right.read_bytes(), (
            f"{name} differs between two renders of the same data"
        )
        # A chart that is a few hundred bytes is an empty canvas.
        assert left.stat().st_size > 10_000, f"{name} looks empty"


@needs_metrics
def test_CHT_1_the_committed_charts_match_a_fresh_render(tmp_path: Path) -> None:
    """The PNGs in the repo are the ones this data produces.

    Without this, the committed images could drift from the committed numbers
    and every chart in the README would be decoration.
    """
    from settle.eval.charts import render_all

    render_all(load_metrics(), tmp_path)
    stale = [
        name
        for name in CHART_FILES
        if (CHARTS_DIR / name).read_bytes() != (tmp_path / name).read_bytes()
    ]
    assert not stale, (
        f"committed charts are stale: {stale}. Run `python -m settle.eval.charts`."
    )


# --------------------------------------------------------------------------
# CHT-2 — charts read from artefacts, never from hardcoded numbers
# --------------------------------------------------------------------------

# Every headline figure. If one of these appears as a literal in the chart or
# report source, the number has been typed in rather than computed.
FORBIDDEN_LITERALS = (
    0.279, 27.9, 0.2565, 25.65, 0.0045, 1.426, 6993, 6576, 2852,
    0.0392, 0.016, 0.0176, 0.0193, 391664, 372686, 0.0145, 0.053,
)


def _literals(path: Path) -> set[float]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[float] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if not isinstance(node.value, bool):
                found.add(float(node.value))
    return found


def test_CHT_2_charts_and_report_hardcode_no_headline_number() -> None:
    """AST, not grep: a number spelled `27.90` and one spelled `0.2790` are the
    same offence, and a comment mentioning one is not."""
    for module in ("charts.py", "report.py"):
        path = REPO_ROOT / "settle" / "eval" / module
        literals = _literals(path)
        offenders = sorted(literals & set(FORBIDDEN_LITERALS))
        assert not offenders, f"{module} hardcodes headline numbers: {offenders}"


def test_CHT_2_charts_read_the_artefacts_and_nothing_else() -> None:
    """`charts.py` opens the metrics artefact; `report.py` reads the sweep and
    builds the batch. Neither invents a source."""
    charts = (REPO_ROOT / "settle" / "eval" / "charts.py").read_text(encoding="utf-8")
    report = (REPO_ROOT / "settle" / "eval" / "report.py").read_text(encoding="utf-8")

    assert "out/metrics.json" in charts
    assert "out/sensitivity.json" in report

    # charts.py must not run a simulation to draw a picture. If it could, the
    # committed PNGs would not be reproducible from the committed JSON.
    for banned in ("generate_batch", "run_case", "OursArm", "reconcile"):
        assert banned not in charts, f"charts.py reaches for {banned}"

    from settle.eval import charts as charts_module

    assert charts_module.METRICS == Path("out/metrics.json")


@needs_metrics
def test_CHT_2_every_chart_input_is_present_in_the_artefact() -> None:
    """The artefact carries what the four charts need, so a fresh clone can
    render them without re-running anything."""
    data = load_metrics()

    assert set(data) >= {
        "meta", "arms", "by_decline_class", "calibration", "sensitivity",
        "retry_timing", "comparison",
    }

    for name, row in data["arms"].items():
        for field in ("incremental_rate", "contacts_per_case", "compliance_violations"):
            assert field in row, f"arm {name} missing {field}"

    for name, row in data["by_decline_class"].items():
        assert "ours_minus_b2_rate" in row and "cases" in row, name

    calibration = data["calibration"]
    assert calibration["reliability"], "no reliability buckets"
    for bucket in calibration["reliability"]:
        assert {"bucket", "n", "predicted", "actual", "extrapolated"} <= set(bucket)

    sensitivity = data["sensitivity"]
    assert sensitivity["available"], sensitivity.get("reason")
    assert sensitivity["members"] and sensitivity["meta"]["multiples"]


@needs_metrics
def test_CHT_2_the_losing_classes_are_present_and_drawn() -> None:
    """Chart 3 exists to show the classes we lose. If the data had none, the
    chart would be vacuous and this test says so rather than passing quietly."""
    data = load_metrics()
    losses = {
        name: row["ours_minus_b2_rate"]
        for name, row in data["by_decline_class"].items()
        if row["ours_minus_b2_rate"] < 0
    }
    assert losses, (
        "no decline class where OURS loses to B2 — chart 3 has nothing to show, "
        "and the README's 'where it loses' section is now false"
    )
    readme = README.read_text(encoding="utf-8")
    for name in losses:
        pretty = name.replace("_", " ").replace("dead instrument", "dead_instrument")
        assert (
            name in readme or name.replace("_", "-") in readme or pretty in readme
        ), f"README does not name the losing class {name}"


# --------------------------------------------------------------------------
# CHT-3 — every number in the README appears in a committed artefact
# --------------------------------------------------------------------------

# Numbers that are structural rather than measured: section numbers, spec
# identifiers, dates, ports, and the small integers that appear in prose. Listed
# explicitly so the exemption is auditable rather than a loose regex.
STRUCTURAL: frozenset[str] = frozenset(
    {
        # section, invariant, gate and spec references
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "14", "15", "45",
        # horizons, windows and grid offsets from SPEC §5.3 / §13.1
        "0", "18", "30", "48", "60", "72", "120", "168", "24",
        # the RBI circular and the year
        "2026", "27", "396",
        # sha256 / hash widths
        "256", "64",
        # version-ish and structural counts used in prose
        "12", "13", "16", "20", "21", "50",
    }
)

NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)")


def _artefact_haystack() -> set[str]:
    """Every number that appears in a committed artefact, in every rendering a
    README would plausibly use.

    Built from the artefacts themselves rather than from a list, so adding a
    number to the README without producing it first cannot pass.
    """
    haystack: set[str] = set()

    def add(value) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            renderings = {
                f"{value}", f"{value:,}", f"{abs(value)}", f"{abs(value):,}",
            }
            for places in (0, 1, 2, 3, 4):
                renderings.add(f"{abs(value):.{places}f}")
                renderings.add(f"{abs(value):,.{places}f}")
                # percentages, the form a README quotes a rate in
                renderings.add(f"{abs(value) * 100:.{places}f}")
                renderings.add(f"{abs(value) * 100:,.{places}f}")
                # paise rendered as rupees
                renderings.add(f"{abs(value) / 100:.{places}f}")
                renderings.add(f"{abs(value) / 100:,.{places}f}")
            haystack.update(r.rstrip(".") for r in renderings)
            return
        if isinstance(value, str):
            haystack.update(NUMBER.findall(value))
            return
        if isinstance(value, dict):
            for key, item in value.items():
                haystack.update(NUMBER.findall(str(key)))
                add(item)
            return
        if isinstance(value, list):
            for item in value:
                add(item)

    for path in (METRICS, MODEL_REPORT, REPO_ROOT / "out" / "razorpay_demo.json"):
        if path.exists():
            add(json.loads(path.read_text(encoding="utf-8")))

    # PRIORS.md and SPEC.md are committed artefacts too, and carry the design
    # constants and provenance tiers the README quotes. They are text, so only
    # the numbers literally written in them count.
    for path in (REPO_ROOT / "PRIORS.md", REPO_ROOT / "SPEC.md"):
        if path.exists():
            haystack.update(NUMBER.findall(path.read_text(encoding="utf-8")))

    return {value.replace(",", "") for value in haystack} | haystack


def _readme_numbers() -> list[str]:
    text = README.read_text(encoding="utf-8")
    # Fenced code blocks are commands and formulae, not claims.
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    # Razorpay ids and file paths carry digits that are identifiers, not figures.
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\]\([^)]*\)", " ", text)
    return NUMBER.findall(text)


def test_CHT_3_every_number_in_the_readme_appears_in_a_committed_artefact() -> None:
    """The test this checkpoint is for.

    Every figure the README quotes must be findable in `out/charts/metrics.json`,
    `out/razorpay_demo.json`, `PRIORS.md` or `SPEC.md`. A number that is none of
    those is either a typo or an invention, and both are the same problem.
    """
    if not METRICS.exists():
        pytest.skip("out/charts/metrics.json not generated")

    haystack = _artefact_haystack()
    unbacked = []
    for raw in _readme_numbers():
        candidate = raw.replace(",", "")
        if candidate in STRUCTURAL or raw in STRUCTURAL:
            continue
        if candidate in haystack or raw in haystack:
            continue
        unbacked.append(raw)

    assert not unbacked, (
        "README quotes numbers that appear in no committed artefact: "
        f"{sorted(set(unbacked))}. Produce the number before claiming it."
    )


def test_CHT_3_is_not_vacuous() -> None:
    """A fabricated number must fail the check the README passes.

    Without this, a haystack that had quietly become permissive — or a README
    regex that matched nothing — would let CHT-3 pass while checking nothing.
    """
    haystack = _artefact_haystack()
    assert _readme_numbers(), "no numbers extracted from the README at all"
    for invented in ("83.719", "41.0071", "9999.31"):
        assert invented not in haystack, (
            f"{invented} was found in the artefacts, so the haystack is too "
            "permissive to catch an invented figure"
        )


@needs_metrics
def test_CHT_3_the_readme_headline_matches_the_artefact_exactly() -> None:
    """Spot-check the claims a judge reads first, against the source of truth.

    CHT-3 proves every README number exists somewhere in the artefacts. This
    proves the specific headline claims are the *right* numbers, which is a
    different failure: 25.65% is a real figure in the artefact and would pass
    the membership test even if the README attributed it to the wrong arm.
    """
    data = load_metrics()
    readme = README.read_text(encoding="utf-8")
    ours, b2 = data["arms"]["OURS"], data["arms"]["B2"]

    assert f"{ours['incremental_rate'] * 100:.2f}%" in readme
    assert f"{b2['incremental_rate'] * 100:.2f}%" in readme
    assert f"{ours['contacts']:,}" in readme or str(ours["contacts"]) in readme
    assert f"{b2['contacts']:,}" in readme
    assert f"{ours['silent_failure_rate'] * 100:.2f}%" in readme
    assert f"{data['arms']['B3']['compliance_violations']:,}" in readme

    # A99's restraint sentence, verbatim. It is the one claim in the README
    # whose exact wording was specified, because "more active and less
    # intrusive" is the distinction the whole result turns on. Compared against
    # a whitespace-normalised copy: it is set as a blockquote, so the source
    # carries "> " prefixes and line breaks the sentence itself does not.
    flat = " ".join(readme.replace("\n> ", " ").replace("> ", "").split())
    assert (
        f"OURS dispatches {ours['dispatches']:,} actions against B2's "
        f"{b2['dispatches']:,}. It is more active and less intrusive. "
        "Far fewer CONTACTS, not less work." in flat
    ), "A99's restraint sentence is not present verbatim"

    # §19 fixes the section order and it does not change.
    positions = [
        readme.index(heading)
        for heading in (
            "## 1. Headline metrics",
            "## 2. Architecture",
            "## 3. Reliability",
            "## 4. Reproduce it",
            "## 5. How the thresholds were chosen",
            "## 6. Priors and provenance",
            "## 7. Known limitations",
            "## 8. Next steps",
            "## 9. Simulated at scale, real at the edges",
        )
    ]
    assert positions == sorted(positions), "README sections are out of SPEC §19 order"

    # The first paragraph carries the framing and the real id (CP13 Part C).
    opening = readme[: readme.index("## 1.")]
    assert "simulated at scale" in opening.lower()
    assert "plink_" in opening

    # Every chart the README embeds is committed.
    for name in CHART_FILES:
        assert name in readme, f"README does not show {name}"
        assert (CHARTS_DIR / name).exists(), f"{name} is referenced but not committed"


@pytest.mark.skipif(not MODEL_REPORT.exists(), reason="out/model_report.json not generated")
def test_CHT_3_the_two_timing_hypotheses_are_reported_separately() -> None:
    """F22. A83's "retry timing" was two hypotheses reported as one.

    Liquidity timing was the stated differentiator and is withdrawn; recency
    survived. Reporting them together understated one and overstated the other,
    so this asserts both documents carry the split and the current figures —
    not the pre-A93 ones the README carried until CP13.1.
    """
    report = json.loads(MODEL_REPORT.read_text(encoding="utf-8"))
    timing = report["retry_timing"]
    liquidity, recency = timing["liquidity"], timing["recency"]

    readme = README.read_text(encoding="utf-8")
    limits = (REPO_ROOT / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")

    ranks = sorted(v["rank"] for v in liquidity["feature_ranks"].values())
    recency_rank = min(v["rank"] for v in recency["feature_ranks"].values())
    total = timing["n_features"]
    span = liquidity["sensitivity"]["margin_span_points"]
    median_points = f"{recency['offset_spread']['median'] * 100:.1f}"
    rows = f"{recency['offset_spread']['n_retry_rows']:,}"

    for document, name in ((readme, "README.md"), (limits, "KNOWN_LIMITATIONS.md")):
        # The two verdicts must be distinguishable, not merged into "timing".
        assert "LIQUIDITY" in document.upper(), f"{name} does not name the liquidity hypothesis"
        assert "RECENCY" in document.upper(), f"{name} does not name the recency hypothesis"

        for rank in ranks:
            assert str(rank) in document, f"{name} missing liquidity rank {rank}"
        assert f"{recency_rank} of {total}" in document, (
            f"{name} does not report the recency rank as {recency_rank} of {total}"
        )
        assert f"{span:.2f}" in document, (
            f"{name} does not quote the liquidity sweep span {span:.2f}"
        )
        assert median_points in document and rows in document, (
            f"{name} does not quote the offset spread {median_points} over {rows} rows"
        )

    # Each liquidity feature is named beside its own rank, in order. Guards the
    # transposition CP13.2 found in its own prescribed text: the ranks were
    # right and the names attached to them were not.
    ordered = sorted(liquidity["feature_ranks"].items(), key=lambda kv: kv[1]["rank"])
    names_in_order = [name for name, _ in ordered]
    positions = [readme.index(f"`{name}`") for name in names_in_order]
    assert positions == sorted(positions), (
        "README lists the liquidity features out of rank order, so a reader "
        f"pairing them positionally gets the wrong rank: expected {names_in_order}"
    )

    # The superseded figures are recorded, not silently dropped.
    superseded = timing["superseded_figures"]
    assert superseded["n_features"] != total
    assert str(superseded["median_spread_points"]) in limits, (
        "KNOWN_LIMITATIONS.md does not record what these figures replaced"
    )


@pytest.mark.skipif(not MODEL_REPORT.exists(), reason="out/model_report.json not generated")
def test_CHT_3_sf2_is_decomposed_not_just_counted() -> None:
    """F23. The bare count conflates opportunity with judgement.

    SF-2 needs a settlement the agent never heard about AND a contact after it.
    Reporting only the outcome makes B3 look disciplined when it is mostly just
    rarely in a position to make the mistake.
    """
    report = json.loads(MODEL_REPORT.read_text(encoding="utf-8"))
    arms = report["sf2_attribution"]["arms"]
    readme = README.read_text(encoding="utf-8")

    for name in ("OURS", "B2", "B3"):
        row = arms[name]
        assert f"{row['blind_set']:,}" in readme, (
            f"README does not report {name}'s blind set ({row['blind_set']:,}); "
            "without it the SF-2 counts cannot be compared"
        )
        assert f"{row['sf2_share_of_blind_set'] * 100:.1f}%" in readme, (
            f"README does not report {name}'s share of its blind set"
        )

    # The attribution sentence, and that it is attached to the number.
    assert "the reconciliation code is identical across arms" in readme.lower()
    assert str(arms["B2"]["sf2"]) in readme and str(arms["OURS"]["contacts"]) in readme

    # OURS and B2 face near-identical blind sets, which is what makes the
    # comparison a statement about behaviour rather than about luck.
    assert abs(arms["OURS"]["blind_set"] - arms["B2"]["blind_set"]) < 50, (
        "OURS and B2 no longer have comparable blind sets, so the README's "
        "central SF-2 comparison needs rewriting"
    )


def test_CHT_3_known_limitations_states_a_cost_for_every_entry() -> None:
    """The standard set on the projection entry: a limitation that only restates
    a design decision is a feature description."""
    path = REPO_ROOT / "KNOWN_LIMITATIONS.md"
    text = path.read_text(encoding="utf-8")

    entries = [line for line in text.splitlines() if line.startswith("### ")]
    assert len(entries) >= 12, f"only {len(entries)} limitation entries"

    # Every prose entry states its cost. The two table-driven sections state
    # theirs in the table's own column instead, so they are counted separately.
    costs = text.count("**What it costs.**")
    assert costs >= len(entries) - 4, (
        f"{len(entries)} entries but only {costs} state a cost"
    )

    for required in (
        "184", "0.0392", "0.0160", "contact_response.rate",
        "Settlement Recon", "Scheduling fired immediately",
        "Dead instruments were unrecoverable",
        "Contacts could not produce settlements",
    ):
        assert required in text, f"KNOWN_LIMITATIONS.md does not mention {required}"
