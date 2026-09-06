"""
Fantasy Football AI Assistant — Tony Romo's DUI Mugshot
Sleeper league: "Loser eats an egg"  |  League ID: 1389359956657967104
"""

from __future__ import annotations

import json
import logging
import os
import random
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google import genai
from pydantic import BaseModel

from app.waivers import get_waiver_recommendations
from app.gm_engine import best_lineup, get_team_analytics, get_player_projection

# ---------------------------------------------------------------------------
# Env + logging
# ---------------------------------------------------------------------------
load_dotenv()

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "test-mock-key")
SLEEPER_LEAGUE_ID = os.getenv("SLEEPER_LEAGUE_ID", "1389359956657967104").strip()
SLEEPER_USER_ID   = os.getenv("SLEEPER_USER_ID",   "pinec0ne").strip()

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
LEAGUE_ID   = SLEEPER_LEAGUE_ID
MY_ROSTER_ID = 4   # pinec0ne / Anthony's roster_id
SLEEPER_BASE = "https://api.sleeper.app/v1"

# ---------------------------------------------------------------------------
# Sleeper helpers
# ---------------------------------------------------------------------------

def _cb() -> str:
    """Cache-buster query param."""
    return f"cb={random.randint(100_000, 999_999)}"

def _get(url: str, timeout: int = 10) -> Any:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

_ALL_PLAYERS_CACHE: dict[str, Any] | None = None

def get_all_players() -> dict[str, Any]:
    global _ALL_PLAYERS_CACHE
    if _ALL_PLAYERS_CACHE is None:
        try:
            log.info("Fetching bulk player data from Sleeper...")
            t0 = time.time()
            _ALL_PLAYERS_CACHE = _get(f"{SLEEPER_BASE}/players/nfl")
            log.info(f"Fetched {len(_ALL_PLAYERS_CACHE)} players in {time.time() - t0:.3f}s")
        except Exception as e:
            log.error(f"Failed to fetch all players: {e}")
            _ALL_PLAYERS_CACHE = {}
    return _ALL_PLAYERS_CACHE

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
        all_players = get_all_players()
        if pid in all_players:
            p = all_players[pid]
            resolved_name = f"{p.get('first_name', '')} {p.get('last_name', '')} ({p.get('team', '?')}, {p.get('position', '?')})".strip()
            if not resolved_name or resolved_name == "()":
                resolved_name = pid
            PLAYER_IDS[pid] = resolved_name
            return resolved_name
    except Exception as e:
        log.warning(f"Error resolving player {pid}: {e}")
        pass
        
    fallback = f"Unknown({pid})"
    PLAYER_IDS[pid] = fallback
    return fallback

def fetch_nfl_state() -> dict:
    return _get(f"{SLEEPER_BASE}/state/nfl?{_cb()}")

def fetch_all_rosters() -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/rosters?{_cb()}")

def fetch_league_users() -> dict[str, str]:
    users = _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/users?{_cb()}")
    owner_map = {}
    for u in users:
        team_name = u.get("metadata", {}).get("team_name") or u.get("display_name", "Unknown")
        owner_id = str(u.get("user_id"))
        owner_map[owner_id] = team_name
    return owner_map

def fetch_matchups(week: int) -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/matchups/{week}?{_cb()}")

def fetch_transactions(week: int) -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/transactions/{week}?{_cb()}")

# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _build_player_dict(pid: str) -> dict:
    name = resolve_player_name(pid)
    clean_name = name.split(" (")[0] if " (" in name else name
    proj = get_player_projection(clean_name)
    pos = proj.get("pos")
    if not pos and " (" in name:
        pos = name.split(", ")[1].replace(")", "")
    
    return {
        "id": pid,
        "name": name,
        "pos": pos,
        "pts": proj.get("pts", 0.0),
        "posrank": proj.get("posrank")
    }

