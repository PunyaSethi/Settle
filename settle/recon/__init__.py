"""Reconciliation and the silent-failure auditor. SPEC §7 — DIFFERENTIATOR.

The runner acts on `ReportedOutcome` and can be wrong. This package runs
afterwards, reads `ActualOutcome`, and produces the truth. It is the only thing
entitled to say a case recovered.

**Narrow, named exception to INV-8.** `settle/recon/` is the one package outside
`settle/sim/` permitted to import `settle.sim.truth`. It exists precisely to
compare what was believed against what happened, which is impossible without
both. The exception is exactly this package and no other; REC-6 asserts that no
third module has quietly joined it. An unstated exception is how INV-8 dies.
"""

__all__ = []
