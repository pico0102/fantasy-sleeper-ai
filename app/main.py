"""
Fantasy Football AI Assistant — Tony Romo's DUI Mugshot (v3.0)
Sleeper league: "Loser eats an egg"  |  League ID: 1389359956657967104

v3.0 changes:
  - Async I/O with httpx (concurrent API calls via asyncio.gather)
  - TTL in-memory cache (5 min) for rosters, matchups, state
  - Data provenance tracking — every response shows which sources loaded/failed
  - Anti-hallucination: strict system prompt rules + injury filtering
  - Live blended projections (preseason + actual season PPG)
  - IR/Out/PUP players excluded from lineup optimizer
  - No duplicate Sleeper API calls per request
  - Startup cache warming for /players/nfl
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import textwrap
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google import genai
from pydantic import BaseModel

from app.waivers import get_waiver_recommendations
from app.gm_engine import (
    best_lineup,
    compute_actual_points,
    get_actual_standings,
    get_blended_projection,
    get_team_analytics,
    get_player_projection,
)

# ---------------------------------------------------------------------------
# Env + logging
# ---------------------------------------------------------------------------
load_dotenv()

GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "test-mock-key")
SLEEPER_LEAGUE_ID = os.getenv("SLEEPER_LEAGUE_ID", "1389359956657967104").strip()
SLEEPER_USER_ID   = os.getenv("SLEEPER_USER_ID", "pinec0ne").strip()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static data (bundled skill files)
# ---------------------------------------------------------------------------
_DATA_DIR = Path(__file__).parent.parent / ".agents" / "skills" / "loser-eats-an-egg" / "data"

def _load_json(name: str) -> Any:
    p = _DATA_DIR / name
    if p.exists():
        with p.open() as f:
            return json.load(f)
    return {}

PLAYER_IDS: dict[str, Any] = _load_json("player_ids.json")

# ---------------------------------------------------------------------------
# League constants
# ---------------------------------------------------------------------------
LEAGUE_ID    = SLEEPER_LEAGUE_ID
MY_ROSTER_ID = 4   # pinec0ne / Anthony's roster_id
SLEEPER_BASE = "https://api.sleeper.app/v1"

# ---------------------------------------------------------------------------
# TTL Cache
# ---------------------------------------------------------------------------
_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

def _cache_get(key: str) -> Any | None:
    """Return cached value if it exists and hasn't expired."""
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL_SECONDS:
        return entry["data"]
    return None

def _cache_set(key: str, data: Any) -> None:
    _cache[key] = {"data": data, "ts": time.time()}

# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------

def _cb() -> str:
    """Cache-buster query param."""
    return f"cb={random.randint(100_000, 999_999)}"

_http_client: httpx.AsyncClient | None = None

def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=15.0)
    return _http_client

async def _aget(url: str, timeout: float = 10.0) -> Any:
    """Async HTTP GET with JSON parsing."""
    client = _get_http_client()
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Bulk player cache (warmed on startup)
# ---------------------------------------------------------------------------
_ALL_PLAYERS_CACHE: dict[str, Any] | None = None

async def get_all_players() -> dict[str, Any]:
    global _ALL_PLAYERS_CACHE
    if _ALL_PLAYERS_CACHE is None:
        try:
            log.info("Fetching bulk player data from Sleeper...")
            t0 = time.time()
            _ALL_PLAYERS_CACHE = await _aget(f"{SLEEPER_BASE}/players/nfl", timeout=30.0)
            log.info(f"Fetched {len(_ALL_PLAYERS_CACHE)} players in {time.time() - t0:.3f}s")
        except Exception as e:
            log.error(f"Failed to fetch all players: {e}")
            _ALL_PLAYERS_CACHE = {}
    return _ALL_PLAYERS_CACHE

def get_all_players_sync() -> dict[str, Any]:
    """Synchronous accessor for modules that need the cache without awaiting."""
    return _ALL_PLAYERS_CACHE or {}

# ---------------------------------------------------------------------------
# Player name resolver
# ---------------------------------------------------------------------------

def resolve_player_name(pid: str) -> str:
    if pid in PLAYER_IDS:
        entry = PLAYER_IDS[pid]
        if isinstance(entry, str):
            return entry
        return (
            f"{entry.get('first_name', '')} {entry.get('last_name', '')} "
            f"({entry.get('team', '?')}, {entry.get('position', '?')})"
        ).strip()
    try:
        all_players = get_all_players_sync()
        if pid in all_players:
            p = all_players[pid]
            resolved_name = f"{p.get('first_name', '')} {p.get('last_name', '')} ({p.get('team', '?')}, {p.get('position', '?')})".strip()
            if not resolved_name or resolved_name == "()":
                resolved_name = pid
            PLAYER_IDS[pid] = resolved_name
            return resolved_name
    except Exception as e:
        log.warning(f"Error resolving player {pid}: {e}")

    fallback = f"Unknown({pid})"
    PLAYER_IDS[pid] = fallback
    return fallback

