"""Data pipeline for loading and validating catalogs, Sleeper API, and The Odds API."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.board_auditor import audit_and_sanitize_board
from core.config import (
    BOARD_PATH,
    CACHE_TTL_SECONDS,
    LEAGUE_ID,
    ODDS_API_BASE,
    ODDS_API_KEY,
    ODDS_CACHE_TTL,
    PLAYER_IDS_PATH,
    SKILL_DATA_DIR,
    SLEEPER_BASE,
)
from core.exceptions import DataValidationError, UpstreamSourceError
from core.provenance import build_provenance_record
from core.schemas import NFLGameOdds, ProvenanceRecord
from core.week_resolver import resolve_canonical_nfl_week

log = logging.getLogger(__name__)

# Cache stores
_CATALOG_CACHE: Dict[str, Any] = {}
_SLEEPER_CACHE: Dict[str, Dict[str, Any]] = {}
_ODDS_CACHE: Dict[str, Any] = {}


# ── Catalogs ───────────────────────────────────────────────────────────────

def _resolve_catalog_path(primary: Path, fallback_filename: str) -> Path:
    if primary.exists():
        return primary
    fallback = SKILL_DATA_DIR / fallback_filename
    if fallback.exists():
        return fallback
    return primary


def load_player_ids_catalog(
    force_refresh: bool = False,
) -> Tuple[Dict[str, str], ProvenanceRecord]:
    """Load player identity catalog mapping player_id to full name."""
    now = datetime.now(timezone.utc).timestamp()
    cached = _CATALOG_CACHE.get("player_ids")
    if not force_refresh and cached:
        return cached["data"], cached["provenance"]

    path = _resolve_catalog_path(PLAYER_IDS_PATH, "player_ids.json")
    if not path.exists():
        raise UpstreamSourceError(
            f"Player IDs catalog not found at {path}",
            source_id="local_player_ids",
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise DataValidationError(f"Expected dict in {path}, got {type(data)}")

        prov = build_provenance_record(
            source_id="local_player_ids",
            source_type="local_catalog",
            endpoint_or_path=str(path),
            status="ok",
            record_count=len(data),
        )
        _CATALOG_CACHE["player_ids"] = {"data": data, "provenance": prov}
        return data, prov

    except Exception as e:
        prov = build_provenance_record(
            source_id="local_player_ids",
            source_type="local_catalog",
            endpoint_or_path=str(path),
            status="failed",
            record_count=0,
            error_detail=str(e),
        )
        raise UpstreamSourceError(
            f"Failed to load player IDs catalog: {e}",
            source_id="local_player_ids",
            detail=str(e),
        ) from e


def load_board_catalog(
    force_refresh: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], ProvenanceRecord]:
    """Load, audit, and sanitize board.json catalog."""
    now = datetime.now(timezone.utc).timestamp()
    cached = _CATALOG_CACHE.get("board")
    if not force_refresh and cached:
        return cached["data"], cached["summary"], cached["provenance"]

    path = _resolve_catalog_path(BOARD_PATH, "board.json")
    if not path.exists():
        raise UpstreamSourceError(
            f"Board catalog not found at {path}",
            source_id="local_board_catalog",
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_board = json.load(f)

        sanitized, summary = audit_and_sanitize_board(raw_board)
        prov = build_provenance_record(
            source_id="local_board_catalog",
            source_type="local_catalog",
            endpoint_or_path=str(path),
            status="ok",
            record_count=len(sanitized),
        )
        _CATALOG_CACHE["board"] = {
            "data": sanitized,
            "summary": summary,
            "provenance": prov,
        }
        return sanitized, summary, prov

    except Exception as e:
        prov = build_provenance_record(
            source_id="local_board_catalog",
            source_type="local_catalog",
            endpoint_or_path=str(path),
            status="failed",
            record_count=0,
            error_detail=str(e),
        )
        raise UpstreamSourceError(
            f"Failed to load and audit board catalog: {e}",
            source_id="local_board_catalog",
            detail=str(e),
        ) from e


# ── Sleeper API Client ─────────────────────────────────────────────────────

class SleeperClient:
    """Async client for Sleeper API with TTL caching and typed provenance."""

    def __init__(self, base_url: str = SLEEPER_BASE, client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def fetch_nfl_state(
        self, force_refresh: bool = False
    ) -> Tuple[Dict[str, Any], ProvenanceRecord]:
        """Fetch current NFL state from Sleeper."""
        cache_key = "nfl_state"
        now = datetime.now(timezone.utc).timestamp()
        cached = _SLEEPER_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"], cached["provenance"]

        endpoint = f"{self.base_url}/state/nfl"
        client = await self._get_client()
        try:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise DataValidationError("Sleeper NFL state payload is not a dictionary.")

            prov = build_provenance_record(
                source_id="sleeper_nfl_state",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="ok",
                record_count=1,
            )
            _SLEEPER_CACHE[cache_key] = {"data": data, "provenance": prov, "ts": now}
            return data, prov
        except Exception as e:
            prov = build_provenance_record(
                source_id="sleeper_nfl_state",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="failed",
                record_count=0,
                error_detail=str(e),
            )
            raise UpstreamSourceError(
                f"Failed to fetch Sleeper NFL state: {e}",
                source_id="sleeper_nfl_state",
                detail=str(e),
            ) from e

    async def fetch_rosters(
        self, league_id: Optional[str] = None, force_refresh: bool = False
    ) -> Tuple[List[Dict[str, Any]], ProvenanceRecord]:
        """Fetch league rosters from Sleeper."""
        lid = league_id or LEAGUE_ID
        cache_key = f"rosters_{lid}"
        now = datetime.now(timezone.utc).timestamp()
        cached = _SLEEPER_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"], cached["provenance"]

        endpoint = f"{self.base_url}/league/{lid}/rosters"
        client = await self._get_client()
        try:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise DataValidationError("Sleeper rosters payload is not a list.")

            prov = build_provenance_record(
                source_id="sleeper_rosters",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="ok",
                record_count=len(data),
            )
            _SLEEPER_CACHE[cache_key] = {"data": data, "provenance": prov, "ts": now}
            return data, prov
        except Exception as e:
            prov = build_provenance_record(
                source_id="sleeper_rosters",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="failed",
                record_count=0,
                error_detail=str(e),
            )
            raise UpstreamSourceError(
                f"Failed to fetch Sleeper rosters: {e}",
                source_id="sleeper_rosters",
                detail=str(e),
            ) from e

    async def fetch_matchups(
        self,
        league_id: Optional[str] = None,
        week: Optional[int] = None,
        force_refresh: bool = False,
    ) -> Tuple[List[Dict[str, Any]], ProvenanceRecord]:
        """Fetch matchups for canonical week."""
        lid = league_id or LEAGUE_ID
        if week is None:
            nfl_state, _ = await self.fetch_nfl_state(force_refresh=force_refresh)
            week = resolve_canonical_nfl_week(nfl_state)

        cache_key = f"matchups_{lid}_{week}"
        now = datetime.now(timezone.utc).timestamp()
        cached = _SLEEPER_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"], cached["provenance"]

        endpoint = f"{self.base_url}/league/{lid}/matchups/{week}"
        client = await self._get_client()
        try:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise DataValidationError("Sleeper matchups payload is not a list.")

            prov = build_provenance_record(
                source_id="sleeper_matchups",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="ok",
                record_count=len(data),
            )
            _SLEEPER_CACHE[cache_key] = {"data": data, "provenance": prov, "ts": now}
            return data, prov
        except Exception as e:
            prov = build_provenance_record(
                source_id="sleeper_matchups",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="failed",
                record_count=0,
                error_detail=str(e),
            )
            raise UpstreamSourceError(
                f"Failed to fetch Sleeper matchups: {e}",
                source_id="sleeper_matchups",
                detail=str(e),
            ) from e

    async def fetch_trending(
        self,
        trend_type: str = "add",
        lookback_hours: int = 24,
        limit: int = 25,
        force_refresh: bool = False,
    ) -> Tuple[List[Dict[str, Any]], ProvenanceRecord]:
        """Fetch trending added or dropped players."""
        cache_key = f"trending_{trend_type}_{lookback_hours}_{limit}"
        now = datetime.now(timezone.utc).timestamp()
        cached = _SLEEPER_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"], cached["provenance"]

        endpoint = (
            f"{self.base_url}/players/nfl/trending/{trend_type}"
            f"?lookback_hours={lookback_hours}&limit={limit}"
        )
        client = await self._get_client()
        try:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise DataValidationError("Sleeper trending payload is not a list.")

            prov = build_provenance_record(
                source_id="sleeper_trending",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="ok",
                record_count=len(data),
            )
            _SLEEPER_CACHE[cache_key] = {"data": data, "provenance": prov, "ts": now}
            return data, prov
        except Exception as e:
            prov = build_provenance_record(
                source_id="sleeper_trending",
                source_type="sleeper_api",
                endpoint_or_path=endpoint,
                status="failed",
                record_count=0,
                error_detail=str(e),
            )
            raise UpstreamSourceError(
                f"Failed to fetch Sleeper trending: {e}",
                source_id="sleeper_trending",
                detail=str(e),
            ) from e

    async def fetch_league(
        self, league_id: Optional[str] = None, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """Fetch league metadata."""
        lid = league_id or LEAGUE_ID
        cache_key = f"league_{lid}"
        now = datetime.now(timezone.utc).timestamp()
        cached = _SLEEPER_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"]

        endpoint = f"{self.base_url}/league/{lid}"
        client = await self._get_client()
        resp = await client.get(endpoint)
        resp.raise_for_status()
        data = resp.json()
        _SLEEPER_CACHE[cache_key] = {"data": data, "ts": now}
        return data

    async def fetch_users(
        self, league_id: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Fetch league user list."""
        lid = league_id or LEAGUE_ID
        cache_key = f"users_{lid}"
        now = datetime.now(timezone.utc).timestamp()
        cached = _SLEEPER_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"]

        endpoint = f"{self.base_url}/league/{lid}/users"
        client = await self._get_client()
        resp = await client.get(endpoint)
        resp.raise_for_status()
        data = resp.json()
        _SLEEPER_CACHE[cache_key] = {"data": data, "ts": now}
        return data


