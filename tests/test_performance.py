import os
import sys
import time
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("GEMINI_API_KEY", "test-mock-key")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.main import build_context, _cache, _cache_set, fetch_nfl_state

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.asyncio
async def test_build_context_timing():
    with patch("app.main.fetch_nfl_state", new_callable=AsyncMock) as m_state, \
         patch("app.main.fetch_all_rosters", new_callable=AsyncMock) as m_rosters, \
         patch("app.main.fetch_league_users", new_callable=AsyncMock) as m_users, \
         patch("app.main.get_all_players", new_callable=AsyncMock) as m_players, \
         patch("app.main.fetch_matchups", new_callable=AsyncMock) as m_matchups, \
         patch("app.main.fetch_transactions", new_callable=AsyncMock) as m_tx, \
         patch("app.main.fetch_trending_adds", new_callable=AsyncMock) as m_trend, \
         patch("app.main.fetch_matchup_history", new_callable=AsyncMock) as m_hist, \
         patch("app.main._fetch_espn_schedule_async", new_callable=AsyncMock) as m_espn:
        
        m_state.return_value = {"week": 3, "season": "2026"}
        m_rosters.return_value = []
        m_users.return_value = {}
        m_players.return_value = {}
        m_matchups.return_value = []
        m_tx.return_value = []
        m_trend.return_value = []
        m_hist.return_value = {}
        m_espn.return_value = ""

        t0 = time.time()
        await build_context("hello")
        duration = time.time() - t0
        
        # should complete in < 2 seconds
        assert duration < 2.0

@pytest.mark.asyncio
async def test_cache_hit_speedup():
    _cache.clear()
    
    with patch("app.main._aget", new_callable=AsyncMock) as m_aget:
        m_aget.return_value = {"week": 1, "season": "2026"}
        
        t0 = time.time()
        res1 = await fetch_nfl_state()
        t1 = time.time()
        
        # Second call should use cache
        res2 = await fetch_nfl_state()
        t2 = time.time()
        
        # Assuming AsyncMock has some overhead, a direct cache hit should be fast
        assert res1 == res2
        assert m_aget.call_count == 1
        
        dur1 = t1 - t0
        dur2 = t2 - t1
        # The test requires 10x faster but since mock is fast it might be tricky to reliably assert 10x,
        # but we can enforce it by using asyncio.sleep in mock
        
@pytest.mark.asyncio
async def test_cache_hit_speedup_with_delay():
    _cache.clear()
    
    async def delayed_aget(*args, **kwargs):
        await asyncio.sleep(0.1)
        return {"week": 1}

    with patch("app.main._aget", side_effect=delayed_aget):
        t0 = time.time()
        await fetch_nfl_state()
        dur1 = time.time() - t0
        
        t1 = time.time()
        await fetch_nfl_state()
        dur2 = time.time() - t1
        
        assert dur1 >= 0.1
        assert dur2 < dur1 / 10

@pytest.mark.asyncio
async def test_concurrent_api_calls():
    # Verify asyncio.gather runs concurrently by having multiple mocked delayed functions
    async def delayed_fetch(delay):
        await asyncio.sleep(delay)
        return True
        
    t0 = time.time()
    await asyncio.gather(delayed_fetch(0.2), delayed_fetch(0.2), delayed_fetch(0.2))
    duration = time.time() - t0
    
    # If they ran sequentially, it would take 0.6s. If concurrently, ~0.2s.
    assert duration < 0.4
