"""The 7 QA Probes Regression Test Suite.

Encodes all 7 post-mortem failure modes from Section 5 of IMPLEMENTATION_PLAN.md:
- Probe 1: Stale Awareness on Benched Players (Kirk Cousins / Fernando Mendoza)
- Probe 2: Deflection When Structured Data is Missing
- Probe 3: Lack of Injury Model (Baker Mayfield Probe)
- Probe 4: Acceptance of Unverified/False User Premises
- Probe 5: Cross-Path Contradictions & Week 5/6 Off-By-One Bug
- Probe 6: Unverifiable Data Provenance for Live Odds & Claims
- Probe 7: Survivor Mode Non-Determinism & Email Team Leak on Missing Odds
"""

import os
import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from core.config import SURVIVOR_FAIL_CLOSED_TEMPLATE
from core.provenance import build_provenance_record
from core.schemas import NFLGameOdds, WorldStateSnapshot
from core.survivor import assert_no_nfl_team_leaked


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_probe_1_kirk_cousins_mendoza_benching():
    """Probe 1: Must identify that Kirk Cousins is benched behind Fernando Mendoza.

    Must warn against pickup, state Mendoza supplanted him, and cite nfl_news_and_depth_charts.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"message": "Should I pick up Kirk Cousins?"})
        assert resp.status_code == 200
        data = resp.json()

        response_text = data["response"]
        # Must name Fernando Mendoza as the starter who supplanted Cousins
        assert "Fernando Mendoza" in response_text or "Mendoza" in response_text
        # Must warn against starting or picking him up
        assert any(w in response_text.lower() for w in ["bench", "benched", "backup", "do not pick up", "warn"])
        # Must cite depth chart / news source
        assert "nfl_news_and_depth_charts" in data["citations"] or "nfl_news_and_depth_charts" in response_text


@pytest.mark.asyncio
async def test_probe_2_no_deflection_on_missing_structured_data():
    """Probe 2: When an optional secondary metric is missing, never deflect.

    State what is missing and complete recommendation from available context.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        prompt = "What are my team's high-leverage red-zone target conversion shares and should I make changes?"
        resp = await client.post("/api/chat", json={"message": prompt})
        assert resp.status_code == 200
        data = resp.json()

        response_text = data["response"]
        # Never deflect or tell user to look elsewhere
        forbidden_deflections = [
            "i cannot help",
            "look elsewhere",
            "check your sleeper app",
            "i don't have access to your team",
            "as an ai language model i cannot",
        ]
        for f in forbidden_deflections:
            assert f not in response_text.lower()

        # States missing context and completes reasoned recommendation
        assert len(response_text) > 100
        assert data["snapshot_id"] != ""


@pytest.mark.asyncio
async def test_probe_3_baker_mayfield_injury_model():
    """Probe 3: Evaluate Baker Mayfield active thumb injury, Out status, and floor volatility."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"message": "Evaluate Baker Mayfield status and floor for this week"})
        assert resp.status_code == 200
        data = resp.json()

        response_text = data["response"]
        # Must surface dislocated thumb and Out status
        assert "thumb" in response_text.lower() or "out" in response_text.lower()
        # Active injury alerts present in payload
        assert any("Baker Mayfield" in a or "Mayfield" in a for a in data["injury_alerts"])
        # Irrelevant alerts must NOT be injected
        assert not any("Kirk Cousins" in a or "Cousins" in a for a in data["injury_alerts"])


@pytest.mark.asyncio
async def test_probe_4_reject_false_bye_week_premise():
    """Probe 4: Explicitly reject and correct false bye week premises."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Gibbs plays for DET. DET is NOT on bye in Week 6.
        prompt = "Since Jahmyr Gibbs is on bye this week in Week 6, should I drop him for a backup?"
        resp = await client.post("/api/chat", json={"message": prompt})
        assert resp.status_code == 200
        data = resp.json()

        response_text = data["response"]
        # Must explicitly reject the premise that Gibbs is on bye
        assert any(phrase in response_text.lower() for phrase in ["not on bye", "isn't on bye", "not having a bye", "not on a bye"])
        # Must cite sleeper_nfl_state
        assert "sleeper_nfl_state" in data["citations"] or "sleeper_nfl_state" in response_text


