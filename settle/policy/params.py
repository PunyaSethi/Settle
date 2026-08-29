"""Policy constants. SPEC §12, §13, INV-10.

Our configuration, not assumptions about the world. `settle.sim.generator.PARAMS`
holds claims about reality that could in principle be checked against published
data; these are choices we made and must defend. The distinction matters when a
judge asks where a number came from, so PRIORS.md keeps them in their own table.

They are still numbers that move reported metrics, which is what INV-10 actually
covers (SPEC §15). `attempt_budget` and `contact_budget` bound B3's violation
count; `frequency_cap_per_window` bounds contacts-per-case directly. PAR-1
enforces the correspondence with PRIORS.md in both directions.
"""

from typing import Final

from settle.schema.enums import DeclineClass

POLICY_PARAMS: Final[dict[str, float]] = {
    # G4 — card network rules cap retries on a declined credential.
    "card_network_retry_cap": 4,
    # S3 — the budgets that bound an unguarded arm.
    "attempt_budget": 6,
    "contact_budget": 5,
    # G2 — SPEC §12.
    "frequency_cap_per_window": 3,
    "frequency_window_hours": 168,
    "min_contact_gap_hours": 20,
    # G9 — SPEC §12, the notified debit window.
    "notice_window_days": 3,
    # The runner's cadence when it has chosen to do nothing and no timer is
    # pending. It sets how many decisions an arm gets across the horizon, and
    # therefore contacts per case, which is a §14.4 headline.
    "decision_cadence_hours": 24,
    # G10 — per-class retry budget, distinct from G4's card-network cap.
    # `ambiguous` is 1 because §9 permits one retry at a different hour and then
    # a message; without the cap those retries run until G4 or S3 fires, which
    # is a different rule than the one §9 states.
    "class_retry_cap.time_shiftable": 4,
    "class_retry_cap.transient": 3,
    "class_retry_cap.dead_instrument": 0,
    "class_retry_cap.auth_abandoned": 0,
    "class_retry_cap.ambiguous": 1,
    "class_retry_cap.terminal": 0,
}


def class_retry_cap(decline_class: DeclineClass) -> int:
    """G10's cap for a class."""
    return int(POLICY_PARAMS[f"class_retry_cap.{decline_class.value}"])
