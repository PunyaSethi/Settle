"""Free text. SPEC §11.

Deterministic-first routing: plain code classifies every reply before any LLM
call. Unambiguous replies — STOP, a clear opt-out, an explicit date commitment,
silence — never reach a model. Only what the deterministic classifier cannot
resolve escalates, and the escalation rate is reported per run.

The LLM path itself lands at CP11. This package establishes the seam, and the
seam is the valuable part: it is what keeps a dictionary lookup from becoming a
network call with a latency budget and a failure mode.
"""

__all__ = []
