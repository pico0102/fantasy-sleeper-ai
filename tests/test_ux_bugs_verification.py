"""Regression tests for UX Bug 1 (Alert Spam), Bug 2 (Unreadable Prose), and Bug 3 (QB Start/Sit Naming)."""

import re
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from core.guardrails import flag_and_sanitize_system_internals
from core.world_model import WorldModelBuilder


SNAKE_CASE_REGEX = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


@pytest.mark.asyncio
async def test_ux_verification_a_who_should_i_start_at_quarterback():
    """Verify: 'who should I start at quarterback?' -> names Bo Nix, zero mention of Cousins, zero snake_case."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"message": "who should I start at quarterback?"})
        assert resp.status_code == 200
        data = resp.json()
        response_text = data["response"]

        # 1. Must explicitly name Bo Nix
        assert "Bo Nix" in response_text or "Nix" in response_text

        # 2. Must end with a named player
        assert response_text.strip().endswith("Bo Nix as your starting quarterback.") or "Bo Nix" in response_text.splitlines()[-1]

        # 3. Must have ZERO mention of Kirk Cousins in response or alerts
        assert "Kirk Cousins" not in response_text and "Cousins" not in response_text
        assert not any("Kirk Cousins" in alt or "Cousins" in alt for alt in data.get("injury_alerts", []))

        # 4. Must contain ZERO snake_case tokens
        snake_matches = SNAKE_CASE_REGEX.findall(response_text)
        assert len(snake_matches) == 0, f"Found snake_case tokens in prose: {snake_matches}"

        # 5. Must not contain backtick-quoted source IDs or field fragments
        assert "`" not in response_text
        assert "status=" not in response_text
        assert "depth_chart_rank=" not in response_text


@pytest.mark.asyncio
async def test_ux_verification_b_should_i_pick_up_kirk_cousins():
    """Verify: 'should I pick up Kirk Cousins?' -> warns about Mendoza benching in plain English, no variable names."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"message": "should I pick up Kirk Cousins?"})
        assert resp.status_code == 200
        data = resp.json()
        response_text = data["response"]

        # 1. Warns about the Mendoza benching in plain English
        assert "Fernando Mendoza" in response_text or "Mendoza" in response_text
        assert any(w in response_text.lower() for w in ["bench", "benched", "backup", "do not pick up", "warn"])

        # 2. No variable names or snake_case tokens in prose
        snake_matches = SNAKE_CASE_REGEX.findall(response_text)
        assert len(snake_matches) == 0, f"Found snake_case tokens in prose: {snake_matches}"
        assert "`" not in response_text
        assert "status=" not in response_text
        assert "depth_chart_rank=" not in response_text
        assert "supplanted_by=" not in response_text

        # 3. Provenance citation lives in structured fields for UI badges
        assert "nfl_news_and_depth_charts" in data["citations"]


def test_guardrail_flags_and_cleanses_snake_case():
    """Guardrail flags any answer containing snake_case tokens or backtick-quoted source IDs and sanitizes."""
    raw_leaked_text = (
        "**Alert (nfl_news_and_depth_charts):** Kirk Cousins is officially benched. "
        "(Source: `sleeper_rosters`, `local_board_catalog`)\n"
        "status=benched, depth_chart_rank=2 (supplanted by Fernando Mendoza)."
    )
    clean_text, flagged = flag_and_sanitize_system_internals(raw_leaked_text)

    # Flagged tokens must be recorded
    assert len(flagged) >= 3
    assert any("sleeper_rosters" in f for f in flagged)
    assert any("nfl_news_and_depth_charts" in f for f in flagged)

    # Clean text must have zero snake_case, zero backticks, and zero debug fragments
    snake_matches = SNAKE_CASE_REGEX.findall(clean_text)
    assert len(snake_matches) == 0, f"Clean text still has snake_case: {snake_matches}"
    assert "`" not in clean_text
    assert "status=" not in clean_text
    assert "depth_chart_rank=" not in clean_text
    assert "**Alert" not in clean_text
    assert "Source:" not in clean_text


@pytest.mark.asyncio
async def test_user_qbs_present_and_clearly_labeled_in_snapshot():
    """Verify plumbing: Anthony's QBs (Bo Nix and Baker Mayfield) are clearly present and labeled in snapshot."""
    builder = WorldModelBuilder()
    snapshot = await builder.build_snapshot(query="who should I start at quarterback?")
    assert snapshot.user_roster is not None

    all_user_players = snapshot.user_roster.starters + snapshot.user_roster.bench
    user_qbs = [p for p in all_user_players if p.position == "QB"]

    qb_names = [p.full_name for p in user_qbs]
    assert "Bo Nix" in qb_names
    assert "Baker Mayfield" in qb_names

    mayfield = next(p for p in user_qbs if p.full_name == "Baker Mayfield")
    assert mayfield.position == "QB"
    assert mayfield.team == "TB"
    assert mayfield.injury.status == "Out"
    assert mayfield.injury.body_part == "Thumb"


@pytest.mark.asyncio
async def test_repro_questions_return_named_players_with_zero_fallback():
    """Verify that all repro questions return specific, named-player answers with ZERO fallback text."""
    test_cases = [
        ("Who should I start at wide receiver this week?", ["Davante Adams", "Jakobi Meyers"]),
        ("Who should I start at running back this week?", ["Christian McCaffrey", "Bucky Irving"]),
        ("Is Davante Adams a good start this week?", ["Davante Adams"]),
        ("Give me waiver wire targets", ["De'Von Achane"]),
        ("help with my lineup", ["Bo Nix", "Christian McCaffrey", "Davante Adams"]),
    ]

    fallback_signature = "red-zone share metrics are currently missing"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for prompt, expected_names in test_cases:
            resp = await client.post("/api/chat", json={"message": prompt})
            assert resp.status_code == 200
            data = resp.json()
            response_text = data["response"]

            # Must NOT contain fallback text
            assert fallback_signature not in response_text.lower(), f"Fallback emitted for '{prompt}': {response_text}"
            assert "note: specific secondary" not in response_text.lower()

            # Must contain specific, named-player answers
            for name in expected_names:
                assert name in response_text, f"Expected '{name}' in response for '{prompt}', got: {response_text}"

