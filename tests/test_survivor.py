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