def build_context(question: str) -> str:
    log.info("Starting build_context...")
    t_start = time.time()
    ctx_parts: list[str] = []

    try:
        t_state = time.time()
        state = fetch_nfl_state()
        log.info(f"fetch_nfl_state took {time.time() - t_state:.3f}s")
        week = state.get("week", "?")
        season = state.get("season", "2026")
        ctx_parts.append(f"## NFL State\nSeason: {season} | Week: {week} | Type: {state.get('season_type', '?')}")
    except Exception as e:
        week = 1
        ctx_parts.append(f"## NFL State\nUnavailable ({e})")

    try:
        t_rosters = time.time()
        rosters = fetch_all_rosters()
        owner_map = fetch_league_users()
        log.info(f"fetch_all_rosters and users took {time.time() - t_rosters:.3f}s")
        t_parse = time.time()
        
        roster_map = {}
        roster_names = {}
        for r in rosters:
            rid = r.get("roster_id")
            owner_id = str(r.get("owner_id"))
            team_name = owner_map.get(owner_id, f"Roster {rid}")
            roster_names[rid] = team_name
            
            plist = r.get("players") or []
            roster_map[rid] = [_build_player_dict(pid) for pid in plist]

        analytics = get_team_analytics(roster_map)
        my_analytics = analytics.get(MY_ROSTER_ID, {})
        my_roster_data = roster_map.get(MY_ROSTER_ID, [])
        my_tot, my_lu = best_lineup(my_roster_data)
        log.info(f"Roster parsing and analytics took {time.time() - t_parse:.3f}s")

        standing_lines = []
        for rid, stat in analytics.items():
            tag = " (ANTHONY)" if rid == MY_ROSTER_ID else ""
            tname = roster_names.get(rid, f"Roster {rid}")
            standing_lines.append(f"{stat['overall_rank']}. {tname}{tag}: {stat['overall_pts']:.1f} pts (RB rank {stat['rb_in_lineup_rank']}, WR rank {stat['wr_in_lineup_rank']})")

        my_starters_str = ", ".join(f"{p['slot']}:{p['name']}" for p in my_lu)
        my_bench_str = ", ".join(p['name'] for p in my_roster_data if p['name'] not in [x['name'] for x in my_lu])

        ctx_parts.append(
            f"## Anthony's Roster (roster_id {MY_ROSTER_ID})\n"
            f"**Projected Starters:** {my_starters_str or 'none'}\n"
            f"**Bench:** {my_bench_str}\n\n"
            f"**Analytics:** Overall Rank {my_analytics.get('overall_rank')}/12 | RB Rank {my_analytics.get('rb_in_lineup_rank')}/12 | WR Rank {my_analytics.get('wr_in_lineup_rank')}/12\n\n"
            f"## League Standings (by Optimal Lineup pts)\n" + "\n".join(standing_lines)
        )
    except Exception as e:
        ctx_parts.append(f"## Anthony's Roster\nUnavailable ({e})")
        roster_names = {}

    try:
        t_matchups = time.time()
        matchups = fetch_matchups(week)
        log.info(f"fetch_matchups took {time.time() - t_matchups:.3f}s")
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

    try:
        t_waivers = time.time()
        waiver_data = get_waiver_recommendations(LEAGUE_ID, MY_ROSTER_ID, resolve_player_name)
        log.info(f"get_waiver_recommendations took {time.time() - t_waivers:.3f}s")
        w_lines = []
        for t in waiver_data.get("targets", [])[:5]:
            w_lines.append(f"- {t['name']} ({t['position']}): {t['recommendation_reason']}")
        rec_drop = waiver_data.get("recommended_drop")
        drop_str = f"\n**Suggested Drop:** {rec_drop['name']} ({rec_drop['reason']})" if rec_drop else ""
        
        ctx_parts.append(f"## Waiver Wire Intelligence\n" + ("\n".join(w_lines) or "None found") + drop_str)
    except Exception as e:
        ctx_parts.append(f"## Waiver Wire Intelligence\nUnavailable ({e})")

    try:
        if str(week).isdigit():
            t_tx = time.time()
            txs = fetch_transactions(int(week))
            log.info(f"fetch_transactions took {time.time() - t_tx:.3f}s")
            tx_lines = []
            for tx in txs[:5]:
                ttype = tx.get("type", "unknown")
                if ttype in ("waiver", "free_agent", "trade"):
                    adds = tx.get("adds") or {}
                    drops = tx.get("drops") or {}
                    add_strs = [f"Added {resolve_player_name(p)}" for p in adds.keys()]
                    drop_strs = [f"Dropped {resolve_player_name(p)}" for p in drops.keys()]
                    status = tx.get("status", "complete")
                    tx_lines.append(f"- {ttype.upper()} ({status}): {', '.join(add_strs + drop_strs)}")
            ctx_parts.append(f"## Recent Transactions\n" + ("\n".join(tx_lines) if tx_lines else "None."))
    except Exception as e:
        ctx_parts.append(f"## Recent Transactions\nUnavailable ({e})")

    stale_note = "  ⚠️ Preseason projections — weight recent usage over these after week 4.\n" if (isinstance(week, int) and week >= 4) else ""
    ctx_parts.append(f"## Notes\n{stale_note}")

    log.info(f"Total build_context took {time.time() - t_start:.3f}s")
    return "\n\n".join(ctx_parts)

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
_gemini_client: genai.Client | None = None

