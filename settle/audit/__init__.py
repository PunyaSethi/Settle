"""The audit ledger. SPEC §5.6, INV-5, INV-6.

Append-only, hash-chained, JSONL on disk. Not a database: a ledger you can
`UPDATE` is not a ledger, and the whole claim of this project is that the
reconciliation pass does not have to trust the executor's account of events.
"""

__all__ = []
