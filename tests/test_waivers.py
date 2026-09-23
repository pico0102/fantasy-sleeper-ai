import os
import sys
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-mock-key")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.waivers import get_waiver_recommendations

def test_waiver_recommendations_basic():
    res = get_waiver_recommendations(
        "league123", 
        4, 
        lambda x: x, 
        nfl_state={"week": 2},
        trending_adds=[{"player_id": "100", "count": 5000}]
    )
    assert res["week"] == 2
    assert "targets" in res

def test_waiver_ir_players_filtered():
    all_players = {
        "100": {"position": "WR", "injury_status": "IR", "first_name": "Injured", "last_name": "Player"}
    }
    trending_adds = [{"player_id": "100", "count": 5000}]
    res = get_waiver_recommendations(
        "league", 4, lambda x: x,
        all_players=all_players,
        trending_adds=trending_adds
    )
    targets = res["targets"]
    assert len(targets) == 0

def test_waiver_drop_uses_actual_ppg():
    rosters = [{"roster_id": 4, "players": ["1", "2"], "starters": ["1"]}]
    all_players = {
        "1": {"position": "QB"},
        "2": {"position": "WR"}
    }
    actual_points = {
        "2": {"ppg": 5.5, "games_played": 3}
    }
    res = get_waiver_recommendations(
        "league", 4, lambda x: "Player2" if x=="2" else x,
        rosters=rosters, all_players=all_players, actual_points=actual_points
    )
    drop = res["recommended_drop"]
    assert drop["player_id"] == "2"
    assert "actual PPG" in drop["reason"]

def test_waiver_reserve_players_excluded():
    rosters = [{"roster_id": 4, "players": ["1", "2", "3"], "starters": ["1"], "reserve": ["2"]}]
    all_players = {
        "2": {"position": "WR", "pts": 500},  # Injured but good
        "3": {"position": "RB", "pts": 5}     # Healthy but bad
    }
    injury_map = {"2": "IR"}
    res = get_waiver_recommendations(
        "league", 4, lambda x: x,
        rosters=rosters, all_players=all_players, injury_map=injury_map
    )
    drop = res["recommended_drop"]
    # Player 2 is in reserve, so they shouldn't be dropped. Player 3 should be dropped.
    assert drop["player_id"] == "3"

def test_waiver_dynamic_wr_need():
    all_players = {"100": {"position": "WR"}}
    trending_adds = [{"player_id": "100", "count": 5000}]
    res = get_waiver_recommendations(
        "league", 4, lambda x: x,
        all_players=all_players, trending_adds=trending_adds,
        wr_rank=11, rb_rank=3
    )
    targets = res["targets"]
    assert targets[0]["priority_rank"] == 1

def test_waiver_dynamic_rb_need():
    all_players = {"100": {"position": "RB"}}
    trending_adds = [{"player_id": "100", "count": 5000}]
    res = get_waiver_recommendations(
        "league", 4, lambda x: x,
        all_players=all_players, trending_adds=trending_adds,
        wr_rank=3, rb_rank=11
    )
    targets = res["targets"]
    assert targets[0]["priority_rank"] == 1

def test_waiver_faab_early_season():
    res = get_waiver_recommendations("league", 4, lambda x: x, nfl_state={"week": 2})
    assert "Aggressive early" in res["faab_guidance"]["strategy"]

def test_waiver_faab_late_season():
    res = get_waiver_recommendations("league", 4, lambda x: x, nfl_state={"week": 12})
    assert "Conservative" in res["faab_guidance"]["strategy"]
