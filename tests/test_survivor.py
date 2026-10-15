import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("GEMINI_API_KEY", "test-mock-key")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.survivor_engine import (
    get_vegas_odds,
    build_survivor_recommendation
)

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.asyncio
async def test_odds_parsing_with_bookmakers():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [
        {
            "home_team": "Team A",
            "away_team": "Team B",
            "commence_time": "2026-09-24T18:00:00Z",
            "bookmakers": [
                {
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [{"name": "Team A", "point": -5.5}]
                        },
                        {
                            "key": "h2h",
                            "outcomes": [{"name": "Team A", "price": -200}, {"name": "Team B", "price": 150}]
                        }
                    ]
                }
            ]
        }
    ]

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.survivor_engine.httpx.AsyncClient", return_value=mock_client):
        with patch("app.survivor_engine.ODDS_API_KEY", "dummy"):
            res = await get_vegas_odds()
            games = res["games"]
            assert len(games) == 1
            assert games[0]["spread_home"] == -5.5
            assert games[0]["moneyline_home"] == -200
            assert games[0]["moneyline_away"] == 150

@pytest.mark.asyncio
async def test_implied_probability_negative_odds():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [
        {
            "home_team": "Team A", "away_team": "Team B", "commence_time": "time",
            "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [{"name": "Team A", "price": -200}]}]}]
        }
    ]

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.survivor_engine.httpx.AsyncClient", return_value=mock_client):
        with patch("app.survivor_engine.ODDS_API_KEY", "dummy"):
            res = await get_vegas_odds()
            games = res["games"]
            assert games[0]["implied_prob_home"] == 0.667

@pytest.mark.asyncio
async def test_implied_probability_positive_odds():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [
        {
            "home_team": "Team A", "away_team": "Team B", "commence_time": "time",
            "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [{"name": "Team B", "price": 150}]}]}]
        }
    ]

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.survivor_engine.httpx.AsyncClient", return_value=mock_client):
        with patch("app.survivor_engine.ODDS_API_KEY", "dummy"):
            res = await get_vegas_odds()
            games = res["games"]
            assert games[0]["implied_prob_away"] == 0.40

@pytest.mark.asyncio
async def test_no_api_key_returns_error_status():
    with patch("app.survivor_engine.ODDS_API_KEY", ""):
        res = await get_vegas_odds()
        assert res["data_source"]["status"] == "error"

@pytest.mark.asyncio
async def test_build_survivor_recommendation_structure():
    with patch("app.survivor_engine.get_survivor_state", new_callable=AsyncMock) as m_state, \
         patch("app.survivor_engine.get_vegas_odds", new_callable=AsyncMock) as m_odds, \
         patch("app.survivor_engine.get_previous_picks", new_callable=AsyncMock) as m_picks, \
         patch("app.survivor_engine.genai.Client") as m_client:
        
        m_state.return_value = {"current_week": 3, "league_name": "Test"}
        m_odds.return_value = {"games": [], "data_source": {"status": "ok"}}
        m_picks.return_value = []
        
        mock_interaction = AsyncMock()
        mock_interaction.output_text = "Mock Recommendation"
        m_client.return_value.interactions.create.return_value = mock_interaction
        
        res = await build_survivor_recommendation("dummy")
        assert "recommendation" in res
        assert "data_sources" in res
        assert res["week"] == 3

def test_survivor_cron_email_addresses():
    from app.survivor_cron import SENDER, RECIPIENT
    assert "pico2014" in SENDER
    assert "pico0102" in RECIPIENT


# ---------------------------------------------------------------------------
# New Comprehensive Tests for Survivor Engine Refactor & Safeguards
# ---------------------------------------------------------------------------

from app.survivor_engine import (
    ODDS_KEY_MISSING_MESSAGE,
    SurvivorPickResult,
    evaluate_survivor_candidates,
    generate_survivor_recommendation,
    probe_survivor_odds_health,
)
from app.survivor_cron import run_survivor_cron, validate_pick_for_email


@pytest.mark.asyncio
async def test_probe_survivor_odds_health_missing_key():
    with patch("os.getenv", return_value=""):
        health = await probe_survivor_odds_health()
        assert health["status"] == "error"
        assert "ODDS_API_KEY is not configured" in health["detail"]


@pytest.mark.asyncio
async def test_api_health_includes_survivor_odds_component():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "components" in data
        assert "survivor_odds" in data["components"]
        assert "status" in data["components"]["survivor_odds"]


