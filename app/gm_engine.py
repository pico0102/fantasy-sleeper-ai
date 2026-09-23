"""
Fantasy GM Engine — Tony Romo's DUI Mugshot (v3.0)
Computes optimal starting lineups using post-FLEX allocation, evaluates trade deltas,
and blends preseason projections with actual in-season performance.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / ".agents" / "skills" / "loser-eats-an-egg" / "data"

def _load_json(name: str) -> Any:
    p = _DATA_DIR / name
    if p.exists():
        with p.open() as f:
            return json.load(f)
    return []

# projections keyed by normalized name -> {pts, pos, posrank}
_BOARD = _load_json("board.json")
_PROJ: Dict[str, Dict[str, Any]] = {}

def _normalize_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n.lower())
    return " ".join(re.sub(r"[^a-z ]", "", n).split())

ALIAS = {
    "kenny gainwell": "kenneth gainwell", "brian thomas": "brian thomas",
    "marvin harrison": "marvin harrison", "harold fannin": "harold fannin",
    "michael pittman": "michael pittman", "luther burden": "luther burden",
    "jeremiyah love": "jeremiyah love", "dj moore": "dj moore"
}

for p in _BOARD:
    if "name" in p:
        key = _normalize_name(p["name"])
        _PROJ[key] = p

# ---------------------------------------------------------------------------
# In-season actual performance tracking
# ---------------------------------------------------------------------------

_ACTUAL_POINTS: Dict[str, Dict[str, Any]] = {}
"""
Stores actual season stats per player_id:
{
    player_id: {
        "total_pts": float,
        "games_played": int,
        "ppg": float,
        "weekly_pts": [float, ...]
    }
}
"""


def compute_actual_points(
    matchup_history: Dict[int, list[dict]],
    current_week: int,
) -> Dict[str, Dict[str, Any]]:
    """
    Build actual season stats from Sleeper matchup data (Weeks 1 through current_week-1).

    matchup_history: {week_num: [matchup_dict, ...]}
      Each matchup_dict has:
        - "roster_id": int
        - "players_points": {player_id: points, ...}  (per-player scoring)
        - "starters_points": [float, ...]
        - "starters": [player_id, ...]

    Returns: {player_id: {"total_pts", "games_played", "ppg", "weekly_pts"}}
    """
    global _ACTUAL_POINTS

    player_stats: Dict[str, Dict[str, Any]] = {}

    for week_num in range(1, current_week):
        matchups = matchup_history.get(week_num, [])
        for m in matchups:
            players_points = m.get("players_points") or {}
            for pid, pts in players_points.items():
                if pts is None:
                    continue
                if pid not in player_stats:
                    player_stats[pid] = {
                        "total_pts": 0.0,
                        "games_played": 0,
                        "weekly_pts": [],
                    }
                player_stats[pid]["total_pts"] += pts
                player_stats[pid]["games_played"] += 1
                player_stats[pid]["weekly_pts"].append(pts)

    # Compute PPG
    for pid, stats in player_stats.items():
        gp = stats["games_played"]
        stats["ppg"] = round(stats["total_pts"] / gp, 2) if gp > 0 else 0.0

    _ACTUAL_POINTS = player_stats
    return player_stats


def get_blended_projection(
    name: str,
    player_id: str = "",
    current_week: int = 1,
    total_weeks: int = 17,
    position: str = "",
) -> Dict[str, Any]:
    """
    Return blended projection that mixes preseason forecast with actual performance.

    Blend schedule:
      - Weeks 1-2: 100% preseason (not enough actual data)
      - Weeks 3-4: 50% preseason / 50% actual
      - Week 5+:   20% preseason / 80% actual

    Returns dict with: pts, pos, posrank, source ("preseason" | "actual" | "blended"),
                        actual_ppg, preseason_pts
    """
    # Get preseason projection
    key = _normalize_name(name)
    key = ALIAS.get(key, key)
    preseason = _PROJ.get(key, {})
    preseason_pts = preseason.get("pts", 0.0)
    pos = preseason.get("pos") or position
    posrank = preseason.get("posrank", "N/A")

    # Get actual stats
    actual = _ACTUAL_POINTS.get(player_id, {})
    actual_ppg = actual.get("ppg", 0.0)
    games_played = actual.get("games_played", 0)

    # If no actual data, use preseason only
    if games_played == 0 or current_week <= 2:
        return {
            "name": name,
            "pos": pos,
            "pts": preseason_pts,
            "posrank": posrank,
            "source": "preseason",
            "actual_ppg": 0.0,
            "preseason_pts": preseason_pts,
        }

    # Project season total from actual PPG
    actual_projected_total = actual_ppg * total_weeks

    # Blend based on week
    if current_week <= 4:
        pre_weight, act_weight = 0.5, 0.5
    else:
        pre_weight, act_weight = 0.2, 0.8

    blended_pts = round(preseason_pts * pre_weight + actual_projected_total * act_weight, 1)
    source = "blended" if preseason_pts > 0 else "actual"

    return {
        "name": name,
        "pos": pos,
        "pts": blended_pts,
        "posrank": posrank,
        "source": source,
        "actual_ppg": actual_ppg,
        "preseason_pts": preseason_pts,
    }


def get_player_projection(name: str, position: str = "") -> Dict[str, Any]:
    """Return dict with pts, pos, posrank for a given player name.
    Falls back to preseason-only when actual data hasn't been loaded."""
    key = _normalize_name(name)
    key = ALIAS.get(key, key)
    if key in _PROJ:
        return _PROJ[key]
    return {"name": name, "pos": position, "pts": 0.0, "posrank": "N/A"}