# ── The Odds API Client ────────────────────────────────────────────────────

# Team normalization mapping for NFL team names to abbreviations
NFL_TEAM_NAME_MAP = {
    "arizona cardinals": "ARI",
    "atlanta falcons": "ATL",
    "baltimore ravens": "BAL",
    "buffalo bills": "BUF",
    "carolina panthers": "CAR",
    "chicago bears": "CHI",
    "cincinnati bengals": "CIN",
    "cleveland browns": "CLE",
    "dallas cowboys": "DAL",
    "denver broncos": "DEN",
    "detroit lions": "DET",
    "green bay packers": "GB",
    "houston texans": "HOU",
    "indianapolis colts": "IND",
    "jacksonville jaguars": "JAC",
    "kansas city chiefs": "KC",
    "las vegas raiders": "LV",
    "los angeles chargers": "LAC",
    "los angeles rams": "LAR",
    "miami dolphins": "MIA",
    "minnesota vikings": "MIN",
    "new england patriots": "NE",
    "new orleans saints": "NO",
    "new york giants": "NYG",
    "new york jets": "NYJ",
    "philadelphia eagles": "PHI",
    "pittsburgh steelers": "PIT",
    "san francisco 49ers": "SF",
    "seattle seahawks": "SEA",
    "tampa bay buccaneers": "TB",
    "tennessee titans": "TEN",
    "washington commanders": "WAS",
}


