"""CP4 — the audit ledger and the executor boundary. SPEC §5.6, INV-4, INV-5, INV-6.

LED-4 is the one that matters. Write-ahead is not a performance detail: an entry
written after the dispatch is an entry that does not exist when the process dies
mid-dispatch, and the next run contacts the customer again. That is SF-3
harassment caused by the audit system meant to prevent it.
"""

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from settle.audit.chain import (
    GENESIS_HASH,
    ChainBreak,
    Ledger,
    entry_hash,
    read_entries,
    verify_entries,
    verify_file,
)
from settle.audit.verify import main as verify_main
from settle.schema.enums import Actor, LedgerKind

REPO_ROOT = Path(__file__).resolve().parent.parent
AT = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)


def write_entries(path: Path, count: int, arm: str = "B0") -> Ledger:
    ledger = Ledger(path)
    for index in range(count):
        ledger.append(
            case_id=f"case_{index % 7:06d}",
            at=AT + timedelta(hours=index),
            kind=LedgerKind.GATE_CHECK,
            actor=Actor.POLICY,
            payload={"index": index, "note": "pandrah tareekh — पंद्रह"},
            reason_code=f"R{index % 5}",
            arm=arm,
        )
    ledger.close()
    return ledger


# --------------------------------------------------------------------------
# LED-1
# --------------------------------------------------------------------------

def test_LED_1_the_chain_verifies_over_a_thousand_entries(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_entries(path, 1000)
    assert verify_file(path) == 1000

    entries = read_entries(path)
    assert entries[0].prev_hash == GENESIS_HASH
    assert [e.seq for e in entries] == list(range(1000))
    for earlier, later in zip(entries, entries[1:]):
        assert later.prev_hash == earlier.hash


def test_LED_1_an_empty_ledger_verifies(tmp_path):
    path = tmp_path / "empty.jsonl"
    Ledger(path).close()
    assert verify_file(path) == 0


# --------------------------------------------------------------------------
# LED-2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tampered_seq", [0, 17, 99])
def test_LED_2_a_tampered_entry_is_detected_at_the_right_seq(tmp_path, tampered_seq):
    path = tmp_path / "audit.jsonl"
    write_entries(path, 100)

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[tampered_seq])
    record["reason_code"] = "TAMPERED"
    lines[tampered_seq] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBreak) as broken:
        verify_file(path)
    assert broken.value.seq == tampered_seq
    assert "does not match its hash" in broken.value.reason


def test_LED_2_a_deleted_entry_is_detected(tmp_path):
    """Removing a link breaks the chain rather than shortening it quietly."""
    path = tmp_path / "audit.jsonl"
    write_entries(path, 50)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[20]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBreak) as broken:
        verify_file(path)
    assert broken.value.seq == 21


def test_LED_2_the_cli_exits_non_zero_and_names_the_seq(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    write_entries(path, 30)
    assert verify_main([str(path)]) == 0

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[11])
    record["payload"] = {"index": 999}
    lines[11] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert verify_main([str(path)]) == 1
    assert "seq 11" in capsys.readouterr().err


# --------------------------------------------------------------------------
# LED-3
# --------------------------------------------------------------------------

def test_LED_3_append_is_the_only_mutation_path():
    """Structural, not conventional. A ledger with an UPDATE is not a ledger."""
    public = {name for name in dir(Ledger) if not name.startswith("_")}
    assert "append" in public
    for forbidden in ("update", "delete", "remove", "truncate", "rewrite", "insert", "pop"):
        assert forbidden not in public, f"Ledger exposes {forbidden}"


def test_LED_3_the_file_handle_is_not_reachable(tmp_path):
    """`__slots__` and no accessor: nothing can seek backwards and overwrite."""
    assert "_handle" in Ledger.__slots__
    assert not hasattr(Ledger, "handle")
    instance = Ledger(tmp_path / "a.jsonl")
    try:
        # No instance __dict__, so no attribute can be bolted on at runtime.
        assert not hasattr(instance, "__dict__")
        with pytest.raises(AttributeError):
            instance.update = lambda: None
    finally:
        instance.close()


def test_LED_3_seq_only_moves_forward(tmp_path):
    ledger = Ledger(tmp_path / "a.jsonl")
    seen = []
    for index in range(20):
        seen.append(
            ledger.append(
                case_id="c", at=AT, kind=LedgerKind.EVENT, actor=Actor.SYSTEM,
                payload={"i": index}, reason_code="R", arm="B0",
            ).seq
        )
    ledger.close()
    assert seen == sorted(seen) == list(range(20))


# --------------------------------------------------------------------------
# LED-4 — write-ahead
# --------------------------------------------------------------------------

def test_LED_4_a_dispatch_that_raises_still_leaves_its_audit_entry(tmp_path):
    """INV-5. The record of intent must survive the dispatch failing."""
    path = tmp_path / "audit.jsonl"
    ledger = Ledger(path)

    ledger.append(
        case_id="case_000001", at=AT, kind=LedgerKind.DISPATCH, actor=Actor.SYSTEM,
        payload={"idempotency_key": "abc123"}, reason_code="DISPATCH_INTENT", arm="B0",
    )
    with pytest.raises(RuntimeError):
        raise RuntimeError("gateway exploded mid-dispatch")

    # Read from a separate handle: the entry is on disk, not in a buffer.
    entries = read_entries(path)
    assert len(entries) == 1
    assert entries[0].kind is LedgerKind.DISPATCH
    assert entries[0].payload["idempotency_key"] == "abc123"
    verify_entries(entries)


