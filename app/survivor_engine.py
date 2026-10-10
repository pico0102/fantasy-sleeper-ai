"""Survivor Pool Engine for Fat Crew Survivor.

Manages odds fetching, candidate evaluation, game theory strategy,
and unified pick recommendations across the chatbot toggle and weekly email cron.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
import sys
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple

from google import genai
import httpx

from app.config import (
    GEMINI_API_KEY,
    ODDS_API_KEY,
    SLEEPER_SURVIVOR_LEAGUE_ID,
    SLEEPER_USER_ID,
)
from app.llm import LLMError, ask_gemini

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NFL Teams & Abbreviation Mappings
# ---------------------------------------------------------------------------

NFL_TEAMS: Dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}

TEAM_NAME_TO_ABBREV: Dict[str, str] = {v.lower(): k for k, v in NFL_TEAMS.items()}
# Common aliases / short names
for abbrev, full in NFL_TEAMS.items():
    mascot = full.split()[-1].lower()
    TEAM_NAME_TO_ABBREV[mascot] = abbrev
    TEAM_NAME_TO_ABBREV[abbrev.lower()] = abbrev

# Plain-language user-facing message when ODDS_API_KEY is missing
ODDS_KEY_MISSING_MESSAGE = (
    "⚠️ **Survivor Odds Feed Not Connected**\n\n"
    "Live Vegas odds are currently unavailable because the odds data feed (`ODDS_API_KEY`) is not configured on the server.\n\n"
    "**Why this matters:**\n"
    "In a survivor pool, making picks without real-time Vegas win probabilities, point spreads, and moneyline lines puts you at a severe disadvantage. "
    "To prevent guesswork and protect your entry, pick recommendations are paused until the odds feed is connected.\n\n"
    "**How to resolve:**\n"
    "Connect your Odds API key in the Cloud Run service configuration. Once active, you'll receive data-backed picks with win probabilities and spread analysis."
)

SURVIVOR_SYSTEM_INSTRUCTION = textwrap.dedent("""\
    You are an expert game theory analyst and NFL sports bettor managing a Sleeper Survivor pool ("Fat Crew Survivor").
    The user's goal is to win the entire pool. They need to survive this week, but also keep elite teams available for future weeks.

    ### Strategy
    - If there is an obvious safe pick with low future value, explain why they are optimal.
    - If public concentration is heavy, discuss the contrarian pivot for +EV leverage.
    - Warn if picking a team severely hurts availability in crucial future weeks.

    ### Anti-Hallucination & Integrity Rules
    - Never invent spread numbers, moneyline odds, or win probabilities. Always use the numbers supplied in context.
    - Never recommend a team the user has already picked in prior weeks.
    - If live odds are missing or empty, state explicitly that odds are unavailable.

    ### Format Requirements
    - State the Primary Pick clearly at the top with Win Probability %, Spread, and Opponent.
    - Provide the Alternate Pick with its strategic rationale.
    - Keep formatting clean, scannable, and mobile-friendly.