# ---------------------------------------------------------------------------
# Lineup optimizer
# ---------------------------------------------------------------------------

def best_lineup(
    players: List[Dict[str, Any]],
    injury_map: Optional[Dict[str, str]] = None,
) -> Tuple[float, List[Dict[str, Any]]]:
    """
    Computes optimal starting lineup.
    Format: 1QB, 2RB, 2WR, 1TE, 2FLEX (RB/WR/TE).
    Ignores K and DEF for core skill position strength.

    players list must have {"name": str, "pos": str, "pts": float}
    injury_map: optional {player_id: injury_status} — players with
                IR/Out/PUP status are excluded from lineup consideration.
    """
    if injury_map is None:
        injury_map = {}

    # Filter out injured players (IR, Out, PUP)
    excluded_statuses = {"IR", "Out", "PUP", "Doubtful"}
    eligible = []
    ir_stashed = []
    for p in players:
        pid = p.get("id", "")
        inj = injury_map.get(pid, "")
        if inj in excluded_statuses:
            ir_stashed.append(p)
            continue
        eligible.append(p)

    pool = {
        pos: sorted(
            [x for x in eligible if x.get("pos") == pos and x.get("pts", 0) is not None],
            key=lambda x: -(x.get("pts") or 0),
        )
        for pos in ("QB", "RB", "WR", "TE")
    }

    used_ids = set()
    total = 0.0
    lineup = []

    def take(pos: str, n: int, label: str):
        nonlocal total
        count = 0
        for x in pool.get(pos, []):
            if count >= n:
                break
            if id(x) in used_ids:
                continue
            used_ids.add(id(x))
            total += (x.get("pts") or 0.0)
            lineup.append({"slot": label, **x})
            count += 1

    take("QB", 1, "QB")
    take("RB", 2, "RB")
    take("WR", 2, "WR")
    take("TE", 1, "TE")

    flex_pool = sorted(
        [x for p in ("RB", "WR", "TE") for x in pool.get(p, []) if id(x) not in used_ids],
        key=lambda x: -(x.get("pts") or 0.0)
    )

    for x in flex_pool[:2]:
        used_ids.add(id(x))
        total += (x.get("pts") or 0.0)
        lineup.append({"slot": "FLEX", **x})

    return total, lineup


# ---------------------------------------------------------------------------
# Trade evaluator
# ---------------------------------------------------------------------------

