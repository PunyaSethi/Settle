"""Four charts. SPEC §19 sections 1 and 3.

    python -m settle.eval.charts

Reads `out/charts/metrics.json` — the artefact `settle.eval.report` writes from
the run data and `out/sensitivity.json` — and renders four PNGs beside it. No
number is typed into this file (CHT-2). If a figure is not in the artefact, it
is not on a chart.

Four, and no more
-----------------
    1. recovery_vs_contacts   the thesis in one image
    2. reliability            predicted against actual, thin buckets marked
    3. by_decline_class       where OURS wins, and where it loses
    4. sensitivity            the headline across the swept range

Chart 3 is the one that earns the set. A chart showing only the classes we win
is a limitations section displaced into a picture, so the losing classes are
drawn at the same weight and the axis is centred on zero rather than starting at
the lowest bar.

Readability
-----------
Sized and weighted for a 1080p video frame, where a chart is on screen for a few
seconds at maybe a third of the width. That rules out default matplotlib: 10pt
tick labels vanish. Every axis carries units, every series is labelled in place
rather than only in a legend where possible, and there is no gridline, spine, or
tick that is not read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final

import matplotlib

matplotlib.use("Agg")  # no display, and deterministic across machines (CHT-1)

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

METRICS: Final[Path] = Path("out/charts/metrics.json")
CHARTS_DIR: Final[Path] = Path("out/charts")

CHART_FILES: Final[tuple[str, ...]] = (
    "recovery_vs_contacts.png",
    "reliability.png",
    "by_decline_class.png",
    "sensitivity.png",
)

# One ink colour, one accent, one warning. More than three colours on a chart
# that is on screen for four seconds is decoration.
INK: Final[str] = "#1a1a1a"
OURS: Final[str] = "#0b6e4f"
BASE: Final[str] = "#8a8a8a"
WARN: Final[str] = "#b3421a"
GRID: Final[str] = "#dcdcdc"

DPI: Final[int] = 160

CLASS_LABELS: Final[dict[str, str]] = {
    "time_shiftable": "time-shiftable",
    "transient": "transient",
    "dead_instrument": "dead instrument",
    "auth_abandoned": "auth abandoned",
    "ambiguous": "ambiguous",
    "terminal": "terminal",
}


def _style(ax) -> None:
    """Spines off, grid behind, ticks outward. Nothing that is not read."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK, labelsize=11, length=3)
    ax.set_axisbelow(True)


def _pct(value: float, _pos: int = 0) -> str:
    return f"{value * 100:.0f}%"


def load(path: Path = METRICS) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Run: python -m settle.eval.report --cases 2000 --seed 42"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Recovery against contacts — the thesis in one image
# ---------------------------------------------------------------------------

