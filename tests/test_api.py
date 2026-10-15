"""FastAPI endpoint integration tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_health_endpoint_subsystems():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()

        assert "status" in data
        assert "canonical_nfl_week" in data
        assert "subsystems" in data

        subs = data["subsystems"]
        required_subs = {
            "player_ids_catalog",
            "board_catalog",
            "sleeper_api",
            "news_depth_chart_feed",
            "odds_api",
            "gemini_reasoner",
        }
        assert required_subs.issubset(set(subs.keys()))
        assert subs["board_catalog"]["audit_passed"] is True


@pytest.mark.asyncio
async def test_api_world_state_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/world-state")
        assert resp.status_code == 200
        data = resp.json()

        assert "snapshot_id" in data
        assert "nfl_week" in data
        assert "provenance" in data
        assert len(data["provenance"]) >= 6


@pytest.mark.asyncio
async def test_api_survivor_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/survivor", json={"used_teams": ["KC", "BUF"]})
        assert resp.status_code == 200
        data = resp.json()

        assert "status" in data
        assert "nfl_week" in data
        assert "reasoning_markdown" in data


@pytest.mark.asyncio
async def test_api_survivor_email_preview():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/survivor/email-preview", json={"used_teams": ["KC"]})
        assert resp.status_code == 200
        data = resp.json()
        assert "email_sent" in data
        assert "nfl_week" in data


@pytest.mark.asyncio
async def test_index_ui_html():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "Tony Romo's DUI Mugshot" in html
        assert "Survivor" in html
        assert "Fantasy GM" in html
