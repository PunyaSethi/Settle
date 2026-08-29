"""The case runner. SPEC §12, §13.

Drives one arm over one case until a stop fires, writing a hash-chained ledger
as it goes. It never reads `HiddenTruth` or `ActualOutcome` — it sees only what
the observability layer reports, which is the condition the whole project is
built to be correct under. RUN-9 asserts it structurally.
"""

__all__ = []