def test_LED_4_entries_are_flushed_rather_than_buffered(tmp_path):
    """An entry sitting in a userspace buffer when the process dies was never
    written. Every append is visible to a second reader immediately."""
    path = tmp_path / "audit.jsonl"
    ledger = Ledger(path)
    for index in range(5):
        ledger.append(
            case_id="c", at=AT, kind=LedgerKind.EVENT, actor=Actor.SYSTEM,
            payload={"i": index}, reason_code="R", arm="B0",
        )
        assert len(read_entries(path)) == index + 1
    ledger.close()


# --------------------------------------------------------------------------
# LED-5
# --------------------------------------------------------------------------

def test_LED_5_the_chain_is_byte_identical_across_two_processes(tmp_path):
    """canonical_json is what makes this true: sorted keys, ASCII escapes, and
    datetimes normalised to UTC, so no hash depends on the process that built it."""
    script = (
        "import sys, json;"
        "from datetime import datetime, timedelta, timezone;"
        "from settle.audit.chain import Ledger;"
        "from settle.schema.enums import Actor, LedgerKind;"
        "AT = datetime(2026,1,1,3,0,tzinfo=timezone.utc);"
        "led = Ledger(sys.argv[1]);"
        "[led.append(case_id='c%d' % (i%7), at=AT+timedelta(hours=i),"
        " kind=LedgerKind.GATE_CHECK, actor=Actor.POLICY,"
        " payload={'i': i, 'note': 'पंद्रह'}, reason_code='R%d' % (i%5), arm='B0')"
        " for i in range(200)];"
        "led.close();"
        "print(open(sys.argv[1]).read(), end='')"
    )
    outputs = []
    for hash_seed in ("0", "1", "random"):
        target = tmp_path / f"ledger_{hash_seed}.jsonl"
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script, str(target)],
                cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
            ).stdout
        )
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0].count("\n") == 200


def test_LED_5_the_hash_is_exactly_what_spec_5_6_says(tmp_path):
    ledger = Ledger(tmp_path / "a.jsonl")
    entry = ledger.append(
        case_id="c", at=AT, kind=LedgerKind.EVENT, actor=Actor.SYSTEM,
        payload={"b": 2, "a": 1}, reason_code="R", arm="B0",
    )
    ledger.close()
    assert entry.prev_hash == GENESIS_HASH
    assert entry.hash == entry_hash(GENESIS_HASH, entry)
    assert len(entry.hash) == 64


# --------------------------------------------------------------------------
# EXE-1 / EXE-2 — the executor boundary
# --------------------------------------------------------------------------

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(package_parts[: len(package_parts) - (node.level - 1)]) if node.level else []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


# The executor is the only module that *acts* on the world. `settle/recon/`
# reads one pure function from it — `natural_recovery_at`, a self-cure that
# happened whatever any arm did — which is a different thing from dispatching,
# and the auditor cannot report B0's recovery without it.
WORLD_READERS = {
    # Dispatches. This is the boundary.
    "settle/execute/executor.py",
    # Reads one pure function — `natural_recovery_at`, a self-cure that happened
    # whatever any arm did. Different from dispatching, and the auditor cannot
    # report B0's recovery without it.
    "settle/recon/reconcile.py",
    # F6. Rebinds `world.ACTION_LIFT` after patching PARAMS. `ACTION_LIFT` is
    # built from PARAMS once at import, so a sweep that patched PARAMS and left
    # it alone would report that `action_lift.*` — a REQUIRED sweep member,
    # upstream of every rupee in §14.4 — moves nothing. A flat sweep row is
    # indistinguishable from a robust result, which makes that the most
    # dangerous wrong answer the sweep can give. It reads the world's constants
    # and never dispatches. Reaching the module through `sys.modules` would have
    # passed this test by evading it, and an unstated exception is how an
    # invariant dies (SPEC §7 says the same of INV-8's).
    "settle/eval/sensitivity.py",
}


def test_EXE_1_the_executor_is_the_only_module_that_acts_on_the_world():
    """Everything upstream is a pure function of its arguments. Keeping the
    dispatch boundary in one module is what makes the rest replayable."""
    offenders = {}
    for module_path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        relative = str(module_path.relative_to(REPO_ROOT))
        if relative in WORLD_READERS:
            continue
        if any(name.startswith("settle.sim.world") for name in _imports(module_path)):
            offenders[relative] = "imports settle.sim.world"
    assert not offenders, offenders

    assert any(
        name.startswith("settle.sim.world")
        for name in _imports(REPO_ROOT / "settle" / "execute" / "executor.py")
    ), "the executor should be the module that does touch the world"

    recon_source = (REPO_ROOT / "settle" / "recon" / "reconcile.py").read_text(encoding="utf-8")
    assert "natural_recovery_at" in recon_source
    assert "attempt(" not in recon_source, "reconciliation must not run actions"


def test_EXE_2_the_idempotency_key_is_built_before_the_audit_entry_is_written():
    """INV-4 then INV-5, in that order. A key derived after the write could not
    have been recorded in it, and the entry would name no dispatch."""
    source = (REPO_ROOT / "settle" / "runner" / "case_runner.py").read_text(encoding="utf-8")
    key_at = source.index("key = dispatch_key(")
    log_at = source.index("LedgerKind.DISPATCH")
    execute_at = source.index("outcome = execute(")
    assert key_at < log_at < execute_at, "write-ahead ordering has been reversed"
    compact = " ".join(source.split())
    assert '"idempotency_key": key' in compact, "the entry does not record the key it wrote ahead of"


def test_EXE_2_the_executor_never_decides_anything():
    """It receives an action that has already passed gates and performs it."""
    source = (REPO_ROOT / "settle" / "execute" / "executor.py").read_text(encoding="utf-8")
    for decider in ("legal_actions", "evaluate_gates", "check_stops", "arm.choose"):
        assert decider not in source, f"executor references {decider}"
