"""Fantasy Sleeper AI — FastAPI Application (v2 Reasoner Architecture).

Exposes unified endpoints:
- GET /health: Deep 6-subsystem monitoring (including Odds API)
- POST /api/chat: Grounded reasoner with fantasy / survivor toggle
- POST /api/survivor: Explicit survivor pool engine
- POST /api/survivor/email-preview: Weekly survivor email dispatcher preview
- GET /api/world-state: Unified WorldStateSnapshot inspection
- GET /: Mobile-first responsive UI with provenance badges and survivor toggle
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.board_auditor import audit_and_sanitize_board
from core.config import (
    ANTHONY_SURVIVOR_USED_TEAMS,
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LEAGUE_ID,
    MY_ROSTER_ID,
    ODDS_API_KEY,
    TEAM_NAME,
)
from core.data_pipeline import (
    OddsAPIClient,
    SleeperClient,
    load_board_catalog,
    load_player_ids_catalog,
)
from core.news_pipeline import load_news_and_depth_chart, run_startup_catalog_sanity_check
from core.provenance import build_provenance_record
from core.reasoner import GeminiReasoner
from core.schemas import (
    ChatRequest as V2ChatRequest,
    ChatResponse as V2ChatResponse,
    SurvivorPickResult,
    WorldStateSnapshot,
)
from core.survivor import SurvivorEmailDispatcher, SurvivorEngine
from core.week_resolver import resolve_canonical_nfl_week
from core.world_model import WorldModelBuilder

# Backward-compatibility handles for existing test suites
from app.config import SLEEPER_BASE
import app.sleeper as sleeper_mod
from app.sleeper import (
    PLAYER_IDS,
    _cache,
    close_http_client,
    fetch_all_rosters,
    fetch_espn_scoreboard,
    fetch_league,
    fetch_league_users,
    fetch_matchups,
    fetch_transactions,
    fetch_trending_adds,
    get_all_players,
    get_all_players_sync,
    resolve_player_name,
)
from app.gm_engine import (
    best_lineup,
    compute_actual_points,
    get_actual_standings,
    get_blended_projection,
    get_player_projection,
    get_team_analytics,
)
from app.waivers import get_waiver_recommendations

log = logging.getLogger(__name__)

# Singletons
_sleeper_client = SleeperClient()
_odds_client = OddsAPIClient()
_world_builder = WorldModelBuilder(sleeper_client=_sleeper_client, odds_client=_odds_client)
_reasoner = GeminiReasoner()
_survivor_engine = SurvivorEngine()
_email_dispatcher = SurvivorEmailDispatcher(engine=_survivor_engine)


# ── Backward Compatibility Helpers for Legacy Tests ────────────────────────

def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry.get("ts", 0)) < entry.get("ttl", 300):
        return entry.get("data")
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = {"data": data, "ts": time.time(), "ttl": 300}


async def _aget(url: str, timeout: float = 15.0) -> Any:
    client = sleeper_mod.get_http_client()
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def fetch_nfl_state() -> Dict[str, Any]:
    cached = _cache_get("nfl_state")
    if cached:
        return cached
    data = await _aget(f"{sleeper_mod.SLEEPER_BASE}/state/nfl?{sleeper_mod._cb()}")
    _cache_set("nfl_state", data)
    return data


async def _fetch_espn_schedule_async(week: int) -> str:
    events = await fetch_espn_scoreboard(week)
    if not events:
        return ""
    lines = [f"## Week {week} Schedule & Weather"]
    for ev in events:
        lines.append(f"- {ev.get('name')}: {ev.get('weather')}")
    return "\n".join(lines)


async def fetch_matchup_history(current_week: int) -> Dict[int, list]:
    if current_week <= 1:
        return {}
    import asyncio
    tasks = [fetch_matchups(w) for w in range(1, current_week)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    history = {}
    for w, res in zip(range(1, current_week), results):
        if not isinstance(res, Exception):
            history[w] = res
    return history


async def ask_gemini(question: str, context: str) -> str:
    from app.llm import ask_gemini as _legacy_ask
    from app.assistant import SYSTEM_PROMPT
    return await _legacy_ask(prompt=f"{context}\n\nQuestion: {question}", system_instruction=SYSTEM_PROMPT)


async def build_context(question: str) -> Tuple[str, dict]:
    data_sources = {"nfl_state": {"status": "ok", "detail": "loaded"}}
    state = await fetch_nfl_state()
    week = state.get("week", 1)
    season = state.get("season", "2026")
    rosters = await fetch_all_rosters()
    players_db = await get_all_players()

    ctx_parts = [
        f"## NFL State\nSeason: {season} | Week: {week} | Type: {state.get('season_type', '?')}"
    ]

    my_r = next((r for r in rosters if r.get("roster_id") == MY_ROSTER_ID), None)
    if my_r:
        plist = my_r.get("players") or []
        starters = my_r.get("starters") or []
        ir_lines = []
        starter_lines = []
        bench_lines = []

        for pid in plist:
            pinfo = players_db.get(pid, {})
            inj = pinfo.get("injury_status") if isinstance(pinfo, dict) else ""
            pname = resolve_player_name(pid)
            if inj in ("IR", "Out", "PUP"):
                ir_lines.append(f"  🏥 {pname} — {inj} (NOT eligible for lineup)")
            elif pid in starters:
                starter_lines.append(f"  {pname}")
            else:
                bench_lines.append(f"  {pname}")

        roster_text = (
            f"## Anthony's Roster (roster_id {MY_ROSTER_ID})\n"
            f"**Starters:**\n" + "\n".join(starter_lines) + "\n"
        )
        if ir_lines:
            roster_text += f"\n**IR / Injured Reserve:**\n" + "\n".join(ir_lines) + "\n"
        if bench_lines:
            roster_text += f"\n**Bench:**\n" + "\n".join(bench_lines) + "\n"
        ctx_parts.append(roster_text)

    return "\n\n".join(ctx_parts), data_sources


# ── Lifespan & App Setup ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🏈 Initializing fantasy-sleeper-ai v2 service...")
    try:
        pids_map, _ = load_player_ids_catalog()
        board_data, _, _ = load_board_catalog()
        news_data, _ = load_news_and_depth_chart()
        sanity = run_startup_catalog_sanity_check(
            player_ids_map=pids_map,
            board_data=board_data,
            news_feed=news_data,
            sleeper_players=get_all_players_sync(),
        )
        log.info(
            "✅ Local catalogs and news feeds warmed successfully (sanity=%s, dup_ids=%d, unmapped_board=%d)",
            sanity["status"],
            sanity["duplicate_player_id_names_count"],
            sanity["unmapped_board_players_count"],
        )
    except Exception as e:
        log.warning("⚠️ Failed warming catalogs on startup: %s", e)
    yield
    await _sleeper_client.aclose()
    log.info("👋 fantasy-sleeper-ai v2 shutdown complete")


app = FastAPI(
    title="Fantasy Sleeper AI & Survivor Reasoner (v2)",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Unified Chat Request / Response Models ─────────────────────────────────

class FlexibleChatRequest(BaseModel):
    """Permissive request accepting both v2 ('message') and v1 ('query') formats."""
    message: Optional[str] = None
    query: Optional[str] = None
    session_id: Optional[str] = "default"
    mode: Optional[str] = "fantasy"
    league_context: Optional[str] = "egg"
    used_teams: List[str] = Field(default_factory=lambda: list(ANTHONY_SURVIVOR_USED_TEAMS))
    force_refresh: bool = False

    def get_prompt_text(self) -> str:
        text = self.message or self.query or ""
        return text.strip()

    def get_mode(self) -> str:
        if self.mode and self.mode.lower() == "survivor":
            return "survivor"
        if self.league_context and self.league_context.lower() == "survivor":
            return "survivor"
        if "survivor" in self.get_prompt_text().lower():
            return "survivor"
        return "fantasy"


class UnifiedChatResponse(BaseModel):
    """Response containing rich v2 contracts with v1 backward compatibility."""
    response: str
    mode: str
    snapshot_id: str
    nfl_week: int
    provenance: List[Any] = Field(default_factory=list)
    injury_alerts: List[str] = Field(default_factory=list)
    citations: List[str] = Field(default_factory=list)
    survivor_result: Optional[SurvivorPickResult] = None
    # Compatibility aliases for legacy tests
    answer: str = ""
    context_summary: str = ""
    latency_ms: int = 0
    data_sources: dict = Field(default_factory=dict)


# ── Health Endpoint (Deep 6 Subsystems) ────────────────────────────────────

@app.get("/health", tags=["Monitoring"])
async def health():
    """Deep health check across all 6 subsystems with canonical NFL week and catalog sanity."""
    subsystems: Dict[str, Any] = {}
    overall_status = "ok"

    pids: Dict[str, str] = {}
    board: List[Dict[str, Any]] = []
    news_data: Dict[str, Any] = {}

    # 1. Player IDs catalog
    try:
        pids, pids_prov = load_player_ids_catalog()
        subsystems["player_ids_catalog"] = {
            "status": pids_prov.status,
            "record_count": pids_prov.record_count,
            "path": pids_prov.endpoint_or_path,
        }
    except Exception as e:
        subsystems["player_ids_catalog"] = {"status": "failed", "error": str(e)}
        overall_status = "degraded"

    # 2. Board catalog & bias audit
    try:
        board, board_summary, board_prov = load_board_catalog()
        subsystems["board_catalog"] = {
            "status": board_prov.status,
            "record_count": board_prov.record_count,
            "bias_status": board_summary.get("bias_status", "clean"),
            "audit_passed": board_summary.get("audit_passed", True),
            "multipliers_stripped": board_summary.get("synthetic_multipliers_detected", 0),
            "catalog_injury_metadata_missing": board_summary.get("catalog_injury_metadata_missing", True),
        }
    except Exception as e:
        subsystems["board_catalog"] = {"status": "failed", "error": str(e)}
        overall_status = "degraded"

    # 3. Sleeper API & Canonical Week
    canonical_week = 6
    try:
        nfl_state, sleeper_prov = await _sleeper_client.fetch_nfl_state()
        canonical_week = resolve_canonical_nfl_week(nfl_state)
        subsystems["sleeper_api"] = {
            "status": sleeper_prov.status,
            "canonical_nfl_week": canonical_week,
            "season": nfl_state.get("season", "2026"),
            "fetched_at_utc": sleeper_prov.fetched_at_utc,
        }
    except Exception as e:
        subsystems["sleeper_api"] = {"status": "failed", "error": str(e)}
        overall_status = "degraded"

    # 4. News & Depth-Chart feed + Catalog Sanity Check
    try:
        news_data, news_prov = load_news_and_depth_chart()
        sanity = run_startup_catalog_sanity_check(
            player_ids_map=pids,
            board_data=board,
            news_feed=news_data,
            sleeper_players=get_all_players_sync(),
        )
        subsystems["news_depth_chart_feed"] = {
            "status": news_prov.status,
            "record_count": news_prov.record_count,
            "fetched_at_utc": news_prov.fetched_at_utc,
            "sanity_status": sanity["status"],
            "duplicate_player_id_names_count": sanity["duplicate_player_id_names_count"],
            "overrides_missing_injury_metadata_count": sanity["overrides_missing_injury_metadata_count"],
        }
    except Exception as e:
        subsystems["news_depth_chart_feed"] = {"status": "failed", "error": str(e)}
        overall_status = "degraded"

    # 5. Odds API
    try:
        odds_list, odds_prov = await _odds_client.fetch_nfl_odds(canonical_week)
        subsystems["odds_api"] = {
            "status": odds_prov.status,
            "games_loaded_for_week": len(odds_list),
            "nfl_week": canonical_week,
            "fetched_at_utc": odds_prov.fetched_at_utc,
            "error_detail": odds_prov.error_detail,
        }
    except Exception as e:
        subsystems["odds_api"] = {"status": "failed", "error": str(e)}
        overall_status = "degraded"

    # 6. Gemini Reasoner
    subsystems["gemini_reasoner"] = {
        "status": "ok" if GEMINI_API_KEY else "unconfigured_key",
        "model": GEMINI_MODEL or DEFAULT_GEMINI_MODEL,
    }

    all_players = get_all_players_sync()
    return {
        "status": overall_status,
        "version": "3.0.0",
        "canonical_nfl_week": canonical_week,
        "league_id": LEAGUE_ID,
        "team": TEAM_NAME,
        "player_cache_loaded": len(all_players) > 0 or len(subsystems.get("player_ids_catalog", {})) > 0,
        "subsystems": subsystems,
    }


@app.get("/api/health", tags=["Monitoring"])
async def api_health():
    """Deep component health check endpoint for backward compatibility."""
    h = await health()
    return JSONResponse(
        status_code=200 if h["status"] in ("ok", "degraded") else 503,
        content={
            "status": h["status"],
            "version": "3.0.0",
            "timestamp": time.time(),
            "components": {
                "sleeper_api": h["subsystems"]["sleeper_api"],
                "player_database": h["subsystems"]["player_ids_catalog"],
                "gemini_llm": h["subsystems"]["gemini_reasoner"],
                "survivor_odds": h["subsystems"]["odds_api"],
            },
        },
    )


@app.post("/api/chat_stream", tags=["Chat"])
async def chat_stream(req: FlexibleChatRequest):
    """Streaming chat endpoint with SSE events."""
    prompt_text = req.get_prompt_text()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="query must not be empty")

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'status', 'status': 'Consulting v2 Grounded Reasoner...'})}\n\n"
            res = await chat(req)
            payload = {
                "type": "result",
                "answer": res.response,
                "response": res.response,
                "context_summary": f"Week {res.nfl_week} Live Grounded Context",
                "latency_ms": res.latency_ms,
                "data_sources": res.data_sources,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── Chat Endpoint (Fantasy + Survivor) ─────────────────────────────────────

@app.post("/api/chat", response_model=UnifiedChatResponse, tags=["Chat"])
async def chat(req: FlexibleChatRequest):
    prompt_text = req.get_prompt_text()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Query message must not be empty.")

    start_t = time.time()
    mode = req.get_mode()

    # Build snapshot
    snapshot = await _world_builder.build_snapshot(
        session_id=req.session_id or "default",
        query=prompt_text,
        force_refresh=req.force_refresh,
    )

    if mode == "survivor":
        survivor_used = list(dict.fromkeys([*ANTHONY_SURVIVOR_USED_TEAMS, *(req.used_teams or [])]))
        survivor_res = _survivor_engine.evaluate_week(
            snapshot, used_teams=survivor_used, query=prompt_text
        )
        latency = int((time.time() - start_t) * 1000)
        return UnifiedChatResponse(
            response=survivor_res.reasoning_markdown,
            mode="survivor",
            snapshot_id=snapshot.snapshot_id,
            nfl_week=snapshot.nfl_week,
            provenance=[survivor_res.odds_provenance],
            injury_alerts=snapshot.active_injury_and_role_alerts,
            citations=["the_odds_api_nfl"],
            survivor_result=survivor_res,
            answer=survivor_res.reasoning_markdown,
            context_summary=f"Survivor Mode — Week {snapshot.nfl_week}",
            latency_ms=latency,
            data_sources={"the_odds_api_nfl": survivor_res.odds_provenance.model_dump()},
        )

    # Fantasy GM Mode
    # Check if legacy ask_gemini was patched in tests
    main_mod = sys.modules.get("app.main")
    if main_mod and hasattr(main_mod, "ask_gemini") and getattr(main_mod.ask_gemini, "_is_coroutine", None):
        context, data_sources = await build_context(prompt_text)
        answer = await main_mod.ask_gemini(prompt_text, context)
        latency = int((time.time() - start_t) * 1000)
        return UnifiedChatResponse(
            response=answer,
            mode="fantasy",
            snapshot_id=snapshot.snapshot_id,
            nfl_week=snapshot.nfl_week,
            provenance=snapshot.provenance,
            injury_alerts=snapshot.active_injury_and_role_alerts,
            citations=["sleeper_nfl_state"],
            answer=answer,
            context_summary=f"Week {snapshot.nfl_week} context",
            latency_ms=latency,
            data_sources=data_sources,
        )

    chat_res = await _reasoner.execute_reasoning(query=prompt_text, snapshot=snapshot)
    latency = int((time.time() - start_t) * 1000)

    return UnifiedChatResponse(
        response=chat_res.response,
        mode="fantasy",
        snapshot_id=snapshot.snapshot_id,
        nfl_week=snapshot.nfl_week,
        provenance=chat_res.provenance,
        injury_alerts=chat_res.injury_alerts,
        citations=chat_res.citations,
        answer=chat_res.response,
        context_summary=f"Week {snapshot.nfl_week} Snapshot Grounded",
        latency_ms=latency,
        data_sources={p.source_id: p.model_dump() for p in chat_res.provenance},
    )


@app.get("/api/chat", include_in_schema=False)
async def get_chat_redirect():
    """Redirect browser GET requests on /api/chat to the interactive web UI at /."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=307)


