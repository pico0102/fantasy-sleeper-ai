"""Deterministic Fantasy GM Engine — Tony Romo's DUI Mugshot.

Computes exact optimal lineups, positional marginal waiver values across all positions,
start/sit comparisons, and matchup previews with mathematically sound reasoning.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# Standard 10-starter league lineup format
ROSTER_SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]
FLEX_ELIGIBLE = {"RB", "WR", "TE"}


# ---------------------------------------------------------------------------
# 1. Lineup Optimizer
# ---------------------------------------------------------------------------

def solve_lineup(
    player_ids: List[str],
    projections: Dict[str, Dict[str, Any]],
    locked_starters: Optional[Dict[str, str]] = None,
    all_players: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes optimal starting lineup for given roster.
    
    Format: 1 QB, 2 RB, 2 WR, 1 TE, 2 FLEX (RB/WR/TE), 1 K, 1 DEF.
    Respects locked_starters ({slot: player_id}) whose games have already kicked off.
    Players with IR, PUP, Out, or 0.0 projections are placed on bench.
    
    Returns:
      {
        "total_pts": float,
        "starters": [ {slot, id, name, pos, pts, injury_status, locked, team, opponent}, ... ],
        "bench": [ {id, name, pos, pts, injury_status, team}, ... ],
      }
    """
    locked_starters = locked_starters or {}
    all_players = all_players or {}

    # Pool eligible players
    available_pool: List[Dict[str, Any]] = []
    used_ids: Set[str] = set()

    for pid in player_ids:
        proj = projections.get(pid, {})
        pts = proj.get("pts", 0.0)
        pos = proj.get("pos", "")
        inj = proj.get("injury_status", "")
        team = proj.get("team", "")
        opp = proj.get("opponent", "")

        # Fallback to bulk player info if missing in projections
        if not pos and pid in all_players:
            pinfo = all_players[pid]
            if isinstance(pinfo, dict):
                pos = pinfo.get("position", "")
                team = team or pinfo.get("team", "")
                inj = inj or pinfo.get("injury_status", "")

        if not pos and not pid.isdigit():
            pos = "DEF"
            team = team or pid

        # Filter out ineligible statuses from starting consideration
        if inj in ("IR", "PUP", "Out", "Doubtful", "Sus", "NA"):
            pts = 0.0

        available_pool.append({
            "id": pid,
            "pts": float(pts or 0.0),
            "pos": pos,
            "team": team,
            "opponent": opp,
            "injury_status": inj,
        })

    # Sort available pool deterministically: (-pts, id)
    available_pool.sort(key=lambda x: (-x["pts"], x["id"]))

    lineup_slots: List[Dict[str, Any]] = []

    # 1. Place locked starters first
    for slot, pid in locked_starters.items():
        match = next((p for p in available_pool if p["id"] == pid), None)
        if match:
            used_ids.add(pid)
            lineup_slots.append({"slot": slot, "locked": True, **match})

    # Helper to fill remaining unfilled slots
    def fill_slot(slot_name: str, allowed_positions: Set[str]) -> bool:
        # Check if already filled by locked starter
        if any(s["slot"] == slot_name for s in lineup_slots):
            return True
        for p in available_pool:
            if p["id"] not in used_ids and p["pos"] in allowed_positions:
                used_ids.add(p["id"])
                lineup_slots.append({"slot": slot_name, "locked": False, **p})
                return True
        return False

    # Dedicated positions
    fill_slot("QB", {"QB"})
    fill_slot("RB1", {"RB"})
    fill_slot("RB2", {"RB"})
    fill_slot("WR1", {"WR"})
    fill_slot("WR2", {"WR"})
    fill_slot("TE", {"TE"})

    # Flex positions (RB/WR/TE)
    fill_slot("FLEX1", FLEX_ELIGIBLE)
    fill_slot("FLEX2", FLEX_ELIGIBLE)

    # Special positions (K, DEF)
    fill_slot("K", {"K"})
    fill_slot("DEF", {"DEF"})

    # Order starters to standard display order
    order_map = {"QB": 1, "RB1": 2, "RB2": 3, "WR1": 4, "WR2": 5, "TE": 6, "FLEX1": 7, "FLEX2": 8, "K": 9, "DEF": 10}
    lineup_slots.sort(key=lambda x: order_map.get(x["slot"], 99))

    total_pts = sum(s["pts"] for s in lineup_slots)

    # Bench is remaining unused players
    bench = [p for p in available_pool if p["id"] not in used_ids]
    bench.sort(key=lambda x: (-x["pts"], x["id"]))

    return {
        "total_pts": round(total_pts, 2),
        "starters": lineup_slots,
        "bench": bench,
    }


