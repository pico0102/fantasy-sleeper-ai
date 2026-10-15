"""Unit tests for data pipeline, news/depth-chart ingestion, and Odds API client."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.data_pipeline import (
    OddsAPIClient,
    SleeperClient,
    load_board_catalog,
    load_player_ids_catalog,
    normalize_nfl_team,
)
from core.news_pipeline import (
    get_notable_role_players,
    hydrate_player_role_and_news,
    load_news_and_depth_chart,
)


def test_load_player_ids_catalog():
    ids, prov = load_player_ids_catalog(force_refresh=True)
    assert len(ids) > 0
    assert prov.status == "ok"
    assert prov.source_id == "local_player_ids"
    assert "1167" in ids
    assert ids["1167"] == "Kirk Cousins"


def test_load_board_catalog():
    board, summary, prov = load_board_catalog(force_refresh=True)
    assert len(board) > 0
    assert prov.status == "ok"
    assert prov.source_id == "local_board_catalog"
    assert summary["audit_passed"] is True
    assert "bias_status" in summary


def test_news_depth_chart_loading_and_hydration():
    feed, prov = load_news_and_depth_chart(force_refresh=True)
    assert len(feed) > 0
    assert prov.status == "ok"
    assert prov.source_id == "nfl_news_and_depth_charts"

    # Verify Kirk Cousins benching context
    cousins_role, cousins_inj = hydrate_player_role_and_news("Kirk Cousins", news_feed=feed)
    assert cousins_role.role_status == "benched"
    assert cousins_role.depth_chart_rank == 2
    assert cousins_role.supplanted_by == "Fernando Mendoza"
    assert cousins_role.is_role_risk is True
    assert len(cousins_role.recent_news_headlines) > 0

    # Verify Baker Mayfield injury context
    mayfield_role, mayfield_inj = hydrate_player_role_and_news("Baker Mayfield", news_feed=feed)
    assert mayfield_role.role_status == "confirmed_starter"
    assert mayfield_inj is not None
    assert mayfield_inj.status == "Out"
    assert mayfield_inj.is_medical_risk is True
    assert mayfield_inj.body_part == "Thumb"

    # Verify notable players list
    notables = get_notable_role_players(news_feed=feed)
    names = [p.full_name for p in notables]
    assert "Kirk Cousins" in names
    assert "Fernando Mendoza" in names


def test_override_sleeper_conflict_cross_check():
    from core.news_pipeline import check_override_sleeper_conflicts

    override = {
        "full_name": "Kirk Cousins",
        "team": "LV",
        "depth_chart_rank": 2,
        "role_status": "benched",
    }
    sleeper_data = {
        "team": "ATL",
        "injury_status": None,
        "depth_chart_order": 1,
    }
    conflicts = check_override_sleeper_conflicts("Kirk Cousins", override, sleeper_data)
    assert len(conflicts) >= 2
    assert any("CONFLICT: TEAM" in c and "LV" in c and "ATL" in c for c in conflicts)
    assert any("CONFLICT: DEPTH_CHART" in c for c in conflicts)


def test_team_name_normalization():
    assert normalize_nfl_team("Detroit Lions") == "DET"
    assert normalize_nfl_team("Green Bay Packers") == "GB"
    assert normalize_nfl_team("det") == "DET"
    assert normalize_nfl_team("KC") == "KC"


@pytest.mark.asyncio
async def test_odds_api_client_missing_key():
    client = OddsAPIClient(api_key="")
    odds, prov = await client.fetch_nfl_odds(nfl_week=6, force_refresh=True)
    assert odds == []
    assert prov.status == "missing_key"
    assert prov.source_id == "the_odds_api_nfl"


@pytest.mark.asyncio
async def test_odds_api_client_with_mocked_response():
    mock_response = [
        {
            "id": "game_det_gb",
            "commence_time": "2026-10-18T17:00:00Z",
            "home_team": "Green Bay Packers",
            "away_team": "Detroit Lions",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Detroit Lions", "price": -110, "point": -7.5},
                                {"name": "Green Bay Packers", "price": -110, "point": 7.5},
                            ],
                        },
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Detroit Lions", "price": -340},
                                {"name": "Green Bay Packers", "price": 270},
                            ],
                        },
                    ],
                }
            ],
        }
    ]

    client = OddsAPIClient(api_key="mock-odds-key")
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        odds, prov = await client.fetch_nfl_odds(nfl_week=6, force_refresh=True)
        assert len(odds) == 1
        assert prov.status == "ok"
        game = odds[0]
        assert game.favorite_team == "DET"
        assert game.underdog_team == "GB"
        assert game.spread_points == -7.5
        assert game.favorite_moneyline == -340
        assert game.implied_win_prob > 0.70


@pytest.mark.asyncio
async def test_sleeper_client_mock():
    mock_nfl_state = {
        "week": 6,
        "season_type": "regular",
        "season": "2026",
        "leg": 6,
        "display_week": 6,
    }
    client = SleeperClient()
    with patch.object(client, "_get_client") as mock_gc:
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_nfl_state
        mock_resp.raise_for_status.return_value = None
        mock_http.get.return_value = mock_resp
        mock_gc.return_value = mock_http

        state, prov = await client.fetch_nfl_state(force_refresh=True)
        assert state["week"] == 6
        assert prov.status == "ok"
        assert prov.source_id == "sleeper_nfl_state"