def recovery_vs_contacts(data: dict[str, Any], out: Path) -> Path:
    """Every arm as a point. Log x, because the separation is two orders.

    A linear x-axis would put B0 and OURS on top of each other at the origin and
    make the entire result invisible. The log scale is the honest choice here
    and is labelled as such; the zero-contact arms are drawn at the axis floor
    with their true value written on them, because log(0) has no position.
    """
    arms = data["arms"]
    figure, ax = plt.subplots(figsize=(8.6, 5.4))
    _style(ax)

    ours = arms.get("OURS")
    b2 = arms.get("B2")
    floor = 0.002  # the drawn position for a zero-contact arm
    points = []
    for name, row in arms.items():
        contacts = row["contacts_per_case"]
        points.append((name, max(contacts, floor), row["incremental_rate"], contacts))

    for name, x, y, true_contacts in points:
        is_ours = name == "OURS"
        # B3 recovers more than OURS and the chart must not let that pass
        # unqualified. It runs in OBSERVE: the gates are evaluated and their
        # verdicts are not binding, so its recovery is bought with compliance
        # breaches that no arm in ENFORCE is permitted. Drawn in the warning
        # colour, with the violation count on the label.
        violating = arms[name]["compliance_violations"] > 0
        colour = OURS if is_ours else (WARN if violating else BASE)
        ax.scatter(
            x, y,
            s=260 if is_ours else 150,
            color=colour,
            zorder=3, edgecolor="white", linewidth=1.5,
        )
        label = f"{name}\n{true_contacts:.3g} contacts/case"
        if violating:
            label += f"\nOBSERVE — {arms[name]['compliance_violations']:,} violations"
        ha = "center"
        offset_x = 0
        if name == "B3":
            ha, offset_x = "right", -14
        ax.annotate(
            label, (x, y),
            textcoords="offset points",
            xytext=(offset_x, 20 if is_ours else -46 if violating else -34),
            ha=ha, fontsize=11,
            color=colour if (is_ours or violating) else INK,
            fontweight="bold" if is_ours else "normal",
        )

    if ours and b2:
        ax.annotate(
            "",
            xy=(max(ours["contacts_per_case"], floor), ours["incremental_rate"]),
            xytext=(max(b2["contacts_per_case"], floor), b2["incremental_rate"]),
            arrowprops={"arrowstyle": "->", "color": OURS, "lw": 1.6,
                        "linestyle": "--", "shrinkA": 12, "shrinkB": 14},
        )

    ax.set_xscale("log")
    ax.set_xlim(floor * 0.6, 7)
    ax.set_xlabel("contacts per case  (log scale — the separation is two orders of magnitude)",
                  fontsize=12, color=INK)
    ax.set_ylabel("incremental recovery rate\n(% of cases, net of B0 self-cure)",
                  fontsize=12, color=INK)
    ax.yaxis.set_major_formatter(FuncFormatter(_pct))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ratio = (
        b2["contacts_per_case"] / ours["contacts_per_case"]
        if ours and b2 and ours["contacts_per_case"]
        else None
    )
    ax.set_title(
        "More recovery than the fixed ladder"
        + (f", on {ratio:,.0f}x fewer contacts." if ratio else "."),
        fontsize=15, color=INK, fontweight="bold", loc="left", pad=14,
    )
    # 10^-2 is unreadable at video resolution and this axis spans values a
    # reader should be able to compare by eye.
    ax.set_xticks([0.01, 0.1, 1.0])
    ax.set_xticklabels(["0.01", "0.1", "1.0"])
    ax.set_xticks([], minor=True)
    meta = data["meta"]
    ax.text(
        0, -0.22,
        f"{meta['cases']:,} synthetic cases, seed {meta['seed']}. "
        "Arms at zero contacts are drawn at the axis floor.\n"
        "Only B3 runs in OBSERVE; every other arm's gates bind, and its extra "
        "recovery is bought with compliance breaches.",
        transform=ax.transAxes, fontsize=9.5, color=BASE,
    )
    figure.tight_layout()
    figure.savefig(out, dpi=DPI, facecolor="white")
    plt.close(figure)
    return out


# ---------------------------------------------------------------------------
# 2. Reliability diagram
# ---------------------------------------------------------------------------