# ---------------------------------------------------------------------------
# 2. Positional Marginal Value Waiver Engine
# ---------------------------------------------------------------------------

def evaluate_waivers(
    my_roster_ids: List[str],
    my_starters_ids: List[str],
    my_reserve_ids: List[str],
    all_rostered_ids: Set[str],
    projections: Dict[str, Dict[str, Any]],
    future_projections: Dict[int, Dict[str, Dict[str, Any]]],
    current_week: int,
    faab_remaining: int = 90,
    all_players: Optional[Dict[str, Any]] = None,
    player_name_resolver: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Evaluates EVERY position by projected marginal value to Anthony's roster.
    
    Considers:
      - Current week + next 2 weeks horizon (weighted 1.0, 0.85, 0.70)
      - Bye weeks & injuries (reflected in 0-point projections)
      - Simulates replacing bench players with waiver candidate
      - Strictly ranks by net marginal lineup gain
      - Recommends best drop candidate for each add
      - Computes runner-up alternative & explanation
    """
    all_players = all_players or {}
    resolver = player_name_resolver or (lambda x: x)

    # 1. Baseline starting lineup score across horizon
    w0_proj = projections
    w1_proj = future_projections.get(current_week + 1, projections)
    w2_proj = future_projections.get(current_week + 2, projections)

    base_w0 = solve_lineup(my_roster_ids, w0_proj, all_players=all_players)["total_pts"]
    base_w1 = solve_lineup(my_roster_ids, w1_proj, all_players=all_players)["total_pts"]
    base_w2 = solve_lineup(my_roster_ids, w2_proj, all_players=all_players)["total_pts"]

    # 2. Identify droppable bench players
    # Exclude: active starters, reserve/IR slot, and untouchable elite injured players
    bench_pids = [
        pid for pid in my_roster_ids
        if pid not in my_starters_ids and pid not in my_reserve_ids
    ]

    # Don't drop K or DEF unless we have multiple
    my_positions = [projections.get(p, {}).get("pos") or (all_players.get(p, {}).get("position") if isinstance(all_players.get(p), dict) else "") for p in my_roster_ids]
    k_count = my_positions.count("K")
    def_count = my_positions.count("DEF")

    eligible_drops = []
    for pid in bench_pids:
        pos = projections.get(pid, {}).get("pos") or (all_players.get(pid, {}).get("position") if isinstance(all_players.get(pid), dict) else "")
        if pos == "K" and k_count <= 1:
            continue
        if pos == "DEF" and def_count <= 1:
            continue
        # Player value on bench (average projected points across horizon)
        val = (
            w0_proj.get(pid, {}).get("pts", 0.0) * 1.0 +
            w1_proj.get(pid, {}).get("pts", 0.0) * 0.85 +
            w2_proj.get(pid, {}).get("pts", 0.0) * 0.70
        ) / 2.55
        eligible_drops.append((pid, val))

    # Sort droppable players ascending (lowest value first)
    eligible_drops.sort(key=lambda x: (x[1], x[0]))

    default_drop_id = eligible_drops[0][0] if eligible_drops else (bench_pids[-1] if bench_pids else None)

    # 3. Gather top unrostered candidates across ALL positions
    candidates_by_pos: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {
        "QB": [], "RB": [], "WR": [], "TE": [], "K": [], "DEF": []
    }

    for pid, pdata in w0_proj.items():
        if pid in all_rostered_ids:
            continue
        pos = pdata.get("pos")
        pts = pdata.get("pts", 0.0)
        inj = pdata.get("injury_status", "")

        # Skip players on IR/Out
        if inj in ("IR", "PUP", "Out", "Doubtful"):
            continue

        if pos in candidates_by_pos and pts > 0.0:
            candidates_by_pos[pos].append((pid, pdata))

    # Keep top 12 per position
    candidate_pids: List[str] = []
    for pos, clist in candidates_by_pos.items():
        clist.sort(key=lambda x: (-x[1]["pts"], x[0]))
        for pid, _ in clist[:12]:
            candidate_pids.append(pid)

    # 4. Evaluate marginal gain for each candidate
    evaluated_targets: List[Dict[str, Any]] = []

    for cpid in candidate_pids:
        cdata = w0_proj.get(cpid, {})
        cpos = cdata.get("pos", "")
        cpts_w0 = cdata.get("pts", 0.0)
        cpts_w1 = w1_proj.get(cpid, {}).get("pts", cpts_w0)
        cpts_w2 = w2_proj.get(cpid, {}).get("pts", cpts_w0)

        best_gain = -999.0
        best_drop = default_drop_id

        # Test against eligible drops
        drops_to_test = [d[0] for d in eligible_drops[:2]] if eligible_drops else ([default_drop_id] if default_drop_id else [])

        for dpid in drops_to_test:
            simulated_roster = [p for p in my_roster_ids if p != dpid] + [cpid]

            new_w0 = solve_lineup(simulated_roster, w0_proj, all_players=all_players)["total_pts"]
            new_w1 = solve_lineup(simulated_roster, w1_proj, all_players=all_players)["total_pts"]
            new_w2 = solve_lineup(simulated_roster, w2_proj, all_players=all_players)["total_pts"]

            delta_w0 = new_w0 - base_w0
            delta_w1 = new_w1 - base_w1
            delta_w2 = new_w2 - base_w2

            horizon_gain = (1.0 * delta_w0 + 0.85 * delta_w1 + 0.70 * delta_w2) / 2.55

            if horizon_gain > best_gain:
                best_gain = horizon_gain
                best_drop = dpid

        cname = resolver(cpid)
        drop_name = resolver(best_drop) if best_drop else "None"

        # Suggested FAAB bid heuristic
        if best_gain >= 3.0:
            suggested_bid = min(int(faab_remaining * 0.35), int(best_gain * 3.5))
        elif best_gain >= 1.0:
            suggested_bid = min(int(faab_remaining * 0.15), max(2, int(best_gain * 2.0)))
        elif best_gain > 0.0:
            suggested_bid = 1
        else:
            suggested_bid = 0

        evaluated_targets.append({
            "player_id": cpid,
            "name": cname,
            "pos": cpos,
            "team": cdata.get("team", ""),
            "injury_status": cdata.get("injury_status", ""),
            "w0_proj": cpts_w0,
            "marginal_gain": round(best_gain, 2),
            "suggested_drop_id": best_drop,
            "suggested_drop_name": drop_name,
            "suggested_bid": suggested_bid,
        })

    # Sort strictly by: (-marginal_gain, -w0_proj, player_id)
    evaluated_targets.sort(key=lambda x: (-x["marginal_gain"], -x["w0_proj"], x["player_id"]))

    # Summary table per position
    best_per_pos: Dict[str, Optional[Dict[str, Any]]] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        best_per_pos[pos] = next((t for t in evaluated_targets if t["pos"] == pos), None)

    # Top recommendations
    top_targets = evaluated_targets[:6]

    # Runner-up comparison
    top_target = top_targets[0] if top_targets else None
    runner_up = top_targets[1] if len(top_targets) > 1 else None
    diff_pos_alt = next((t for t in top_targets[1:] if t["pos"] != (top_target["pos"] if top_target else "")), None)

    runner_up_reasoning = ""
    if top_target and runner_up:
        gap = round(top_target["marginal_gain"] - runner_up["marginal_gain"], 2)
        if gap > 0:
            runner_up_reasoning = (
                f"Runner-up: {runner_up['name']} ({runner_up['pos']}, +{runner_up['marginal_gain']:.1f} pts/wk). "
                f"Lost to {top_target['name']} by {gap:.1f} weekly marginal points."
            )
        else:
            runner_up_reasoning = (
                f"Runner-up: {runner_up['name']} ({runner_up['pos']}) tied on marginal value (+{runner_up['marginal_gain']:.1f} pts/wk). "
                f"{top_target['name']} wins tiebreaker on Week {current_week} projected ceiling ({top_target['w0_proj']:.1f} vs {runner_up['w0_proj']:.1f} pts)."
            )

    hold_faab = (top_target["marginal_gain"] < 0.5) if top_target else True

    return {
        "current_week": current_week,
        "faab_remaining": faab_remaining,
        "baseline_lineup_pts": round(base_w0, 2),
        "hold_faab": hold_faab,
        "top_targets": top_targets,
        "top_target": top_target,
        "runner_up": runner_up,
        "runner_up_reasoning": runner_up_reasoning,
        "different_pos_alternative": diff_pos_alt,
        "best_per_position": best_per_pos,
    }


# ---------------------------------------------------------------------------
# 3. Start / Sit Optimizer
# ---------------------------------------------------------------------------

def evaluate_start_sit(
    my_roster_ids: List[str],
    current_starters_ids: List[str],
    projections: Dict[str, Dict[str, Any]],
    scoreboard: List[Dict[str, Any]],
    player_name_resolver: Optional[Any] = None,
    all_players: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compares Anthony's currently set starting lineup in Sleeper against the optimal lineup.
    Detects recommended swaps, close calls (< 2.0 pts), and injury/weather risks.
    """
    resolver = player_name_resolver or (lambda x: x)
    all_players = all_players or {}

    # Detect locked teams
    locked_teams: Set[str] = set()
    for ev in scoreboard:
        if ev.get("state") in ("in", "post"):
            for t in ev.get("teams", []):
                locked_teams.add(t)

    # Detect locked starters
    locked_starters: Dict[str, str] = {}
    for i, pid in enumerate(current_starters_ids):
        if i < len(ROSTER_SLOTS):
            slot = ROSTER_SLOTS[i]
            team = projections.get(pid, {}).get("team", "")
            if team in locked_teams:
                locked_starters[slot] = pid

    optimal = solve_lineup(
        my_roster_ids,
        projections,
        locked_starters=locked_starters,
        all_players=all_players,
    )

    optimal_starter_ids = set(s["id"] for s in optimal["starters"])
    current_set_starter_ids = set(current_starters_ids)

    # Identify swaps needed
    players_to_bench = [pid for pid in current_starters_ids if pid not in optimal_starter_ids]
    players_to_start = [s["id"] for s in optimal["starters"] if s["id"] not in current_set_starter_ids]

    swaps: List[Dict[str, Any]] = []
    for bench_pid, start_pid in zip(players_to_bench, players_to_start):
        bproj = projections.get(bench_pid, {}).get("pts", 0.0)
        sproj = projections.get(start_pid, {}).get("pts", 0.0)
        swaps.append({
            "bench_player": resolver(bench_pid),
            "bench_pts": bproj,
            "start_player": resolver(start_pid),
            "start_pts": sproj,
            "pts_gain": round(sproj - bproj, 2),
        })

    # Identify close calls: starters where the best bench replacement is within 2.0 pts
    close_calls: List[Dict[str, Any]] = []
    bench_by_pos: Dict[str, List[Dict[str, Any]]] = {}
    for b in optimal["bench"]:
        bench_by_pos.setdefault(b["pos"], []).append(b)

    for starter in optimal["starters"]:
        slot = starter["slot"]
        pos = starter["pos"]
        pts = starter["pts"]

        # Eligible bench challengers
        challengers = bench_by_pos.get(pos, [])
        if "FLEX" in slot:
            challengers = [b for b in optimal["bench"] if b["pos"] in FLEX_ELIGIBLE]

        if challengers:
            top_bench = challengers[0]
            margin = round(pts - top_bench["pts"], 2)
            if 0.0 <= margin <= 2.2:
                close_calls.append({
                    "slot": slot,
                    "starter_name": resolver(starter["id"]),
                    "starter_pts": pts,
                    "starter_injury": starter.get("injury_status", ""),
                    "bench_name": resolver(top_bench["id"]),
                    "bench_pts": top_bench["pts"],
                    "bench_injury": top_bench.get("injury_status", ""),
                    "margin": margin,
                    "reasoning": f"Start {resolver(starter['id'])} over {resolver(top_bench['id'])} by {margin:.1f} projected pts."
                })

    return {
        "optimal_total_pts": optimal["total_pts"],
        "optimal_starters": optimal["starters"],
        "swaps_recommended": swaps,
        "is_already_optimal": len(swaps) == 0,
        "close_calls": close_calls,
        "bench": optimal["bench"],
    }


# ---------------------------------------------------------------------------
# 4. Matchup Preview Engine
# ---------------------------------------------------------------------------

def evaluate_matchup_preview(
    my_matchup: Dict[str, Any],
    opponent_matchup: Dict[str, Any],
    my_roster_ids: List[str],
    opponent_roster_ids: List[str],
    opponent_name: str,
    projections: Dict[str, Dict[str, Any]],
    scoreboard: List[Dict[str, Any]],
    player_name_resolver: Optional[Any] = None,
    all_players: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Projects matchup outcome, remaining points, rough win probability, and swing players.
    """
    resolver = player_name_resolver or (lambda x: x)
    all_players = all_players or {}

    my_live_pts = float(my_matchup.get("points", 0.0) or 0.0)
    opp_live_pts = float(opponent_matchup.get("points", 0.0) or 0.0)

    my_opt = solve_lineup(my_roster_ids, projections, all_players=all_players)
    opp_opt = solve_lineup(opponent_roster_ids, projections, all_players=all_players)

    # Detect locked teams
    locked_teams: Set[str] = set()
    for ev in scoreboard:
        if ev.get("state") in ("in", "post"):
            for t in ev.get("teams", []):
                locked_teams.add(t)

    # Remaining projected points for unlocked starters
    my_remaining = sum(s["pts"] for s in my_opt["starters"] if s.get("team") not in locked_teams)
    opp_remaining = sum(s["pts"] for s in opp_opt["starters"] if s.get("team") not in locked_teams)

    my_projected_total = round(my_live_pts + my_remaining, 2)
    opp_projected_total = round(opp_live_pts + opp_remaining, 2)

    # Rough win probability using normal distribution approximation
    # Estimated standard deviation ~ 5.5 pts per remaining unlocked starter
    my_unlocked_count = len([s for s in my_opt["starters"] if s.get("team") not in locked_teams])
    opp_unlocked_count = len([s for s in opp_opt["starters"] if s.get("team") not in locked_teams])

    sigma_my = math.sqrt(max(1, my_unlocked_count)) * 5.2
    sigma_opp = math.sqrt(max(1, opp_unlocked_count)) * 5.2
    sigma_diff = math.sqrt(sigma_my**2 + sigma_opp**2)

    diff = my_projected_total - opp_projected_total
    if sigma_diff > 0:
        z = diff / sigma_diff
        win_prob = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    else:
        win_prob = 1.0 if diff > 0 else (0.0 if diff < 0 else 0.5)

    # Key pivot swing players (highest projected remaining starters)
    my_swing = sorted(
        [s for s in my_opt["starters"] if s.get("team") not in locked_teams],
        key=lambda x: -x["pts"]
    )[:3]
    opp_swing = sorted(
        [s for s in opp_opt["starters"] if s.get("team") not in locked_teams],
        key=lambda x: -x["pts"]
    )[:3]

    return {
        "opponent_name": opponent_name,
        "my_live_pts": my_live_pts,
        "opp_live_pts": opp_live_pts,
        "my_projected_total": my_projected_total,
        "opp_projected_total": opp_projected_total,
        "projected_margin": round(my_projected_total - opp_projected_total, 2),
        "win_probability_pct": round(win_prob * 100, 1),
        "my_key_players": [
            {"name": resolver(s["id"]), "slot": s["slot"], "pts": s["pts"], "team": s["team"]}
            for s in my_swing
        ],
        "opp_key_players": [
            {"name": resolver(s["id"]), "slot": s["slot"], "pts": s["pts"], "team": s["team"]}
            for s in opp_swing
        ],
    }
