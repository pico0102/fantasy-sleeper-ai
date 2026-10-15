"""Unit tests for pre-reasoner premise detection and post-reasoner grounding."""

from core.guardrails import enforce_post_reasoner_guardrails, scan_user_premises
from core.provenance import build_provenance_record
from core.schemas import (
    InjuryContext,
    RoleAndNewsContext,
    ValidatedPlayer,
    WorldStateSnapshot,
)


def _build_test_snapshot(teams_on_bye=None, notable=None) -> WorldStateSnapshot:
    prov = [
        build_provenance_record("sleeper_nfl_state", "sleeper_api", "state"),
        build_provenance_record("nfl_news_and_depth_charts", "news_depth_chart", "news"),
    ]
    return WorldStateSnapshot(
        snapshot_id="test_snapshot_123",
        created_at_utc="2026-10-14T00:00:00Z",
        nfl_season="2026",
        nfl_week=6,
        nfl_teams_on_bye=teams_on_bye or ["KC", "MIA", "MIN", "LAR"],
        notable_role_changes=notable or [],
        provenance=prov,
    )


def test_false_bye_week_premise_detected():
    # Jahmyr Gibbs plays for DET. DET is NOT on bye in Week 6.
    gibbs = ValidatedPlayer(
        player_id="9221",
        full_name="Jahmyr Gibbs",
        position="RB",
        team="DET",
        bye_week=10,
    )
    snapshot = _build_test_snapshot(teams_on_bye=["KC", "MIA", "MIN", "LAR"], notable=[gibbs])

    query = "Since Jahmyr Gibbs is on bye this week in Week 6, should I drop him for a backup?"
    directives, has_false_premise = scan_user_premises(query, snapshot)

    assert has_false_premise is True
    assert len(directives) >= 1
    assert "PREMISE CORRECTION MANDATE" in directives[0]
    assert "sleeper_nfl_state" in directives[0]
    assert "NOT on bye in Week 6" in directives[0]


def test_valid_bye_week_premise_not_flagged():
    # Player on MIA (which is actually on bye in Week 6)
    achane = ValidatedPlayer(
        player_id="9226",
        full_name="De'Von Achane",
        position="RB",
        team="MIA",
        bye_week=6,
    )
    snapshot = _build_test_snapshot(teams_on_bye=["KC", "MIA", "MIN", "LAR"], notable=[achane])

    query = "Since De'Von Achane is on bye this week, who should I start?"
    directives, has_false_premise = scan_user_premises(query, snapshot)
    assert has_false_premise is False


def test_benching_directive_for_kirk_cousins():
    cousins = ValidatedPlayer(
        player_id="1167",
        full_name="Kirk Cousins",
        position="QB",
        team="LV",
        role_and_news=RoleAndNewsContext(
            depth_chart_position="QB",
            depth_chart_rank=2,
            role_status="benched",
            supplanted_by="Fernando Mendoza",
        ),
    )
    snapshot = _build_test_snapshot(notable=[cousins])

    query = "Should I pick up Kirk Cousins from waivers?"
    directives, has_false_premise = scan_user_premises(query, snapshot)

    assert len(directives) >= 1
    assert "Fernando Mendoza" in directives[0]
    assert "nfl_news_and_depth_charts" in directives[0]
    assert "BENCHED" in directives[0]


def test_post_reasoner_guardrail_enforces_mendoza():
    cousins = ValidatedPlayer(
        player_id="1167",
        full_name="Kirk Cousins",
        position="QB",
        team="LV",
        role_and_news=RoleAndNewsContext(
            depth_chart_position="QB",
            depth_chart_rank=2,
            role_status="benched",
            supplanted_by="Fernando Mendoza",
        ),
    )
    snapshot = _build_test_snapshot(notable=[cousins])

    # Suppose a raw LLM response omitted Mendoza's name:
    raw_response = "Kirk Cousins has veteran experience and could be useful."
    final_text, alerts, citations = enforce_post_reasoner_guardrails(
        raw_response, snapshot, directives=[], has_false_premise=False
    )

    # Post-reasoner guardrail must enforce the alert naming Mendoza!
    assert "Fernando Mendoza" in final_text
    assert "nfl_news_and_depth_charts" in citations