@pytest.mark.asyncio
async def test_missing_odds_key_returns_plain_language_error():
    with patch("app.survivor_engine.ODDS_API_KEY", ""):
        res = await generate_survivor_recommendation()
        assert res.is_valid is False
        assert res.status == "error"
        assert res.primary_team == ""
        assert res.recommendation_text == ODDS_KEY_MISSING_MESSAGE
        assert "Survivor Odds Feed Not Connected" in res.recommendation_text
        assert "Why this matters:" in res.recommendation_text


def test_candidate_ranking_strictly_eliminates_previously_picked_teams():
    games = [
        {
            "home": "Baltimore Ravens",  # Burned in prior week!
            "away": "Cleveland Browns",
            "implied_prob_home": 0.88,
            "implied_prob_away": 0.12,
            "spread_home": -9.5,
            "moneyline_home": -450,
            "moneyline_away": 350,
        },
        {
            "home": "Los Angeles Rams",  # Available!
            "away": "Arizona Cardinals",
            "implied_prob_home": 0.85,
            "implied_prob_away": 0.15,
            "spread_home": -8.0,
            "moneyline_home": -400,
            "moneyline_away": 310,
        },
        {
            "home": "San Francisco 49ers",  # Available!
            "away": "Seattle Seahawks",  # Seattle burned in prior week!
            "implied_prob_home": 0.78,
            "implied_prob_away": 0.22,
            "spread_home": -6.5,
            "moneyline_home": -320,
            "moneyline_away": 260,
        },
    ]
    previously_picked = ["BAL", "SEA", "LAC", "BUF"]

    candidates = evaluate_survivor_candidates(games, previously_picked)
    eligible = [c for c in candidates if c.is_eligible]
    ineligible = [c for c in candidates if not c.is_eligible]

    # Ravens and Seahawks must be marked ineligible
    ineligible_teams = {c.abbrev for c in ineligible}
    assert "BAL" in ineligible_teams
    assert "SEA" in ineligible_teams

    # Top eligible candidate must be Rams (0.85 prob), NOT Ravens (0.88 prob)
    assert eligible[0].abbrev == "LAR"
    assert eligible[0].prob == 0.85
    # Second eligible candidate is 49ers
    assert eligible[1].abbrev == "SF"
    assert eligible[1].prob == 0.78


def test_email_safeguard_structural_validations():
    # 1. None result -> rejected
    valid, reason = validate_pick_for_email(None)
    assert not valid

    # 2. Invalid status -> rejected
    res_err = SurvivorPickResult(
        status="error",
        is_valid=False,
        week=5,
        league_name="Fat Crew Survivor",
        primary_team="",
        primary_abbrev="",
        primary_opponent="",
        primary_prob=0.0,
        primary_spread=None,
        primary_ml=None,
        alternate_team="",
        alternate_abbrev="",
        alternate_opponent="",
        alternate_prob=0.0,
        alternate_spread=None,
        alternate_ml=None,
        previously_picked=["SEA"],
        candidates=[],
        recommendation_text="Error occurred",
        recommendation_html="<p>Error</p>",
        data_sources={},
        error="Odds unavailable",
    )
    valid, reason = validate_pick_for_email(res_err)
    assert not valid
    assert "invalid" in reason.lower()

    # 3. Already picked team as primary -> rejected
    res_duplicate = SurvivorPickResult(
        status="ok",
        is_valid=True,
        week=5,
        league_name="Fat Crew Survivor",
        primary_team="Seattle Seahawks",
        primary_abbrev="SEA",
        primary_opponent="Cardinals",
        primary_prob=0.80,
        primary_spread=-7.0,
        primary_ml=-350,
        alternate_team="Rams",
        alternate_abbrev="LAR",
        alternate_opponent="49ers",
        alternate_prob=0.75,
        alternate_spread=-6.0,
        alternate_ml=-300,
        previously_picked=["SEA", "LAC", "BUF", "BAL"],
        candidates=[],
        recommendation_text="Pick Seattle",
        recommendation_html="<p>Pick Seattle</p>",
        data_sources={},
    )
    valid, reason = validate_pick_for_email(res_duplicate)
    assert not valid
    assert "already picked" in reason.lower()

    # 4. Failure token in text -> rejected
    res_token = SurvivorPickResult(
        status="ok",
        is_valid=True,
        week=5,
        league_name="Fat Crew Survivor",
        primary_team="Los Angeles Rams",
        primary_abbrev="LAR",
        primary_opponent="Cardinals",
        primary_prob=0.85,
        primary_spread=-8.0,
        primary_ml=-400,
        alternate_team="49ers",
        alternate_abbrev="SF",
        alternate_opponent="Browns",
        alternate_prob=0.75,
        alternate_spread=-6.0,
        alternate_ml=-300,
        previously_picked=["SEA"],
        candidates=[],
        recommendation_text="⚠️ Survivor analysis temporarily unavailable: 429 quota exceeded",
        recommendation_html="<p>Picks</p>",
        data_sources={},
    )
    valid, reason = validate_pick_for_email(res_token)
    assert not valid
    assert "token" in reason.lower()

    # 5. Low win probability (< 0.50) -> rejected
    res_underdog = SurvivorPickResult(
        status="ok",
        is_valid=True,
        week=5,
        league_name="Fat Crew Survivor",
        primary_team="Carolina Panthers",
        primary_abbrev="CAR",
        primary_opponent="Chiefs",
        primary_prob=0.35,
        primary_spread=+7.0,
        primary_ml=+250,
        alternate_team="Rams",
        alternate_abbrev="LAR",
        alternate_opponent="Cardinals",
        alternate_prob=0.30,
        alternate_spread=+8.0,
        alternate_ml=+300,
        previously_picked=[],
        candidates=[],
        recommendation_text="Pick Panthers",
        recommendation_html="<p>Pick Panthers</p>",
        data_sources={},
    )
    valid, reason = validate_pick_for_email(res_underdog)
    assert not valid
    assert "win probability" in reason.lower()