@pytest.mark.asyncio
async def test_probe_5_no_cross_path_contradiction_or_week_5_6_bug():
    """Probe 5: Ensure Week 6 is single-sourced across all paths without off-by-one drift."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Path A: Standard Chat
        chat_resp = await client.post("/api/chat", json={"message": "Current status", "mode": "fantasy"})
        assert chat_resp.status_code == 200
        week_a = chat_resp.json()["nfl_week"]

        # Path B: World-State API
        ws_resp = await client.get("/api/world-state")
        assert ws_resp.status_code == 200
        week_b = ws_resp.json()["nfl_week"]

        # Path C: Survivor Toggle Chat
        surv_chat_resp = await client.post("/api/chat", json={"message": "Survivor pick", "mode": "survivor"})
        assert surv_chat_resp.status_code == 200
        week_c = surv_chat_resp.json()["nfl_week"]

        # Path D: Survivor Email Preview
        email_resp = await client.post("/api/survivor/email-preview", json={"used_teams": []})
        assert email_resp.status_code == 200
        week_d = email_resp.json()["nfl_week"]

        # All four paths MUST return Week 6 with zero contradiction
        assert week_a == 6
        assert week_b == 6
        assert week_c == 6
        assert week_d == 6


@pytest.mark.asyncio
async def test_probe_6_inspectable_provenance_on_chat_and_odds():
    """Probe 6: Both fantasy and survivor chat responses must return valid provenance records."""
    mock_odds = [
        NFLGameOdds(
            game_id="g_det_gb",
            nfl_week=6,
            commence_time_utc="2026-10-18T17:00:00Z",
            home_team="GB",
            away_team="DET",
            favorite_team="DET",
            underdog_team="GB",
            spread_points=-7.5,
            implied_win_prob=0.74,
        )
    ]

    with patch("core.data_pipeline.OddsAPIClient.fetch_nfl_odds") as mock_fetch:
        mock_fetch.return_value = (
            mock_odds,
            build_provenance_record("the_odds_api_nfl", "odds_api", "https://api.the-odds-api.com", status="ok", record_count=1),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Fantasy chat
            fan_resp = await client.post("/api/chat", json={"message": "General lineup advice", "mode": "fantasy"})
            assert fan_resp.status_code == 200
            fan_data = fan_resp.json()
            assert len(fan_data["provenance"]) >= 6

            # Survivor chat
            surv_resp = await client.post("/api/chat", json={"message": "Survivor pick", "mode": "survivor"})
            assert surv_resp.status_code == 200
            surv_data = surv_resp.json()
            assert len(surv_data["provenance"]) >= 1

            for p in surv_data["provenance"]:
                assert "source_id" in p
                assert "fetched_at_utc" in p
                assert p["status"] == "ok"


@pytest.mark.asyncio
async def test_probe_7_survivor_fail_closed_and_email_zero_team_leak():
    """Probe 7: When odds are missing, fail-closed refusal is byte-for-byte deterministic,

    and weekly email run strictly emits zero NFL team names.
    """
    with patch("core.data_pipeline.OddsAPIClient.fetch_nfl_odds") as mock_fetch:
        # Simulate missing key or failed upstream API
        mock_fetch.return_value = (
            [],
            build_provenance_record(
                "the_odds_api_nfl",
                "odds_api",
                "https://api.the-odds-api.com",
                status="missing_key",
                record_count=0,
                error_detail="API key unconfigured",
            ),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # (a) Two identical Survivor chat queries must return identical string
            r1 = await client.post("/api/chat", json={"message": "Survivor pick", "mode": "survivor", "force_refresh": True})
            r2 = await client.post("/api/chat", json={"message": "Survivor pick", "mode": "survivor", "force_refresh": True})

            assert r1.status_code == 200
            assert r2.status_code == 200
            d1 = r1.json()
            d2 = r2.json()

            expected_refusal = SURVIVOR_FAIL_CLOSED_TEMPLATE.format(status="missing_key")
            assert d1["response"] == expected_refusal
            assert d1["response"] == d2["response"], "Must be 100% byte-for-byte identical"
            assert d1["survivor_result"]["recommended_team"] is None

            # (b) Survivor email preview must abort and pass zero-team-leak assert
            email_resp = await client.post("/api/survivor/email-preview", json={"used_teams": []})
            assert email_resp.status_code == 200
            email_data = email_resp.json()

            assert email_data["email_sent"] is False
            assert email_data["recommended_team"] is None
            assert email_data["abort_reason"] == "fail_closed_no_odds"

            # 32 NFL team zero-leak scan
            assert_no_nfl_team_leaked(email_data["detail"])


@pytest.mark.asyncio
async def test_probe_8_qb_start_sit_names_specific_player_no_vague_closings():
    """Probe 8: QB start/sit path must name a specific player and forbid vague closings."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"message": "Should I start Baker Mayfield this week or someone else?"})
        assert resp.status_code == 200
        data = resp.json()
        response_text = data["response"]

        # Must explicitly name players to START and SIT
        assert any(w in response_text for w in ["SIT", "Sit", "sit"])
        assert any(w in response_text for w in ["START", "Start", "start"])
        assert "Baker Mayfield" in response_text or "Mayfield" in response_text

        # Must NOT contain vague closings
        vague_phrases = [
            "consider other options",
            "consider your options",
            "weigh your alternatives",
            "monitor the situation",
            "depending on your bench",
        ]
        for vp in vague_phrases:
            assert vp not in response_text.lower()


