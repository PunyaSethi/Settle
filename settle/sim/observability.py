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

from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from settle.schema.enums import ReportedStatus
from settle.schema.outcome import ReportedOutcome
from settle.sim.streams import Streams, derive_unit_float
from settle.sim.truth import ActualOutcome

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


# How far back an out-of-order report is shifted. Structure, not a prior: the
# parameter that carries the number is `out_of_order_rate`, and this is only the
# shape of the inversion it produces.
OUT_OF_ORDER_SHIFT_HOURS: Final[int] = 6


def report(
    actual: ActualOutcome,
    *,
    case_id: str,
    tick: int,
    config: ObservabilityConfig,
    streams: Streams,
    authorised_at: datetime,
    authorised: bool = True,
) -> ReportedOutcome:
    """Push one outcome through the reporting layer. SPEC §6.

    Every one of the five parameters is applied here, which is the point: until
    CP6 two of them were declared, carried a PRIORS row implying they mattered,
    and were read by nothing. A parameter nobody reads is worse than a literal —
    it looks like evidence.

    A drop does not become a failure. It becomes silence: `status == "none"`
    means "we heard nothing", which the agent cannot distinguish from "nothing
    happened". That gap is SF-2, and it is the whole project.
    """
    if not authorised:
        return ReportedOutcome(
            case_id=case_id, at=authorised_at, status=ReportedStatus.FAILED, arrival_count=1
        )

    # A gateway reports an authorisation, not a settlement. An authorisation
    # that never becomes money is still reported `captured`, which is the whole
    # mechanism behind SF-1 and the reason INV-1 refuses to treat the two as the
    # same thing (§6, auth_no_settle_rate).

    if streams.value(case_id, "webhook_drop", tick) < config.webhook_drop_rate:
        return ReportedOutcome(
            case_id=case_id, at=authorised_at, status=ReportedStatus.NONE, arrival_count=1
        )

    # The confirmation arrives after the money does. This is the reporting lag,
    # not the settlement lag: the world's is `settlement_lag_h.mean` in PARAMS.
    reported_at = authorised_at + timedelta(hours=config.settlement_lag_reporting)

    # Out of order: the report is stamped before an event that preceded it, so a
    # consumer sorting by `at` reconstructs the wrong sequence.
    # §14.2's named stream list has `webhook_drop` and `webhook_dup` but not
    # `out_of_order`, and `streams.py` closes that list deliberately. Drawn from
    # its own address until the omission is fixed — see OQ-34.
    if derive_unit_float(streams.master_seed, "out_of_order", case_id, tick) < config.out_of_order_rate:
        reported_at = reported_at - timedelta(hours=OUT_OF_ORDER_SHIFT_HOURS)

    duplicated = (
        streams.value(case_id, "webhook_dup", tick) < config.webhook_duplicate_rate
    )
    return ReportedOutcome(
        case_id=case_id,
        at=reported_at,
        status=ReportedStatus.CAPTURED,
        payment_id=f"pay_{case_id}_{tick}",
        amount_paise=actual.amount_paise if actual.settled else None,
        arrival_count=2 if duplicated else 1,
    )


def reversal_reported_at(reversed_at: datetime, config: ObservabilityConfig) -> datetime:
    """When a reversal becomes visible. SPEC §6.

    Read by reconciliation: SF-7 asks whether the agent could have reopened the
    case, which depends on when it could have known, not on when the money moved.
    """
    return reversed_at + timedelta(hours=config.reversal_reporting_delay)