def evaluate_trade(
    my_roster: List[Dict[str, Any]], 
    opp_roster: List[Dict[str, Any]], 
    give_names: List[str], 
    get_names: List[str],
    injury_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Evaluates a trade by computing optimal lineup deltas.
    Each roster list is a list of player dicts (name, pos, pts).
    """
    give_normalized = set(_normalize_name(n) for n in give_names)
    get_normalized = set(_normalize_name(n) for n in get_names)

    my_give_players = [p for p in my_roster if _normalize_name(p["name"]) in give_normalized]
    opp_get_players = [p for p in opp_roster if _normalize_name(p["name"]) in get_normalized]

    my_roster_post = [p for p in my_roster if _normalize_name(p["name"]) not in give_normalized] + opp_get_players
    opp_roster_post = [p for p in opp_roster if _normalize_name(p["name"]) not in get_normalized] + my_give_players

    my_pre_tot, _ = best_lineup(my_roster, injury_map)
    my_post_tot, _ = best_lineup(my_roster_post, injury_map)
    opp_pre_tot, _ = best_lineup(opp_roster, injury_map)
    opp_post_tot, _ = best_lineup(opp_roster_post, injury_map)

    my_delta = my_post_tot - my_pre_tot
    opp_delta = opp_post_tot - opp_pre_tot

    return {
        "my_delta": my_delta,
        "opp_delta": opp_delta,
        "is_win_win": (my_delta > 0 and opp_delta > 0)
    }


# ---------------------------------------------------------------------------
# Team analytics
# ---------------------------------------------------------------------------

def get_actual_standings(
    rosters: list[dict],
    owner_map: dict[str, str],
) -> list[dict]:
    """
    Compute actual W-L standings and points-for from Sleeper roster data.
    Returns sorted list of team records.
    """
    standings = []
    for r in rosters:
        rid = r.get("roster_id")
        settings = r.get("settings") or {}
        owner_id = str(r.get("owner_id", ""))
        team_name = owner_map.get(owner_id, f"Roster {rid}")

        wins = settings.get("wins", 0)
        losses = settings.get("losses", 0)
        ties = settings.get("ties", 0)
        fpts = settings.get("fpts", 0)
        fpts_decimal = settings.get("fpts_decimal", 0)
        total_fpts = fpts + (fpts_decimal / 100 if fpts_decimal else 0)

        standings.append({
            "roster_id": rid,
            "team_name": team_name,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
            "points_for": round(total_fpts, 2),
        })

    # Sort by wins desc, then points_for desc
    standings.sort(key=lambda x: (-x["wins"], -x["points_for"]))

    # Add rank
    for i, s in enumerate(standings, 1):
        s["rank"] = i

    return standings


def get_team_analytics(
    rosters_map: Dict[int, List[Dict[str, Any]]],
    injury_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Calculates standings and positional strengths (RB vs WR) across the league.
    rosters_map: {roster_id: [{"name": name, "pos": pos, "pts": pts}, ...]}
    """
    if injury_map is None:
        injury_map = {}

    overall_scores = []
    rb_scores = []
    wr_scores = []
    
    for rid, players in rosters_map.items():
        tot, lu = best_lineup(players, injury_map)
        overall_scores.append((tot, rid))
        
        rb_pts = sum((p.get("pts") or 0.0) for p in lu if p.get("pos") == "RB")
        wr_pts = sum((p.get("pts") or 0.0) for p in lu if p.get("pos") == "WR")
        
        rb_scores.append((rb_pts, rid))
        wr_scores.append((wr_pts, rid))

    overall_scores.sort(key=lambda x: -x[0])
    rb_scores.sort(key=lambda x: -x[0])
    wr_scores.sort(key=lambda x: -x[0])
    
    def get_rank(scores_list, target_rid):
        for i, (pts, rid) in enumerate(scores_list, 1):
            if rid == target_rid:
                return i, pts
        return len(scores_list), 0.0

    analytics = {}
    for rid in rosters_map.keys():
        o_rank, o_pts = get_rank(overall_scores, rid)
        rb_rank, rb_pts = get_rank(rb_scores, rid)
        wr_rank, wr_pts = get_rank(wr_scores, rid)
        
        analytics[rid] = {
            "overall_rank": o_rank, "overall_pts": o_pts,
            "rb_in_lineup_rank": rb_rank, "rb_in_lineup_pts": rb_pts,
            "wr_in_lineup_rank": wr_rank, "wr_in_lineup_pts": wr_pts,
        }
        
    return analytics
