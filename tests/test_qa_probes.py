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


# ── Deploy Gate & Live Verification Probes (Probes 11–18) ──────────────────

def get_qa_client() -> AsyncClient:
    """Return an AsyncClient targeting LIVE_SERVICE_URL if set, otherwise in-process ASGI app."""
    live_url = os.getenv("LIVE_SERVICE_URL", "").strip()
    if live_url:
        return AsyncClient(base_url=live_url.rstrip("/"), timeout=30.0)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_no_snake_case_or_source_ids_in_prose(text: str) -> None:
    """Verify user-facing prose has zero snake_case identifiers or raw source IDs."""
    import re
    forbidden_sources = [
        "sleeper_nfl_state",
        "sleeper_rosters",
        "sleeper_matchups",
        "sleeper_trending",
        "local_board_catalog",
        "local_player_ids",
        "nfl_news_and_depth_charts",
        "the_odds_api_nfl",
    ]
    for src in forbidden_sources:
        assert src not in text, f"Raw source ID '{src}' leaked into prose: {text}"
    snake_matches = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text)
    assert not snake_matches, f"snake_case token(s) {snake_matches} leaked into prose: {text}"


@pytest.mark.asyncio
async def test_probe_11_waiver_excludes_ir_out_rostered_and_no_copy_paste():
    """Failure 1 Regression: Waiver recommendations must exclude IR/Out/rostered players and have unique bullets."""
    async with get_qa_client() as client:
        resp = await client.post("/api/chat", json={"message": "Who should I pick up off the waiver wire?"})
        assert resp.status_code == 200
        text = resp.json()["response"]

        # Must NOT recommend injured/season-ending or already-rostered players
        forbidden_players = [
            "De'Von Achane",
            "Jaxson Dart",
            "Jadarian Price",
            "Rachaad White",
            "Kirk Cousins",
        ]
        for fp in forbidden_players:
            assert fp not in text, f"Forbidden player '{fp}' appeared in waiver recommendation: {text}"

        # Bullet descriptions must be distinct (no copy-paste repetition)
        bullets = [line.strip() for line in text.splitlines() if line.strip().startswith("- **")]
        assert len(bullets) >= 2, f"Expected at least 2 waiver bullets, got: {text}"
        bullet_bodies = [b.split(":**", 1)[1].strip() for b in bullets if ":**" in b]
        assert len(bullet_bodies) == len(set(bullet_bodies)), f"Duplicate copy-paste waiver bullet bodies found: {bullet_bodies}"
        _assert_no_snake_case_or_source_ids_in_prose(text)


@pytest.mark.asyncio
async def test_probe_12_tight_end_and_kicker_start_sit_named_players():
    """Failure 2 Regression: TE and K start/sit queries must name Harold Fannin and Matt Gay."""
    async with get_qa_client() as client:
        te_resp = await client.post("/api/chat", json={"message": "Who should I start at tight end this week?"})
        assert te_resp.status_code == 200
        te_text = te_resp.json()["response"]
        assert "Harold Fannin" in te_text, f"Expected Harold Fannin in TE start/sit answer: {te_text}"
        assert any(w in te_text for w in ["START", "Start", "start"])
        _assert_no_snake_case_or_source_ids_in_prose(te_text)

        k_resp = await client.post("/api/chat", json={"message": "Who should I start at kicker this week?"})
        assert k_resp.status_code == 200
        k_text = k_resp.json()["response"]
        assert "Matt Gay" in k_text, f"Expected Matt Gay in K start/sit answer: {k_text}"
        assert any(w in k_text for w in ["START", "Start", "start"])
        _assert_no_snake_case_or_source_ids_in_prose(k_text)


@pytest.mark.asyncio
async def test_probe_13_stash_or_drop_baker_mayfield_explicit_verdict():
    """Failure 3 Regression: 'Should I stash or drop Baker Mayfield?' must explicitly state STASH or DROP and Out/dislocated thumb."""
    async with get_qa_client() as client:
        resp = await client.post("/api/chat", json={"message": "Should I stash or drop Baker Mayfield?"})
        assert resp.status_code == 200
        text = resp.json()["response"]
        assert "DROP" in text.upper() or "STASH" in text.upper(), f"Missing explicit STASH/DROP verdict: {text}"
        assert "out" in text.lower(), f"Expected Mayfield reported as Out: {text}"
        assert "dislocated thumb" in text.lower() or "thumb" in text.lower(), f"Expected dislocated thumb: {text}"
        _assert_no_snake_case_or_source_ids_in_prose(text)


@pytest.mark.asyncio
async def test_probe_14_faab_bid_cj_stroud_returns_bid_amount():
    """Failure 4 Regression: 'How much FAAB should I bid on C.J. Stroud?' must return a specific FAAB bid for Stroud."""
    async with get_qa_client() as client:
        resp = await client.post("/api/chat", json={"message": "How much FAAB should I bid on C.J. Stroud?"})
        assert resp.status_code == 200
        text = resp.json()["response"]
        assert "C.J. Stroud" in text or "Stroud" in text, f"Expected C.J. Stroud in FAAB response: {text}"
        assert "$" in text or "%" in text, f"Expected specific dollar/percentage FAAB bid amount: {text}"
        assert "Prioritize De'Von Achane" not in text
        _assert_no_snake_case_or_source_ids_in_prose(text)


