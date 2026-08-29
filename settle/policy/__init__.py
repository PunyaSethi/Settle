"""Gates, stops and the legal action set. SPEC §12, §13, §9.

Pure decision logic. No I/O, no clock, no randomness, and nothing here imports
`settle.sim` — the agent must not be able to reach hidden truth (INV-8), and a
gate that read a clock could not be replayed from the ledger.
"""

__all__ = []