""")

# In-memory odds cache: {"data": dict, "timestamp": float}
_ODDS_CACHE: Dict[str, Any] = {"data": None, "timestamp": 0.0}
ODDS_CACHE_TTL = 900  # 15 minutes


def normalize_team_name(name: str) -> str:
    """Returns canonical full team name if recognized, else original string."""
    cleaned = name.strip()
    abbrev = TEAM_NAME_TO_ABBREV.get(cleaned.lower())
    if abbrev and abbrev in NFL_TEAMS:
        return NFL_TEAMS[abbrev]
    return cleaned


def get_team_abbreviation(name: str) -> str:
    """Returns 2-3 char abbreviation if known, else uppercase first 3 chars."""
    cleaned = name.strip()
    if cleaned.upper() in NFL_TEAMS:
        return cleaned.upper()
    abbrev = TEAM_NAME_TO_ABBREV.get(cleaned.lower())
    if abbrev:
        return abbrev
    return cleaned[:3].upper()


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class SurvivorCandidate:
    team: str
    abbrev: str
    opponent: str
    opponent_abbrev: str
    prob: float
    spread: Optional[float]
    moneyline: Optional[int]
    is_home: bool
    is_eligible: bool = True
    ineligibility_reason: Optional[str] = None


@dataclass
class SurvivorPickResult:
    status: str  # "ok" or "error"
    is_valid: bool
    week: int
    league_name: str
    primary_team: str
    primary_abbrev: str
    primary_opponent: str
    primary_prob: float
    primary_spread: Optional[float]
    primary_ml: Optional[int]
    alternate_team: str
    alternate_abbrev: str
    alternate_opponent: str
    alternate_prob: float
    alternate_spread: Optional[float]
    alternate_ml: Optional[int]
    previously_picked: List[str]  # e.g. ["SEA", "LAC", "BUF", "BAL"]
    candidates: List[Dict[str, Any]]
    recommendation_text: str  # Markdown for chatbot
    recommendation_html: str  # HTML for email cron
    data_sources: Dict[str, Any]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Health Probe
# ---------------------------------------------------------------------------

async def probe_survivor_odds_health() -> Dict[str, Any]:
    """Lightweight health check for survivor odds feed."""
    api_key = os.getenv("ODDS_API_KEY", ODDS_API_KEY).strip()
    if not api_key or api_key == "your-odds-api-key-here" or api_key == "test-mock-key":
        return {
            "status": "error",
            "detail": "ODDS_API_KEY is not configured — live odds disabled",
        }

    # If recent cached odds exist, report healthy immediately
    now = time.time()
    if _ODDS_CACHE.get("data") and (now - _ODDS_CACHE.get("timestamp", 0) < 1800):
        games = _ODDS_CACHE["data"].get("games", [])
        return {
            "status": "ok",
            "detail": f"Live Vegas odds connected ({len(games)} games loaded)",
        }

    # Lightweight probe: GET /v4/sports/?apiKey=... does not consume request quota
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                remaining = resp.headers.get("x-requests-remaining")
                detail = "Live Vegas odds connected"
                if remaining:
                    detail += f" ({remaining} requests remaining)"
                return {"status": "ok", "detail": detail}
            elif resp.status_code == 401:
                return {
                    "status": "error",
                    "detail": "ODDS_API_KEY was rejected by The Odds API (invalid key)",
                }
            else:
                return {
                    "status": "degraded",
                    "detail": f"The Odds API returned HTTP {resp.status_code}",
                }
    except Exception as e:
        return {
            "status": "degraded",
            "detail": f"Odds probe unreachable: {str(e)}",
        }


# ---------------------------------------------------------------------------
# Sleeper Survivor Pool State & Roster Data
# ---------------------------------------------------------------------------

async def get_survivor_state(league_id: Optional[str] = None) -> dict:
    lid = league_id or os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", SLEEPER_SURVIVOR_LEAGUE_ID)
    url = f"https://api.sleeper.app/v1/league/{lid}"
    state_url = "https://api.sleeper.app/v1/state/nfl"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            league_resp, state_resp = await asyncio.gather(
                client.get(url),
                client.get(state_url),
            )
            league_resp.raise_for_status()
            state_resp.raise_for_status()

            league_data = league_resp.json()
            nfl_state = state_resp.json()

            current_week = nfl_state.get("display_week", 1)
            metadata = league_data.get("metadata", {})
            leg_id = metadata.get("current_pickem_leg_id", "")
            if leg_id and ":" in leg_id:
                try:
                    current_week = int(leg_id.split(":")[-1])
                except ValueError:
                    pass

            return {
                "current_week": current_week,
                "league_name": league_data.get("name", "Fat Crew Survivor"),
                "season": league_data.get("season", "2026"),
            }
    except Exception as e:
        log.warning(f"Failed to fetch survivor state: {e}")
        return {"current_week": 1, "league_name": "Fat Crew Survivor", "season": "2026"}


async def get_survivor_pool_rosters(league_id: Optional[str] = None) -> Tuple[List[str], Dict[str, Any]]:
    """
    Fetches rosters and users from the Sleeper survivor pool.
    Returns:
      (anthony_previous_picks, pool_metadata)
      e.g. (['SEA', 'LAC', 'BUF', 'BAL'], {'total_rosters': 63, ...})
    """
    lid = league_id or os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", SLEEPER_SURVIVOR_LEAGUE_ID)
    users_url = f"https://api.sleeper.app/v1/league/{lid}/users"
    rosters_url = f"https://api.sleeper.app/v1/league/{lid}/rosters"

    anthony_picks: List[str] = []
    pool_meta: Dict[str, Any] = {"total_rosters": 0, "active_rosters": 0}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            users_resp, rosters_resp = await asyncio.gather(
                client.get(users_url),
                client.get(rosters_url),
            )
            if users_resp.status_code == 200 and rosters_resp.status_code == 200:
                users = users_resp.json()
                rosters = rosters_resp.json()
                pool_meta["total_rosters"] = len(rosters)

                # Find Anthony's user ID (pinec0ne)
                user_id = None
                target_user = os.getenv("SLEEPER_USER_ID", SLEEPER_USER_ID).lower()
                for u in users:
                    display_name = u.get("display_name", "").lower()
                    if display_name == target_user or "pinec0ne" in display_name:
                        user_id = u.get("user_id")
                        break

                for r in rosters:
                    is_eliminated = r.get("metadata", {}).get("is_eliminated") == "true"
                    if not is_eliminated:
                        pool_meta["active_rosters"] += 1

                    owner_id = r.get("owner_id")
                    if owner_id == user_id or (not user_id and r.get("roster_id") == 46):
                        picks_dict = r.get("metadata", {}).get("previous_picks", {})
                        for _, leg_picks in picks_dict.items():
                            if isinstance(leg_picks, list):
                                anthony_picks.extend(leg_picks)
                            elif isinstance(leg_picks, str):
                                anthony_picks.append(leg_picks)

    except Exception as e:
        log.warning(f"Failed to fetch survivor pool rosters: {e}")

    # Fallback to known previous picks if Sleeper is unreachable
    if not anthony_picks:
        # Default safety: SEA, LAC, BUF, BAL
        anthony_picks = ["SEA", "LAC", "BUF", "BAL"]

    # Ensure all uppercase and unique order preserved
    seen = set()
    cleaned_picks = []
    for p in anthony_picks:
        p_up = p.upper()
        if p_up not in seen:
            seen.add(p_up)
            cleaned_picks.append(p_up)

    return cleaned_picks, pool_meta


async def get_previous_picks(league_id: str, current_week: int) -> list:
    """Backward-compatible helper returning previous picks formatted as list."""
    anthony_picks, _ = await get_survivor_pool_rosters(league_id)
    return [{"week": i + 1, "picks": [{"user": "pinec0ne", "pick": p}]} for i, p in enumerate(anthony_picks)]


# ---------------------------------------------------------------------------
# Vegas Odds Fetcher
# ---------------------------------------------------------------------------

def clear_odds_cache() -> None:
    global _ODDS_CACHE
    _ODDS_CACHE = {"data": None, "timestamp": 0.0}


async def get_vegas_odds(force_refresh: bool = False) -> dict:
    """
    Fetches NFL moneyline and spread odds from The Odds API.
    Caches results for 15 minutes to preserve monthly request quota.
    """
    global _ODDS_CACHE

    data_source = {
        "status": "ok",
        "error": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    games = []

    # Check key first (supporting module-level monkeypatching in tests)
    mod = sys.modules.get("app.survivor_engine")
    api_key = getattr(mod, "ODDS_API_KEY", None)
    if api_key is None:
        api_key = os.getenv("ODDS_API_KEY", ODDS_API_KEY)
    if api_key is not None:
        api_key = api_key.strip()

    if not api_key or api_key == "your-odds-api-key-here":
        data_source["status"] = "error"
        data_source["error"] = "ODDS_API_KEY not set"
        return {"games": games, "data_source": data_source}

    now = time.time()
    if not force_refresh and _ODDS_CACHE.get("data") and (now - _ODDS_CACHE.get("timestamp", 0) < ODDS_CACHE_TTL):
        log.info("Returning cached Vegas odds")
        return _ODDS_CACHE["data"]

    url = (
        f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
        f"?apiKey={api_key}&regions=us&markets=h2h,spreads&oddsFormat=american"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

            for g in data:
                home = normalize_team_name(g["home_team"])
                away = normalize_team_name(g["away_team"])
                start = g.get("commence_time")

                spread = None
                moneyline_home = None
                moneyline_away = None

                bookmakers = g.get("bookmakers", [])
                for bm in bookmakers:
                    for m in bm.get("markets", []):
                        if m["key"] == "spreads" and spread is None:
                            for out in m.get("outcomes", []):
                                out_name = normalize_team_name(out["name"])
                                if out_name == home:
                                    spread = out.get("point")
                        elif m["key"] == "h2h" and (moneyline_home is None or moneyline_away is None):
                            for out in m.get("outcomes", []):
                                out_name = normalize_team_name(out["name"])
                                if out_name == home:
                                    moneyline_home = out.get("price")
                                elif out_name == away:
                                    moneyline_away = out.get("price")

                home_prob = None
                away_prob = None

                if moneyline_home is not None:
                    if moneyline_home < 0:
                        home_prob = abs(moneyline_home) / (abs(moneyline_home) + 100)
                    else:
                        home_prob = 100 / (moneyline_home + 100)

                if moneyline_away is not None:
                    if moneyline_away < 0:
                        away_prob = abs(moneyline_away) / (abs(moneyline_away) + 100)
                    else:
                        away_prob = 100 / (moneyline_away + 100)

                # fallback to spread for probability if moneyline is missing
                if home_prob is None and spread is not None:
                    home_prob = 1.0 / (1.0 + 10.0 ** (spread / 7.0))
                    away_prob = 1.0 - home_prob

                games.append({
                    "home": home,
                    "away": away,
                    "start": start,
                    "spread_home": spread,
                    "moneyline_home": moneyline_home,
                    "moneyline_away": moneyline_away,
                    "implied_prob_home": round(home_prob, 3) if home_prob is not None else None,
                    "implied_prob_away": round(away_prob, 3) if away_prob is not None else None,
                })

            result = {"games": games, "data_source": data_source}
            _ODDS_CACHE = {"data": result, "timestamp": time.time()}
            return result

    except Exception as e:
        log.warning(f"Failed to fetch odds: {e}")
        data_source["status"] = "error"
        data_source["error"] = str(e)
        return {"games": games, "data_source": data_source}


# ---------------------------------------------------------------------------
# Candidate Evaluation
# ---------------------------------------------------------------------------

def evaluate_survivor_candidates(
    games: List[Dict[str, Any]],
    previously_picked: List[str],
) -> List[SurvivorCandidate]:
    """
    Ranks all teams playing this week by win probability.
    Strictly flags and eliminates any teams Anthony has already picked.
    """
    picked_set = {p.upper() for p in previously_picked}
    # Also add full names of picked teams
    picked_full_names = {NFL_TEAMS.get(p, "").lower() for p in picked_set if p in NFL_TEAMS}

    candidates: List[SurvivorCandidate] = []

    for g in games:
        home_name = normalize_team_name(g["home"])
        away_name = normalize_team_name(g["away"])
        home_abbrev = get_team_abbreviation(home_name)
        away_abbrev = get_team_abbreviation(away_name)

        p_home = g.get("implied_prob_home") or 0.50
        p_away = g.get("implied_prob_away") or 0.50

        spread_home = g.get("spread_home")
        spread_away = -spread_home if spread_home is not None else None

        ml_home = g.get("moneyline_home")
        ml_away = g.get("moneyline_away")

        # Home Candidate
        home_cand = SurvivorCandidate(
            team=home_name,
            abbrev=home_abbrev,
            opponent=away_name,
            opponent_abbrev=away_abbrev,
            prob=p_home,
            spread=spread_home,
            moneyline=ml_home,
            is_home=True,
        )
        if home_abbrev in picked_set or home_name.lower() in picked_full_names:
            home_cand.is_eligible = False
            home_cand.ineligibility_reason = f"Already used in previous week ({home_abbrev})"
        candidates.append(home_cand)

        # Away Candidate
        away_cand = SurvivorCandidate(
            team=away_name,
            abbrev=away_abbrev,
            opponent=home_name,
            opponent_abbrev=home_abbrev,
            prob=p_away,
            spread=spread_away,
            moneyline=ml_away,
            is_home=False,
        )
        if away_abbrev in picked_set or away_name.lower() in picked_full_names:
            away_cand.is_eligible = False
            away_cand.ineligibility_reason = f"Already used in previous week ({away_abbrev})"
        candidates.append(away_cand)

    # Sort all candidates by win probability descending
    candidates.sort(key=lambda c: c.prob, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Core Unified Pick Recommendation Generator
# ---------------------------------------------------------------------------

async def generate_survivor_recommendation(
    query: Optional[str] = None,
    league_id: Optional[str] = None,
) -> SurvivorPickResult:
    """
    Unified entry point for Survivor pool pick generation.
    Used by both the chat toggle (/api/chat_stream, /api/chat) and weekly email cron.
    """
    lid = league_id or os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", SLEEPER_SURVIVOR_LEAGUE_ID)
    api_key_odds = os.getenv("ODDS_API_KEY", ODDS_API_KEY).strip()
    api_key_gemini = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY).strip()

    # Pre-check 1: ODDS_API_KEY
    if not api_key_odds or api_key_odds == "your-odds-api-key-here" or api_key_odds == "test-mock-key":
        return SurvivorPickResult(
            status="error",
            is_valid=False,
            week=1,
            league_name="Fat Crew Survivor",
            primary_team="",
            primary_abbrev="",
            primary_opponent="",
            primary_prob=0.0,
            primary_spread=None,
            primary_ml=None,
            alternate_team="",
            alternate_abbrev="",
            alternate_opponent="",
            alternate_prob=0.0,
            alternate_spread=None,
            alternate_ml=None,
            previously_picked=[],
            candidates=[],
            recommendation_text=ODDS_KEY_MISSING_MESSAGE,
            recommendation_html="<p>ODDS_API_KEY is not configured. Live odds unavailable.</p>",
            data_sources={"survivor_odds": {"status": "error", "error": "ODDS_API_KEY not configured"}},
            error="ODDS_API_KEY is not configured",
        )

    # Fetch live pool state, previous picks, and Vegas odds concurrently
    state_task = asyncio.create_task(get_survivor_state(lid))
    roster_task = asyncio.create_task(get_survivor_pool_rosters(lid))
    odds_task = asyncio.create_task(get_vegas_odds())

    state, (previously_picked, pool_meta), odds_data = await asyncio.gather(
        state_task, roster_task, odds_task
    )

    current_week = state.get("current_week", 1)
    league_name = state.get("league_name", "Fat Crew Survivor")
    data_source = odds_data.get("data_source", {})
    games = odds_data.get("games", [])

    # Pre-check 2: Verify odds data loaded successfully
    if data_source.get("status") != "ok" or not games:
        err_msg = data_source.get("error") or "No live NFL games found in odds feed"
        return SurvivorPickResult(
            status="error",
            is_valid=False,
            week=current_week,
            league_name=league_name,
            primary_team="",
            primary_abbrev="",
            primary_opponent="",
            primary_prob=0.0,
            primary_spread=None,
            primary_ml=None,
            alternate_team="",
            alternate_abbrev="",
            alternate_opponent="",
            alternate_prob=0.0,
            alternate_spread=None,
            alternate_ml=None,
            previously_picked=previously_picked,
            candidates=[],
            recommendation_text=f"⚠️ **Vegas Odds Unavailable**: {err_msg}",
            recommendation_html=f"<p>Vegas odds feed error: {err_msg}</p>",
            data_sources={"survivor_odds": data_source},
            error=err_msg,
        )

    # Evaluate and rank candidates
    all_candidates = evaluate_survivor_candidates(games, previously_picked)
    eligible_candidates = [c for c in all_candidates if c.is_eligible]

    if not eligible_candidates:
        return SurvivorPickResult(
            status="error",
            is_valid=False,
            week=current_week,
            league_name=league_name,
            primary_team="",
            primary_abbrev="",
            primary_opponent="",
            primary_prob=0.0,
            primary_spread=None,
            primary_ml=None,
            alternate_team="",
            alternate_abbrev="",
            alternate_opponent="",
            alternate_prob=0.0,
            alternate_spread=None,
            alternate_ml=None,
            previously_picked=previously_picked,
            candidates=[asdict(c) for c in all_candidates],
            recommendation_text="⚠️ No eligible NFL teams available for selection this week.",
            recommendation_html="<p>No eligible teams available.</p>",
            data_sources={"survivor_odds": data_source},
            error="No eligible teams available",
        )

    # Primary Pick: Highest win probability available team
    primary = eligible_candidates[0]

    # Alternate Pick: Second-highest win probability available team
    # (Preferably from a different game if possible)
    alternate = eligible_candidates[1] if len(eligible_candidates) > 1 else eligible_candidates[0]
    for cand in eligible_candidates[1:]:
        if cand.opponent != primary.team and cand.team != primary.opponent:
            alternate = cand
            break

    # Build context for LLM reasoning
    used_teams_str = ", ".join(f"{p} ({NFL_TEAMS.get(p, p)})" for p in previously_picked)
    top_candidates_summary = "\n".join([
        f"- {c.team} ({c.abbrev}) vs {c.opponent}: Win Prob {c.prob*100:.1f}%, Spread {c.spread or 'PK'}, ML {c.moneyline or 'N/A'}"
        for c in eligible_candidates[:6]
    ])

    llm_context = textwrap.dedent(f"""\
        League: {league_name} (NFL Week {current_week})
        User's Previously Burned Teams (LOCKED - CANNOT PICK): {used_teams_str}

        DETERMINED SELECTIONS:
        - PRIMARY PICK: {primary.team} ({primary.abbrev}) vs {primary.opponent}
          * Win Probability: {primary.prob*100:.1f}%
          * Spread: {primary.spread or 'PK'}
          * Moneyline: {primary.moneyline or 'N/A'}
          * Home/Away: {'Home' if primary.is_home else 'Away'}

        - ALTERNATE PICK: {alternate.team} ({alternate.abbrev}) vs {alternate.opponent}
          * Win Probability: {alternate.prob*100:.1f}%
          * Spread: {alternate.spread or 'PK'}
          * Moneyline: {alternate.moneyline or 'N/A'}
          * Home/Away: {'Home' if alternate.is_home else 'Away'}

        Top Available Matchups:
        {top_candidates_summary}
    """)

    user_prompt = query.strip() if query and query.strip() else f"Provide the official Week {current_week} Survivor Pool recommendation."
    llm_prompt = f"Live Survivor Data Context:\n<data>\n{llm_context}\n</data>\n\nUser Question:\n<user_question>\n{user_prompt}\n</user_question>"

    # Call Gemini via unified ask_gemini
    llm_analysis = ""
    try:
        llm_analysis = await ask_gemini(
            prompt=llm_prompt,
            system_instruction=SURVIVOR_SYSTEM_INSTRUCTION,
            timeout=45.0,
        )
    except Exception as e:
        log.warning(f"Gemini call failed during survivor pick analysis: {e}")
        llm_analysis = (
            f"**Recommendation**: Take the **{primary.team}** ({primary.prob*100:.1f}% win probability, {primary.spread or 'PK'}) "
            f"against {primary.opponent}. They are the highest-probability favorite available on your board.\n\n"
            f"**Alternate Pivot**: Consider the **{alternate.team}** ({alternate.prob*100:.1f}% win probability) "
            f"against {alternate.opponent} if you wish to preserve future value."
        )

    # Format Markdown response for Chatbot
    rec_markdown = (
        f"### 🏈 Week {current_week} Survivor Pick: **{primary.team}**\n\n"
        f"**Primary Choice**: **{primary.team}** ({primary.prob*100:.1f}% Win Probability, Spread: {primary.spread or 'PK'})\n"
        f"- **Matchup**: vs {primary.opponent} ({'Home' if primary.is_home else 'Away'})\n"
        f"- **Alternate Pivot**: **{alternate.team}** ({alternate.prob*100:.1f}% Win Probability, Spread: {alternate.spread or 'PK'})\n"
        f"- **Teams Burned**: {', '.join(previously_picked)}\n\n"
        f"---\n\n"
        f"{llm_analysis}"
    )

    # Format clean HTML response for Email
    rec_html = f"""
    <h2>🏈 Week {current_week} Recommendation: {primary.team}</h2>
    <p><b>Primary Pick:</b> <span style="color:#ea580c; font-size:16px;"><b>{primary.team}</b></span> ({primary.prob*100:.1f}% Win Probability, {primary.spread or 'PK'} spread)</p>
    <p><b>Opponent:</b> vs {primary.opponent} ({'Home' if primary.is_home else 'Away'})</p>
    
    <div style="background:#f1f5f9; padding:12px; border-radius:6px; margin:15px 0;">
      <h3 style="margin-top:0; color:#334155;">⚖️ Strategic Pivot</h3>
      <p style="margin-bottom:0;"><b>Contrarian Alternate:</b> {alternate.team} ({alternate.prob*100:.1f}% Win Prob, {alternate.spread or 'PK'} spread vs {alternate.opponent})</p>
    </div>

    <h3>📊 Analysis & Game Theory</h3>
    <div>
      {llm_analysis.replace(chr(10), '<br>')}
    </div>

    <p style="font-size:12px; color:#64748b; margin-top:20px;">
      <b>Previously Locked Teams:</b> {', '.join(previously_picked)}<br>
      <i>Source: Live Vegas Odds Feed via The Odds API</i>
    </p>
    """

    return SurvivorPickResult(
        status="ok",
        is_valid=True,
        week=current_week,
        league_name=league_name,
        primary_team=primary.team,
        primary_abbrev=primary.abbrev,
        primary_opponent=primary.opponent,
        primary_prob=primary.prob,
        primary_spread=primary.spread,
        primary_ml=primary.moneyline,
        alternate_team=alternate.team,
        alternate_abbrev=alternate.abbrev,
        alternate_opponent=alternate.opponent,
        alternate_prob=alternate.prob,
        alternate_spread=alternate.spread,
        alternate_ml=alternate.moneyline,
        previously_picked=previously_picked,
        candidates=[asdict(c) for c in all_candidates],
        recommendation_text=rec_markdown,
        recommendation_html=rec_html,
        data_sources={"survivor_odds": data_source},
    )


# ---------------------------------------------------------------------------
# Backward Compatibility Wrappers
# ---------------------------------------------------------------------------

async def fetch_survivor_context() -> Tuple[str, dict, int]:
    state = await get_survivor_state()
    current_week = state.get("current_week", 1)
    odds_data = await get_vegas_odds()
    previous_picks = await get_previous_picks(SLEEPER_SURVIVOR_LEAGUE_ID, current_week)

    context = (
        f"League: {state.get('league_name')}\n"
        f"Current Week: {current_week}\n"
        f"Data Sources: {json.dumps(odds_data.get('data_source'))}\n"
        f"Odds/Matchups: {json.dumps(odds_data.get('games'), indent=2)}\n"
        f"Previous Picks in League: {json.dumps(previous_picks, indent=2)}\n"
    )
    return context, odds_data, current_week


async def ask_survivor_gemini_direct(api_key: str, context: str, custom_prompt: Optional[str] = None) -> str:
    question = custom_prompt if custom_prompt else "Please provide your survivor recommendation for this week."
    prompt = (
        f"Live Survivor Data:\n<data>\n{context}\n</data>\n\n"
        f"Answer this request:\n<user_question>\n{question}\n</user_question>"
    )
    try:
        return await ask_gemini(
            prompt=prompt,
            system_instruction=SURVIVOR_SYSTEM_INSTRUCTION,
            timeout=45.0,
        )
    except Exception as e:
        log.warning(f"ask_survivor_gemini_direct failure: {e}")
        return f"⚠️ Survivor analysis temporarily unavailable: {type(e).__name__}: {str(e)}"


async def build_survivor_recommendation(api_key: Optional[str] = None, custom_prompt: Optional[str] = None) -> dict:
    result = await generate_survivor_recommendation(query=custom_prompt)
    return {
        "recommendation": result.recommendation_text,
        "recommendation_html": result.recommendation_html,
        "data_sources": result.data_sources,
        "week": result.week,
        "is_valid": result.is_valid,
        "primary_team": result.primary_team,
        "alternate_team": result.alternate_team,
    }
