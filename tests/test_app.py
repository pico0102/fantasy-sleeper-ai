import os
import sys
import time
import pytest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("GEMINI_API_KEY", "test-mock-key")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from httpx import AsyncClient, ASGITransport
from app.main import app, resolve_player_name, build_context, _cache_set, _cache_get, _cache, PLAYER_IDS

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == "3.0.0"
    assert "player_cache_loaded" in data

@pytest.mark.asyncio
async def test_ui_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/")
    assert response.status_code == 200
    assert "v3.0" in response.text

def test_player_id_resolution_known():
    with patch("app.main.get_all_players_sync") as mock_get_all:
        mock_get_all.return_value = {
            "11563": {"first_name": "Bo", "last_name": "Nix", "team": "DEN", "position": "QB"}
        }
        # Clear out any predefined in PLAYER_IDS for test reliability
        if "11563" in PLAYER_IDS:
            del PLAYER_IDS["11563"]
        name = resolve_player_name("11563")
        assert "Bo Nix" in name

def test_player_id_resolution_unknown_no_crash():
    with patch("app.main.get_all_players_sync") as mock_get_all:
        mock_get_all.return_value = {}
        name = resolve_player_name("unknown-999")
        assert "unknown-999" in name or "Unknown" in name

@pytest.fixture
def mock_external_calls():
    with patch("app.main.fetch_nfl_state", new_callable=AsyncMock) as m_state, \
         patch("app.main.fetch_all_rosters", new_callable=AsyncMock) as m_rosters, \
         patch("app.main.fetch_league_users", new_callable=AsyncMock) as m_users, \
         patch("app.main.get_all_players", new_callable=AsyncMock) as m_players, \
         patch("app.main.fetch_matchups", new_callable=AsyncMock) as m_matchups, \
         patch("app.main.fetch_transactions", new_callable=AsyncMock) as m_tx, \
         patch("app.main.fetch_trending_adds", new_callable=AsyncMock) as m_trend, \
         patch("app.main.fetch_matchup_history", new_callable=AsyncMock) as m_hist, \
         patch("app.main._fetch_espn_schedule_async", new_callable=AsyncMock) as m_espn:
        
        m_state.return_value = {"week": 3, "season": "2026", "season_type": "regular"}
        m_rosters.return_value = []
        m_users.return_value = {}
        m_players.return_value = {}
        m_matchups.return_value = []
        m_tx.return_value = []
        m_trend.return_value = []
        m_hist.return_value = {}
        m_espn.return_value = ""
        
        yield {
            "state": m_state, "rosters": m_rosters, "users": m_users, "players": m_players,
            "matchups": m_matchups, "tx": m_tx, "trend": m_trend, "hist": m_hist, "espn": m_espn
        }

@pytest.mark.asyncio
async def test_build_context_structure(mock_external_calls):
    ctx_str, ds_dict = await build_context("How is my team?")
    assert isinstance(ctx_str, str)
    assert isinstance(ds_dict, dict)
    assert "NFL State" in ctx_str
    assert "Season: 2026" in ctx_str

@pytest.mark.asyncio
async def test_build_context_data_sources(mock_external_calls):
    ctx_str, ds_dict = await build_context("test")
    assert "nfl_state" in ds_dict
    assert ds_dict["nfl_state"]["status"] == "ok"

@pytest.mark.asyncio
async def test_build_context_ir_players_excluded(mock_external_calls):
    mock_external_calls["players"].return_value = {
        "100": {"first_name": "Bob", "last_name": "IR", "position": "WR", "injury_status": "IR"},
        "101": {"first_name": "Alice", "last_name": "Active", "position": "WR", "injury_status": ""}
    }
    mock_external_calls["rosters"].return_value = [
        {"roster_id": 4, "owner_id": "1", "players": ["100", "101"]}
    ]
    with patch("app.main.get_blended_projection") as m_blend:
        m_blend.side_effect = lambda name, **k: {"pts": 15.0} if "Active" in name else {"pts": 10.0}
        ctx_str, ds_dict = await build_context("test IR")
        
        # Check that IR players show in IR section
        assert "IR / Injured Reserve" in ctx_str
        assert "NOT eligible for lineup" in ctx_str

@pytest.mark.asyncio
async def test_chat_endpoint_mocked(mock_external_calls):
    with patch("app.main.ask_gemini", new_callable=AsyncMock) as m_ask:
        m_ask.return_value = "This is a mock answer."
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/api/chat", json={"query": "Who should I start?"})
        assert response.status_code == 200
        data = response.json()
        assert data["answer"] == "This is a mock answer."
        assert "data_sources" in data

@pytest.mark.asyncio
async def test_chat_endpoint_empty_query():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/api/chat", json={"query": ""})
    assert response.status_code == 400

@pytest.mark.asyncio
async def test_chat_response_has_data_sources(mock_external_calls):
    with patch("app.main.ask_gemini", new_callable=AsyncMock) as m_ask:
        m_ask.return_value = "Answer"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/api/chat", json={"query": "hello"})
        data = response.json()
        assert "data_sources" in data
        assert isinstance(data["data_sources"], dict)

def test_cache_ttl():
    _cache.clear()
    _cache_set("test_key", "value1")
    assert _cache_get("test_key") == "value1"
    
    # Manually modify ts to simulate expiry
    _cache["test_key"]["ts"] = time.time() - 301 # 5 min = 300s
    assert _cache_get("test_key") is None
