"""Unit tests for the canonical single-sourced NFL week resolver."""

from datetime import datetime, timezone
import pytest
from core.exceptions import DataValidationError
from core.week_resolver import resolve_canonical_nfl_week


def test_standard_week_resolution():
    payload = {
        "week": 6,
        "season_type": "regular",
        "season": "2026",
        "leg": 6,
        "display_week": 6,
    }
    assert resolve_canonical_nfl_week(payload) == 6


def test_week_5_week_6_boundary_bug_regression():
    # The classic v1 bug: Sleeper has advanced week=6, but display_week is lagging at 5
    payload = {
        "week": 6,
        "season_type": "regular",
        "season": "2026",
        "leg": 6,
        "display_week": 5,
    }
    # Mid-week reference (Wednesday 14:00 UTC)
    ref_dt = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
    week = resolve_canonical_nfl_week(payload, reference_utc=ref_dt)
    assert week == 6, "Must resolve to Week 6 and not lag at Week 5"


def test_tuesday_boundary_rollover():
    payload = {
        "week": 6,
        "season_type": "regular",
        "season": "2026",
        "leg": 6,
        "display_week": 5,
    }
    # Tuesday 10:00 UTC (after Tuesday 08:00 UTC cutoff) -> resolves to week 6
    post_cutoff = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    assert resolve_canonical_nfl_week(payload, reference_utc=post_cutoff) == 6

    # Tuesday 05:00 UTC with leg=5 (before Tuesday 08:00 UTC cutoff) -> stays at display_week 5
    early_payload = {
        "week": 6,
        "season_type": "regular",
        "season": "2026",
        "leg": 5,
        "display_week": 5,
    }
    pre_cutoff = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)
    assert resolve_canonical_nfl_week(early_payload, reference_utc=pre_cutoff) == 5


def test_preseason_defaults_to_week_1():
    payload = {
        "week": 2,
        "season_type": "pre",
        "season": "2026",
    }
    assert resolve_canonical_nfl_week(payload) == 1


def test_invalid_payload_raises():
    with pytest.raises(DataValidationError):
        resolve_canonical_nfl_week({})

    with pytest.raises(DataValidationError):
        resolve_canonical_nfl_week({"season_type": "regular"})  # missing week

    with pytest.raises(DataValidationError):
        resolve_canonical_nfl_week({"week": "not-an-int"})

    with pytest.raises(DataValidationError):
        resolve_canonical_nfl_week({"week": 25})  # out of bounds (> 18)

    with pytest.raises(DataValidationError):
        resolve_canonical_nfl_week({"week": 0})   # out of bounds (< 1)