def normalize_nfl_team(name_or_abbr: str) -> str:
    """Normalize team name or abbreviation to canonical 2-3 uppercase abbreviation."""
    cleaned = name_or_abbr.strip().lower()
    return NFL_TEAM_NAME_MAP.get(cleaned, name_or_abbr.strip().upper())


class OddsAPIClient:
    """Live NFL point spread and moneyline client with fail-closed provenance."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = ODDS_API_BASE):
        if api_key is not None:
            self.api_key = api_key.strip()
        else:
            self.api_key = ODDS_API_KEY.strip()
        self.base_url = base_url.rstrip("/")

    async def fetch_nfl_odds(
        self,
        nfl_week: int,
        force_refresh: bool = False,
    ) -> Tuple[List[NFLGameOdds], ProvenanceRecord]:
        """Fetch live spreads and moneylines for the given canonical week."""
        endpoint = f"{self.base_url}/sports/americanfootball_nfl/odds/"

        # Fail-closed check: missing API key
        if not self.api_key:
            prov = build_provenance_record(
                source_id="the_odds_api_nfl",
                source_type="odds_api",
                endpoint_or_path=endpoint,
                status="missing_key",
                record_count=0,
                error_detail="ODDS_API_KEY is not configured in environment.",
            )
            return [], prov

        now = datetime.now(timezone.utc).timestamp()
        cache_key = f"odds_week_{nfl_week}_{self.api_key}"
        cached = _ODDS_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < ODDS_CACHE_TTL:
            return cached["data"], cached["provenance"]

        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": "spreads,h2h",
            "oddsFormat": "american",
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(endpoint, params=params)
                resp.raise_for_status()
                raw_games = resp.json()

            if not isinstance(raw_games, list):
                raise DataValidationError(f"Expected list of games, got {type(raw_games)}")

            odds_list: List[NFLGameOdds] = []
            for g in raw_games:
                game_id = g.get("id", "")
                commence_time = g.get("commence_time", "")
                home_raw = g.get("home_team", "")
                away_raw = g.get("away_team", "")
                home_team = normalize_nfl_team(home_raw)
                away_team = normalize_nfl_team(away_raw)

                bookmakers = g.get("bookmakers", [])
                if not bookmakers:
                    continue

                # Consolidate primary bookmaker (e.g. DraftKings, FanDuel, or first available)
                bm = bookmakers[0]
                markets = {m.get("key"): m.get("outcomes", []) for m in bm.get("markets", [])}

                spread_outcomes = markets.get("spreads", [])
                h2h_outcomes = markets.get("h2h", [])

                if not spread_outcomes:
                    continue

                # Determine favorite vs underdog
                fav_team = home_team
                dog_team = away_team
                fav_spread = 0.0

                for outcome in spread_outcomes:
                    t_name = normalize_nfl_team(outcome.get("name", ""))
                    pt = float(outcome.get("point", 0.0))
                    if pt < 0:
                        fav_team = t_name
                        fav_spread = pt
                    elif pt > 0:
                        dog_team = t_name

                # Moneylines
                fav_ml: Optional[int] = None
                dog_ml: Optional[int] = None
                for outcome in h2h_outcomes:
                    t_name = normalize_nfl_team(outcome.get("name", ""))
                    price = outcome.get("price")
                    if price is not None:
                        if t_name == fav_team:
                            fav_ml = int(price)
                        elif t_name == dog_team:
                            dog_ml = int(price)

                # Compute implied win probability
                if fav_ml is not None:
                    if fav_ml < 0:
                        prob = abs(fav_ml) / (abs(fav_ml) + 100.0)
                    else:
                        prob = 100.0 / (fav_ml + 100.0)
                else:
                    prob = min(0.95, max(0.50, 0.50 + abs(fav_spread) * 0.03))

                game_odds = NFLGameOdds(
                    game_id=game_id,
                    nfl_week=nfl_week,
                    commence_time_utc=commence_time,
                    home_team=home_team,
                    away_team=away_team,
                    favorite_team=fav_team,
                    underdog_team=dog_team,
                    spread_points=fav_spread,
                    favorite_moneyline=fav_ml,
                    underdog_moneyline=dog_ml,
                    implied_win_prob=round(prob, 3),
                    bookmaker_count=len(bookmakers),
                )
                odds_list.append(game_odds)

            prov = build_provenance_record(
                source_id="the_odds_api_nfl",
                source_type="odds_api",
                endpoint_or_path=endpoint,
                status="ok",
                record_count=len(odds_list),
            )
            _ODDS_CACHE[cache_key] = {"data": odds_list, "provenance": prov, "ts": now}
            return odds_list, prov

        except Exception as e:
            prov = build_provenance_record(
                source_id="the_odds_api_nfl",
                source_type="odds_api",
                endpoint_or_path=endpoint,
                status="failed",
                record_count=0,
                error_detail=str(e),
            )
            return [], prov
