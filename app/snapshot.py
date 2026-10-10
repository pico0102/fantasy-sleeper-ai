"""League snapshot orchestrator with concurrent fetching and provenance tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.config import LEAGUE_ID, MY_ROSTER_ID
from app.sleeper import (
    fetch_all_rosters,
    fetch_espn_scoreboard,
    fetch_league,
    fetch_league_users,
    fetch_matchups,
    fetch_nfl_state,
    fetch_transactions,
    fetch_trending_adds,
    fetch_weekly_projections,
    fetch_weekly_stats,
    get_all_players,
)

log = logging.getLogger(__name__)


class DataSources:
    """Tracks the status of each external source for provenance and health reporting."""

    def __init__(self):
        self._sources: Dict[str, Dict[str, Any]] = {}

    def ok(self, name: str, detail: str = "") -> None:
        self._sources[name] = {
            "status": "ok",
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def error(self, name: str, err: str) -> None:
        self._sources[name] = {
            "status": "error",
            "error": str(err),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._sources)

    def is_healthy(self) -> bool:
        """True if critical sources (state, rosters, league) are ok."""
        for crit in ("nfl_state", "rosters", "league"):
            if self._sources.get(crit, {}).get("status") == "error":
                return False
        return True


class LeagueSnapshot:
    """Immutable snapshot of live fantasy league state at a single moment in time."""

    def __init__(
        self,
        season: str,
        week: int,
        season_type: str,
        league: Dict[str, Any],
        rosters: List[Dict[str, Any]],
        owner_map: Dict[str, str],
        all_players: Dict[str, Any],
        matchups: List[Dict[str, Any]],
        transactions: List[Dict[str, Any]],
        trending_adds: List[Dict[str, Any]],
        projections: Dict[str, Dict[str, Any]],
        future_projections: Dict[int, Dict[str, Dict[str, Any]]],
        actual_stats_by_week: Dict[int, Dict[str, Dict[str, Any]]],
        scoreboard: List[Dict[str, Any]],
        data_sources: Dict[str, Dict[str, Any]],
    ):
        self.season = season
        self.week = week
        self.season_type = season_type
        self.league = league
        self.rosters = rosters
        self.owner_map = owner_map
        self.all_players = all_players
        self.matchups = matchups
        self.transactions = transactions
        self.trending_adds = trending_adds
        self.projections = projections
        self.future_projections = future_projections
        self.actual_stats_by_week = actual_stats_by_week
        self.scoreboard = scoreboard
        self.data_sources = data_sources

        # Locate my roster
        self.my_roster = next((r for r in rosters if r.get("roster_id") == MY_ROSTER_ID), {})

        # Locate my matchup and opponent
        self.my_matchup: Dict[str, Any] = {}
        self.opponent_matchup: Dict[str, Any] = {}
        self.opponent_roster: Dict[str, Any] = {}
        self.opponent_name: str = "Unknown"
        self.opponent_roster_id: Optional[int] = None

        my_matchup_id = None
        for m in matchups:
            if m.get("roster_id") == MY_ROSTER_ID:
                self.my_matchup = m
                my_matchup_id = m.get("matchup_id")
                break

        if my_matchup_id is not None:
            for m in matchups:
                if m.get("matchup_id") == my_matchup_id and m.get("roster_id") != MY_ROSTER_ID:
                    self.opponent_matchup = m
                    self.opponent_roster_id = m.get("roster_id")
                    self.opponent_roster = next(
                        (r for r in rosters if r.get("roster_id") == self.opponent_roster_id), {}
                    )
                    opp_owner_id = str(self.opponent_roster.get("owner_id", ""))
                    self.opponent_name = self.owner_map.get(
                        opp_owner_id, f"Roster {self.opponent_roster_id}"
                    )
                    break

        # Compute locked teams (whose games have kicked off or finished)
        self.locked_teams: Set[str] = set()
        for ev in scoreboard:
            if ev.get("state") in ("in", "post"):
                for t in ev.get("teams", []):
                    self.locked_teams.add(t)


async def load_league_snapshot(horizon_weeks: int = 2) -> LeagueSnapshot:
    """
    Concurrently load complete state, rosters, weekly projections, actuals, and weather.
    """
    t0 = time.time()
    ds = DataSources()

    # Step 1: Base league state, rosters, league metadata, and bulk players
    try:
        (
            state,
            league,
            rosters,
            owner_map,
            all_players,
        ) = await asyncio.gather(
            fetch_nfl_state(),
            fetch_league(),
            fetch_all_rosters(),
            fetch_league_users(),
            get_all_players(),
        )
        season = str(state.get("season", "2026"))
        week = int(state.get("week", 1))
        season_type = str(state.get("season_type", "regular"))
        ds.ok("nfl_state", f"Week {week}, {season}")
        ds.ok("league", f"League {LEAGUE_ID}")
        ds.ok("rosters", f"{len(rosters)} teams loaded")
        ds.ok("users", f"{len(owner_map)} managers mapped")
        ds.ok("players_db", f"{len(all_players)} players in database")
    except Exception as e:
        log.error(f"Core state fetch failed: {e}")
        ds.error("core_state", str(e))
        season, week, season_type = "2026", 1, "regular"
        league, rosters, owner_map, all_players = {}, [], {}, {}

    # Step 2: Concurrent secondary fetches (Matchups, Transactions, Trending, Scoreboard, Projections)
    future_weeks = [week + i for i in range(1, horizon_weeks + 1) if week + i <= 18]
    past_weeks = list(range(1, week))

    try:
        matchup_task = fetch_matchups(week)
        tx_task = fetch_transactions(week)
        trending_task = fetch_trending_adds()
        scoreboard_task = fetch_espn_scoreboard(week)
        curr_proj_task = fetch_weekly_projections(season, week)

        # Future projections tasks
        future_proj_tasks = [fetch_weekly_projections(season, fw) for fw in future_weeks]

        # Past stats tasks (for real actual performance)
        past_stats_tasks = [fetch_weekly_stats(season, pw) for pw in past_weeks]

        results = await asyncio.gather(
            matchup_task,
            tx_task,
            trending_task,
            scoreboard_task,
            curr_proj_task,
            *future_proj_tasks,
            *past_stats_tasks,
            return_exceptions=True,
        )

        matchups = results[0] if not isinstance(results[0], Exception) else []
        transactions = results[1] if not isinstance(results[1], Exception) else []
        trending_adds = results[2] if not isinstance(results[2], Exception) else []
        scoreboard = results[3] if not isinstance(results[3], Exception) else []
        projections = results[4] if not isinstance(results[4], Exception) else {}

        if isinstance(results[0], Exception):
            ds.error("matchups", str(results[0]))
        else:
            ds.ok("matchups", f"Week {week}")

        if isinstance(results[1], Exception):
            ds.error("transactions", str(results[1]))
        else:
            ds.ok("transactions", f"Week {week}")

        if isinstance(results[2], Exception):
            ds.error("trending_adds", str(results[2]))
        else:
            ds.ok("trending_adds", f"{len(trending_adds)} players")

        if isinstance(results[3], Exception):
            ds.error("espn_weather", str(results[3]))
        else:
            ds.ok("espn_weather", f"{len(scoreboard)} games")

        if isinstance(results[4], Exception):
            ds.error("projections", str(results[4]))
        else:
            ds.ok("projections", f"Week {week}: {len(projections)} projections")

        # Parse future projections
        future_projections: Dict[int, Dict[str, Dict[str, Any]]] = {}
        idx = 5
        for fw in future_weeks:
            r = results[idx]
            if not isinstance(r, Exception):
                future_projections[fw] = r
            idx += 1

        # Parse past stats
        actual_stats_by_week: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for pw in past_weeks:
            r = results[idx]
            if not isinstance(r, Exception):
                actual_stats_by_week[pw] = r
            idx += 1

    except Exception as e:
        log.error(f"Secondary fetches failed: {e}")
        ds.error("secondary_data", str(e))
        matchups, transactions, trending_adds, scoreboard, projections = [], [], [], [], {}
        future_projections, actual_stats_by_week = {}, {}

    log.info(f"Loaded full LeagueSnapshot in {time.time() - t0:.2f}s")
    return LeagueSnapshot(
        season=season,
        week=week,
        season_type=season_type,
        league=league,
        rosters=rosters,
        owner_map=owner_map,
        all_players=all_players,
        matchups=matchups,
        transactions=transactions,
        trending_adds=trending_adds,
        projections=projections,
        future_projections=future_projections,
        actual_stats_by_week=actual_stats_by_week,
        scoreboard=scoreboard,
        data_sources=ds.to_dict(),
    )