@pytest.mark.asyncio
async def test_cron_blocks_email_when_odds_key_missing(capsys):
    with patch("app.survivor_cron.ODDS_API_KEY", ""):
        with patch("os.getenv", side_effect=lambda k, default="": "" if k == "ODDS_API_KEY" else default):
            sent = await run_survivor_cron(dry_run=True)
            assert sent is False
            captured = capsys.readouterr()
            # Ensure no email was simulated or sent
            assert "--- EMAIL DRY RUN ---" not in captured.out


@pytest.mark.asyncio
async def test_unified_pick_agreement_between_chat_and_email():
    """Confirms both chatbot and email cron receive identical primary and alternate picks."""
    games = [
        {
            "home": "Los Angeles Rams",
            "away": "Arizona Cardinals",
            "start": "2026-10-11T13:00:00Z",
            "spread_home": -8.0,
            "moneyline_home": -400,
            "moneyline_away": 320,
            "implied_prob_home": 0.80,
            "implied_prob_away": 0.20,
        },
        {
            "home": "San Francisco 49ers",
            "away": "Washington Commanders",
            "start": "2026-10-11T16:25:00Z",
            "spread_home": -6.5,
            "moneyline_home": -300,
            "moneyline_away": 240,
            "implied_prob_home": 0.75,
            "implied_prob_away": 0.25,
        },
    ]
    mock_odds = {"games": games, "data_source": {"status": "ok"}}
    mock_state = {"current_week": 5, "league_name": "Fat Crew Survivor"}
    mock_roster_data = (["SEA", "LAC", "BUF", "BAL"], {"total_rosters": 63})

    mock_espn = [
        {"name": "Arizona at LA Rams", "short_name": "ARI @ LAR", "teams": ["LAR", "ARI"], "state": "pre"},
        {"name": "Washington at San Francisco", "short_name": "WAS @ SF", "teams": ["SF", "WAS"], "state": "pre"},
    ]

    with patch("app.survivor_engine.get_survivor_state", new_callable=AsyncMock) as m_state, \
         patch("app.survivor_engine.get_survivor_pool_rosters", new_callable=AsyncMock) as m_roster, \
         patch("app.survivor_engine.get_vegas_odds", new_callable=AsyncMock) as m_odds, \
         patch("app.survivor_engine.fetch_espn_scoreboard", new_callable=AsyncMock) as m_espn, \
         patch("app.survivor_engine.ask_gemini", new_callable=AsyncMock) as m_llm:

        m_state.return_value = mock_state
        m_roster.return_value = mock_roster_data
        m_odds.return_value = mock_odds
        m_espn.return_value = mock_espn
        m_llm.return_value = "Detailed strategic analysis."

        # 1. Chatbot path
        chat_result = await generate_survivor_recommendation(query="Who should I pick in survivor?")

        # 2. Email cron path
        email_result = await generate_survivor_recommendation(query=None)

        # Confirm 100% agreement between toggle and email
        assert chat_result.is_valid is True
        assert email_result.is_valid is True
        assert chat_result.primary_team == email_result.primary_team == "Los Angeles Rams"
        assert chat_result.primary_prob == email_result.primary_prob == 0.80
        assert chat_result.alternate_team == email_result.alternate_team == "San Francisco 49ers"
        assert chat_result.alternate_prob == email_result.alternate_prob == 0.75
        assert chat_result.week == email_result.week == 5

        # Confirm validation passes for email
        valid, _ = validate_pick_for_email(email_result)
        assert valid is True