# ---------------------------------------------------------------------------
# Async Sleeper helpers
# ---------------------------------------------------------------------------

async def fetch_nfl_state() -> dict:
    cached = _cache_get("nfl_state")
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/state/nfl?{_cb()}")
    _cache_set("nfl_state", data)
    return data

async def fetch_all_rosters() -> list[dict]:
    cached = _cache_get("rosters")
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/rosters?{_cb()}")
    _cache_set("rosters", data)
    return data

async def fetch_league_users() -> dict[str, str]:
    cached = _cache_get("users")
    if cached:
        return cached
    users = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/users?{_cb()}")
    owner_map = {}
    for u in users:
        team_name = u.get("metadata", {}).get("team_name") or u.get("display_name", "Unknown")
        owner_id = str(u.get("user_id"))
        owner_map[owner_id] = team_name
    _cache_set("users", owner_map)
    return owner_map

async def fetch_matchups(week: int) -> list[dict]:
    cache_key = f"matchups_{week}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/matchups/{week}?{_cb()}")
    _cache_set(cache_key, data)
    return data

async def fetch_transactions(week: int) -> list[dict]:
    return await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/transactions/{week}?{_cb()}")

async def fetch_trending_adds() -> list[dict]:
    return await _aget(
        f"{SLEEPER_BASE}/players/nfl/trending/add?lookback_hours=24&limit=25&{_cb()}"
    )

async def fetch_matchup_history(current_week: int) -> Dict[int, list[dict]]:
    """Fetch matchup data for all completed weeks (for actual points)."""
    if current_week <= 1:
        return {}

    tasks = []
    cached_results = {}
    uncached_weeks = []
    weeks = list(range(1, current_week))

    for w in weeks:
        cache_key = f"matchups_{w}"
        cached = _cache_get(cache_key)
        if cached:
            cached_results[w] = cached
        else:
            uncached_weeks.append(w)

    # Fetch only uncached weeks concurrently
    if uncached_weeks:
        fetch_tasks = [
            _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/matchups/{w}?{_cb()}")
            for w in uncached_weeks
        ]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for w, result in zip(uncached_weeks, results):
            if isinstance(result, Exception):
                log.warning(f"Failed to fetch matchups for week {w}: {result}")
                continue
            _cache_set(f"matchups_{w}", result)
            cached_results[w] = result

    return cached_results

# ---------------------------------------------------------------------------
# Data provenance tracker
# ---------------------------------------------------------------------------

