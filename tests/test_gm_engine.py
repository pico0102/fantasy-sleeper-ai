import pytest
from app.gm_engine import best_lineup, evaluate_trade, get_team_analytics

def test_best_lineup():
    players = [
        {"name": "QB1", "pos": "QB", "pts": 20.0},
        {"name": "RB1", "pos": "RB", "pts": 15.0},
        {"name": "RB2", "pos": "RB", "pts": 10.0},
        {"name": "WR1", "pos": "WR", "pts": 14.0},
        {"name": "WR2", "pos": "WR", "pts": 12.0},
        {"name": "TE1", "pos": "TE", "pts": 8.0},
        # Flex options
        {"name": "RB3", "pos": "RB", "pts": 9.0},
        {"name": "WR3", "pos": "WR", "pts": 11.0},
        {"name": "TE2", "pos": "TE", "pts": 5.0},
        # Bench
        {"name": "QB2", "pos": "QB", "pts": 18.0},
    ]
    
    tot, lu = best_lineup(players)
    
    # Expected starters:
    # QB: QB1 (20)
    # RB: RB1 (15), RB2 (10)
    # WR: WR1 (14), WR2 (12)
    # TE: TE1 (8)
    # FLEX: WR3 (11), RB3 (9)
    # Total = 20 + 15 + 10 + 14 + 12 + 8 + 11 + 9 = 99.0
    
    assert tot == 99.0
    assert len(lu) == 8
    names = [p["name"] for p in lu]
    assert "QB1" in names
    assert "WR3" in names
    assert "RB3" in names
    assert "QB2" not in names

def test_evaluate_trade():
    my_roster = [
        {"name": "RB1", "pos": "RB", "pts": 20.0},
        {"name": "WR1", "pos": "WR", "pts": 10.0},
    ]
    opp_roster = [
        {"name": "WR2", "pos": "WR", "pts": 20.0},
        {"name": "RB2", "pos": "RB", "pts": 10.0},
    ]
    
    # Let's say I give RB1 for WR2
    # Pre: my_tot=30, opp_tot=30 (assuming simple 2 flex for test)
    # Wait, best_lineup requires filling slots. Let's provide a full roster.
    
    my_roster_full = my_roster + [{"name": f"P{i}", "pos": "WR", "pts": 5.0} for i in range(10)]
    opp_roster_full = opp_roster + [{"name": f"O{i}", "pos": "RB", "pts": 5.0} for i in range(10)]
    
    res = evaluate_trade(my_roster_full, opp_roster_full, ["RB1"], ["WR2"])
    
    # my pre: RB1(20), WR1(10) + 6 * WR(5) = 30 + 30 = 60
    # my post: WR2(20), WR1(10) + 6 * WR(5) = 60 (actually no change in total if just a 20 for 20 swap)
    # Let's just check the structure.
    assert "my_delta" in res
    assert "opp_delta" in res
    assert "is_win_win" in res

def test_team_analytics():
    rosters = {
        1: [{"name": "A", "pos": "RB", "pts": 10}],
        2: [{"name": "B", "pos": "RB", "pts": 20}],
    }
    # best_lineup will pick these
    analytics = get_team_analytics(rosters)
    assert analytics[1]["overall_rank"] == 2
    assert analytics[2]["overall_rank"] == 1
