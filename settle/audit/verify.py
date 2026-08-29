"""Verify a ledger. SPEC §5.6, INV-6.

    python -m settle.audit.verify out/audit.jsonl

Re-derives every hash and reports the first break with its seq. Exits non-zero
on any break, so it can gate a run rather than merely inform one.
"""

import sys
from pathlib import Path

from settle.audit.chain import ChainBreak, read_entries, verify_entries


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m settle.audit.verify <ledger.jsonl>", file=sys.stderr)
        return 2

    path = Path(args[0])
    if not path.exists():
        print(f"verify: no such ledger: {path}", file=sys.stderr)
        return 2

    try:
        entries = read_entries(path)
    except Exception as exc:  # a malformed line is itself a break
        print(f"verify: {path} is not readable as a ledger: {exc}", file=sys.stderr)
        return 1

    try:
        verify_entries(entries)
    except ChainBreak as broken:
        print(f"verify: FAIL  {path}", file=sys.stderr)
        print(f"  first break at seq {broken.seq}: {broken.reason}", file=sys.stderr)
        print(f"  {len(entries)} entries read, {broken.seq} verified before the break", file=sys.stderr)
        return 1

    print(f"verify: ok  {path}  {len(entries)} entries, chain intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