class DataSources:
    """Tracks the status of every external data fetch for transparency."""

    def __init__(self):
        self._sources: Dict[str, Dict[str, Any]] = {}

    def ok(self, name: str, detail: str = "") -> None:
        self._sources[name] = {
            "status": "ok",
            "detail": detail,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def error(self, name: str, err: str) -> None:
        self._sources[name] = {
            "status": "error",
            "error": str(err),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._sources)

    def to_context_block(self) -> str:
        """Format for injection into Gemini context."""
        lines = ["## ⚠️ Data Source Status"]
        for name, info in self._sources.items():
            label = name.replace("_", " ").title()
            if info["status"] == "ok":
                detail = info.get("detail", "")
                lines.append(f"✅ {label}: {detail}")
            else:
                err = info.get("error", "unknown error")
                lines.append(f"❌ {label} FAILED: {err} — do NOT make recommendations based on this data")
        lines.append("")
        lines.append("IMPORTANT: If any source above shows ❌, explicitly tell the user you couldn't load that data. Do NOT guess or make up information to fill the gap.")
        return "\n".join(lines)

# ---------------------------------------------------------------------------
# Context assembly (async, concurrent)
# ---------------------------------------------------------------------------

def _build_player_dict(pid: str, all_players: dict, current_week: int = 1) -> dict:
    """Build a player dict with blended projection and injury status."""
    name = resolve_player_name(pid)
    clean_name = name.split(" (")[0] if " (" in name else name

    # Get injury status from bulk player data
    pinfo = all_players.get(pid, {})
    injury_status = ""
    if isinstance(pinfo, dict):
        injury_status = pinfo.get("injury_status") or ""

    # Get blended projection (preseason + actual)
    proj = get_blended_projection(
        clean_name,
        player_id=pid,
        current_week=current_week,
    )
    pos = proj.get("pos")
    if not pos and isinstance(pinfo, dict):
        pos = pinfo.get("position", "")
    if not pos and " (" in name:
        parts = name.split(", ")
        if len(parts) >= 2:
            pos = parts[1].replace(")", "")

    result = {
        "id": pid,
        "name": name,
        "pos": pos,
        "pts": proj.get("pts", 0.0),
        "posrank": proj.get("posrank"),
        "source": proj.get("source", "preseason"),
        "injury_status": injury_status,
    }

    # Add actual PPG if available
    if proj.get("actual_ppg", 0) > 0:
        result["actual_ppg"] = proj["actual_ppg"]

    return result


async def build_context(question: str) -> tuple[str, dict]:
    """
    Assemble full league context for Gemini. Returns (context_str, data_sources_dict).
    Uses asyncio.gather for concurrent API calls.
    """
    log.info("Starting build_context...")
    t_start = time.time()
    ds = DataSources()
    ctx_parts: list[str] = []

    # ── Phase 1: Concurrent data fetching ──────────────────────────────
    try:
        t_fetch = time.time()
        (
            state,
            rosters,
            owner_map,
            all_players,
        ) = await asyncio.gather(
            fetch_nfl_state(),
            fetch_all_rosters(),
            fetch_league_users(),
            get_all_players(),
        )
        log.info(f"Phase 1 (state + rosters + users + players) took {time.time() - t_fetch:.3f}s")

        week = state.get("week", 1)
        season = state.get("season", "2026")
        ds.ok("nfl_state", f"Week {week}, {season} {state.get('season_type', '?')}")
        ds.ok("rosters", f"{len(rosters)} teams loaded")
    except Exception as e:
        log.error(f"Phase 1 failed: {e}")
        ds.error("nfl_state", str(e))
        ds.error("rosters", str(e))
        week = 1
        season = "2026"
        state = {"week": 1, "season": "2026", "season_type": "?"}
        rosters = []
        owner_map = {}
        all_players = {}

    ctx_parts.append(f"## NFL State\nSeason: {season} | Week: {week} | Type: {state.get('season_type', '?')}")

    # ── Phase 2: Concurrent secondary fetches ──────────────────────────
    try:
        t_phase2 = time.time()
        async def _empty_list():
            return []

        matchup_task = fetch_matchups(week) if isinstance(week, int) else _empty_list()
        tx_task = fetch_transactions(int(week)) if isinstance(week, int) or (isinstance(week, str) and week.isdigit()) else _empty_list()
        trending_task = fetch_trending_adds()
        history_task = fetch_matchup_history(int(week) if isinstance(week, int) else 1)

        espn_week = int(week) if isinstance(week, int) else 1
        espn_task = _fetch_espn_schedule_async(espn_week)

        matchups, transactions, trending_adds, matchup_history, espn_data = await asyncio.gather(
            matchup_task, tx_task, trending_task, history_task, espn_task,
            return_exceptions=True,
        )
        log.info(f"Phase 2 (matchups + tx + trending + history + espn) took {time.time() - t_phase2:.3f}s")

        # Handle individual failures
        if isinstance(matchups, Exception):
            ds.error("matchups", str(matchups))
            matchups = []
        else:
            ds.ok("matchups", f"Week {week}")

        if isinstance(transactions, Exception):
            ds.error("transactions", str(transactions))
            transactions = []
        else:
            ds.ok("transactions", f"Week {week}")

        if isinstance(trending_adds, Exception):
            ds.error("trending_adds", str(trending_adds))
            trending_adds = []
        else:
            ds.ok("trending_adds", f"{len(trending_adds)} trending players")

        if isinstance(matchup_history, Exception):
            ds.error("matchup_history", str(matchup_history))
            matchup_history = {}
        else:
            weeks_loaded = len(matchup_history)
            if weeks_loaded > 0:
                ds.ok("matchup_history", f"Weeks 1-{max(matchup_history.keys())} loaded")

        if isinstance(espn_data, Exception):
            ds.error("espn_weather", str(espn_data))
            espn_data = ""
        else:
            ds.ok("espn_weather", "Schedule loaded")

    except Exception as e:
        log.error(f"Phase 2 failed entirely: {e}")
        matchups, transactions, trending_adds, matchup_history, espn_data = [], [], [], {}, ""
        ds.error("matchups", str(e))

    # ── Phase 3: Compute actual season stats ───────────────────────────
    current_week = int(week) if isinstance(week, int) else 1
    actual_points = {}
    if matchup_history and current_week > 1:
        try:
            actual_points = compute_actual_points(matchup_history, current_week)
            log.info(f"Computed actual stats for {len(actual_points)} players")
        except Exception as e:
            log.warning(f"Failed to compute actual points: {e}")

    # ── Phase 4: Build roster analytics ────────────────────────────────
    try:
        t_parse = time.time()

        # Build injury map from bulk player data
        injury_map: Dict[str, str] = {}
        for pid_str, pinfo in all_players.items():
            if isinstance(pinfo, dict):
                inj = pinfo.get("injury_status")
                if inj:
                    injury_map[pid_str] = inj

        roster_map = {}
        roster_names = {}
        for r in rosters:
            rid = r.get("roster_id")
            owner_id = str(r.get("owner_id"))
            team_name = owner_map.get(owner_id, f"Roster {rid}")
            roster_names[rid] = team_name

            plist = r.get("players") or []
            roster_map[rid] = [_build_player_dict(pid, all_players, current_week) for pid in plist]

        analytics = get_team_analytics(roster_map, injury_map)
        my_analytics = analytics.get(MY_ROSTER_ID, {})
        my_roster_data = roster_map.get(MY_ROSTER_ID, [])
        my_tot, my_lu = best_lineup(my_roster_data, injury_map)
        log.info(f"Roster parsing and analytics took {time.time() - t_parse:.3f}s")

        # ── Actual W-L standings ───────────────────────────────────────
        actual_standings = get_actual_standings(rosters, owner_map)
        standings_lines = []
        for s in actual_standings:
            tag = " ⭐ (ANTHONY)" if s["roster_id"] == MY_ROSTER_ID else ""
            standings_lines.append(
                f"{s['rank']}. {s['team_name']}{tag}: {s['record']} ({s['points_for']:.1f} PF)"
            )

        # ── Projected strength rankings ────────────────────────────────
        proj_ranking_lines = []
        for rid, stat in analytics.items():
            tag = " (ANTHONY)" if rid == MY_ROSTER_ID else ""
            tname = roster_names.get(rid, f"Roster {rid}")
            source_note = ""
            if current_week >= 3:
                source_note = " [blended preseason+actual]"
            proj_ranking_lines.append(
                f"  {stat['overall_rank']}. {tname}{tag}: {stat['overall_pts']:.1f} pts{source_note}"
            )

        # ── Anthony's roster with injury + source tags ─────────────────
        starters_lines = []
        for p in my_lu:
            tags = []
            if p.get("injury_status"):
                tags.append(f"⚠️{p['injury_status']}")
            if p.get("actual_ppg"):
                tags.append(f"PPG:{p['actual_ppg']:.1f}")
            if p.get("source") and p["source"] != "preseason":
                tags.append(f"[{p['source']}]")
            tag_str = f" ({', '.join(tags)})" if tags else ""
            starters_lines.append(f"  {p['slot']}: {p['name']}{tag_str}")

        # IR-stashed players
        ir_players = [p for p in my_roster_data if injury_map.get(p.get("id", "")) in ("IR", "Out", "PUP")]
        ir_lines = []
        for p in ir_players:
            status = injury_map.get(p.get("id", ""), "")
            ir_lines.append(f"  🏥 {p['name']} — {status} (NOT eligible for lineup)")

        bench_names = [p['name'] for p in my_roster_data if p['name'] not in [x['name'] for x in my_lu] and p not in ir_players]
        bench_str = ", ".join(bench_names) if bench_names else "None"

        ctx_parts.append(
            f"## Anthony's Roster (roster_id {MY_ROSTER_ID})\n"
            f"**Optimal Starters (projected {my_tot:.1f} pts):**\n"
            + "\n".join(starters_lines) + "\n"
            + (f"\n**IR / Injured Reserve:**\n" + "\n".join(ir_lines) + "\n" if ir_lines else "")
            + f"\n**Bench:** {bench_str}\n\n"
            f"**Projected Rankings:** Overall #{my_analytics.get('overall_rank')}/12 | "
            f"RB #{my_analytics.get('rb_in_lineup_rank')}/12 | "
            f"WR #{my_analytics.get('wr_in_lineup_rank')}/12\n"
            + (f"*Note: Rankings use {'blended preseason + actual Weeks 1-' + str(current_week - 1) + ' data' if current_week >= 3 else 'preseason projections (no games played yet)'}*\n" if True else "")
        )

        # ── Actual standings ───────────────────────────────────────────
        ctx_parts.append(
            f"## Actual Standings (W-L Record)\n"
            + "\n".join(standings_lines)
        )

        # ── Projected strength ─────────────────────────────────────────
        ctx_parts.append(
            f"## Projected Lineup Strength\n"
            + "\n".join(proj_ranking_lines)
        )

        # ── Opponent rosters ───────────────────────────────────────────
        all_rosters_lines = ["\n## Opponent Rosters"]
        for rid, tname in roster_names.items():
            if str(rid) == str(MY_ROSTER_ID):
                continue
            opp_roster_data = roster_map.get(rid, [])
            opp_tot, opp_lu = best_lineup(opp_roster_data, injury_map)
            starters = ", ".join(f"{p['slot']}:{p['name']}" for p in opp_lu)
            bench = ", ".join(p['name'] for p in opp_roster_data if p['name'] not in [x['name'] for x in opp_lu])
            all_rosters_lines.append(f"### {tname} (Roster {rid})")
            all_rosters_lines.append(f"Starters ({opp_tot:.1f} pts): {starters}")
            all_rosters_lines.append(f"Bench: {bench}\n")

        ctx_parts.append("\n".join(all_rosters_lines))

    except Exception as e:
        log.error(f"Roster analytics failed: {e}", exc_info=True)
        ctx_parts.append(f"## Anthony's Roster\nUnavailable ({e})")
        roster_names = {}
        analytics = {}
        injury_map = {}
        my_analytics = {}
        trending_adds = []

    # ── Matchup ────────────────────────────────────────────────────────
    try:
        my_matchup_id = None
        opponent_rid = None
        my_points, opp_points = 0, 0
        for m in matchups:
            if m.get("roster_id") == MY_ROSTER_ID:
                my_matchup_id = m.get("matchup_id")
                my_points = m.get("points", 0)
                break
        if my_matchup_id:
            for m in matchups:
                if m.get("matchup_id") == my_matchup_id and m.get("roster_id") != MY_ROSTER_ID:
                    opponent_rid = m.get("roster_id")
                    opp_points = m.get("points", 0)
                    break
        opp_name = roster_names.get(opponent_rid, f"roster {opponent_rid}")
        ctx_parts.append(f"## Week {week} Matchup\nMy score: {my_points} | Opponent ({opp_name}): {opp_points}")
    except Exception as e:
        ctx_parts.append(f"## Week {week} Matchup\nUnavailable ({e})")
        ds.error("matchups", str(e))

    # ── Waiver intelligence (using pre-fetched data) ───────────────────
    try:
        t_waivers = time.time()
        wr_rank = my_analytics.get("wr_in_lineup_rank") if my_analytics else None
        rb_rank = my_analytics.get("rb_in_lineup_rank") if my_analytics else None

        waiver_data = get_waiver_recommendations(
            LEAGUE_ID,
            MY_ROSTER_ID,
            resolve_player_name,
            nfl_state=state if 'state' in dir() else None,
            rosters=rosters if 'rosters' in dir() else None,
            all_players=all_players if 'all_players' in dir() else None,
            trending_adds=trending_adds if isinstance(trending_adds, list) else None,
            actual_points=actual_points,
            injury_map=injury_map if 'injury_map' in dir() else None,
            wr_rank=wr_rank,
            rb_rank=rb_rank,
        )
        log.info(f"get_waiver_recommendations took {time.time() - t_waivers:.3f}s")
        ds.ok("waivers", f"{len(waiver_data.get('targets', []))} targets found")

        w_lines = []
        for t in waiver_data.get("targets", [])[:5]:
            w_lines.append(f"- {t['name']} ({t['position']}): {t['recommendation_reason']}")
        rec_drop = waiver_data.get("recommended_drop")
        drop_str = f"\n**Suggested Drop:** {rec_drop['name']} ({rec_drop['reason']})" if rec_drop else ""
        handcuff_notes = waiver_data.get("handcuff_notes", [])
        hc_str = "\n**Handcuff Alerts:**\n" + "\n".join(handcuff_notes) if handcuff_notes else ""
        faab = waiver_data.get("faab_guidance", {})
        faab_str = f"\n**FAAB Strategy:** {faab.get('strategy', '')}" if faab else ""

        ctx_parts.append(f"## Waiver Wire Intelligence\n" + ("\n".join(w_lines) or "None found") + hc_str + drop_str + faab_str)
    except Exception as e:
        ctx_parts.append(f"## Waiver Wire Intelligence\nUnavailable ({e})")
        ds.error("waivers", str(e))

    # ── Recent transactions (for waiver history context) ───────────────
    try:
        tx_lines = []
        if isinstance(transactions, list):
            for tx in transactions[:10]:
                ttype = tx.get("type", "unknown")
                if ttype in ("waiver", "free_agent", "trade"):
                    adds = tx.get("adds") or {}
                    drops = tx.get("drops") or {}
                    add_strs = [f"Added {resolve_player_name(p)}" for p in adds.keys()]
                    drop_strs = [f"Dropped {resolve_player_name(p)}" for p in drops.keys()]
                    status = tx.get("status", "complete")
                    # Show which roster made the move
                    roster_ids = tx.get("roster_ids") or []
                    who = ""
                    if roster_ids:
                        names = [roster_names.get(rid, f"R{rid}") for rid in roster_ids]
                        who = f" by {', '.join(names)}"
                    tx_lines.append(f"- {ttype.upper()} ({status}){who}: {', '.join(add_strs + drop_strs)}")
        ctx_parts.append(f"## Recent Transactions\n" + ("\n".join(tx_lines) if tx_lines else "None."))
    except Exception as e:
        ctx_parts.append(f"## Recent Transactions\nUnavailable ({e})")

    # ── ESPN schedule & weather ────────────────────────────────────────
    if isinstance(espn_data, str) and espn_data:
        ctx_parts.append(espn_data)

    # ── Data freshness note ────────────────────────────────────────────
    if current_week >= 4:
        ctx_parts.append("## ⚠️ Data Freshness\nPreseason projections are stale at Week 4+. All rankings above use blended actual performance (Weeks 1-{}) weighted at 80% plus preseason at 20%. Cite 'blended projection' when referencing these numbers.".format(current_week - 1))
    elif current_week >= 2:
        ctx_parts.append(f"## Data Freshness\nBlending preseason projections (50%) with actual Weeks 1-{current_week - 1} performance (50%).")
    else:
        ctx_parts.append("## Data Freshness\nUsing preseason projections only — no games played yet.")

    # ── Inject data provenance at the top ──────────────────────────────
    provenance = ds.to_context_block()
    full_context = provenance + "\n\n" + "\n\n".join(ctx_parts)

    log.info(f"Total build_context took {time.time() - t_start:.3f}s")
    return full_context, ds.to_dict()


async def _fetch_espn_schedule_async(week: int) -> str:
    """Async ESPN schedule & weather fetch."""
    try:
        client = _get_http_client()
        r = await client.get(
            f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}",
            timeout=5.0,
        )
        data = r.json()
        lines = []
        for ev in data.get("events", []):
            name = ev.get("name")
            venue = ev.get("competitions", [{}])[0].get("venue", {})
            indoor = venue.get("indoor", False)
            weather = ev.get("weather", {}).get("displayValue", "Clear")
            temp = ev.get("weather", {}).get("temperature", "")
            if indoor:
                weather_str = "Dome (Indoor)"
            else:
                weather_str = f"{weather}, {temp}F" if temp else weather
            lines.append(f"- {name}: {weather_str}")
        return f"## Week {week} Schedule & Weather\n" + "\n".join(lines)
    except Exception as e:
        log.warning(f"Failed to fetch ESPN schedule: {e}")
        raise  # Let gather handle it

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
_gemini_client: genai.Client | None = None

