"""Unit tests for the deterministic GM engine, marginal waiver valuation, and typed SSE."""

import pytest
from app.engine import (
    evaluate_matchup_preview,
    evaluate_start_sit,
    evaluate_waivers,
    solve_lineup,
)
from app.intents import Intent, classify_intent
from httpx import ASGITransport, AsyncClient
from app.main import app


def test_lineup_solver_full_slots():
    # 1 QB, 2 RB, 2 WR, 1 TE, 2 FLEX, 1 K, 1 DEF + bench
    player_ids = ["q1", "r1", "r2", "r3", "w1", "w2", "w3", "t1", "k1", "d1", "bench1"]
    projections = {
        "q1": {"pts": 20.0, "pos": "QB", "team": "DEN"},
        "r1": {"pts": 18.0, "pos": "RB", "team": "SF"},
        "r2": {"pts": 15.0, "pos": "RB", "team": "ARI"},
        "r3": {"pts": 12.0, "pos": "RB", "team": "TB"},
        "w1": {"pts": 16.0, "pos": "WR", "team": "NE"},
        "w2": {"pts": 14.0, "pos": "WR", "team": "LAR"},
        "w3": {"pts": 11.0, "pos": "WR", "team": "KC"},
        "t1": {"pts": 10.0, "pos": "TE", "team": "CLE"},
        "k1": {"pts": 8.0, "pos": "K", "team": "LV"},
        "d1": {"pts": 7.0, "pos": "DEF", "team": "SEA"},
        "bench1": {"pts": 4.0, "pos": "WR", "team": "LV"},
    }

    res = solve_lineup(player_ids, projections)
    starters = res["starters"]
    assert len(starters) == 10
    slots = [s["slot"] for s in starters]
    assert "QB" in slots
    assert "RB1" in slots and "RB2" in slots
    assert "WR1" in slots and "WR2" in slots
    assert "TE" in slots
    assert "FLEX1" in slots and "FLEX2" in slots
    assert "K" in slots
    assert "DEF" in slots
    assert res["total_pts"] == round(20 + 18 + 15 + 16 + 14 + 10 + 12 + 11 + 8 + 7, 2)
    assert len(res["bench"]) == 1
    assert res["bench"][0]["id"] == "bench1"


def test_lineup_solver_ir_and_out_excluded():
    player_ids = ["r1", "r2", "r3"]
    projections = {
        "r1": {"pts": 25.0, "pos": "RB", "injury_status": "IR"},
        "r2": {"pts": 15.0, "pos": "RB", "injury_status": ""},
        "r3": {"pts": 12.0, "pos": "RB", "injury_status": "Out"},
    }
    res = solve_lineup(player_ids, projections)
    # r1 and r3 should have 0 pts and not be preferred
    starter_ids = [s["id"] for s in res["starters"]]
    assert "r2" in starter_ids
    r2_starter = next(s for s in res["starters"] if s["id"] == "r2")
    assert r2_starter["pts"] == 15.0


def test_lineup_solver_locked_starter_respected():
    player_ids = ["w1", "w2", "w3"]
    projections = {
        "w1": {"pts": 8.0, "pos": "WR", "team": "TB"},   # Locked from Thursday game
        "w2": {"pts": 16.0, "pos": "WR", "team": "KC"},
        "w3": {"pts": 14.0, "pos": "WR", "team": "LAR"},
    }
    locked = {"WR1": "w1"}
    res = solve_lineup(player_ids, projections, locked_starters=locked)
    s_map = {s["slot"]: s for s in res["starters"]}
    assert s_map["WR1"]["id"] == "w1"
    assert s_map["WR1"]["locked"] is True
    assert s_map["WR2"]["id"] == "w2"


