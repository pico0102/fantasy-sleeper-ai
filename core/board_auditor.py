"""Audit and sanitize board.json to eliminate v1 synthetic positional bias and multipliers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from core.exceptions import DataValidationError

# Known synthetic fields from v1 heuristic rules engine
SYNTHETIC_BIAS_KEYS = {
    "pos_multiplier",
    "multiplier",
    "synthetic_multiplier",
    "rb_multiplier",
    "wr_multiplier",
    "qb_multiplier",
    "bias_weight",
    "v1_weight",
    "synthetic_weight",
}


def compute_tier(overall_rank: int) -> int:
    """Map overall rank to standard fantasy tiers (1-5)."""
    if overall_rank <= 12:
        return 1
    if overall_rank <= 24:
        return 2
    if overall_rank <= 48:
        return 3
    if overall_rank <= 75:
        return 4
    return 5


def audit_and_sanitize_board(
    raw_board: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Inspect and strip v1 positional multipliers or synthetic weight biases.

    Args:
        raw_board: Raw list of player records loaded from board.json.

    Returns:
        Tuple of (sanitized_board_list, board_audit_summary_dict).

    Raises:
        DataValidationError: If board is empty or entries lack required fields.
    """
    if not isinstance(raw_board, list) or len(raw_board) == 0:
        raise DataValidationError("board.json must be a non-empty list of player records.")

    sanitized: List[Dict[str, Any]] = []
    synthetic_multipliers_detected = 0
    stripped_keys_found = set()
    tier_distribution: Dict[str, Dict[str, int]] = {
        f"Tier {t}": {"RB": 0, "WR": 0, "QB": 0, "TE": 0, "OTHER": 0}
        for t in range(1, 6)
    }
    pos_counts: Dict[str, int] = {"RB": 0, "WR": 0, "QB": 0, "TE": 0, "OTHER": 0}

    for idx, raw_entry in enumerate(raw_board, start=1):
        if not isinstance(raw_entry, dict):
            raise DataValidationError(f"Invalid record at index {idx} in board.json: expected dict.")

        entry = dict(raw_entry)
        has_bias_field = False

        # Detect and strip synthetic multipliers
        for key in list(entry.keys()):
            if key in SYNTHETIC_BIAS_KEYS:
                entry.pop(key, None)
                stripped_keys_found.add(key)
                has_bias_field = True

        if has_bias_field:
            synthetic_multipliers_detected += 1

        name = entry.get("name")
        pos = entry.get("pos", "OTHER")
        if not name:
            raise DataValidationError(f"Record at index {idx} in board.json is missing 'name'.")

        pos_key = pos if pos in pos_counts else "OTHER"
        pos_counts[pos_key] = pos_counts.get(pos_key, 0) + 1

        # Transparent tier assignment based on rank
        tier = entry.get("tier") or compute_tier(idx)
        entry["tier"] = tier
        entry["rank"] = idx
        entry["board_bias_adjusted"] = has_bias_field

        tier_label = f"Tier {tier}" if f"Tier {tier}" in tier_distribution else "Tier 5"
        tier_distribution[tier_label][pos_key] = (
            tier_distribution[tier_label].get(pos_key, 0) + 1
        )

        sanitized.append(entry)

    summary: Dict[str, Any] = {
        "audit_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_players_audited": len(sanitized),
        "synthetic_multipliers_detected": synthetic_multipliers_detected,
        "synthetic_fields_stripped": sorted(list(stripped_keys_found)),
        "bias_status": "adjusted" if synthetic_multipliers_detected > 0 else "clean",
        "position_counts": pos_counts,
        "tier_distribution": tier_distribution,
        "audit_passed": True,
    }

    return sanitized, summary