def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={"retry_options": {"attempts": 1}}
        )
    return _gemini_client

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert fantasy football analyst and strategist for a 12-team, half-PPR Sleeper league called "Loser eats an egg."
    You manage Anthony's team, **Tony Romo's DUI Mugshot** (roster_id 4).

    ### League scoring
    - Passing: 0.04/yd · 4 per TD · -1 per INT · 2 per 2PC
    - Rushing: 0.1/yd · 6 per TD · 2 per 2PC
    - Receiving: 0.5 per reception · 0.1/yd · 6 per TD · 2 per 2PC
    - Kicker: distance-tiered (0-39=3, 40-49=4, 50-59=5, 60+=6)

    ### Strict Rules — No Hallucination
    - NEVER invent statistics, snap counts, target shares, or injury statuses.
    - If a data section says "Unavailable" or a data source shows ❌, tell the user explicitly: "I couldn't load [X] data."
    - If a player's injury_status is IR, Out, or PUP, say so explicitly — NEVER suggest starting or trading for them as if they're active.
    - Prefix uncertain claims with their data source: "Based on preseason projections (may be stale)" or "Based on Weeks 1-3 actual data".
    - Always cite which data backs your recommendation: "[actual PPG]", "[preseason projection]", or "[blended]".
    - If you don't have enough data to make a confident recommendation, say so. It's better to say "I'm not sure" than to guess wrong.

    ### Your job (Full GM Mode)
    - Act as a comprehensive General Manager. Proactively evaluate trade opportunities using the optimal lineup delta method.
    - Leverage the structured GM analytics provided in the context (Overall/RB/WR ranks) to identify team weaknesses.
    - Use ACTUAL W-L STANDINGS when discussing league position, not just projected lineup strength.
    - Suggest optimal waiver claims using the positional priority shown in the waiver intelligence section (dynamic, not hardcoded).
    - Never recommend 2nd QB or TE unless starter is out.
    - **Strict Waiver Drops:** ONLY recommend dropping the exact "Suggested Drop" provided in the Waiver Wire Intelligence section. Do not invent your own drop candidates or recommend dropping kickers/defenses.
    - **Advanced GM - Matchups & Weather:** Use the provided schedule & weather data to break ties. Upgrade kickers in Domes. Downgrade pass-catchers in heavy rain/wind. If weather data is unavailable, say so.
    - **Advanced GM - Bye Weeks:** When evaluating trades, ALWAYS check for future bye week conflicts that could leave the roster with an unfillable hole.
    - **Advanced GM - Handcuffs:** Elevate proactive RB handcuffs if you see their starter is injured on the waiver wire intelligence report.
    - Keep answers mobile-friendly: scannable, short bullet points, narrow tables.
    - Flag uncertainty clearly — say when you're guessing.