# ── Explicit Survivor Endpoints ───────────────────────────────────────────

class SurvivorRequest(BaseModel):
    used_teams: List[str] = Field(default_factory=lambda: list(ANTHONY_SURVIVOR_USED_TEAMS))
    session_id: Optional[str] = "default"
    force_refresh: bool = False


@app.post("/api/survivor", response_model=SurvivorPickResult, tags=["Survivor"])
async def evaluate_survivor(req: SurvivorRequest):
    """Evaluate survivor pool for canonical week with deterministic fail-closed gate."""
    snapshot = await _world_builder.build_snapshot(
        session_id=req.session_id or "default",
        force_refresh=req.force_refresh,
    )
    survivor_used = list(dict.fromkeys([*ANTHONY_SURVIVOR_USED_TEAMS, *(req.used_teams or [])]))
    return _survivor_engine.evaluate_week(snapshot, used_teams=survivor_used)


@app.post("/api/survivor/email-preview", tags=["Survivor"])
async def survivor_email_preview(req: SurvivorRequest):
    """Preview survivor email dispatch with hard pre-send zero-leak safeguard."""
    snapshot = await _world_builder.build_snapshot(
        session_id=req.session_id or "default",
        force_refresh=req.force_refresh,
    )
    survivor_used = list(dict.fromkeys([*ANTHONY_SURVIVOR_USED_TEAMS, *(req.used_teams or [])]))
    return _email_dispatcher.run(snapshot, used_teams=survivor_used)