@pytest.mark.asyncio
async def test_probe_9_survivor_burned_teams_history_plumbed():
    """Probe 9: Anthony's burned survivor teams (SEA, LAC, BUF, BAL, HOU) can never be recommended."""
    mock_odds = [
        # BUF is the heaviest favorite (-14.0), but is burned in Anthony's history
        NFLGameOdds(
            game_id="g_buf",
            nfl_week=6,
            commence_time_utc="2026-10-18T17:00:00Z",
            home_team="BUF",
            away_team="NE",
            favorite_team="BUF",
            underdog_team="NE",
            spread_points=-14.0,
            favorite_moneyline=-900,
            implied_win_prob=0.90,
        ),
        # DET is eligible (-7.5)
        NFLGameOdds(
            game_id="g_det",
            nfl_week=6,
            commence_time_utc="2026-10-18T17:00:00Z",
            home_team="GB",
            away_team="DET",
            favorite_team="DET",
            underdog_team="GB",
            spread_points=-7.5,
            favorite_moneyline=-350,
            implied_win_prob=0.77,
        ),
    ]

    with patch("core.data_pipeline.OddsAPIClient.fetch_nfl_odds") as mock_fetch:
        mock_fetch.return_value = (
            mock_odds,
            build_provenance_record("the_odds_api_nfl", "odds_api", "odds", status="ok", record_count=2),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/survivor", json={})
            assert resp.status_code == 200
            data = resp.json()

            # BUF must NOT be recommended despite being the biggest favorite, because it was burned!
            assert data["recommended_team"] != "BUF"
            assert data["recommended_team"] == "DET"


@pytest.mark.asyncio
async def test_probe_10_only_inject_relevant_alerts():
    """Probe 10: Only inject role/injury alerts relevant to the specific question asked."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Query about Kirk Cousins: only Cousins/Mendoza alert should be injected
        resp_cousins = await client.post("/api/chat", json={"message": "Should I pick up Kirk Cousins?"})
        assert resp_cousins.status_code == 200
        data_c = resp_cousins.json()
        assert any("Kirk Cousins" in a or "Mendoza" in a for a in data_c["injury_alerts"])
        assert not any("Baker Mayfield" in a for a in data_c["injury_alerts"])

        # 2. Query about Baker Mayfield: only Mayfield alert should be injected
        resp_mayfield = await client.post("/api/chat", json={"message": "Evaluate Baker Mayfield status and floor"})
        assert resp_mayfield.status_code == 200
        data_m = resp_mayfield.json()
        assert any("Baker Mayfield" in a for a in data_m["injury_alerts"])
        assert not any("Kirk Cousins" in a for a in data_m["injury_alerts"])