@pytest.mark.asyncio
async def test_deterministic_refusal_consistency():
    """Confirms that when ODDS_API_KEY is unset, the refusal is 100% deterministic and never bypassed."""
    with patch("app.survivor_engine.ODDS_API_KEY", ""):
        with patch("app.survivor_engine.ask_gemini", new_callable=AsyncMock) as m_llm:
            for i in range(5):
                res = await generate_survivor_recommendation(query=f"Pick request #{i}")
                assert res.is_valid is False
                assert res.status == "error"
                assert res.primary_team == ""
                assert res.primary_prob == 0.0
                assert res.recommendation_text == ODDS_KEY_MISSING_MESSAGE
            # Verify LLM was NEVER called
            assert m_llm.call_count == 0


@pytest.mark.asyncio
async def test_future_week_and_completed_matchups_strictly_discarded():
    """
    Guarantees that games from future weeks (e.g. Week 6 Rams vs Arizona, 49ers vs Washington)
    and completed games (e.g. Thursday night TB @ DAL) are never recommended for Week 5.
    """
    raw_odds = [
        # WEEK 6 GAMES (Should be discarded)
        {"home": "Los Angeles Rams", "away": "Arizona Cardinals", "start": "2026-10-18T20:05:00Z", "spread_home": -10.5, "implied_prob_home": 0.86, "moneyline_home": -600},
        {"home": "San Francisco 49ers", "away": "Washington Commanders", "start": "2026-10-20T00:15:00Z", "spread_home": -7.5, "implied_prob_home": 0.81, "moneyline_home": -400},
        # WEEK 5 COMPLETED GAME (Should be discarded)
        {"home": "Dallas Cowboys", "away": "Tampa Bay Buccaneers", "start": "2026-10-08T20:15:00Z", "spread_home": -3.0, "implied_prob_home": 0.60, "moneyline_home": -150},
        # REAL WEEK 5 UPCOMING GAMES (Eligible)
        {"home": "Jacksonville Jaguars", "away": "Philadelphia Eagles", "start": "2026-10-11T13:30:00Z", "spread_home": -7.5, "implied_prob_home": 0.798, "moneyline_home": -395},
        {"home": "Tennessee Titans", "away": "Houston Texans", "start": "2026-10-11T17:00:00Z", "spread_home": 7.0, "implied_prob_away": 0.789, "moneyline_away": -375},
    ]

    mock_espn = [
        {"name": "TB at DAL", "short_name": "TB @ DAL", "teams": ["DAL", "TB"], "state": "post"},  # completed
        {"name": "PHI at JAX", "short_name": "PHI @ JAX", "teams": ["JAX", "PHI"], "state": "pre"},
        {"name": "HOU at TEN", "short_name": "HOU @ TEN", "teams": ["TEN", "HOU"], "state": "pre"},
    ]

    with patch("app.survivor_engine.get_survivor_state", new_callable=AsyncMock) as m_state, \
         patch("app.survivor_engine.get_survivor_pool_rosters", new_callable=AsyncMock) as m_roster, \
         patch("app.survivor_engine.get_vegas_odds", new_callable=AsyncMock) as m_odds, \
         patch("app.survivor_engine.fetch_espn_scoreboard", new_callable=AsyncMock) as m_espn, \
         patch("app.survivor_engine.ask_gemini", new_callable=AsyncMock) as m_llm:

        m_state.return_value = {"current_week": 5, "league_name": "Fat Crew Survivor"}
        m_roster.return_value = (["SEA", "LAC", "BUF", "BAL"], {"total_rosters": 63})
        m_odds.return_value = {"games": raw_odds, "data_source": {"status": "ok"}}
        m_espn.return_value = mock_espn
        m_llm.return_value = "Detailed breakdown."

        result = await generate_survivor_recommendation()
        assert result.is_valid is True
        # Must NOT pick Week 6 Rams or 49ers
        assert result.primary_team != "Los Angeles Rams"
        assert result.primary_team != "San Francisco 49ers"
        assert result.primary_opponent != "Arizona Cardinals"
        assert result.primary_opponent != "Washington Commanders"
        # Must pick real Week 5 favorite
        assert result.primary_team == "Jacksonville Jaguars"
        assert result.primary_opponent == "Philadelphia Eagles"
        assert result.primary_prob == 0.798
        assert result.alternate_team == "Houston Texans"