def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert fantasy football analyst and strategist for a 12-team, half-PPR Sleeper league called "Loser eats an egg."
    You manage Anthony's team, **Tony Romo's DUI Mugshot** (roster_id 4).

    ### League scoring
    - Passing: 0.04/yd · 4 per TD · -1 per INT · 2 per 2PC
    - Rushing: 0.1/yd · 6 per TD · 2 per 2PC
    - Receiving: 0.5 per reception · 0.1/yd · 6 per TD · 2 per 2PC
    - Kicker: distance-tiered (0-39=3, 40-49=4, 50-59=5, 60+=6)

    ### Your job (Full GM Mode)
    - Act as a comprehensive General Manager. Proactively evaluate trade opportunities using the optimal lineup delta method.
    - Leverage the structured GM analytics provided in the context (Overall/RB/WR ranks) to identify team weaknesses.
    - Suggest optimal waiver claims using the strict priority: WR upside first, RB handcuffs second.
    - Never recommend 2nd QB or TE unless starter is out.
    - Keep answers mobile-friendly: scannable, short bullet points, narrow tables.
    - Flag uncertainty clearly — say when you're guessing.
""")

def ask_gemini(question: str, context: str) -> str:
    client = get_gemini_client()
    prompt = (
        f"Live league data context:\n<league_data>\n{context}\n</league_data>\n\n"
        f"Answer this question as the GM:\n<user_question>\n{question}\n</user_question>"
    )
    # The user updated settings to use Gemini 3.1 Pro (High), but asked the bot to use a fast/light model
    models_to_try = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]

    last_error = None
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                log.info(f"Asking Gemini with model {model_name} (Attempt {attempt+1})...")
                t_llm = time.time()
                interaction = client.interactions.create(
                    model=model_name,
                    input=prompt,
                    system_instruction=SYSTEM_PROMPT,
                    store=False,
                )
                log.info(f"Gemini {model_name} responded in {time.time() - t_llm:.3f}s")
                if interaction.output_text:
                    return interaction.output_text
            except Exception as e:
                last_error = e
                log.warning(f"Attempt {attempt + 1} with {model_name} failed: {e}")
                time.sleep(1.0)

    # Return a clean error message rather than crashing the server
    return f"⚠️ Analysis engine temporarily unavailable. All models overloaded or encountered an error. (Last error: {last_error})"

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Fantasy Football AI GM API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    query: str

class ChatResponse(BaseModel):
    answer: str
    context_summary: str
    latency_ms: int

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

@app.get("/health", tags=["Meta"])
async def health():
    return {"status": "ok", "league_id": LEAGUE_ID, "team": "Tony Romo's DUI Mugshot"}

@app.get("/api/waivers", tags=["GM Endpoints"])
async def api_waivers():
    try:
        return get_waiver_recommendations(LEAGUE_ID, MY_ROSTER_ID, resolve_player_name)
    except Exception as e:
        log.error("Waiver endpoint error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch waiver recommendations")

@app.get("/api/gm/overview", tags=["GM Endpoints"])
async def api_gm_overview():
    try:
        rosters = fetch_all_rosters()
        roster_map = {}
        for r in rosters:
            rid = r.get("roster_id")
            plist = r.get("players") or []
            roster_map[rid] = [_build_player_dict(pid) for pid in plist]
        analytics = get_team_analytics(roster_map)
        
        my_analytics = analytics.get(MY_ROSTER_ID, {})
        my_roster_data = roster_map.get(MY_ROSTER_ID, [])
        my_tot, my_lu = best_lineup(my_roster_data)

        return {
            "roster_id": MY_ROSTER_ID,
            "analytics": my_analytics,
            "optimal_lineup_pts": my_tot,
            "optimal_lineup": my_lu,
            "league_standings": analytics
        }
    except Exception as e:
        log.error("GM overview error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to generate GM overview")

@app.post("/api/chat", response_model=ChatResponse, tags=["Chat"])
async def chat(req: ChatRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    start_t = time.time()
    context = build_context(req.query)
    answer  = ask_gemini(req.query, context)
    latency = int((time.time() - start_t) * 1000)

    summary = context[:300].replace("\n", " ") + "…"
    return ChatResponse(answer=answer, context_summary=summary, latency_ms=latency)

@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def index():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>🏈 Fantasy Assistant</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
</head>
<body class="bg-gray-50 min-h-screen flex flex-col font-sans p-4">
    <div class="max-w-2xl mx-auto w-full">
        <h1 class="text-2xl font-bold mb-4">Tony Romo's DUI Mugshot - GM API</h1>
        <p>API endpoints running natively.</p>
        <ul class="list-disc pl-5 mt-4 space-y-2 text-blue-600">
            <li><a href="/api/waivers">/api/waivers</a></li>
            <li><a href="/api/gm/overview">/api/gm/overview</a></li>
        </ul>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)
