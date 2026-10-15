"""Single-sourced canonical NFL week resolver.

Eliminates the v1 Week 5 / Week 6 off-by-one bug and cross-path discrepancies
by providing a single, deterministic function used across all modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from core.exceptions import DataValidationError


def resolve_canonical_nfl_week(
    sleeper_state_payload: Dict[str, Any],
    reference_utc: Optional[datetime] = None,
) -> int:
    """Resolve the canonical active NFL week from Sleeper state.

    Args:
        sleeper_state_payload: Dictionary response from Sleeper's /v1/state/nfl.
        reference_utc: Optional reference datetime in UTC (defaults to current UTC).

    Returns:
        Canonical NFL week as an integer in [1, 18].

    Raises:
        DataValidationError: If payload is invalid, corrupt, or resolves out of bounds.
    """
    if not isinstance(sleeper_state_payload, dict) or not sleeper_state_payload:
        raise DataValidationError("Sleeper state payload must be a non-empty dictionary.")

    season_type = sleeper_state_payload.get("season_type", "regular")
    if season_type == "pre":
        # Preseason defaults to week 1 for regular season preparation
        return 1

    raw_week = sleeper_state_payload.get("week")
    if raw_week is None:
        raise DataValidationError("Missing 'week' in Sleeper NFL state payload.")

    try:
        week = int(raw_week)
    except (ValueError, TypeError) as e:
        raise DataValidationError(f"Invalid 'week' value '{raw_week}': must be an integer.") from e

    display_week_val = sleeper_state_payload.get("display_week")
    display_week = int(display_week_val) if display_week_val is not None else week

    leg_val = sleeper_state_payload.get("leg")
    leg = int(leg_val) if leg_val is not None else week

    # Resolve reference time
    ref_dt = reference_utc or datetime.now(timezone.utc)
    if ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=timezone.utc)

    # Reconcile when display_week lags week (the classic Week 5 / Week 6 bug):
    # NFL Tuesday 08:00 UTC is the official weekly advancement boundary.
    # Tuesday is weekday 1.
    # If display_week lags week:
    # If the current day is Tuesday at or after 08:00 UTC, Wednesday (2), Thursday (3),
    # Friday (4), Saturday (5), Sunday (6), or Monday (0), the week has advanced to `week`.
    # Even if Tuesday before 08:00 UTC, if leg == week, week is active.
    if display_week < week:
        # Tuesday = 1; before 08:00 UTC on Tuesday is the only narrow lag window
        weekday = ref_dt.weekday()
        hour = ref_dt.hour
        is_early_tuesday = (weekday == 1 and hour < 8)

        if not is_early_tuesday or leg == week:
            canonical_week = week
        else:
            canonical_week = display_week
    else:
        canonical_week = week

    if canonical_week < 1 or canonical_week > 18:
        raise DataValidationError(
            f"Resolved canonical NFL week {canonical_week} is outside regular season bounds [1, 18]."
        )

    return canonical_week