# ---------------------------------------------------------------------------
# V2 Unified Survivor Engine & Fail-Closed Gate Tests
# ---------------------------------------------------------------------------
from core.config import SURVIVOR_FAIL_CLOSED_TEMPLATE
from core.provenance import build_provenance_record
from core.schemas import NFLGameOdds, WorldStateSnapshot
from core.survivor import (
    SurvivorEmailDispatcher,
    SurvivorEngine,
    assert_no_nfl_team_leaked,
)


def _build_v2_snapshot(odds=None, odds_status="ok") -> WorldStateSnapshot:
    prov = [
        build_provenance_record("the_odds_api_nfl", "odds_api", "odds", status=odds_status, record_count=len(odds or []))
    ]
    return WorldStateSnapshot(
        snapshot_id="snap_v2_survivor",
        created_at_utc="2026-10-14T00:00:00Z",
        nfl_season="2026",
        nfl_week=6,
        current_week_odds=odds or [],
        provenance=prov,
    )


def test_v2_survivor_deterministic_fail_closed_refusal():
    # 1. When odds are missing_key, evaluate_week must short circuit pre-LLM
    snapshot = _build_v2_snapshot(odds=[], odds_status="missing_key")
    res1 = SurvivorEngine.evaluate_week(snapshot, used_teams=["KC", "BUF"])
    res2 = SurvivorEngine.evaluate_week(snapshot, used_teams=["KC", "BUF"])

    assert res1.status == "fail_closed_no_odds"
    assert res1.recommended_team is None
    expected_refusal = SURVIVOR_FAIL_CLOSED_TEMPLATE.format(status="missing_key")
    assert res1.reasoning_markdown == expected_refusal
    # Deterministic byte-for-byte identity
    assert res1.reasoning_markdown == res2.reasoning_markdown


def test_v2_survivor_verified_pick_path():
    odds = [
        NFLGameOdds(
            game_id="g1",
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
        NFLGameOdds(
            game_id="g2",
            nfl_week=6,
            commence_time_utc="2026-10-18T17:00:00Z",
            home_team="KC",
            away_team="LV",
            favorite_team="KC",
            underdog_team="LV",
            spread_points=-9.0,
            favorite_moneyline=-450,
            implied_win_prob=0.81,
        ),
    ]
    snapshot = _build_v2_snapshot(odds=odds, odds_status="ok")
    # KC already used, so DET should be recommended
    result = SurvivorEngine.evaluate_week(snapshot, used_teams=["KC"])
    assert result.status == "verified_pick"
    assert result.recommended_team == "DET"
    assert result.opponent_team == "GB"
    assert result.spread == -7.5


def test_v2_survivor_email_dispatcher_safeguard():
    # Missing odds must abort email sending and pass zero-team-leak assert
    snapshot = _build_v2_snapshot(odds=[], odds_status="failed")
    dispatcher = SurvivorEmailDispatcher()
    email_res = dispatcher.run(snapshot, used_teams=["KC"])

    assert email_res["email_sent"] is False
    assert email_res["recommended_team"] is None
    assert email_res["abort_reason"] == "fail_closed_no_odds"

    # Zero-team-leak check
    assert_no_nfl_team_leaked(email_res["detail"])

    # Regex should catch actual team leaks
    with pytest.raises(AssertionError):
        assert_no_nfl_team_leaked("We recommend picking the Detroit Lions this week.")

    with pytest.raises(AssertionError):
        assert_no_nfl_team_leaked("Safe pick: DET favored by 7.")