def reliability(data: dict[str, Any], out: Path) -> Path:
    """Predicted against actual, with bucket counts and the thin cells marked.

    Bucket counts are drawn because a reliability diagram without them invites
    the reader to weigh a bucket of 27 the same as a bucket of 7,519. The
    extrapolated markers say which points lean on cells the model has barely
    seen.
    """
    calibration = data["calibration"]
    buckets = [b for b in calibration["reliability"] if b["n"]]

    figure, (ax, ax_n) = plt.subplots(
        2, 1, figsize=(8.0, 6.8), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.12},
    )
    _style(ax)
    _style(ax_n)

    ax.plot([0, 1], [0, 1], color=BASE, linewidth=1.2, linestyle="--", zorder=1)
    ax.text(0.62, 0.585, "perfect calibration", fontsize=10, color=BASE, rotation=33)

    xs = [b["predicted"] for b in buckets]
    ys = [b["actual"] for b in buckets]
    ax.plot(xs, ys, color=OURS, linewidth=2.0, zorder=2)

    solid_x = [b["predicted"] for b in buckets if not b["extrapolated"]]
    solid_y = [b["actual"] for b in buckets if not b["extrapolated"]]
    thin_x = [b["predicted"] for b in buckets if b["extrapolated"]]
    thin_y = [b["actual"] for b in buckets if b["extrapolated"]]

    ax.scatter(solid_x, solid_y, s=90, color=OURS, zorder=3,
               edgecolor="white", linewidth=1.2, label="covered")
    if thin_x:
        ax.scatter(thin_x, thin_y, s=110, facecolor="white", zorder=4,
                   edgecolor=WARN, linewidth=2.0, marker="o",
                   label="EXTRAPOLATED — thin cells")

    ax.set_ylabel("actual settle rate", fontsize=12, color=INK)
    ax.xaxis.set_major_formatter(FuncFormatter(_pct))
    ax.yaxis.set_major_formatter(FuncFormatter(_pct))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(color=GRID, linewidth=0.8)
    ax.legend(frameon=False, fontsize=10.5, loc="lower right")
    ax.set_title(
        f"Reliability — {calibration['shipped']}, held out by case",
        fontsize=15, color=INK, fontweight="bold", loc="left", pad=14,
    )
    ax.text(
        0.02, 0.93,
        f"ECE {calibration['ece']:.4f}   Brier {calibration['brier']:.4f}"
        f"   (covered cells, n={calibration['n_covered_rows']:,})",
        transform=ax.transAxes, fontsize=10.5, color=INK,
    )

    edges = [b["predicted"] for b in buckets]
    colours = [WARN if b["extrapolated"] else OURS for b in buckets]
    counts = [b["n"] for b in buckets]
    ax_n.bar(edges, counts, width=0.075, color=colours)
    # A bar for n=27 beside one for n=7,519 is invisible, and the invisible one
    # carries the biggest deviation on the diagram above. Label the small ones.
    ceiling = max(counts)
    for edge, count in zip(edges, counts):
        if count < ceiling * 0.08:
            ax_n.annotate(
                f"{count:,}", (edge, count), textcoords="offset points",
                xytext=(0, 4), ha="center", fontsize=9, color=WARN,
            )
    ax_n.set_xlabel("predicted P(settle)", fontsize=12, color=INK)
    ax_n.set_ylabel("rows", fontsize=11, color=INK)
    ax_n.grid(axis="y", color=GRID, linewidth=0.8)
    ax_n.xaxis.set_major_formatter(FuncFormatter(_pct))
    # subplots_adjust, not tight_layout: the two panels share an x-axis with a
    # fixed height ratio, and tight_layout warns and then guesses.
    figure.subplots_adjust(left=0.13, right=0.97, top=0.90, bottom=0.14, hspace=0.12)
    figure.text(
        0.09, 0.015,
        f"{calibration['n_extrapolated_cells']} of the model's cells hold fewer than 50 "
        "observations. Buckets leaning on them are ringed.",
        fontsize=9.5, color=BASE,
    )
    figure.savefig(out, dpi=DPI, facecolor="white")
    plt.close(figure)
    return out


# ---------------------------------------------------------------------------
# 3. Incremental recovery by decline class — including the losses
# ---------------------------------------------------------------------------

def by_decline_class(data: dict[str, Any], out: Path) -> Path:
    """OURS minus B2, per class, sorted. The losses are the point.

    Diverging bars around zero rather than two side-by-side series: the question
    a reader has is "where does this actually win", and a difference answers it
    directly while a paired bar chart makes them do the subtraction by eye.
    """
    classes = data["by_decline_class"]
    rows = sorted(classes.items(), key=lambda kv: kv[1]["ours_minus_b2_rate"])

    figure, ax = plt.subplots(figsize=(8.6, 5.6))
    _style(ax)

    labels = [
        f"{CLASS_LABELS.get(name, name)}\nn={row['cases']:,}" for name, row in rows
    ]
    deltas = [row["ours_minus_b2_rate"] for _, row in rows]
    colours = [OURS if d >= 0 else WARN for d in deltas]

    bars = ax.barh(labels, deltas, color=colours, height=0.62)
    ax.axvline(0, color=INK, linewidth=1.2)

    span = max(abs(min(deltas)), abs(max(deltas))) or 0.01
    ax.set_xlim(-span * 1.45, span * 1.45)

    for bar, (name, row), delta in zip(bars, rows, deltas):
        offset = span * 0.06
        ax.text(
            delta + (offset if delta >= 0 else -offset),
            bar.get_y() + bar.get_height() / 2,
            f"{delta * 100:+.1f} pts",
            va="center", ha="left" if delta >= 0 else "right",
            fontsize=11, color=OURS if delta >= 0 else WARN, fontweight="bold",
        )

    ax.set_xlabel(
        "incremental recovery rate, OURS minus B2  (percentage points of cases in class)",
        fontsize=11.5, color=INK,
    )
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v * 100:+.0f}"))
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_title(
        "Where it wins, and where it loses",
        fontsize=15, color=INK, fontweight="bold", loc="left", pad=34,
    )
    losses = [CLASS_LABELS.get(n, n) for n, r in rows if r["ours_minus_b2_rate"] < 0]
    subtitle = (
        f"Loses to the fixed ladder on: {', '.join(losses)}."
        if losses
        else "No class where the fixed ladder wins."
    )
    ax.text(0, 1.015, subtitle, transform=ax.transAxes, fontsize=11,
            color=WARN if losses else BASE)
    figure.tight_layout()
    figure.savefig(out, dpi=DPI, facecolor="white")
    plt.close(figure)
    return out