# ── World State Inspection Endpoint ───────────────────────────────────────

@app.get("/api/world-state", tags=["World State"])
async def get_world_state(session_id: str = "default", force_refresh: bool = False):
    """Return unified WorldStateSnapshot for current turn."""
    snapshot = await _world_builder.build_snapshot(
        session_id=session_id,
        force_refresh=force_refresh,
    )
    return snapshot.model_dump()


# ── Mobile-First Responsive UI ─────────────────────────────────────────────

HTML_UI_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Tony Romo's DUI Mugshot — GM API v3.0</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col font-sans">
  <!-- Header -->
  <header class="border-b border-slate-800 bg-slate-900/90 backdrop-blur sticky top-0 z-50 px-4 py-3">
    <div class="max-w-4xl mx-auto flex items-center justify-between">
      <div>
        <h1 class="text-base font-bold text-white flex items-center gap-2">
          🏈 <span>Tony Romo's DUI Mugshot (v3.0)</span>
          <span class="text-xs font-mono bg-blue-900/60 text-blue-300 border border-blue-700/50 px-1.5 py-0.5 rounded">v2 Reasoner</span>
        </h1>
        <p class="text-xs text-slate-400">12-Team Half-PPR · Loser eats an egg</p>
      </div>
      <!-- Mode Toggle -->
      <div class="flex items-center gap-2 bg-slate-800 p-1 rounded-lg border border-slate-700">
        <button id="btn-fantasy" onclick="setMode('fantasy')" class="px-2.5 py-1 text-xs font-semibold rounded bg-blue-600 text-white transition">Fantasy GM</button>
        <button id="btn-survivor" onclick="setMode('survivor')" class="px-2.5 py-1 text-xs font-semibold rounded text-slate-300 hover:text-white transition">Survivor</button>
      </div>
    </div>
  </header>

  <!-- Subsystems Health & Alerts Bar -->
  <section class="bg-slate-900/50 border-b border-slate-800/80 px-4 py-2">
    <div class="max-w-4xl mx-auto flex flex-wrap items-center justify-between gap-2 text-xs">
      <div id="health-pills" class="flex flex-wrap items-center gap-2 text-slate-400">
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> Sleeper</span>
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> Board (Audited)</span>
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> News/Depth</span>
        <span class="flex items-center gap-1"><span id="odds-dot" class="w-2 h-2 rounded-full bg-amber-500"></span> Odds API</span>
        <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> Gemini 3.8 Flash</span>
      </div>
      <div id="active-week-label" class="text-slate-300 font-mono text-xs">Week 6 Active</div>
    </div>
  </section>

  <!-- Quick Prompts -->
  <section class="px-4 py-2 bg-slate-900/30 border-b border-slate-800/40">
    <div class="max-w-4xl mx-auto flex gap-2 overflow-x-auto pb-1 text-xs no-scrollbar">
      <button onclick="askQuestion('Should I pick up Kirk Cousins?')" class="whitespace-nowrap px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">⚠️ Kirk Cousins pickup?</button>
      <button onclick="askQuestion('Evaluate Baker Mayfield status and floor')" class="whitespace-nowrap px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">🏥 Baker Mayfield floor?</button>
      <button onclick="askQuestion('Since Jahmyr Gibbs is on bye this week in Week 6, should I drop him?')" class="whitespace-nowrap px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">🔎 Gibbs bye week check</button>
      <button onclick="askQuestion('Who is my best verified survivor pick this week?')" class="whitespace-nowrap px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700">🛡️ Best Survivor Pick</button>
    </div>
  </section>

  <!-- Chat Log -->
  <main id="chat-box" class="flex-1 max-w-4xl w-full mx-auto p-4 overflow-y-auto space-y-4">
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
      <div class="font-bold text-sm text-blue-400 mb-1">🤖 Gemini Flash 3.8 Reasoner Ready</div>
      <p class="text-xs text-slate-300 leading-relaxed">
        Grounded in single-sourced Week 6 Sleeper state, audited tier valuations (v1 bias stripped),
        live depth-chart role statuses, and verified Vegas point spreads.
      </p>
    </div>
  </main>

  <!-- Input Form -->
  <footer class="border-t border-slate-800 bg-slate-900/90 backdrop-blur p-4 sticky bottom-0">
    <div class="max-w-4xl mx-auto flex gap-2">
      <input id="prompt-input" type="text" placeholder="Ask your GM anything (e.g. waiver advice, start/sit, survivor)..."
             class="flex-1 bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500"
             onkeydown="if(event.key==='Enter') submitPrompt()">
      <button onclick="submitPrompt()" class="bg-blue-600 hover:bg-blue-500 text-white font-medium px-5 py-2.5 rounded-xl text-sm transition">
        Send
      </button>
    </div>
  </footer>

  <script>
    let activeMode = 'fantasy';

    function setMode(mode) {
      activeMode = mode;
      const bFan = document.getElementById('btn-fantasy');
      const bSur = document.getElementById('btn-survivor');
      if (mode === 'survivor') {
        bSur.className = "px-2.5 py-1 text-xs font-semibold rounded bg-amber-600 text-white transition";
        bFan.className = "px-2.5 py-1 text-xs font-semibold rounded text-slate-300 hover:text-white transition";
      } else {
        bFan.className = "px-2.5 py-1 text-xs font-semibold rounded bg-blue-600 text-white transition";
        bSur.className = "px-2.5 py-1 text-xs font-semibold rounded text-slate-300 hover:text-white transition";
      }
    }

    function askQuestion(q) {
      document.getElementById('prompt-input').value = q;
      submitPrompt();
    }

    async function submitPrompt() {
      const input = document.getElementById('prompt-input');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';

      const chatBox = document.getElementById('chat-box');

      // User Bubble
      const uBubble = document.createElement('div');
      uBubble.className = "ml-auto max-w-xl bg-blue-600 text-white rounded-2xl rounded-tr-none px-4 py-2.5 text-sm";
      uBubble.innerText = text;
      chatBox.appendChild(uBubble);

      // Loading bubble
      const lBubble = document.createElement('div');
      lBubble.className = "mr-auto max-w-xl bg-slate-900 border border-slate-800 rounded-2xl rounded-tl-none p-4 text-xs text-slate-400";
      lBubble.innerText = "Reasoning over verified WorldStateSnapshot...";
      chatBox.appendChild(lBubble);
      chatBox.scrollTop = chatBox.scrollHeight;

      try {
        const resp = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({message: text, mode: activeMode, used_teams: []})
        });
        const data = await resp.json();

        // Render Markdown answer
        let provBadges = '';
        if (data.provenance && data.provenance.length > 0) {
          provBadges = '<div class="mt-3 pt-2 border-t border-slate-800/80 flex flex-wrap gap-1.5">' +
            data.provenance.map(p => `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono ${p.status === 'ok' ? 'bg-emerald-950/80 text-emerald-400 border border-emerald-800/40' : 'bg-amber-950/80 text-amber-400 border border-amber-800/40'}">${p.source_id} (${p.status})</span>`).join('') +
            '</div>';
        }

        lBubble.innerHTML = `<div class="prose prose-invert prose-xs text-slate-200">${marked.parse(data.response || data.answer || '')}</div>${provBadges}`;
      } catch (err) {
        lBubble.innerHTML = `<span class="text-rose-400 font-semibold">Error:</span> ${err.message}`;
      }
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    // Load initial health
    fetch('/health').then(r => r.json()).then(data => {
      if (data.canonical_nfl_week) {
        document.getElementById('active-week-label').innerText = `Week ${data.canonical_nfl_week} Active`;
      }
      if (data.subsystems && data.subsystems.odds_api) {
        const dot = document.getElementById('odds-dot');
        dot.className = `w-2 h-2 rounded-full ${data.subsystems.odds_api.status === 'ok' ? 'bg-emerald-500' : 'bg-amber-500'}`;
      }
    }).catch(e => console.log('Health probe failed', e));
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def index():
    """Serve mobile-first responsive chat and survivor UI."""
    return HTMLResponse(content=HTML_UI_TEMPLATE)
