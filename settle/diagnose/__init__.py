"""Deterministic diagnosis. SPEC §9.

Decline code to class, pure lookup. The LLM is never invoked on a decline code —
a gateway string is structured data, and routing it through a model would add
latency, cost and non-determinism to a dictionary lookup.
"""

__all__ = []
