"""The reporting layer. SPEC §6 — DIFFERENTIATOR.

Sits between the world and the agent. Every outcome the agent learns about
passes through here, and some of them do not make it.

Every comparable system assumes this layer does not exist: the executor acts,
the outcome is reported, the outcome is believed. In production that is false,
and a recovery agent that assumes otherwise chases customers who have already
paid.

What is NOT here, and why
-------------------------
`auth_no_settle_rate` used to live in this module. It does not belong here.
Whether an authorised payment actually settles is a fact about the bank, not
about our reporting. Keeping it here meant `perfect_observability()` zeroed it,
which made authorisation equivalent to settlement and silently abolished SF-1 —
a real-world failure class, not a reporting artefact. It now lives in world
PARAMS, where zeroing the reporting layer cannot reach it.

The same reasoning splits the two lag parameters. The world has a settlement lag
(`settlement_lag_h.mean` in PARAMS) and a reversal rate (`will_reverse_rate`);
this layer owns only how late it hears about either.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

REPORTING_PARAMETERS: Final[tuple[str, ...]] = (
    "webhook_drop_rate",
    "webhook_duplicate_rate",
    "out_of_order_rate",
    "settlement_lag_reporting",
    "reversal_reporting_delay",
)


class ObservabilityConfig(BaseModel):
    """The five reporting-layer parameters of SPEC §6.

    Defaults are ASSERTED pending D4 and carry rows in PRIORS.md.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    # Outcome never reported. The agent keeps chasing a paid customer (SF-2).
    webhook_drop_rate: float = Field(default=0.021, ge=0.0, le=1.0)
    # Outcome reported 2+ times. This is what INV-4 is defending against.
    webhook_duplicate_rate: float = Field(default=0.037, ge=0.0, le=1.0)
    # Events arrive in the wrong sequence.
    out_of_order_rate: float = Field(default=0.014, ge=0.0, le=1.0)
    # Hours between money settling and the settlement being reported. Not the
    # settlement lag itself — that is the world's, and it is in PARAMS.
    settlement_lag_reporting: float = Field(default=6.0, ge=0.0)
    # Hours between a reversal happening and it being reported. Whether a
    # payment reverses at all is `will_reverse_rate`, in PARAMS.
    reversal_reporting_delay: float = Field(default=18.0, ge=0.0)

    @property
    def is_perfect(self) -> bool:
        """True when reporting is instant, complete and in order."""
        return all(getattr(self, name) == 0.0 for name in REPORTING_PARAMETERS)


def perfect_observability() -> ObservabilityConfig:
    """All five reporting parameters at zero.

    Exists only to quantify what unreliable reporting costs (SPEC §6). It does
    NOT make the world perfect: payments still fail to settle, still reverse and
    still lag. A headline produced under this config is a claim about reporting,
    not about production.
    """
    return ObservabilityConfig(**{name: 0.0 for name in REPORTING_PARAMETERS})


OBSERVABILITY_DEFAULTS: Final[dict[str, float]] = {
    f"observability.{name}": ObservabilityConfig.model_fields[name].default
    for name in REPORTING_PARAMETERS
}