def test_waiver_marginal_value_kicker_on_bye_favors_kicker():
    # If Anthony's Kicker is on bye (0.0 pts), a waiver Kicker should have massive marginal value (+8.0 pts)
    my_roster = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k_bye", "d1", "bench_wr"]
    my_starters = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k_bye", "d1"]
    all_rostered = set(my_roster)

    projections = {
        "q1": {"pts": 20.0, "pos": "QB"},
        "r1": {"pts": 15.0, "pos": "RB"},
        "r2": {"pts": 14.0, "pos": "RB"},
        "w1": {"pts": 16.0, "pos": "WR"},
        "w2": {"pts": 14.0, "pos": "WR"},
        "t1": {"pts": 10.0, "pos": "TE"},
        "f1": {"pts": 12.0, "pos": "RB"},
        "f2": {"pts": 11.0, "pos": "WR"},
        "k_bye": {"pts": 0.0, "pos": "K"},  # On bye!
        "d1": {"pts": 6.0, "pos": "DEF"},
        "bench_wr": {"pts": 5.0, "pos": "WR"},  # Drop candidate
        # Unrostered waiver targets:
        "waiver_wr": {"pts": 9.0, "pos": "WR"},  # Does not crack lineup (FLEX2 is 11.0) -> gain = 0.0
        "waiver_k": {"pts": 8.5, "pos": "K"},    # Replaces 0.0 K -> gain = +8.5!
    }

    res = evaluate_waivers(
        my_roster_ids=my_roster,
        my_starters_ids=my_starters,
        my_reserve_ids=[],
        all_rostered_ids=all_rostered,
        projections=projections,
        future_projections={},
        current_week=5,
        faab_remaining=90,
    )

    top = res["top_target"]
    assert top is not None
    # Kicker must win #1 because Anthony has 0 pts at Kicker!
    assert top["player_id"] == "waiver_k"
    assert top["marginal_gain"] > 7.0
    assert top["suggested_bid"] > 0


def test_waiver_marginal_value_runner_up_reasoning():
    my_roster = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k1", "d1", "bench1"]
    my_starters = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k1", "d1"]
    all_rostered = set(my_roster)

    projections = {
        "q1": {"pts": 20.0, "pos": "QB"},
        "r1": {"pts": 15.0, "pos": "RB"},
        "r2": {"pts": 14.0, "pos": "RB"},
        "w1": {"pts": 16.0, "pos": "WR"},
        "w2": {"pts": 14.0, "pos": "WR"},
        "t1": {"pts": 10.0, "pos": "TE"},
        "f1": {"pts": 10.0, "pos": "RB"},
        "f2": {"pts": 9.0, "pos": "WR"},   # Lowest starter is 9.0
        "k1": {"pts": 7.0, "pos": "K"},
        "d1": {"pts": 6.0, "pos": "DEF"},
        "bench1": {"pts": 3.0, "pos": "RB"},
        # Waiver options:
        "w_opt1": {"pts": 13.0, "pos": "WR"},  # Gain over 9.0 = +4.0
        "w_opt2": {"pts": 11.5, "pos": "RB"},  # Gain over 9.0 = +2.5
    }

    res = evaluate_waivers(
        my_roster_ids=my_roster,
        my_starters_ids=my_starters,
        my_reserve_ids=[],
        all_rostered_ids=all_rostered,
        projections=projections,
        future_projections={},
        current_week=5,
        faab_remaining=90,
    )

    assert res["top_target"]["player_id"] == "w_opt1"
    assert res["runner_up"]["player_id"] == "w_opt2"
    assert "Runner-up" in res["runner_up_reasoning"]
    assert "weekly marginal points" in res["runner_up_reasoning"]