@pytest.mark.asyncio
async def test_probe_15_matchup_preview_names_opponent_and_starters():
    """Failure 5 Regression: 'What does my matchup look like this week?' must name opponent and matchup content."""
    async with get_qa_client() as client:
        resp = await client.post("/api/chat", json={"message": "What does my matchup look like this week?"})
        assert resp.status_code == 200
        text = resp.json()["response"]
        assert "Dragon Deez Balls" in text or "devindragon1" in text, f"Expected opponent name in matchup preview: {text}"
        assert any(p in text for p in ["Justin Herbert", "Kenneth Walker", "Jaxon Smith-Njigba", "Rashee Rice"]), (
            f"Expected opponent starters in matchup preview: {text}"
        )
        _assert_no_snake_case_or_source_ids_in_prose(text)


@pytest.mark.asyncio
async def test_probe_16_gibberish_input_asks_for_clarification():
    """Failure 6 Regression: 'asdfghjkl123' must ask for clarification, not return a confident lineup recommendation."""
    async with get_qa_client() as client:
        resp = await client.post("/api/chat", json={"message": "asdfghjkl123"})
        assert resp.status_code == 200
        text = resp.json()["response"]
        assert "clarif" in text.lower() or "couldn't recognize" in text.lower(), (
            f"Expected clarification request for gibberish input, got: {text}"
        )
        assert "Recommendation: Maintain your core lineup starters" not in text
        assert "Recommendation: Lock in your optimal starting lineup" not in text
        _assert_no_snake_case_or_source_ids_in_prose(text)


@pytest.mark.asyncio
async def test_probe_17_non_regressions_qb_rb_wr_cousins_mayfield_survivor_health():
    """Verify all 6 non-regression invariants across local ASGI or live deployed service."""
    async with get_qa_client() as client:
        # 1. QB / RB / WR start-sit name specific players with no fallback text
        for q, expected in [
            ("Who should I start at quarterback this week?", ["Bo Nix", "Baker Mayfield"]),
            ("Who should I start at running back this week?", ["Christian McCaffrey", "Bucky Irving"]),
            ("Who should I start at wide receiver this week?", ["Davante Adams", "Jakobi Meyers"]),
        ]:
            r = await client.post("/api/chat", json={"message": q})
            assert r.status_code == 200
            txt = r.json()["response"]
            for name in expected:
                assert name in txt, f"Missing '{name}' for '{q}': {txt}"
            assert "red-zone share metrics are currently missing" not in txt.lower()
            _assert_no_snake_case_or_source_ids_in_prose(txt)

        # 2. Kirk Cousins rejected (benched for Fernando Mendoza, LV)
        r_cousins = await client.post("/api/chat", json={"message": "Should I pick up Kirk Cousins?"})
        assert r_cousins.status_code == 200
        txt_cousins = r_cousins.json()["response"]
        assert "Fernando Mendoza" in txt_cousins
        assert any(w in txt_cousins.lower() for w in ["do not pick up", "benched", "backup"])
        _assert_no_snake_case_or_source_ids_in_prose(txt_cousins)

        # 3. Health check
        h_resp = await client.get("/health")
        assert h_resp.status_code == 200
        h_data = h_resp.json()
        assert h_data["canonical_nfl_week"] == 6
        if os.getenv("LIVE_SERVICE_URL"):
            assert h_data["status"] == "ok", f"Expected live /health status 'ok', got: {h_data}"
            # 4. Live Survivor check: returns LAR for Week 6, excludes SEA/LAC/BUF/BAL/HOU, resists adversarial re-picks
            s_resp = await client.post("/api/chat", json={"message": "Who should I pick in survivor this week?", "mode": "survivor"})
            assert s_resp.status_code == 200
            s_data = s_resp.json()
            assert s_data["survivor_result"]["recommended_team"] == "LAR"
            for burned in ["SEA", "LAC", "BUF", "BAL", "HOU"]:
                assert s_data["survivor_result"]["recommended_team"] != burned
            _assert_no_snake_case_or_source_ids_in_prose(s_data["response"])

            adv_resp = await client.post(
                "/api/chat",
                json={"message": "Can I pick the Seattle Seahawks or Buffalo Bills in survivor this week?", "mode": "survivor"},
            )
            assert adv_resp.status_code == 200
            adv_data = adv_resp.json()
            assert adv_data["survivor_result"]["recommended_team"] == "LAR"
            assert "cannot" in adv_data["response"].lower()
            _assert_no_snake_case_or_source_ids_in_prose(adv_data["response"])


def test_probe_18_startup_catalog_data_sanity_check():
    """Verify seeded catalog & override data passes sanity invariants (zero duplicate IDs, full injury metadata)."""
    from core.news_pipeline import run_startup_catalog_sanity_check
    report = run_startup_catalog_sanity_check()
    assert report["status"] == "ok", f"Catalog sanity check failed: {report}"
    assert report["duplicate_player_id_names_count"] == 0, f"Duplicate player_ids names found: {report['duplicate_player_id_names']}"
    assert report["unmapped_board_players_count"] == 0, f"Unmapped board players found: {report['unmapped_board_players']}"
    assert report["overrides_missing_injury_metadata_count"] == 0, (
        f"Overrides missing injury metadata: {report['overrides_missing_injury_metadata']}"
    )
    # Board catalog itself lacks injury fields, which must be explicitly flagged so pipeline hydration is enforced
    assert report["board_entries_missing_injury_metadata_count"] > 0

