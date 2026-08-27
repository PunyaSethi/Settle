"""Frozen contracts, per SPEC §5.

This package must import nothing from `settle.sim`. Hidden truth
(`HiddenTruth`, `ActualOutcome`) lives in `settle/sim/truth.py` precisely so
that the agent can never reach it by importing a schema module.

Nothing here carries behaviour. Shapes only.
"""

__all__ = []
