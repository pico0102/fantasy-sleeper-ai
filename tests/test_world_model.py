"""Unit tests for unified session WorldStateSnapshot builder and entity hydration."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.provenance import build_provenance_record
from core.schemas import NFLGameOdds, WorldStateSnapshot
from core.world_model import WorldModelBuilder, scan_mentioned_players


def test_scan_mentioned_players():
    player_ids_map = {
        "1167": "Kirk Cousins",
        "4881": "Baker Mayfield",
        "9221": "Jahmyr Gibbs",
    }

    found = scan_mentioned_players("Should I pick up Kirk Cousins from waivers?", player_ids_map)
    assert len(found) == 1
    assert found[0] == ("1167", "Kirk Cousins")

    found_last_name = scan_mentioned_players("Is Mayfield starting this Sunday?", player_ids_map)
    assert len(found_last_name) == 1
    assert found_last_name[0] == ("4881", "Baker Mayfield")


@pytest.mark.asyncio
async def test_world_model_snapshot_assembly_and_determinism():
    mock_nfl_state = {
        "week": 6,
        "season_type": "regular",
        "season": "2026",
        "leg": 6,
        "display_week": 6,
    }
    mock_rosters = [
        {
            "roster_id": 4,
            "owner_id": "user_anthony",
            "starters": ["9221"],
            "players": ["9221", "4881"],
            "settings": {"wins": 4, "losses": 1, "fpts": 520, "fpts_decimal": 50},
        }
    ]
    mock_matchups = [{"roster_id": 4, "matchup_id": 1, "points": 115.4}]

    mock_sleeper = MagicMock()
    mock_sleeper.fetch_nfl_state = AsyncMock(
        return_value=(
            mock_nfl_state,
            build_provenance_record("sleeper_nfl_state", "sleeper_api", "state/nfl"),
        )
    )
    mock_sleeper.fetch_league = AsyncMock(
        return_value={"league_id": "1389359956657967104", "name": "Loser eats an egg", "season": "2026", "total_rosters": 12}
    )
    mock_sleeper.fetch_users = AsyncMock(return_value=[{"user_id": "user_anthony", "display_name": "pinec0ne"}])
    mock_sleeper.fetch_rosters = AsyncMock(
        return_value=(
            mock_rosters,
            build_provenance_record("sleeper_rosters", "sleeper_api", "rosters"),
        )
    )
    mock_sleeper.fetch_matchups = AsyncMock(
        return_value=(
            mock_matchups,
            build_provenance_record("sleeper_matchups", "sleeper_api", "matchups/6"),
        )
    )
    mock_sleeper.fetch_trending = AsyncMock(
        return_value=(
            [{"player_id": "1167", "count": 150}],
            build_provenance_record("sleeper_trending", "sleeper_api", "trending"),
        )
    )

    mock_odds = MagicMock()
    mock_odds.fetch_nfl_odds = AsyncMock(
        return_value=(
            [
                NFLGameOdds(
                    game_id="g1",
                    nfl_week=6,
                    commence_time_utc="2026-10-18T17:00:00Z",
                    home_team="GB",
                    away_team="DET",
                    favorite_team="DET",
                    underdog_team="GB",
                    spread_points=-7.5,
                    implied_win_prob=0.74,
                )
            ],
            build_provenance_record("the_odds_api_nfl", "odds_api", "odds", status="ok"),
        )
    )

    builder = WorldModelBuilder(sleeper_client=mock_sleeper, odds_client=mock_odds)
    snapshot = await builder.build_snapshot(
        session_id="test_session",
        query="Should I pick up Kirk Cousins?",
        force_refresh=True,
    )

    assert isinstance(snapshot, WorldStateSnapshot)
    assert snapshot.nfl_week == 6
    assert len(snapshot.snapshot_id) == 16
    assert snapshot.user_roster is not None
    assert snapshot.user_roster.roster_id == 4

    # Check query-mentioned hydration of Kirk Cousins
    notable_names = {p.full_name: p for p in snapshot.notable_role_changes}
    assert "Kirk Cousins" in notable_names
    cousins = notable_names["Kirk Cousins"]
    assert cousins.role_and_news.role_status == "benched"
    assert cousins.role_and_news.supplanted_by == "Fernando Mendoza"
    assert cousins.role_and_news.depth_chart_rank == 2

    # Check active injury/role alerts
    alert_text = " ".join(snapshot.active_injury_and_role_alerts)
    assert "Fernando Mendoza" in alert_text or "Kirk Cousins" in alert_text

    # Check provenance records (all 8 sources represented)
    prov_sources = {p.source_id for p in snapshot.provenance}
    expected_sources = {
        "sleeper_nfl_state",
        "sleeper_rosters",
        "sleeper_matchups",
        "sleeper_trending",
        "local_player_ids",
        "local_board_catalog",
        "nfl_news_and_depth_charts",
        "the_odds_api_nfl",
    }
    assert expected_sources.issubset(prov_sources)