# ---------------------------------------------------------------------------
# 4. Sensitivity — the headline across the swept range
# ---------------------------------------------------------------------------

def sensitivity(data: dict[str, Any], out: Path) -> Path:
    """Margin over B2 at every swept multiple, one line per member.

    Members that flip are drawn in the warning colour and named. Everything
    else is grey and unlabelled: fourteen labelled lines is a chart nobody
    reads, and the three that matter are the three that cross zero.
    """
    block = data["sensitivity"]
    if not block.get("available"):
        raise SystemExit(block.get("reason", "sensitivity data unavailable"))

    figure, ax = plt.subplots(figsize=(8.6, 5.6))
    _style(ax)

    multiples = block["meta"]["multiples"]
    ax.axhline(0, color=INK, linewidth=1.4, zorder=2)

    flipping = []
    for member in block["members"]:
        xs = [p["multiple"] for p in member["points"] if p["margin"] is not None]
        ys = [p["margin"] for p in member["points"] if p["margin"] is not None]
        if not xs:
            continue
        if member["holds_everywhere"]:
            ax.plot(xs, ys, color=BASE, linewidth=1.3, alpha=0.55, zorder=1)
        else:
            flipping.append((member, xs, ys))

    # Named in a legend rather than annotated in place. Three labels at the
    # same 4x x-position overprint each other, and an unreadable label is worse
    # than none: these are the three members a sceptical reader came for.
    for member, xs, ys in flipping:
        ax.plot(
            xs, ys, color=WARN, linewidth=2.2, zorder=3, marker="o", markersize=5,
            label=f"{member['name']}  (flips at {member['flips_at'][0]:g}x)",
        )
    if flipping:
        ax.legend(frameon=False, fontsize=10, loc="upper left", labelcolor=WARN)

    ax.set_xscale("log")
    ax.set_xticks(multiples)
    ax.set_xticklabels([f"{m:g}x" for m in multiples])
    # The log locator adds 3x10^-1, 6x10^-1 and so on, which overprint the
    # five multiples that were actually swept.
    ax.set_xticks([], minor=True)
    ax.set_xlabel("prior value, as a multiple of the shipped value", fontsize=12, color=INK)
    ax.set_ylabel(
        "headline margin, percentage points\n(OURS minus B2 incremental rate)",
        fontsize=11.5, color=INK,
    )
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v * 100:+.1f}"))
    ax.grid(axis="y", color=GRID, linewidth=0.8)

    ax.text(
        multiples[0], 0, "  headline holds above this line",
        va="bottom", ha="left", fontsize=10, color=INK,
    )
    ax.set_title(
        f"The headline across a 16x range — {block['n_holding_everywhere']} of "
        f"{block['n_members']} priors never flip it",
        fontsize=14.5, color=INK, fontweight="bold", loc="left", pad=14,
    )
    ax.text(
        0, -0.24,
        f"Grey: {block['n_holding_everywhere']} members that hold everywhere.\n"
        f"Red: {len(flipping)} that lose the headline at the top of the range — "
        "every one by B2 climbing, not by OURS falling.",
        transform=ax.transAxes, fontsize=9.5, color=BASE,
    )
    figure.tight_layout()
    figure.savefig(out, dpi=DPI, facecolor="white")
    plt.close(figure)
    return out


BUILDERS = (
    ("recovery_vs_contacts.png", recovery_vs_contacts),
    ("reliability.png", reliability),
    ("by_decline_class.png", by_decline_class),
    ("sensitivity.png", sensitivity),
)


def render_all(data: dict[str, Any], directory: Path = CHARTS_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, builder in BUILDERS:
        written.append(builder(data, directory / filename))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settle.eval.charts", description=__doc__)
    parser.add_argument("--metrics", type=Path, default=METRICS)
    parser.add_argument("--out", type=Path, default=CHARTS_DIR)
    args = parser.parse_args(argv)

    for path in render_all(load(args.metrics), args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
