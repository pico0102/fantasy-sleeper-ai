"""
Waiver Wire Engine — Tony Romo's DUI Mugshot (v3.0)
Analyzes national trending adds, cross-references against live league rosters,
applies dynamic positional priority, and suggests drops based on actual performance.

v3.0 changes:
  - Accepts pre-fetched data (no duplicate API calls)
  - Uses actual season PPG for drop candidates (not preseason projections)
  - Dynamic positional needs based on current roster performance
  - Pre-indexed handcuff scan (eliminates O(320K) linear scan)
  - Removed hardcoded "#11 WR standing" — uses real data
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger(__name__)


def get_waiver_recommendations(
    league_id: str,
    my_roster_id: int = 4,
    player_id_resolver: Optional[Any] = None,
    *,
    nfl_state: Optional[dict] = None,
    rosters: Optional[list[dict]] = None,
    all_players: Optional[dict[str, Any]] = None,
    trending_adds: Optional[list[dict]] = None,
    actual_points: Optional[dict[str, dict]] = None,
    injury_map: Optional[dict[str, str]] = None,
    wr_rank: Optional[int] = None,
    rb_rank: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Generate priority waiver wire recommendations for Anthony's team.

    Accepts pre-fetched data to avoid duplicate API calls.
    Falls back to empty data (with warnings) if data isn't provided.
    """
    current_week = 1
    if nfl_state:
        current_week = nfl_state.get("week", 1)

    if rosters is None:
        rosters = []
    if all_players is None:
        all_players = {}
    if trending_adds is None:
        trending_adds = []
    if actual_points is None:
        actual_points = {}
    if injury_map is None:
        injury_map = {}

    # ── 1. Build rostered player set ────────────────────────────────────
    all_rostered_ids: Set[str] = set()
    my_roster_players: List[str] = []
    my_starters: List[str] = []

    for r in rosters:
        rid = r.get("roster_id")
        plist = r.get("players") or []
        all_rostered_ids.update(plist)
        if rid == my_roster_id:
            my_roster_players = plist
            my_starters = r.get("starters") or []

    # ── 2. Compute dynamic positional needs ─────────────────────────────
    # Instead of hardcoded "#11 WR", use actual rank data
    wr_need_label = ""
    rb_need_label = ""
    if wr_rank is not None:
        if wr_rank >= 9:
            wr_need_label = f"Addresses team's #{wr_rank}/12 WR weakness."
        elif wr_rank >= 6:
            wr_need_label = f"WR depth (currently #{wr_rank}/12)."
        else:
            wr_need_label = f"WR surplus depth (#{wr_rank}/12 — already strong)."

    if rb_rank is not None:
        if rb_rank >= 9:
            rb_need_label = f"Addresses team's #{rb_rank}/12 RB weakness."
        elif rb_rank >= 6:
            rb_need_label = f"RB depth (currently #{rb_rank}/12)."
        else:
            rb_need_label = f"RB handcuff depth (#{rb_rank}/12 — already strong)."

    # Determine priority: which position is the bigger need?
    wr_is_bigger_need = (wr_rank or 6) >= (rb_rank or 6)

    # ── 3. Filter trending adds to unrostered players ───────────────────
    unrostered_targets: List[Dict[str, Any]] = []
    for target in trending_adds:
        pid = target.get("player_id")
        if not pid or pid in all_rostered_ids:
            continue

        # Resolve player details
        name = pid
        pos = "UNKNOWN"
        team = "FA"

        pinfo = all_players.get(pid)
        if pinfo and isinstance(pinfo, dict):
            # Filter out players actively on IR, PUP, or Out
            inj = pinfo.get("injury_status")
            if inj in ("IR", "PUP", "Out"):
                continue

            first = pinfo.get("first_name", "")
            last = pinfo.get("last_name", "")
            if first or last:
                name = f"{first} {last}".strip()
            pos = pinfo.get("position") or "FLEX"
            team = pinfo.get("team") or "FA"
        elif player_id_resolver:
            name = player_id_resolver(pid)

        count = target.get("count", 0)

        # Check actual production if available
        actual = actual_points.get(pid, {})
        actual_ppg = actual.get("ppg", 0.0)
        actual_note = f" Actual PPG: {actual_ppg:.1f}." if actual_ppg > 0 else ""

        # Apply dynamic positional priority
        if pos == "WR":
            priority = 1 if wr_is_bigger_need else 2
            reason = f"{'High' if wr_is_bigger_need else 'Secondary'} priority: WR upside (+{count:,} national adds).{actual_note} {wr_need_label}"
        elif pos == "RB":
            priority = 2 if wr_is_bigger_need else 1
            reason = f"{'Secondary' if wr_is_bigger_need else 'High'} priority: RB depth/handcuff (+{count:,} national adds).{actual_note} {rb_need_label}"
        elif pos in ("QB", "TE"):
            priority = 3
            reason = f"Low priority: Backup {pos}. (Rule: Never roster 2nd QB/TE unless starter is out)."
        else:
            priority = 4
            reason = f"Flex depth target (+{count:,} national adds).{actual_note}"

        unrostered_targets.append({
            "player_id": pid,
            "name": name,
            "position": pos,
            "team": team,
            "add_count": count,
            "priority_rank": priority,
            "recommendation_reason": reason.strip(),
        })

    # Sort by priority first, then by add count
    unrostered_targets.sort(key=lambda x: (x["priority_rank"], -x["add_count"]))

    # ── 4. Proactive Handcuffing Scan (pre-indexed by team) ─────────────
    handcuff_notes = []
    try:
        from app.gm_engine import _BOARD

        # Pre-index all_players by team+position for fast lookups
        team_rbs: Dict[str, list] = {}
        for pid, pinfo in all_players.items():
            if isinstance(pinfo, dict) and pinfo.get("position") == "RB":
                team_key = pinfo.get("team", "")
                if team_key:
                    team_rbs.setdefault(team_key, []).append((pid, pinfo))

        for p in _BOARD:
            pos = p.get("pos")
            posrank = p.get("posrank", "")
            if pos == "RB" and posrank.startswith("RB"):
                try:
                    rank_num = int(posrank.replace("RB", ""))
                    if rank_num <= 32:
                        name = p.get("name", "")
                        team = p.get("team", "")
                        # Fast lookup by team instead of scanning all 10K players
                        for pid, pinfo in team_rbs.get(team, []):
                            if pinfo.get("last_name", "") in name:
                                inj = pinfo.get("injury_status")
                                if inj in ("IR", "Out", "PUP"):
                                    handcuff_notes.append(
                                        f"⚠️ {name} is {inj}. Prioritize their backup!"
                                    )
                                break
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log.warning(f"Handcuff scan failed: {e}")

    # ── 5. Identify recommended drop candidate ──────────────────────────
    bench_pids = [p for p in my_roster_players if p not in my_starters]
    recommended_drop = None

    try:
        from app.gm_engine import get_player_projection

        valid_drops = []
        for pid in bench_pids:
            pinfo = all_players.get(pid, {})
            pos = pinfo.get("position", "") if isinstance(pinfo, dict) else ""
            inj = injury_map.get(pid, "")

            # Do not ever drop Defense or Kicker since we usually only carry 1
            if pos in ("K", "DEF"):
                continue

            dname = ""
            if player_id_resolver:
                dname = player_id_resolver(pid)
            if not dname:
                dname = pid

            clean_name = dname.split(" (")[0] if " (" in dname else dname

            # Prefer actual PPG for drop decision; fall back to preseason
            actual = actual_points.get(pid, {})
            if actual.get("games_played", 0) >= 2:
                # Use actual PPG — more reliable after 2+ games
                sort_value = actual["ppg"]
                reason_prefix = f"Lowest actual PPG ({actual['ppg']:.1f}) on bench"
            else:
                # Not enough actual data — use preseason projection
                proj_pts = get_player_projection(clean_name).get("pts", 0.0)
                sort_value = proj_pts
                reason_prefix = f"Lowest projected value on bench (preseason)"

            # IR-stashed players are always the top drop candidate
            if inj in ("IR", "Out", "PUP"):
                sort_value = -1000  # Force to top of drop list
                reason_prefix = f"Currently {inj}"

            valid_drops.append((pid, dname, sort_value, reason_prefix))

        if valid_drops:
            valid_drops.sort(key=lambda x: x[2])  # lowest value first
            drop_pid, drop_name, _, reason_prefix = valid_drops[0]
            recommended_drop = {
                "player_id": drop_pid,
                "name": drop_name,
                "reason": f"{reason_prefix} among non-K/DEF bench players.",
            }
        elif bench_pids:
            # Fallback
            drop_pid = bench_pids[-1]
            drop_name = player_id_resolver(drop_pid) if player_id_resolver else drop_pid
            recommended_drop = {
                "player_id": drop_pid,
                "name": drop_name,
                "reason": "End-of-bench piece.",
            }
    except Exception as e:
        log.warning(f"Error calculating drop candidate: {e}")
        if bench_pids:
            drop_pid = bench_pids[-1]
            drop_name = player_id_resolver(drop_pid) if player_id_resolver else drop_pid
            recommended_drop = {
                "player_id": drop_pid,
                "name": drop_name,
                "reason": "End-of-bench piece.",
            }

    # ── 6. FAAB guidance ────────────────────────────────────────────────
    # Dynamic based on week — more aggressive early, conservative late
    if current_week <= 4:
        faab_strategy = "Aggressive early: $8-$18 FAAB on top target; $1-$3 for depth."
    elif current_week <= 10:
        faab_strategy = "Balanced: $5-$12 FAAB on high-upside targets; $0-$2 for depth."
    else:
        faab_strategy = "Conservative late-season: $3-$8 FAAB unless a league-winner emerges."

    return {
        "league_id": league_id,
        "roster_id": my_roster_id,
        "week": current_week,
        "targets": unrostered_targets[:8],
        "handcuff_notes": handcuff_notes,
        "recommended_drop": recommended_drop,
        "faab_guidance": {
            "budget_total": 100,
            "strategy": faab_strategy,
        },
    }
