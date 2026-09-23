import os
import sys
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-mock-key")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.gm_engine import (
    best_lineup,
    compute_actual_points,
    get_blended_projection,
    evaluate_trade,
    get_team_analytics,
    get_actual_standings
)

def test_best_lineup():
    players = [
        {"id": "1", "name": "QB1", "pos": "QB", "pts": 20.0},
        {"id": "2", "name": "RB1", "pos": "RB", "pts": 15.0},
        {"id": "3", "name": "RB2", "pos": "RB", "pts": 14.0},
        {"id": "4", "name": "WR1", "pos": "WR", "pts": 16.0},
        {"id": "5", "name": "WR2", "pos": "WR", "pts": 15.0},
        {"id": "6", "name": "TE1", "pos": "TE", "pts": 10.0},
        {"id": "7", "name": "FLEX1", "pos": "WR", "pts": 12.0},
        {"id": "8", "name": "FLEX2", "pos": "RB", "pts": 11.0},
        {"id": "9", "name": "BENCH", "pos": "WR", "pts": 5.0},
    ]
    total, lineup = best_lineup(players)
    assert len(lineup) == 8  # 1QB + 2RB + 2WR + 1TE + 2FLEX = 8
    expected_total = sum([20, 15, 14, 16, 15, 10, 12, 11])
    assert total == expected_total

def test_best_lineup_ir_filtering():
    players = [
        {"id": "1", "name": "QB1", "pos": "QB", "pts": 20.0},
        {"id": "2", "name": "RB1", "pos": "RB", "pts": 25.0}, # Best RB but IR
        {"id": "3", "name": "RB2", "pos": "RB", "pts": 14.0},
        {"id": "4", "name": "RB3", "pos": "RB", "pts": 10.0},
    ]
    injury_map = {"2": "IR"}
    total, lineup = best_lineup(players, injury_map)
    # RB1 shouldn't be in the lineup
    names = [p["name"] for p in lineup]
    assert "RB1" not in names

def test_best_lineup_out_filtering():
    players = [
        {"id": "1", "name": "QB1", "pos": "QB", "pts": 20.0},
        {"id": "2", "name": "WR1", "pos": "WR", "pts": 25.0}, 
    ]
    injury_map = {"2": "Out"}
    total, lineup = best_lineup(players, injury_map)
    names = [p["name"] for p in lineup]
    assert "WR1" not in names

def test_compute_actual_points():
    matchup_history = {
        1: [{"players_points": {"1": 10.0, "2": 5.0}}],
        2: [{"players_points": {"1": 20.0, "2": 5.0}}],
    }
    stats = compute_actual_points(matchup_history, 3)
    assert stats["1"]["total_pts"] == 30.0
    assert stats["1"]["games_played"] == 2
    assert stats["1"]["ppg"] == 15.0

def test_get_blended_projection_week1():
    proj = get_blended_projection("Test Player", "1", 1)
    assert proj["source"] == "preseason"

def test_get_blended_projection_week3():
    # Pre-populate actual points for test
    from app.gm_engine import _ACTUAL_POINTS, _PROJ
    _ACTUAL_POINTS["1"] = {"ppg": 20.0, "games_played": 2}
    _PROJ["test player"] = {"pts": 100.0, "pos": "WR"}
    proj = get_blended_projection("Test Player", "1", 3, total_weeks=17)
    # Week 3-4 = 50% pre / 50% actual
    # Actual project = 20 * 17 = 340
    # Blend = 100*0.5 + 340*0.5 = 50 + 170 = 220
    assert proj["pts"] == 220.0
    
def test_get_blended_projection_week5():
    from app.gm_engine import _ACTUAL_POINTS, _PROJ
    _ACTUAL_POINTS["2"] = {"ppg": 10.0, "games_played": 4}
    _PROJ["another player"] = {"pts": 100.0, "pos": "RB"}
    proj = get_blended_projection("Another Player", "2", 5, total_weeks=17)
    # Week 5+ = 20% pre / 80% actual
    # Actual = 10*17 = 170
    # Blend = 100*0.2 + 170*0.8 = 20 + 136 = 156.0
    assert proj["pts"] == 156.0

def test_evaluate_trade():
    my_roster = [{"name": "P1", "pos": "QB", "pts": 20}, {"name": "P2", "pos": "RB", "pts": 10}]
    opp_roster = [{"name": "P3", "pos": "QB", "pts": 25}, {"name": "P4", "pos": "RB", "pts": 5}]
    res = evaluate_trade(my_roster, opp_roster, ["P1"], ["P3"])
    assert "my_delta" in res
    assert "opp_delta" in res

def test_team_analytics():
    rosters_map = {
        1: [{"name": "P1", "pos": "QB", "pts": 20}, {"name": "P2", "pos": "RB", "pts": 10}],
        2: [{"name": "P3", "pos": "QB", "pts": 10}, {"name": "P4", "pos": "RB", "pts": 5}]
    }
    analytics = get_team_analytics(rosters_map)
    assert analytics[1]["overall_rank"] == 1
    assert analytics[2]["overall_rank"] == 2

def test_team_analytics_with_injury_map():
    rosters_map = {
        1: [{"id": "1", "name": "P1", "pos": "RB", "pts": 20}, {"id": "2", "name": "P2", "pos": "RB", "pts": 10}],
        2: [{"id": "3", "name": "P3", "pos": "RB", "pts": 15}]
    }
    injury_map = {"1": "IR"}
    analytics = get_team_analytics(rosters_map, injury_map)
    # Roster 1 loses P1, so they have 10 pts. Roster 2 has 15 pts.
    assert analytics[2]["overall_rank"] == 1
    assert analytics[1]["overall_rank"] == 2

def test_get_actual_standings():
    rosters = [
        {"roster_id": 1, "owner_id": "o1", "settings": {"wins": 5, "losses": 2, "fpts": 100}},
        {"roster_id": 2, "owner_id": "o2", "settings": {"wins": 7, "losses": 0, "fpts": 120}},
    ]
    owner_map = {"o1": "Team A", "o2": "Team B"}
    standings = get_actual_standings(rosters, owner_map)
    assert standings[0]["roster_id"] == 2 # 7 wins
    assert standings[0]["rank"] == 1
    assert standings[1]["roster_id"] == 1 # 5 wins