""")


async def ask_gemini(question: str, context: str) -> str:
    """Call Gemini with timeout protection."""
    client = get_gemini_client()
    prompt = (
        f"Live league data context:\n<league_data>\n{context}\n</league_data>\n\n"
        f"Answer this question as the GM:\n<user_question>\n{question}\n</user_question>"
    )
    models_to_try = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]

    last_error = None
    for model_name in models_to_try:
        try:
            log.info(f"Asking Gemini with model {model_name}...")
            t_llm = time.time()

            # Run the synchronous Gemini call in a thread to avoid blocking
            loop = asyncio.get_event_loop()
            interaction = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: client.interactions.create(
                        model=model_name,
                        input=prompt,
                        system_instruction=SYSTEM_PROMPT,
                        store=False,
                    )
                ),
                timeout=30.0,
            )

            log.info(f"Gemini {model_name} responded in {time.time() - t_llm:.3f}s")
            if interaction.output_text:
                return interaction.output_text
        except asyncio.TimeoutError:
            last_error = f"Gemini {model_name} timed out after 30 seconds"
            log.warning(last_error)
        except Exception as e:
            last_error = e
            log.warning(f"Request with {model_name} failed: {e}")

    return f"⚠️ Analysis engine temporarily unavailable. All models overloaded or timed out. (Last error: {last_error})"

# ---------------------------------------------------------------------------
# FastAPI app with lifespan (startup cache warming)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm caches on startup to avoid cold-start penalty."""
    log.info("🏈 Starting Fantasy AI GM service — warming caches...")
    try:
        await get_all_players()
        log.info("✅ Player cache warmed on startup")
    except Exception as e:
        log.warning(f"⚠️ Failed to warm player cache on startup: {e}")
    yield
    # Cleanup
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None
    log.info("👋 Fantasy AI GM service shutting down")


