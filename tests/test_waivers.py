import pytest
from unittest.mock import patch
from app.waivers import get_waiver_recommendations

def mock_get(url, *args, **kwargs):
    if "state/nfl" in url:
        return {"week": 2, "season": "2026"}
    if "rosters" in url:
        return [
            {"roster_id": 4, "players": ["111", "222", "333"], "starters": ["111", "222"]},
            {"roster_id": 1, "players": ["999"]}
        ]
    if "trending" in url:
        return [
            {"player_id": "444", "count": 1000}, # FA WR
            {"player_id": "999", "count": 2000}, # Rostered
            {"player_id": "555", "count": 500},  # FA RB
        ]
    if "players/nfl/" in url:
        if "444" in url:
            return {"first_name": "New", "last_name": "WR", "position": "WR", "team": "XYZ"}
        if "555" in url:
            return {"first_name": "New", "last_name": "RB", "position": "RB", "team": "ABC"}
    return {}

@patch("app.waivers._get", side_effect=mock_get)
def test_get_waiver_recommendations(mock_api):
    res = get_waiver_recommendations("123", 4)
    
    assert res["week"] == 2
    assert res["roster_id"] == 4
    
    targets = res["targets"]
    assert len(targets) == 2
    
    # Priority should sort WR before RB
    assert targets[0]["player_id"] == "444"
    assert targets[0]["position"] == "WR"
    assert targets[0]["priority_rank"] == 1
    
    assert targets[1]["player_id"] == "555"
    assert targets[1]["position"] == "RB"
    assert targets[1]["priority_rank"] == 2
    
    drop = res["recommended_drop"]
    assert drop is not None
    assert drop["player_id"] == "333" # Bench player

