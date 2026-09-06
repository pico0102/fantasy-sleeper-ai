"""
Fantasy GM Engine — Tony Romo's DUI Mugshot
Computes optimal starting lineups using post-FLEX allocation and evaluates trade deltas.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


def get_player_projection(name: str, position: str = "") -> Dict[str, Any]:
    """Return dict with pts, pos, posrank for a given player name."""
    key = _normalize_name(name)
    key = ALIAS.get(key, key)
    if key in _PROJ:
        return _PROJ[key]
    return {"name": name, "pos": position, "pts": 0.0, "posrank": "N/A"}


def best_lineup(players: List[Dict[str, Any]]) -> Tuple[float, List[Dict[str, Any]]]:
    """
    Computes optimal starting lineup.
    Format: 1QB, 2RB, 2WR, 1TE, 2FLEX (RB/WR/TE).
    Ignores K and DEF for core skill position strength.
    players list must have {"name": str, "pos": str, "pts": float}
    """
    pool = {
        pos: sorted([x for x in players if x.get("pos") == pos and x.get("pts", 0) is not None],
                    key=lambda x: -(x.get("pts") or 0))
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


def evaluate_trade(
    my_roster: List[Dict[str, Any]], 
    opp_roster: List[Dict[str, Any]], 
    give_names: List[str], 
    get_names: List[str]
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

    my_pre_tot, _ = best_lineup(my_roster)
    my_post_tot, _ = best_lineup(my_roster_post)
    opp_pre_tot, _ = best_lineup(opp_roster)
    opp_post_tot, _ = best_lineup(opp_roster_post)

    my_delta = my_post_tot - my_pre_tot
    opp_delta = opp_post_tot - opp_pre_tot

    return {
        "my_delta": my_delta,
        "opp_delta": opp_delta,
        "is_win_win": (my_delta > 0 and opp_delta > 0)
    }

def get_team_analytics(rosters_map: Dict[int, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Calculates standings and positional strengths (RB vs WR) across the league.
    rosters_map: {roster_id: [{"name": name, "pos": pos, "pts": pts}, ...]}
    """
    overall_scores = []
    rb_scores = []
    wr_scores = []
    
    for rid, players in rosters_map.items():
        tot, lu = best_lineup(players)
        overall_scores.append((tot, rid))
        
        rb_pts = sum((p.get("pts") or 0.0) for p in lu if p["pos"] == "RB")
        wr_pts = sum((p.get("pts") or 0.0) for p in lu if p["pos"] == "WR")
        
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
