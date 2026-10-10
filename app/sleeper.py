"""Async Sleeper API client, TTL caching, player resolution, and data fetching."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import (
    CACHE_TTL_SECONDS,
    LEAGUE_ID,
    PROJECTIONS_CACHE_TTL,
    SKILL_DATA_DIR,
    SLEEPER_BASE,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static data (bundled player IDs)
# ---------------------------------------------------------------------------
def _load_bundled_player_ids() -> Dict[str, Any]:
    p = SKILL_DATA_DIR / "player_ids.json"
    if p.exists():
        try:
            with p.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Failed to load player_ids.json: {e}")
    return {}

PLAYER_IDS: Dict[str, Any] = _load_bundled_player_ids()

# ---------------------------------------------------------------------------
# TTL Cache (module-level dict accessed by app.main._cache)
# ---------------------------------------------------------------------------
_cache: Dict[str, Dict[str, Any]] = {}

def cache_get(key: str) -> Any | None:
    """Return cached value if exists and not expired."""
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < entry.get("ttl", CACHE_TTL_SECONDS):
        return entry["data"]
    return None

def cache_set(key: str, data: Any, ttl: int = CACHE_TTL_SECONDS) -> None:
    _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}

def cache_clear() -> None:
    _cache.clear()

def _cb() -> str:
    return f"cb={random.randint(100_000, 999_999)}"

# ---------------------------------------------------------------------------
# HTTP Client Management
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None

def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    return _http_client

async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None

async def _aget(url: str, timeout: float = 15.0) -> Any:
    # Allow patching of app.main._aget
    import sys
    main_mod = sys.modules.get("app.main")
    if main_mod and hasattr(main_mod, "_aget") and main_mod._aget != _aget:
        return await main_mod._aget(url, timeout=timeout)

    client = get_http_client()
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Bulk player cache
# ---------------------------------------------------------------------------
_ALL_PLAYERS_CACHE: Dict[str, Any] | None = None

async def get_all_players() -> Dict[str, Any]:
    global _ALL_PLAYERS_CACHE
    import sys
    main_mod = sys.modules.get("app.main")
    if main_mod and hasattr(main_mod, "get_all_players") and main_mod.get_all_players != get_all_players:
        return await main_mod.get_all_players()

    if _ALL_PLAYERS_CACHE is not None and len(_ALL_PLAYERS_CACHE) > 0:
        return _ALL_PLAYERS_CACHE

    cached = cache_get("bulk_players")
    if cached:
        _ALL_PLAYERS_CACHE = cached
        return cached

    try:
        log.info("Fetching bulk player database from Sleeper...")
        t0 = time.time()
        data = await _aget(f"{SLEEPER_BASE}/players/nfl", timeout=45.0)
        if isinstance(data, dict):
            _ALL_PLAYERS_CACHE = data
            cache_set("bulk_players", data, ttl=86400)
            log.info(f"Loaded {len(data)} players in {time.time() - t0:.2f}s")
            return _ALL_PLAYERS_CACHE
    except Exception as e:
        log.error(f"Failed to fetch bulk players: {e}")

    _ALL_PLAYERS_CACHE = _ALL_PLAYERS_CACHE or {}
    return _ALL_PLAYERS_CACHE

def get_all_players_sync() -> Dict[str, Any]:
    import sys
    main_mod = sys.modules.get("app.main")
    if main_mod and hasattr(main_mod, "get_all_players_sync") and main_mod.get_all_players_sync != get_all_players_sync:
        return main_mod.get_all_players_sync()
    return _ALL_PLAYERS_CACHE or {}

def resolve_player_name(pid: str) -> str:
    """Resolve a player ID or DEF string to formatted name with team and position."""
    if not pid:
        return "Unknown"

    if not pid.isdigit():
        return f"{pid} DEF"

    if pid in PLAYER_IDS:
        val = PLAYER_IDS[pid]
        if isinstance(val, str):
            return val
        first = val.get("first_name", "")
        last = val.get("last_name", "")
        team = val.get("team", "?")
        pos = val.get("position", "?")
        return f"{first} {last} ({team}, {pos})".strip()

    all_players = get_all_players_sync()
    if pid in all_players:
        p = all_players[pid]
        if isinstance(p, dict):
            first = p.get("first_name", "")
            last = p.get("last_name", "")
            team = p.get("team") or "FA"
            pos = p.get("position") or "?"
            name_str = f"{first} {last} ({team}, {pos})".strip()
            PLAYER_IDS[pid] = name_str
            return name_str

    fallback = f"Player {pid}"
    PLAYER_IDS[pid] = fallback
    return fallback

# ---------------------------------------------------------------------------
# Sleeper Core Endpoints
# ---------------------------------------------------------------------------
async def fetch_nfl_state() -> Dict[str, Any]:
    cached = cache_get("nfl_state")
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/state/nfl?{_cb()}")
    cache_set("nfl_state", data, ttl=60)
    return data

async def fetch_league() -> Dict[str, Any]:
    cached = cache_get("league_info")
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}?{_cb()}")
    cache_set("league_info", data, ttl=CACHE_TTL_SECONDS)
    return data

async def fetch_all_rosters() -> List[Dict[str, Any]]:
    cached = cache_get("rosters")
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/rosters?{_cb()}")
    cache_set("rosters", data, ttl=CACHE_TTL_SECONDS)
    return data

async def fetch_league_users() -> Dict[str, str]:
    cached = cache_get("users")
    if cached:
        return cached
    users = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/users?{_cb()}")
    owner_map = {}
    for u in users:
        team_name = u.get("metadata", {}).get("team_name") or u.get("display_name", "Unknown")
        owner_id = str(u.get("user_id"))
        owner_map[owner_id] = team_name
    cache_set("users", owner_map, ttl=CACHE_TTL_SECONDS)
    return owner_map

async def fetch_matchups(week: int) -> List[Dict[str, Any]]:
    cache_key = f"matchups_{week}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/matchups/{week}?{_cb()}")
    cache_set(cache_key, data, ttl=120)
    return data

async def fetch_transactions(week: int) -> List[Dict[str, Any]]:
    cache_key = f"tx_{week}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/transactions/{week}?{_cb()}")
    cache_set(cache_key, data, ttl=180)
    return data

async def fetch_trending_adds() -> List[Dict[str, Any]]:
    cached = cache_get("trending_adds")
    if cached:
        return cached
    data = await _aget(f"{SLEEPER_BASE}/players/nfl/trending/add?lookback_hours=24&limit=30&{_cb()}")
    cache_set("trending_adds", data, ttl=300)
    return data

async def fetch_weekly_projections(season: str, week: int) -> Dict[str, Dict[str, Any]]:
    cache_key = f"projections_{season}_{week}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    url = (
        f"https://api.sleeper.app/projections/nfl/{season}/{week}"
        f"?season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF"
    )
    try:
        t0 = time.time()
        data = await _aget(url, timeout=20.0)
        proj_map: Dict[str, Dict[str, Any]] = {}
        if isinstance(data, list):
            for row in data:
                pid = str(row.get("player_id") or "")
                if not pid:
                    continue
                stats = row.get("stats") or {}
                player_meta = row.get("player") or {}
                
                pts = stats.get("pts_half_ppr")
                if pts is None:
                    pts = stats.get("pts_std") or 0.0

                fgm_60p = stats.get("fgm_60p", 0.0) or 0.0
                if fgm_60p > 0:
                    pts += fgm_60p * 1.0

                proj_map[pid] = {
                    "pts": round(float(pts), 2),
                    "pos": player_meta.get("position") or row.get("team") and "DEF" or "",
                    "team": player_meta.get("team") or row.get("team") or "",
                    "opponent": row.get("opponent") or "",
                    "injury_status": player_meta.get("injury_status") or "",
                    "stats": stats,
                    "first_name": player_meta.get("first_name", ""),
                    "last_name": player_meta.get("last_name", ""),
                }
            log.info(f"Loaded {len(proj_map)} projections for Week {week} in {time.time() - t0:.2f}s")
            cache_set(cache_key, proj_map, ttl=PROJECTIONS_CACHE_TTL)
            return proj_map
    except Exception as e:
        log.warning(f"Failed to fetch weekly projections for Week {week}: {e}")

    return {}

async def fetch_weekly_stats(season: str, week: int) -> Dict[str, Dict[str, Any]]:
    if week < 1:
        return {}
    cache_key = f"stats_{season}_{week}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{SLEEPER_BASE}/stats/nfl/regular/{season}/{week}"
    try:
        data = await _aget(url, timeout=15.0)
        if isinstance(data, dict):
            cache_set(cache_key, data, ttl=PROJECTIONS_CACHE_TTL)
            return data
    except Exception as e:
        log.warning(f"Failed to fetch weekly stats for Week {week}: {e}")
    return {}

async def fetch_espn_scoreboard(week: int) -> List[Dict[str, Any]]:
    cache_key = f"espn_scoreboard_{week}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&seasontype=2"
    try:
        data = await _aget(url, timeout=8.0)
        events = []
        for ev in data.get("events", []):
            name = ev.get("name", "")
            short_name = ev.get("shortName", "")
            status_obj = ev.get("status", {})
            state = status_obj.get("type", {}).get("state", "pre")
            comp = ev.get("competitions", [{}])[0]
            venue = comp.get("venue", {})
            indoor = venue.get("indoor", False)
            weather = ev.get("weather", {})
            desc = weather.get("displayValue") or ("Dome (Indoor)" if indoor else "Clear")
            temp = weather.get("temperature")

            teams = []
            for c in comp.get("competitors", []):
                abbr = c.get("team", {}).get("abbreviation", "")
                if abbr == "WSH":
                    abbr = "WAS"
                teams.append(abbr)

            events.append({
                "name": name,
                "short_name": short_name,
                "teams": teams,
                "state": state,
                "indoor": indoor,
                "weather": desc,
                "temp": temp,
            })
        cache_set(cache_key, events, ttl=300)
        return events
    except Exception as e:
        log.warning(f"Failed to fetch ESPN scoreboard: {e}")
        return []