app = FastAPI(title="Fantasy Football AI GM API", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    query: str
    league_context: str = "egg"

class ChatResponse(BaseModel):
    answer: str
    context_summary: str
    latency_ms: int
    data_sources: dict = {}

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

@app.get("/health", tags=["Meta"])
async def health():
    return {
        "status": "ok",
        "version": "3.0.0",
        "league_id": LEAGUE_ID,
        "team": "Tony Romo's DUI Mugshot",
        "player_cache_loaded": _ALL_PLAYERS_CACHE is not None and len(_ALL_PLAYERS_CACHE) > 0,
    }

@app.get("/api/waivers", tags=["GM Endpoints"])
async def api_waivers():
    try:
        all_players = await get_all_players()
        state = await fetch_nfl_state()
        rosters = await fetch_all_rosters()
        trending = await fetch_trending_adds()
        return get_waiver_recommendations(
            LEAGUE_ID, MY_ROSTER_ID, resolve_player_name,
            nfl_state=state, rosters=rosters,
            all_players=all_players, trending_adds=trending,
        )
    except Exception as e:
        log.error("Waiver endpoint error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch waiver recommendations")

@app.get("/api/gm/overview", tags=["GM Endpoints"])
async def api_gm_overview():
    try:
        all_players = await get_all_players()
        rosters = await fetch_all_rosters()
        owner_map = await fetch_league_users()
        state = await fetch_nfl_state()
        current_week = state.get("week", 1)

        # Build injury map
        injury_map = {}
        for pid, pinfo in all_players.items():
            if isinstance(pinfo, dict) and pinfo.get("injury_status"):
                injury_map[pid] = pinfo["injury_status"]

        roster_map = {}
        for r in rosters:
            rid = r.get("roster_id")
            plist = r.get("players") or []
            roster_map[rid] = [_build_player_dict(pid, all_players, current_week) for pid in plist]

        analytics = get_team_analytics(roster_map, injury_map)
        actual_standings = get_actual_standings(rosters, owner_map)

        my_analytics = analytics.get(MY_ROSTER_ID, {})
        my_roster_data = roster_map.get(MY_ROSTER_ID, [])
        my_tot, my_lu = best_lineup(my_roster_data, injury_map)

        return {
            "roster_id": MY_ROSTER_ID,
            "analytics": my_analytics,
            "optimal_lineup_pts": my_tot,
            "optimal_lineup": my_lu,
            "league_standings": analytics,
            "actual_standings": actual_standings,
        }
    except Exception as e:
        log.error("GM overview error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate GM overview")

from fastapi.responses import StreamingResponse

@app.post("/api/chat_stream", tags=["Chat"])
async def chat_stream(req: ChatRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    async def event_generator():
        start_t = time.time()
        
        try:
            if req.league_context == "survivor":
                yield f"data: {json.dumps({'status': 'Syncing survivor pool state...'})}\n\n"
                from app.survivor_engine import fetch_survivor_context, ask_survivor_gemini_direct
                context, odds_data, current_week = await fetch_survivor_context()
                
                yield f"data: {json.dumps({'status': 'Consulting the AI GM...'})}\n\n"
                answer = await ask_survivor_gemini_direct(GEMINI_API_KEY, context, custom_prompt=req.query)
                data_sources = {"survivor_odds": odds_data.get("data_source")}
                context_summary = f"Fat Crew Survivor Pool — Week {current_week} data loaded... 🏈"
            else:
                yield f"data: {json.dumps({'status': 'Fetching live league data...'})}\n\n"
                context, data_sources = await build_context(req.query)
                
                yield f"data: {json.dumps({'status': 'Consulting the AI GM...'})}\n\n"
                answer = await ask_gemini(req.query, context)
                context_summary = context[:300].replace("\n", " ") + "…"

            latency = int((time.time() - start_t) * 1000)

            final_resp = {
                "answer": answer,
                "context_summary": context_summary,
                "latency_ms": latency,
                "data_sources": data_sources,
            }
            yield f"data: {json.dumps(final_resp)}\n\n"

        except Exception as e:
            log.error(f"Error in chat_stream: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/chat", response_model=ChatResponse, tags=["Chat"])
async def chat(req: ChatRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    start_t = time.time()
    data_sources = {}

    if req.league_context == "survivor":
        from app.survivor_engine import build_survivor_recommendation
        try:
            result = await build_survivor_recommendation(GEMINI_API_KEY, custom_prompt=req.query)
            answer = result["recommendation"]
            data_sources = result.get("data_sources", {})
            context_summary = f"Fat Crew Survivor Pool — Week {result.get('week', '?')} data loaded... 🏈"
        except Exception as e:
            log.error(f"Survivor engine error: {e}")
            answer = f"⚠️ Survivor engine error: {e}"
            context_summary = "Error loading survivor data"
            data_sources = {"survivor_state": {"status": "error", "error": str(e)}}
    else:
        context, data_sources = await build_context(req.query)
        answer = await ask_gemini(req.query, context)
        context_summary = context[:300].replace("\n", " ") + "…"

    latency = int((time.time() - start_t) * 1000)

    return ChatResponse(
        answer=answer,
        context_summary=context_summary,
        latency_ms=latency,
        data_sources=data_sources,
    )

@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def index():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>🏈 Fantasy Assistant</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen flex flex-col font-sans p-4">
    <div class="max-w-2xl mx-auto w-full">
        <h1 class="text-2xl font-bold mb-4">Tony Romo's DUI Mugshot - GM API v3.0</h1>
        <p>API endpoints running natively. <span class="text-green-600 font-semibold">Async + cached.</span></p>
        <ul class="list-disc pl-5 mt-4 space-y-2 text-blue-600">
            <li><a href="/api/waivers">/api/waivers</a></li>
            <li><a href="/api/gm/overview">/api/gm/overview</a></li>
            <li><a href="/health">/health</a></li>
        </ul>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)
