"""
Waiver Wire Engine — Tony Romo's DUI Mugshot
Analyzes national trending adds, cross-references against live league rosters,
applies team priority rules (WR upside first, RB handcuffs second), and suggests drops & FAAB bids.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional, Set

import requests

log = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"


def _cb() -> str:
    return f"cb={random.randint(100_000, 999_999)}"


def _get(url: str, timeout: int = 10) -> Any:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_waiver_recommendations(
    league_id: str,
    my_roster_id: int = 4,
    player_id_resolver: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Fetch live Sleeper trending adds and league rosters, cross-reference unrostered players,
    and generate priority waiver wire recommendations for Anthony's team.
    """
    # 1. Fetch live NFL state to get week number
    try:
        nfl_state = _get(f"{SLEEPER_BASE}/state/nfl?{_cb()}")
        current_week = nfl_state.get("week", 1)
    except Exception as e:
        log.warning("Could not fetch NFL state for waivers: %s", e)
        current_week = 1

    # 2. Fetch all rosters in the league
    rosters: List[Dict[str, Any]] = []
    all_rostered_ids: Set[str] = set()
    my_roster_players: List[str] = []
    my_starters: List[str] = []

    try:
        rosters = _get(f"{SLEEPER_BASE}/league/{league_id}/rosters?{_cb()}")
        for r in rosters:
            rid = r.get("roster_id")
            plist = r.get("players") or []
            all_rostered_ids.update(plist)
            if rid == my_roster_id:
                my_roster_players = plist
                my_starters = r.get("starters") or []
    except Exception as e:
        log.error("Failed to fetch league rosters for waiver engine: %s", e)

    # 3. Fetch national trending adds
    trending_adds: List[Dict[str, Any]] = []
    try:
        trending_adds = _get(
            f"{SLEEPER_BASE}/players/nfl/trending/add?lookback_hours=24&limit=25&{_cb()}"
        )
    except Exception as e:
        log.error("Failed to fetch trending adds: %s", e)

    # 4. Filter trending adds to unrostered players in this league
    unrostered_targets: List[Dict[str, Any]] = []
    for target in trending_adds:
        pid = target.get("player_id")
        if not pid or pid in all_rostered_ids:
            continue

        # Resolve player name / details
        name = pid
        pos = "UNKNOWN"
        team = "FA"
        if player_id_resolver:
            name = player_id_resolver(pid)
        
        # Check single player endpoint if pos is missing
        try:
            # Only do this if player_id_resolver wasn't provided or pos is still UNKNOWN
            # To avoid N+1, we will try to use the bulk players fetch if possible
            # We import get_all_players from app.main locally to avoid circular imports if needed
            from app.main import get_all_players
            all_players = get_all_players()
            
            pinfo = all_players.get(pid)
            if pinfo and isinstance(pinfo, dict):
                first = pinfo.get("first_name", "")
                last = pinfo.get("last_name", "")
                if first or last:
                    name = f"{first} {last}".strip()
                pos = pinfo.get("position") or "FLEX"
                team = pinfo.get("team") or "FA"
        except Exception:
            pass

        count = target.get("count", 0)

        # Apply positional priority scoring per SKILL.md
        if pos == "WR":
            priority = 1
            reason = f"High priority: WR upside (+{count:,} national adds). Addresses team's #11 WR standing."
        elif pos == "RB":
            priority = 2
            reason = f"Secondary priority: RB depth/handcuff (+{count:,} national adds)."
        elif pos in ("QB", "TE"):
            priority = 3
            reason = f"Low priority: Backup {pos}. (Rule: Never roster 2nd QB/TE unless starter is out)."
        else:
            priority = 4
            reason = f"Flex depth target (+{count:,} national adds)."

        unrostered_targets.append({
            "player_id": pid,
            "name": name,
            "position": pos,
            "team": team,
            "add_count": count,
            "priority_rank": priority,
            "recommendation_reason": reason,
        })

    unrostered_targets.sort(key=lambda x: (x["priority_rank"], -x["add_count"]))

    # 5. Identify recommended drop candidate from Anthony's bench
    bench_pids = [p for p in my_roster_players if p not in my_starters]
    recommended_drop = None
    if bench_pids:
        drop_pid = bench_pids[-1]
        drop_name = player_id_resolver(drop_pid) if player_id_resolver else drop_pid
        recommended_drop = {
            "player_id": drop_pid,
            "name": drop_name,
            "reason": "End-of-bench piece; low projected starting lineup allocation.",
        }

    return {
        "league_id": league_id,
        "roster_id": my_roster_id,
        "week": current_week,
        "targets": unrostered_targets[:8],
        "recommended_drop": recommended_drop,
        "faab_guidance": {
            "budget_total": 100,
            "strategy": "Aggressive $5-$15 FAAB bid on top WR target; $0-$2 free agent claim for backup RBs.",
        },
    }
