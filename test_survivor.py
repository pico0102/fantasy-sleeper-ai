import pytest
import os
from app.survivor_engine import get_survivor_state, get_vegas_odds

def test_get_survivor_state():
    # Use the mock or live league
    league_id = os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", "1393775027442167808")
    state = get_survivor_state(league_id)
    assert "current_week" in state
    assert "league_name" in state

def test_get_vegas_odds():
    odds = get_vegas_odds()
    assert isinstance(odds, list)
    if odds:
        assert "home" in odds[0]
        assert "away" in odds[0]

