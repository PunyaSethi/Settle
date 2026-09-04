"""CP14 — the three-screen viewer. SPEC §3, §16, §19.

VIW-4 is the one that could have been decoration. "Opens from file:// with no
server" is easy to assert about and hard to assert *of*, and a test that only
checked for the absence of a `<script src>` would pass on a page that threw on
line one.

So it executes. There is no jsdom here and adding one would mean an npm
dependency in a project whose viewer constraint is "no build step", so the test
carries a small DOM shim — enough of `document` for the page's render path — and
runs the real script under Node against the real committed data. What it asserts
is what came out the other side: the headline table, the arm names, the case
picker, the alternatives.

VIW-2 is the discipline that makes the rest safe. The viewer renders and never
computes, so every number on screen is one Python put there. A page that did its
own arithmetic would be a second implementation of the metrics, and the two would
disagree the first time either changed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VIEWER = REPO_ROOT / "viewer" / "index.html"
DATA = REPO_ROOT / "out" / "viewer_data.json"

NODE = shutil.which("node")

needs_data = pytest.mark.skipif(
    not DATA.exists(), reason="out/viewer_data.json not generated — run settle.eval.report"
)
needs_node = pytest.mark.skipif(NODE is None, reason="node not available")


def load_data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def page() -> str:
    return VIEWER.read_text(encoding="utf-8")


def embedded_data() -> dict:
    html = page()
    start = html.index('<script id="viewer-data" type="application/json">')
    start = html.index(">", start) + 1
    end = html.index("</script><!-- /viewer-data -->")
    return json.loads(html[start:end])


# --------------------------------------------------------------------------
# The DOM shim. Enough of a browser for the page's render path, and no more.
# --------------------------------------------------------------------------

DOM_SHIM = r"""
class Node {
  constructor(tag) {
    this.tagName = (tag || "").toUpperCase();
    this.children = []; this.attrs = {}; this._text = "";
    this.className = ""; this.hidden = false; this.dataset = {};
    this.style = {};
  }
  get classList() {
    const self = this;
    return { add(c) { self.className += " " + c; },
             remove(c) { self.className = self.className.replace(c, ""); } };
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = String(v);
  }
  getAttribute(k) { return this.attrs[k]; }
  set textContent(v) { this._text = String(v); this.children = []; }
  // Element children are joined with a newline, where a browser would
  // concatenate. Deliberate: two adjacent table cells holding 42 and 186 would
  // otherwise read as the token 42186, and the extraction test would report a
  // number the page never rendered. Text nodes still concatenate.
  get textContent() {
    const parts = this.children.map(c =>
      c.textContent === undefined ? String(c) : c.textContent);
    const joined = this.children.some(c => c && c.nodeType === 1)
      ? parts.join("\n") : parts.join("");
    return this._text + (this._text && joined ? "\n" : "") + joined;
  }
  set innerHTML(v) { this._text = String(v).replace(/<[^>]*>/g, ""); }
  get innerHTML() { return this._text; }
  append(...kids) { for (const k of kids) this.children.push(k); }
  appendChild(k) { this.children.push(k); return k; }
  replaceChildren(...kids) { this.children = []; this._text = ""; this.append(...kids); }
  addEventListener() {}
  scrollIntoView() {}
  get nodeType() { return 1; }
  _walk(out) {
    out.push(this);
    for (const k of this.children) if (k && k._walk) k._walk(out);
    return out;
  }
  querySelectorAll(sel) {
    const all = this._walk([]);
    const m = sel.match(/^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
    if (!m) return [];
    const [, tag, attr, val] = m;
    return all.filter(n =>
      (!tag || n.tagName === tag.toUpperCase()) &&
      (!attr || (n.attrs[attr] !== undefined && (val === undefined || n.attrs[attr] === val))));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest() { return null; }
}

const byId = {};
for (const id of ["batch", "trace", "voice", "provenance", "viewer-data"]) {
  byId[id] = new Node("div"); byId[id].id = id;
}
byId["viewer-data"].textContent = __DATA__;

globalThis.document = {
  createElement: tag => new Node(tag),
  createTextNode: t => ({ textContent: String(t), nodeType: 3 }),
  getElementById: id => byId[id] || null,
  querySelector: sel => sel === "nav" ? new Node("nav") : null,
  querySelectorAll: () => [],
};
globalThis.location = { protocol: "file:" };
globalThis.fetch = () => { throw new Error("file:// must not fetch"); };
globalThis.FormData = class {};
globalThis.__result = byId;
"""


def run_page_in_node(tmp_path: Path) -> dict:
    """Execute the page's script against the shim and return the rendered text."""
    html = page()
    script = html[html.rindex("<script>\n") + len("<script>\n"): html.rindex("</script>")]
    data = json.dumps(json.dumps(embedded_data()))  # a JS string literal

    harness = tmp_path / "run.mjs"
    harness.write_text(
        DOM_SHIM.replace("__DATA__", data)
        + "\n"
        + script.replace('"use strict";', "")
        + """
await new Promise(r => setTimeout(r, 0));
console.log(JSON.stringify({
  batch: globalThis.__result.batch.textContent,
  trace: globalThis.__result.trace.textContent,
  voice: globalThis.__result.voice.textContent,
  provenance: globalThis.__result.provenance.textContent,
}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(harness)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (
        f"the page threw when executed with no server:\n{result.stderr[-3000:]}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# VIW-1 — deterministic regeneration
# --------------------------------------------------------------------------

@needs_data
def test_VIW_1_viewer_data_is_deterministic_and_matches_the_embedded_copy() -> None:
    """The page's embedded copy and the committed JSON are the same run.

    They are written in the same breath by `report.py`, so a mismatch means one
    was regenerated and the other was not — and the page a judge opens would
    then show different numbers from the file the tests check.
    """
    on_disk = load_data()
    in_page = embedded_data()

    # `generated_at` is a timestamp and is the one field allowed to differ if
    # only one was rewritten; everything a reader sees must match.
    for key in ("batch", "traces", "demo_cases", "filters"):
        assert on_disk[key] == in_page[key], f"{key} differs between page and file"
    assert on_disk["meta"]["cases"] == in_page["meta"]["cases"]
    assert on_disk["meta"]["seed"] == in_page["meta"]["seed"]


@needs_data
def test_VIW_1_the_data_is_built_from_the_committed_artefacts() -> None:
    """Every headline number in the viewer is the one in out/metrics.json.

    Two files, one source. If the viewer drifted from the metrics artefact the
    README is checked against, the repo would be making two different claims.
    """
    metrics_path = REPO_ROOT / "out" / "metrics.json"
    if not metrics_path.exists():
        pytest.skip("out/metrics.json not generated")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    viewer = load_data()

    assert viewer["meta"]["cases"] == metrics["meta"]["cases"]
    assert viewer["meta"]["seed"] == metrics["meta"]["seed"]
    for arm, row in metrics["arms"].items():
        shown = viewer["batch"]["arms"][arm]
        assert shown["incremental_rate"] == row["incremental_rate"], arm
        assert shown["contacts"] == row["contacts"], arm
        assert shown["compliance_violations"] == row["compliance_violations"], arm


# --------------------------------------------------------------------------
# VIW-2 — the page renders, it does not compute
# --------------------------------------------------------------------------

# Arithmetic on a value. `i + 1` in a loop is not what this is about; a rate
# being derived in the page is. Matching operators against identifiers that
# look like data is the closest a regex gets, and the list of what it may not
# touch is explicit rather than clever.
FORBIDDEN_JS = (
    re.compile(r"\*\s*100"),
    re.compile(r"/\s*100\b"),
    re.compile(r"toFixed\s*\("),
    re.compile(r"toLocaleString\s*\("),
    re.compile(r"Math\.(round|floor|ceil|abs)\s*\("),
    re.compile(r"\breduce\s*\("),
    re.compile(r"parseFloat\s*\("),
)


def test_VIW_2_the_viewer_does_no_arithmetic() -> None:
    """JS renders; Python computes.

    The same span-locate / code-evaluate split the text reader uses, applied to
    the UI. A number computed here would be a second implementation of a metric
    that already exists in `settle/eval/report.py`.
    """
    script = page()
    offenders = []
    for pattern in FORBIDDEN_JS:
        for match in pattern.finditer(script):
            line = script[: match.start()].count("\n") + 1
            snippet = script.splitlines()[line - 1].strip()
            if snippet.startswith(("*", "//", "<!--")):
                continue
            offenders.append(f"line {line}: {snippet}")
    assert not offenders, (
        "the viewer is computing rather than rendering:\n" + "\n".join(offenders)
    )


@needs_data
def test_VIW_2_every_rendered_number_exists_in_the_data() -> None:
    """Numbers the page shows are strings the data already carried.

    Executed rather than inspected: the rendered text is scanned for numeric
    tokens and each one must appear somewhere in the JSON. A page that derived
    a percentage would produce a token the data does not contain.
    """
    if NODE is None:
        pytest.skip("node not available")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        rendered = run_page_in_node(Path(tmp))

    haystack = json.dumps(load_data())
    number = re.compile(r"\d[\d,]*\.?\d*")
    unbacked = []
    for screen in ("batch", "trace"):
        for token in number.findall(rendered[screen]):
            if token in haystack or token.replace(",", "") in haystack:
                continue
            unbacked.append(f"{screen}: {token}")
    assert not unbacked, (
        "the viewer rendered numbers that are not in viewer_data.json: "
        f"{sorted(set(unbacked))[:20]}"
    )


# --------------------------------------------------------------------------
# VIW-3 — every alternative, rejected ones included
# --------------------------------------------------------------------------

@needs_data
def test_VIW_3_every_alternative_carries_legal_and_block_gate() -> None:
    """SPEC §5.4's `Alternative` is the point of the case trace.

    Recording why a rejected option was rejected — and whether it lost on
    economics or was stopped by a gate — is the difference between a decision
    log and a number dump.
    """
    data = load_data()
    with_decisions = [t for t in data["traces"] if t["decisions"]]
    assert with_decisions, "no trace carries a decision, so there is nothing to show"

    blocked_seen = 0
    for trace in with_decisions:
        for decision in trace["decisions"]:
            assert decision["alternatives"], f"{trace['case_id']} decision with no options"
            # Every option the policy priced is present, not just the winner.
            assert len(decision["alternatives"]) == decision["n_alternatives"]
            for alt in decision["alternatives"]:
                assert "legal" in alt and isinstance(alt["legal"], bool)
                assert "block_gate" in alt
                assert alt["p_settle_pct"].endswith("%")
                if not alt["legal"]:
                    assert alt["block_gate"], (
                        f"{trace['case_id']}: an option is illegal with no gate named"
                    )
                    blocked_seen += 1
                else:
                    assert alt["block_gate"] is None
            assert sum(1 for a in decision["alternatives"] if a["chosen"]) <= 1

    assert blocked_seen, (
        "no rejected-by-gate alternative anywhere in the traces — the screen's "
        "most important row never appears"
    )


@needs_data
def test_VIW_3_the_rendered_trace_shows_a_blocked_option_with_its_gate() -> None:
    """Present in the data is not the same as on the screen."""
    if NODE is None:
        pytest.skip("node not available")
    import tempfile

    data = load_data()
    blocked = next(
        (
            (t, d, a)
            for t in data["traces"] for d in t["decisions"] for a in d["alternatives"]
            if not a["legal"]
        ),
        None,
    )
    assert blocked, "no blocked alternative to render"

    with tempfile.TemporaryDirectory() as tmp:
        rendered = run_page_in_node(Path(tmp))

    trace_text = rendered["trace"]
    assert "blocked" in trace_text.lower()
    assert "Blocked by" in trace_text, "the gate column is missing from the trace"
    assert "P(settle)" in trace_text and "EV (₹)" in trace_text


# --------------------------------------------------------------------------
# VIW-4 — file:// with no server
# --------------------------------------------------------------------------

def test_VIW_4_the_page_is_self_contained() -> None:
    """No CDN, no npm, no build step, no external anything.

    The constraint is not aesthetic. A page that reached for a CDN would stop
    working the moment a judge opened it on a train, and this one has to open
    from a cloned repo with nothing running.
    """
    html = page()
    assert "<script src=" not in html, "an external script would break file://"
    assert not re.search(r'(?:href|src)\s*=\s*"https?://', html), "external asset"
    for banned in ("cdn.", "unpkg", "jsdelivr", "googleapis", "react", "import "):
        assert banned not in html.lower().replace("important", ""), banned
    # Assets it does reference are relative, so they resolve from the file tree.
    for src in re.findall(r'src:\s*"([^"]+)"', html):
        assert not src.startswith(("http", "/")), src


@needs_node
@needs_data
def test_VIW_4_screens_1_and_2_render_from_file_protocol_with_no_server() -> None:
    """Execute the page with `location.protocol = "file:"` and `fetch` fatal.

    The shim throws on any fetch, so a page that depended on one to render
    screens 1 or 2 fails here rather than passing a structural check and
    breaking in front of a judge.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        rendered = run_page_in_node(Path(tmp))

    data = load_data()

    batch = rendered["batch"]
    assert "Headline metrics" in batch
    assert "Incremental recovery rate" in batch
    for arm in ("OURS", "B2 fixed ladder", "B3 max pressure", "B0 do nothing"):
        assert arm in batch, f"{arm} missing from the headline table"
    assert "OBSERVE" in batch, "B3 is not marked OBSERVE"
    assert data["batch"]["arms"]["OURS"]["incremental_rate_pct"] in batch
    assert "by decline class" in batch.lower()
    assert "Blind set" in batch, "the blind-set comparison is missing"
    for chart in data["batch"]["charts"]:
        assert chart["title"] in batch

    trace = rendered["trace"]
    assert "Case trace" in trace
    assert "What the agent could see" in trace
    assert "Diagnosis" in trace
    assert "Reconciliation" in trace
    assert data["demo_cases"][0]["case_id"] in trace

    # Screen 3 says what it needs rather than failing silently.
    assert "needs the API" in rendered["voice"]
    assert data["meta"]["seed"] is not None
    assert "cases, seed" in rendered["provenance"]


# --------------------------------------------------------------------------
# VIW-5 — the three demo cases
# --------------------------------------------------------------------------

@needs_data
def test_VIW_5_three_demo_cases_each_show_something_worth_showing() -> None:
    """Pre-selected by id so the demo does not depend on hunting.

    One restraint decision, one gate block, one silent failure — the three
    things this project claims. Each is checked for the property it was picked
    for, because a demo case that no longer demonstrates anything is worse than
    no shortcut at all.
    """
    data = load_data()
    demos = data["demo_cases"]
    assert len(demos) == 3, f"expected three demo cases, found {len(demos)}"

    index = {(t["case_id"], t["arm"]): t for t in data["traces"]}
    traces = []
    for demo in demos:
        key = (demo["case_id"], demo["arm"])
        assert key in index, f"demo case {key} is not in the traces"
        assert demo["why"], "a demo case with no stated reason is a bookmark"
        traces.append(index[key])

    restraint, blocked, failure = traces

    # One restraint decision: options priced and declined on their own numbers.
    assert any(
        d["reason_code"] in {"DO_NOTHING_DOMINATES", "S7_ECONOMIC_STOP"}
        and d["n_alternatives"] >= 4
        for d in restraint["decisions"]
    ), "the restraint case shows no priced-and-declined decision"

    # One gate block, with the gate named.
    gates = [
        a["block_gate"]
        for d in blocked["decisions"] for a in d["alternatives"] if not a["legal"]
    ]
    assert gates, "the gate-block case has no blocked alternative"
    assert all(gates), "a blocked alternative does not name its gate"

    # One silent failure, with its class.
    assert failure["reconciliation"]["silent_failures"], (
        "the silent-failure case shows no silent failure"
    )
    assert all(
        cls.startswith("SF-") for cls in failure["reconciliation"]["silent_failures"]
    )


@needs_data
def test_VIW_5_the_picker_filters_have_something_behind_them() -> None:
    """A filter that matches nothing is a dead control on a demo screen."""
    data = load_data()
    traces = data["traces"]

    for arm in data["filters"]["arms"]:
        assert any(t["arm"] == arm for t in traces), arm
    for cls in data["filters"]["decline_classes"]:
        assert any(t["diagnosis"]["decline_class"] == cls for t in traces), cls

    assert any(t["reconciliation"]["silent_failures"] for t in traces), (
        "the silent-failure filter matches nothing"
    )
    assert any(not t["reconciliation"]["silent_failures"] for t in traces)
    assert any(t["counts"]["blocked_alternatives"] > 0 for t in traces), (
        "the gate-block filter matches nothing"
    )
    assert len(data["filters"]["arms"]) >= 2, "the arm filter has one option"


# --------------------------------------------------------------------------
# VIW-6 — the charts resolve under BOTH origins
# --------------------------------------------------------------------------

def chart_srcs() -> list[str]:
    """Every image the page actually asks for, read out of the page itself.

    Parsed rather than listed, so a chart added to the viewer without a route
    fails here instead of appearing broken to whoever opens it next.
    """
    html = page()
    prefixes = re.findall(r'src:\s*"([^"]*out/charts/)"', html)
    assert prefixes, "the page no longer builds a chart src the way this test reads"
    data = load_data() if DATA.exists() else {"batch": {"charts": []}}
    return [prefix + chart["file"] for prefix in prefixes
            for chart in data["batch"]["charts"]]


@needs_data
def test_VIW_6_every_chart_resolves_from_the_filesystem() -> None:
    """file:// — the mode a judge with a cloned repo and nothing running is in.

    `../out/charts/x.png` is resolved against `viewer/index.html`, which is what
    a browser does with a relative src on a file: origin.
    """
    for src in chart_srcs():
        resolved = (VIEWER.parent / src).resolve()
        assert resolved.is_file(), f"{src} does not resolve from viewer/: {resolved}"
        assert resolved.stat().st_size > 10_000, f"{src} is empty"
        # It has to be inside the repo, not somewhere a relative path escaped to.
        assert REPO_ROOT in resolved.parents


@needs_data
def test_VIW_6_every_chart_resolves_when_served() -> None:
    """http:// — the origin screen 3 needs, and the one that was broken.

    The page asks for `../out/charts/x.png` from `/`, which a browser resolves
    to `/out/charts/x.png`. That had no route until CP16.1, so a judge who
    started the server to try the voice lab saw a page with no charts on it.
    """
    from fastapi.testclient import TestClient

    from settle.api.app import app

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        for src in chart_srcs():
            # What the browser would request: the relative src resolved against
            # the page's URL, which is the site root.
            url = "/" + src.removeprefix("../")
            response = client.get(url)
            assert response.status_code == 200, f"{url} -> {response.status_code}"
            assert response.headers["content-type"] == "image/png", url
            assert response.content[:8] == b"\x89PNG\r\n\x1a\n", f"{url} is not a PNG"
            assert len(response.content) > 10_000, f"{url} is empty"


@needs_data
def test_VIW_6_the_two_origins_serve_the_same_bytes() -> None:
    """A chart that differed between origins would be worse than one missing."""
    from fastapi.testclient import TestClient

    from settle.api.app import app

    with TestClient(app) as client:
        for src in chart_srcs():
            on_disk = (VIEWER.parent / src).resolve().read_bytes()
            served = client.get("/" + src.removeprefix("../")).content
            assert on_disk == served, f"{src} differs between file:// and http://"


def test_VIW_6_the_mount_does_not_widen_the_api_surface() -> None:
    """SPEC §16 fixes the route table at exactly three.

    `StaticFiles` is a sub-application rather than a route, so it does not enter
    the OpenAPI schema — which is why the charts could be served without the
    fourth route CP14 rejected for exactly this reason.
    """
    from settle.api.app import app

    declared = {
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert declared == {
        ("/webhooks/razorpay", "POST"),
        ("/voice/extract", "POST"),
        ("/", "GET"),
    }, "the chart mount widened the API surface"


def test_VIW_6_the_mount_serves_only_the_charts() -> None:
    """Read-only, and scoped to one directory of committed images.

    A viewer convenience is not a reason to hand out arbitrary file reads, so
    the mount is checked for what it refuses as well as what it serves.
    """
    from fastapi.testclient import TestClient

    from settle.api.app import app

    with TestClient(app) as client:
        for escape in ("/out/charts/../metrics.json", "/out/charts/../../SPEC.md",
                       "/out/metrics.json", "/out/viewer_data.json"):
            assert client.get(escape).status_code == 404, f"{escape} was served"
        assert client.post("/out/charts/reliability.png").status_code in {404, 405}
