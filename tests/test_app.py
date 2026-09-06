"""
Automated test suite for the Fantasy Football AI assistant.
Run with:  pytest -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# Ensure GEMINI_API_KEY is set before app/main.py is imported (it reads the
# env var at module level, so we must patch os.environ BEFORE the import).
# ---------------------------------------------------------------------------
os.environ.setdefault("GEMINI_API_KEY", "test-mock-key")
os.environ.setdefault("SLEEPER_LEAGUE_ID", "1389359956657967104")
os.environ.setdefault("SLEEPER_USER_ID", "pinec0ne")

# Make sure the repo root is on sys.path so `app.main` is importable when
# pytest is run from any directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402 (after sys.path fix)

from app.main import app, resolve_player_name, build_context, LEAGUE_ID  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test client
# ---------------------------------------------------------------------------
client = TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. Health check
# ===========================================================================

def test_health_check():
    """GET /health returns 200 and the correct league ID."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["league_id"] == "1389359956657967104", (
        f"Expected league_id '1389359956657967104', got {body['league_id']!r}"
    )
    assert "team" in body


# ===========================================================================
# 2. UI endpoint
# ===========================================================================

def test_ui_endpoint():
    """GET / serves a 200 HTML page containing the chat interface."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text

    # Key UI elements that must be present
    assert "Tony Romo" in body, \
        "Page title / branding not found in HTML"
    assert "/api/waivers" in body, "Waivers API link missing"
    assert "/api/gm/overview" in body, "GM API link missing"


# ===========================================================================
# 3. Player ID resolution
# ===========================================================================

def test_player_id_resolution_known():
    """Known player ID 11563 resolves to 'Bo Nix' from bundled player_ids.json."""
    name = resolve_player_name("11563")
    assert "Bo Nix" in name, f"Expected 'Bo Nix', got {name!r}"


def test_player_id_resolution_known_irving():
    """Known player ID 11584 resolves to 'Bucky Irving'."""
    name = resolve_player_name("11584")
    assert "Bucky Irving" in name, f"Expected 'Bucky Irving', got {name!r}"


def test_player_id_resolution_unknown_no_crash():
    """Unknown player ID falls back gracefully — no unhandled exception."""
    # Patch _get so we don't hit the real Sleeper API in unit tests
    with patch("app.main._get", side_effect=Exception("network blocked")):
        name = resolve_player_name("FAKE_ID_99999")
    # Must return a string, never raise
    assert isinstance(name, str)
    assert "Unknown" in name or "FAKE_ID_99999" in name, \
        f"Unexpected fallback string: {name!r}"


# ===========================================================================
# 4. build_context structure (Sleeper API mocked)
# ===========================================================================

_MOCK_STATE = {"week": 1, "season": "2026", "season_type": "regular"}

_MOCK_ROSTERS = [
    {
        "roster_id": 4,
        "players": ["11563", "11584", "8228"],  # Bo Nix, Bucky Irving, Jaylen Warren
        "starters": ["11563", "11584"],
        "settings": {"wins": 0, "losses": 0},
    },
    {
        "roster_id": 7,
        "players": ["4034"],  # Christian McCaffrey (opponent)
        "starters": ["4034"],
        "settings": {"wins": 0, "losses": 0},
    },
]

_MOCK_MATCHUPS = [
    {"roster_id": 4, "matchup_id": 1, "points": 0.0, "starters": ["11563", "11584"]},
    {"roster_id": 7, "matchup_id": 1, "points": 0.0, "starters": ["4034"]},
]

_MOCK_TRENDING = [
    {"player_id": "TREND_1", "count": 50000},
    {"player_id": "TREND_2", "count": 30000},
]


def _mock_get(url: str, timeout: int = 10):
    """Route mock _get calls to appropriate fixture data by URL substring."""
    if "/state/nfl" in url:
        return _MOCK_STATE
    if "/rosters" in url:
        return _MOCK_ROSTERS
    if "/matchups/" in url:
        return _MOCK_MATCHUPS
    if "/trending/add" in url:
        return _MOCK_TRENDING
    if "/transactions/" in url:
        return []
    # Single-player fallback for unknown IDs in trending list
    return {"first_name": "Trending", "last_name": "Player", "team": "XYZ", "position": "WR"}


def test_build_context_structure():
    """build_context assembles all five expected sections."""
    with patch("app.main._get", side_effect=_mock_get), \
         patch("app.waivers._get", side_effect=_mock_get):
        ctx = build_context("Should I start Bo Nix?")

    assert isinstance(ctx, str) and len(ctx) > 50, "Context is empty or too short"

    # Section headers
    assert "NFL State" in ctx,           "Missing 'NFL State' section"
    assert "Anthony's Roster" in ctx,    "Missing roster section"
    assert "Matchup" in ctx,             "Missing matchup section"
    assert "Waiver Wire" in ctx,         "Missing Waiver Wire section"

    # Roster content: our starters should appear by name
    assert "Bo Nix" in ctx,              "Bo Nix not found in roster context"
    assert "Bucky Irving" in ctx,        "Bucky Irving not found in roster context"

    # Week is populated
    assert "Week: 1" in ctx or "week" in ctx.lower(), "Week number missing from context"


# ===========================================================================
# 5. POST /api/chat — Gemini mocked
# ===========================================================================

_MOCK_GEMINI_ANSWER = (
    "**Start Bucky Irving.** He has a better matchup this week "
    "(TB vs a weak run defense) and Irving's snap count trend is up. "
    "Jaylen Warren is fine on the bench — Pittsburgh is in a passing game script."
)


def test_chat_endpoint_mocked():
    """POST /api/chat returns structured JSON; Gemini and Sleeper are both mocked."""
    mock_interaction = MagicMock()
    mock_interaction.output_text = _MOCK_GEMINI_ANSWER

    mock_client = MagicMock()
    mock_client.interactions.create.return_value = mock_interaction

    with (
        patch("app.main._get", side_effect=_mock_get),
        patch("app.waivers._get", side_effect=_mock_get),
        patch("app.main.get_gemini_client", return_value=mock_client),
    ):
        resp = client.post(
            "/api/chat",
            json={"query": "Should I start Bucky Irving or Jaylen Warren?"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()

    assert "answer" in body,          "Response missing 'answer' field"
    assert "context_summary" in body, "Response missing 'context_summary' field"
    assert len(body["answer"]) > 10,  "Answer is suspiciously short"

    # Confirm mock answer was returned (not a live Gemini call)
    assert "Irving" in body["answer"] or "Warren" in body["answer"], \
        f"Expected player names in answer, got: {body['answer'][:200]}"


def test_chat_endpoint_empty_query():
    """POST /api/chat with empty query returns 400."""
    resp = client.post("/api/chat", json={"query": "   "})
    assert resp.status_code == 400
    assert "empty" in resp.json().get("detail", "").lower()


# ===========================================================================
# 6. Sleeper API smoke test (real network call — skipped in offline envs)
# ===========================================================================

@pytest.mark.skipif(
    os.getenv("CI_OFFLINE") == "1",
    reason="Skipped in offline / no-network CI environments",
)
def test_sleeper_api_smoke():
    """Live call to Sleeper /state/nfl returns a valid week and season."""
    try:
        resp = requests.get(
            "https://api.sleeper.app/v1/state/nfl",
            timeout=10,
        )
    except requests.exceptions.ConnectionError:
        pytest.skip("No network access — skipping Sleeper smoke test")

    assert resp.status_code == 200, f"Sleeper API returned {resp.status_code}"
    data = resp.json()

    assert "week" in data, "Response missing 'week' key"
    assert "season" in data, "Response missing 'season' key"
    assert "season_type" in data, "Response missing 'season_type' key"

    week = data["week"]
    assert isinstance(week, int) and week >= 0, \
        f"Expected non-negative integer week, got {week!r}"

    season = data["season"]
    assert str(season).isdigit() and int(season) >= 2024, \
        f"Unexpected season value: {season!r}"
def test_api_waivers_endpoint():
    with patch('app.main.get_waiver_recommendations') as mock_rec:
        mock_rec.return_value = {'targets': []}
        resp = client.get('/api/waivers')
        assert resp.status_code == 200
        assert 'targets' in resp.json()

def test_api_gm_overview_endpoint():
    with patch('app.main.fetch_all_rosters', return_value=[]):
        resp = client.get('/api/gm/overview')
        assert resp.status_code == 200
        assert 'analytics' in resp.json()