def test_waiver_deterministic_repeatability():
    my_roster = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k1", "d1", "bench1"]
    my_starters = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k1", "d1"]
    all_rostered = set(my_roster)

    projections = {
        "q1": {"pts": 20.0, "pos": "QB"},
        "r1": {"pts": 15.0, "pos": "RB"},
        "r2": {"pts": 14.0, "pos": "RB"},
        "w1": {"pts": 16.0, "pos": "WR"},
        "w2": {"pts": 14.0, "pos": "WR"},
        "t1": {"pts": 10.0, "pos": "TE"},
        "f1": {"pts": 10.0, "pos": "RB"},
        "f2": {"pts": 9.0, "pos": "WR"},
        "k1": {"pts": 7.0, "pos": "K"},
        "d1": {"pts": 6.0, "pos": "DEF"},
        "bench1": {"pts": 3.0, "pos": "RB"},
        "w_a": {"pts": 12.0, "pos": "WR"},
        "w_b": {"pts": 12.0, "pos": "WR"},
    }

    # Run twice
    res1 = evaluate_waivers(my_roster, my_starters, [], all_rostered, projections, {}, 5, 90)
    res2 = evaluate_waivers(my_roster, my_starters, [], all_rostered, projections, {}, 5, 90)

    # Identical order and fields
    assert [t["player_id"] for t in res1["top_targets"]] == [t["player_id"] for t in res2["top_targets"]]
    assert res1["top_target"]["player_id"] == res2["top_target"]["player_id"]


def test_start_sit_close_calls():
    my_roster = ["q1", "r1", "r2", "w1", "w2", "t1", "f1", "f2", "k1", "d1", "bench_w"]
    # Started bench_w (7.0) instead of w2 (12.0)
    current_starters = ["q1", "r1", "r2", "w1", "bench_w", "t1", "f1", "f2", "k1", "d1"]
    projections = {
        "q1": {"pts": 20.0, "pos": "QB"},
        "r1": {"pts": 15.0, "pos": "RB"},
        "r2": {"pts": 14.0, "pos": "RB"},
        "w1": {"pts": 16.0, "pos": "WR"},
        "w2": {"pts": 12.0, "pos": "WR"},
        "t1": {"pts": 10.0, "pos": "TE"},
        "f1": {"pts": 11.0, "pos": "RB"},
        "f2": {"pts": 10.5, "pos": "WR"},
        "k1": {"pts": 8.0, "pos": "K"},
        "d1": {"pts": 7.0, "pos": "DEF"},
        "bench_w": {"pts": 7.0, "pos": "WR"},
    }

    res = evaluate_start_sit(
        my_roster_ids=my_roster,
        current_starters_ids=current_starters,
        projections=projections,
        scoreboard=[],
    )

    assert not res["is_already_optimal"]
    assert len(res["swaps_recommended"]) == 1
    swap = res["swaps_recommended"][0]
    assert swap["start_player"] == "w2"
    assert swap["bench_player"] == "bench_w"
    assert swap["pts_gain"] == 5.0


def test_intent_classification():
    assert classify_intent("Analyze my lineup for this week and tell me who to start.") == Intent.LINEUP
    assert classify_intent("What are the best waiver wire pickups available for my team right now?") == Intent.WAIVERS
    assert classify_intent("Give me a start/sit decision breakdown for my flex spots and any close calls this week.") == Intent.START_SIT
    assert classify_intent("Give me a matchup preview for this week — who are the key players and what does this week turn on?") == Intent.MATCHUP
    assert classify_intent("Should I start Jakobi Meyers or Khalil Shakir?") == Intent.START_SIT
    assert classify_intent("Who is my opponent this week?") == Intent.MATCHUP
    assert classify_intent("Should I trade Dobbins for Brown?") == Intent.TRADE


@pytest.mark.asyncio
async def test_deep_health_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "components" in data
    assert "sleeper_api" in data["components"]
    assert "gemini_llm" in data["components"]


@pytest.mark.asyncio
async def test_chat_stream_typed_events():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/api/chat_stream", json={"query": "Analyze my lineup"})
    assert response.status_code == 200
    text = response.text
    # Verify stream output includes typed data events
    assert "data: " in text
    assert '"type": "status"' in text
    assert '"type": "result"' in text
    assert '"type": "done"' in text
