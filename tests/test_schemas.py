"""Unit tests for Pydantic v2 schemas and board bias auditor."""

import pytest
from core.exceptions import DataValidationError
from core.schemas import (
    InjuryContext,
    ProvenanceRecord,
    RoleAndNewsContext,
    SurvivorPickResult,
    ValidatedPlayer,
)
from core.board_auditor import audit_and_sanitize_board


def test_provenance_record_validation():
    rec = ProvenanceRecord(
        source_id="sleeper_nfl_state",
        source_type="sleeper_api",
        endpoint_or_path="https://api.sleeper.app/v1/state/nfl",
        fetched_at_utc="2026-10-14T00:00:00Z",
        age_seconds=12.5,
        status="ok",
        record_count=1,
    )
    assert rec.source_id == "sleeper_nfl_state"
    assert rec.status == "ok"
    assert rec.record_count == 1


def test_injury_context_risk_computation():
    active = InjuryContext(status="Active")
    assert not active.is_medical_risk
    assert not active.is_unstartable_medical

    questionable = InjuryContext(status="Questionable", body_part="Knee")
    assert questionable.is_medical_risk
    assert not questionable.is_unstartable_medical

    out_player = InjuryContext(status="Out", body_part="Hamstring")
    assert out_player.is_medical_risk
    assert out_player.is_unstartable_medical

    ir_player = InjuryContext(status="IR")
    assert ir_player.is_medical_risk
    assert ir_player.is_unstartable_medical


def test_role_and_news_context_benching():
    starter = RoleAndNewsContext(
        depth_chart_position="QB",
        depth_chart_rank=1,
        role_status="confirmed_starter",
    )
    assert not starter.is_role_risk

    # Kirk Cousins scenario: benched for Fernando Mendoza
    cousins_ctx = RoleAndNewsContext(
        depth_chart_position="QB",
        depth_chart_rank=2,
        role_status="benched",
        supplanted_by="Fernando Mendoza",
        recent_news_headlines=["Kirk Cousins benched in favor of Fernando Mendoza"],
    )
    assert cousins_ctx.is_role_risk
    assert cousins_ctx.role_status == "benched"
    assert cousins_ctx.supplanted_by == "Fernando Mendoza"

    # Backup QB without explicit 'benched' label is still an acute role risk
    backup_qb = RoleAndNewsContext(
        depth_chart_position="QB",
        depth_chart_rank=2,
        role_status="backup",
    )
    assert backup_qb.is_role_risk


def test_survivor_pick_result_fail_closed_invariant():
    prov = ProvenanceRecord(
        source_id="the_odds_api_nfl",
        source_type="odds_api",
        endpoint_or_path="https://api.the-odds-api.com/v4",
        fetched_at_utc="2026-10-14T00:00:00Z",
        status="missing_key",
        record_count=0,
    )
    # Even if someone accidentally passes recommended_team when status != "verified_pick",
    # the validator strictly resets recommended_team to None
    result = SurvivorPickResult(
        status="fail_closed_no_odds",
        nfl_week=6,
        recommended_team="DET",
        reasoning_markdown="Fail closed",
        odds_provenance=prov,
        snapshot_id="snapshot123",
    )
    assert result.recommended_team is None
    assert result.status == "fail_closed_no_odds"

    # Verified pick preserves recommended_team
    prov_ok = ProvenanceRecord(
        source_id="the_odds_api_nfl",
        source_type="odds_api",
        endpoint_or_path="https://api.the-odds-api.com/v4",
        fetched_at_utc="2026-10-14T00:00:00Z",
        status="ok",
        record_count=16,
    )
    valid_pick = SurvivorPickResult(
        status="verified_pick",
        nfl_week=6,
        recommended_team="DET",
        opponent_team="GB",
        spread=-7.5,
        implied_win_prob=0.74,
        reasoning_markdown="Verified pick based on point spread",
        odds_provenance=prov_ok,
        snapshot_id="snapshot123",
    )
    assert valid_pick.recommended_team == "DET"


def test_board_bias_audit_and_sanitization():
    raw_board = [
        {"name": "Jahmyr Gibbs", "pos": "RB", "pts": 331.4, "pos_multiplier": 1.25, "vorp": 203.7},
        {"name": "Puka Nacua", "pos": "WR", "pts": 281.3, "vorp": 130.0},
        {"name": "Josh Allen", "pos": "QB", "pts": 422.1, "bias_weight": 0.9, "vorp": 110.0},
    ]

    sanitized, summary = audit_and_sanitize_board(raw_board)
    assert summary["audit_passed"] is True
    assert summary["synthetic_multipliers_detected"] == 2
    assert "pos_multiplier" in summary["synthetic_fields_stripped"]
    assert "bias_weight" in summary["synthetic_fields_stripped"]
    assert summary["bias_status"] == "adjusted"

    # Ensure synthetic multiplier keys are stripped from entries
    assert "pos_multiplier" not in sanitized[0]
    assert "bias_weight" not in sanitized[2]
    assert sanitized[0]["board_bias_adjusted"] is True
    assert sanitized[1]["board_bias_adjusted"] is False
    assert sanitized[0]["tier"] == 1
    assert sanitized[0]["rank"] == 1


def test_board_auditor_rejects_empty():
    with pytest.raises(DataValidationError):
        audit_and_sanitize_board([])
