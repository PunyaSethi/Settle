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
    # --- A71, the action grid -------------------------------------------
    # Actions carry parameters, so the space has to be finite and declared.
    # Eight offsets, chosen to span two dimensions the policy actually needs:
    #
    #   a different hour of the same day   0, 6
    #   a different hour of another day    18, 30   (30 = 24 + 6)
    #   the same hour some days later      48, 72, 120, 168
    #
    # §9 asks `ambiguous` for "one retry at a different hour", which needs an
    # offset that is not a multiple of 24. Reaching a liquidity window needs
    # multi-day offsets. Eight is the largest grid that still leaves EXPLORE
    # able to cover every cell at 30k cases — see EXP-6.
    "action_grid.offset_now": 0,
    "action_grid.offset_later_today": 6,
    "action_grid.offset_next_morning": 18,
    "action_grid.offset_next_evening": 30,
    "action_grid.offset_two_days": 48,
    "action_grid.offset_three_days": 72,
    "action_grid.offset_five_days": 120,
    "action_grid.offset_one_week": 168,
    # Nothing may be scheduled past the horizon the agent stops acting at.
    "action_grid.max_horizon_h": 720,
}

_GRID_PREFIX = "action_grid.offset_"


def hour_offsets() -> tuple[int, ...]:
    """The declared offsets, sorted. A71.

    Derived from POLICY_PARAMS rather than written out a second time, so the
    grid EXPLORE samples and the grid OURS searches cannot drift apart. That
    drift is the failure A71 exists to prevent: an estimator trained on one grid
    and queried on another has zero coverage exactly where it is asked to
    predict.
    """
    return tuple(
        sorted(int(value) for key, value in POLICY_PARAMS.items() if key.startswith(_GRID_PREFIX))
    )


def max_horizon_h() -> int:
    """Nothing may be scheduled past the decision horizon."""
    return int(POLICY_PARAMS["action_grid.max_horizon_h"])


def class_retry_cap(decline_class: DeclineClass) -> int:
    """G10's cap for a class."""
    return int(POLICY_PARAMS[f"class_retry_cap.{decline_class.value}"])
