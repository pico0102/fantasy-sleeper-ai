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
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google import genai
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Env + logging
# ---------------------------------------------------------------------------
load_dotenv()

GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
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

PLAYER_IDS: dict[str, Any] = _load_json("player_ids.json")   # id → name (str) OR {first_name,last_name,...}
BOARD:      list[dict]      = _load_json("board.json")        # preseason VORP board
BLENDED:    list[dict]      = _load_json("blended.json")      # blended projections

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


def resolve_player_name(pid: str) -> str:
    """Return a human-readable player label for a Sleeper player ID.

    player_ids.json stores values as plain name strings ("Chris Rodriguez").
    Falls back to the live single-player endpoint for IDs not in the file.
    """
    if pid in PLAYER_IDS:
        entry = PLAYER_IDS[pid]
        if isinstance(entry, str):
            return entry  # flat format: just the name
        # nested dict format (future-proof)
        return (
            f"{entry.get('first_name', '')} {entry.get('last_name', '')} "
            f"({entry.get('team', '?')}, {entry.get('position', '?')})"
        ).strip()
    try:
        p = _get(f"{SLEEPER_BASE}/players/nfl/{pid}")
        resolved_name = f"{p.get('first_name', '')} {p.get('last_name', '')} ({p.get('team', '?')}, {p.get('position', '?')})".strip()
        if not resolved_name or resolved_name == "()":
            resolved_name = pid # fallback if Sleeper returns empty object for defenses sometimes
        PLAYER_IDS[pid] = resolved_name
        return resolved_name
    except Exception:
        fallback = f"Unknown({pid})"
        PLAYER_IDS[pid] = fallback
        return fallback


def fetch_nfl_state() -> dict:
    return _get(f"{SLEEPER_BASE}/state/nfl?{_cb()}")


def fetch_all_rosters() -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/rosters?{_cb()}")


def fetch_matchups(week: int) -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/matchups/{week}?{_cb()}")


def fetch_trending_adds(hours: int = 24, limit: int = 25) -> list[dict]:
    return _get(
        f"{SLEEPER_BASE}/players/nfl/trending/add"
        f"?lookback_hours={hours}&limit={limit}&{_cb()}"
    )


def fetch_transactions(week: int) -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/transactions/{week}?{_cb()}")


def fetch_users() -> list[dict]:
    return _get(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/users?{_cb()}")

# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def build_context(question: str) -> str:
    """Fetch fresh Sleeper data and assemble a context block for the AI."""
    ctx_parts: list[str] = []

    # 1. NFL state / current week
    try:
        state = fetch_nfl_state()
        week  = state.get("week", "?")
        season = state.get("season", "2026")
        ctx_parts.append(
            f"## NFL State\nSeason: {season}  |  Week: {week}  |  "
            f"Season type: {state.get('season_type', '?')}"
        )
    except Exception as e:
        week = 1
        ctx_parts.append(f"## NFL State\nUnavailable ({e})")

    # 2. All rosters → isolate Anthony's roster
    my_roster_players: list[str] = []
    my_starters:       list[str] = []
    try:
        rosters = fetch_all_rosters()
        roster_map: dict[int, list[str]] = {}
        for r in rosters:
            rid   = r.get("roster_id")
            plist = r.get("players") or []
            roster_map[rid] = plist
            if rid == MY_ROSTER_ID:
                my_roster_players = plist
                my_starters       = r.get("starters") or []

        my_player_names  = [resolve_player_name(pid) for pid in my_roster_players]
        my_starter_names = [resolve_player_name(pid) for pid in my_starters]

        # Build standings
        standings = []
        for r in rosters:
            rid = r.get("roster_id")
            s = r.get("settings", {})
            wins = s.get("wins", 0)
            losses = s.get("losses", 0)
            pts = s.get("fpts", 0) + (s.get("fpts_decimal", 0) / 100.0)
            standings.append((wins, pts, rid))
        standings.sort(key=lambda x: (x[0], x[1]), reverse=True)
        
        standing_lines = []
        for i, (w, pts, rid) in enumerate(standings, 1):
            tag = " (ANTHONY)" if rid == MY_ROSTER_ID else ""
            standing_lines.append(f"{i}. Roster {rid}{tag}: {w} W - {losses} L, {pts:.1f} pts")

        ctx_parts.append(
            f"## Anthony's Roster (roster_id {MY_ROSTER_ID})\n"
            f"**Starters:** {', '.join(my_starter_names) or 'none set'}\n"
            f"**Full roster:** {', '.join(my_player_names)}\n\n"
            f"## League Standings\n" + "\n".join(standing_lines)
        )
    except Exception as e:
        ctx_parts.append(f"## Anthony's Roster\nUnavailable ({e})")

    # 3. Current week matchup
    try:
        matchups = fetch_matchups(week)
        my_matchup_id = None
        opponent_rid  = None
        my_points     = None
        opp_points    = None
        for m in matchups:
            if m.get("roster_id") == MY_ROSTER_ID:
                my_matchup_id = m.get("matchup_id")
                my_points     = m.get("points", 0)
                break
        if my_matchup_id:
            for m in matchups:
                if m.get("matchup_id") == my_matchup_id and m.get("roster_id") != MY_ROSTER_ID:
                    opponent_rid = m.get("roster_id")
                    opp_points   = m.get("points", 0)
                    break
        opp_players = [resolve_player_name(p) for p in (roster_map.get(opponent_rid) or [])]
        ctx_parts.append(
            f"## Week {week} Matchup\n"
            f"My score: {my_points}  |  Opponent (roster {opponent_rid}): {opp_points}\n"
            f"Opponent roster: {', '.join(opp_players) or 'unknown'}"
        )
    except Exception as e:
        ctx_parts.append(f"## Week {week} Matchup\nUnavailable ({e})")

    # 4. Trending adds (waiver wire intelligence)
    try:
        trending = fetch_trending_adds()
        owned_set = set(my_roster_players)
        # Build a list of what's in this league
        all_rostered: set[str] = set()
        for plist in roster_map.values():
            all_rostered.update(plist)
        # Best targets: national trend AND unrostered in this league
        gems = [
            t for t in trending
            if t.get("player_id") not in all_rostered
        ][:8]
        gem_lines = [
            f"  - {resolve_player_name(t['player_id'])} (+{t.get('count', '?')} adds)"
            for t in gems
        ]
        ctx_parts.append(
            f"## Trending Waiver Adds (national, unrostered in league)\n"
            + ("\n".join(gem_lines) or "  None found")
        )
    except Exception as e:
        ctx_parts.append(f"## Trending Adds\nUnavailable ({e})")

    # 4.5 Recent League Transactions
    try:
        if str(week).isdigit():
            txs = fetch_transactions(int(week))
            tx_lines = []
            for tx in txs[:10]: # Up to 10 most recent transactions
                ttype = tx.get("type", "unknown")
                if ttype in ("waiver", "free_agent", "trade"):
                    adds = tx.get("adds") or {}
                    drops = tx.get("drops") or {}
                    add_strs = [f"Added {resolve_player_name(p)}" for p in adds.keys()]
                    drop_strs = [f"Dropped {resolve_player_name(p)}" for p in drops.keys()]
                    status = tx.get("status", "complete")
                    tx_lines.append(f"- {ttype.upper()} ({status}): {', '.join(add_strs + drop_strs)}")
            
            ctx_parts.append(
                f"## Recent League Transactions (Week {week})\n"
                + ("\n".join(tx_lines) if tx_lines else "  No recent transactions.")
            )
    except Exception as e:
        ctx_parts.append(f"## Recent Transactions\nUnavailable ({e})")

    # 5. Preseason projection summary for Anthony's roster (flag if week ≥ 4)
    try:
        if BOARD:
            # board.json schema: {name, team, pos, pts, posrank, vorp}
            board_map = {p.get("name"): p for p in BOARD if "name" in p}
            proj_lines: list[str] = []
            for pid in my_roster_players:
                pname = resolve_player_name(pid)
                # Match by first token of name (e.g. "Christian McCaffrey")
                bp = board_map.get(pname)
                if bp:
                    proj_lines.append(
                        f"  - {pname}: {bp.get('pts', '?')} pts "
                        f"({bp.get('posrank', '?')})"
                    )
            stale_note = (
                "  ⚠️ Preseason projections — weight recent usage over these after week 4.\n"
                if (isinstance(week, int) and week >= 4) else ""
            )
            ctx_parts.append(
                f"## Preseason Projections (board.json)\n{stale_note}"
                + ("\n".join(proj_lines) or "  No board data for rostered players")
            )
    except Exception as e:
        ctx_parts.append(f"## Projections\nUnavailable ({e})")

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
    You are an expert fantasy football analyst and strategist for a 12-team, half-PPR Sleeper
    league called "Loser eats an egg."

    You manage Anthony's team, **Tony Romo's DUI Mugshot** (roster_id 4, username pinec0ne).

    ### League scoring (exact — do not assume)
    - Passing: 0.04/yd · 4 per TD · -1 per INT · 2 per 2PC
    - Rushing: 0.1/yd · 6 per TD · 2 per 2PC
    - Receiving: **0.5 per reception** (no TE premium) · 0.1/yd · 6 per TD · 2 per 2PC
    - Kicker: FG 0-39=3, 40-49=4, 50-59=5, 60+=6 (distance-tiered)
    - No yardage bonuses, no first-down scoring

    ### Lineup slots
    1 QB · 2 RB · 2 WR · 1 TE · 2 FLEX (W/R/T) · 1 K · 1 DEF | 5 bench + 1 IR

    ### Standing context
    Anthony's structural edge is **RB surplus** (drafted 5 RBs in first 4 rounds).
    His structural weakness is **WR-in-lineup** (RBs dominate both FLEX slots).
    Always use the **lineup-allocation method** for positional rank — never raw depth stats.
    Priority for waiver adds: receiver upside first.

    ### Your job (Full GM Mode)
    - Act as a comprehensive General Manager for Anthony's team.
    - Proactively analyze the provided League Standings and Recent Transactions to identify shifting power dynamics.
    - Suggest trades by looking at other rosters that might be weak at RB but strong at WR (or vice versa), using the standings and recent transaction trends.
    - Lead with the recommendation, then the reasoning.
    - Show numbers behind every claim (projected points, snap %, target share when available).
    - Flag uncertainty clearly — say when you're guessing.
    - Keep answers mobile-friendly: scannable, short bullet points, narrow tables.
    - Evaluate trades by change in optimal starting lineup, not raw player value.
    - After week 4, weight recent usage/snap counts over preseason projections (and say so).
    - Never recommend a second QB or TE unless the starter is out.
""")


def ask_gemini(question: str, context: str) -> str:
    client = get_gemini_client()
    prompt = (
        f"Please use the following live league data as context to answer the user's question:\n\n"
        f"<league_data>\n"
        f"{context}\n"
        f"</league_data>\n\n"
        f"Answer this question as the General Manager:\n"
        f"<user_question>\n"
        f"{question}\n"
        f"</user_question>"
    )
    models_to_try = [
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
    ]

    last_error = None
    for model_name in models_to_try:
        for attempt in range(2):  # Try each model twice
            try:
                # Use the new Interactions API (SDK >= 2.0.0)
                interaction = client.interactions.create(
                    model=model_name,
                    input=prompt,
                    system_instruction=SYSTEM_PROMPT,
                    store=False,  # We don't need persistent multi-turn storage for this simple bot
                )
                
                # output_text is a convenience property on the Interaction object
                return interaction.output_text or "(Empty response from model)"
            except Exception as e:
                last_error = e
                log.warning(f"Attempt {attempt + 1} with {model_name} failed: {e}")
                time.sleep(1.5)  # Backoff before retrying

    raise HTTPException(
        status_code=502, 
        detail=f"All Gemini models are currently overloaded. Please wait a minute and try again. Last error: {last_error}"
    )

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Fantasy Football AI — Tony Romo's DUI Mugshot",
    description="AI-powered assistant for the 'Loser eats an egg' Sleeper league",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str


class ChatResponse(BaseModel):
    answer: str
    context_summary: str

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Meta"])
async def health():
    """Liveness check."""
    return {"status": "ok", "league_id": LEAGUE_ID, "team": "Tony Romo's DUI Mugshot"}


@app.post("/api/chat", response_model=ChatResponse, tags=["Chat"])
async def chat(req: ChatRequest):
    """
    Accept a fantasy-football question, pull fresh Sleeper data,
    and return a Gemini-powered strategic answer.
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    log.info("Chat request: %s", req.query[:120])

    context = build_context(req.query)
    answer  = ask_gemini(req.query, context)

    # Light context summary for the UI (first 300 chars)
    summary = context[:300].replace("\n", " ") + "…"

    return ChatResponse(answer=answer, context_summary=summary)


@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def index():
    """Mobile-first chat UI."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>🏈 Fantasy Assistant</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            brand: { 50:'#fff7ed', 500:'#f97316', 600:'#ea580c', 700:'#c2410c' }
          }
        }
      }
    }
  </script>
  <!-- marked.js for markdown rendering -->
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    /* Custom scrollbar */
    #chat-log::-webkit-scrollbar { width: 4px; }
    #chat-log::-webkit-scrollbar-track { background: transparent; }
    #chat-log::-webkit-scrollbar-thumb { background: #f97316; border-radius: 9999px; }

    /* Prose for AI markdown */
    .prose-answer h1,.prose-answer h2,.prose-answer h3 { font-weight:700; margin:.75em 0 .25em; }
    .prose-answer h2 { font-size:1.05rem; color:#c2410c; }
    .prose-answer h3 { font-size:.95rem; color:#ea580c; }
    .prose-answer p  { margin:.35em 0; }
    .prose-answer ul,.prose-answer ol { padding-left:1.25em; margin:.35em 0; }
    .prose-answer li { margin:.15em 0; }
    .prose-answer table { border-collapse:collapse; width:100%; font-size:.82rem; margin:.5em 0; }
    .prose-answer th  { background:#fff7ed; color:#c2410c; padding:4px 8px; text-align:left; border-bottom:2px solid #f97316; }
    .prose-answer td  { padding:3px 8px; border-bottom:1px solid #fed7aa; }
    .prose-answer code { background:#fff7ed; color:#c2410c; padding:1px 4px; border-radius:4px; font-size:.85em; }
    .prose-answer pre code { background:transparent; padding:0; }
    .prose-answer pre  { background:#1e293b; color:#e2e8f0; padding:.75em 1em; border-radius:8px; overflow-x:auto; font-size:.82em; margin:.5em 0; }
    .prose-answer strong { color:#1e293b; }
    .prose-answer blockquote { border-left:3px solid #f97316; padding-left:.75em; color:#57534e; font-style:italic; }

    .dot-flashing { display:inline-flex; gap:4px; }
    .dot-flashing span { width:7px; height:7px; border-radius:50%; background:#f97316; animation:dotFlash 1.2s infinite; }
    .dot-flashing span:nth-child(2) { animation-delay:.2s; }
    .dot-flashing span:nth-child(3) { animation-delay:.4s; }
    @keyframes dotFlash { 0%,80%,100%{opacity:.2} 40%{opacity:1} }
  </style>
</head>
<body class="bg-gray-50 min-h-screen flex flex-col font-sans">

  <!-- Header -->
  <header class="bg-brand-600 text-white px-4 py-3 shadow-md flex items-center gap-3 sticky top-0 z-10">
    <span class="text-2xl">🏈</span>
    <div>
      <p class="font-bold text-sm leading-tight">Tony Romo's DUI Mugshot</p>
      <p class="text-xs text-orange-200 leading-tight">Loser eats an egg · half-PPR</p>
    </div>
    <span id="status-dot" class="ml-auto w-2.5 h-2.5 rounded-full bg-green-300 shadow shadow-green-300/50" title="API online"></span>
  </header>

  <!-- Chat log -->
  <main id="chat-log" class="flex-1 overflow-y-auto px-3 py-4 space-y-3 max-w-2xl w-full mx-auto">
    <!-- Welcome bubble -->
    <div class="flex gap-2 items-start">
      <span class="text-xl flex-shrink-0 mt-0.5">🤖</span>
      <div class="bg-white border border-orange-100 rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm max-w-prose">
        <p class="text-sm text-gray-700">Hey Anthony 👋 — I'm watching your roster live. Ask me anything or tap a quick button below.</p>
      </div>
    </div>
  </main>

  <!-- Quick-action buttons -->
  <section class="px-3 pb-2 max-w-2xl w-full mx-auto">
    <div id="quick-btns" class="flex gap-2 flex-wrap">
      <button onclick="sendQuick('Analyze my lineup for this week and tell me who to start.')"
        class="quick-btn bg-brand-50 border border-brand-500 text-brand-700 text-xs font-semibold px-3 py-1.5 rounded-full hover:bg-brand-500 hover:text-white transition-colors">
        📋 Analyze Lineup
      </button>
      <button onclick="sendQuick('What are the best waiver wire pickups available for my team right now?')"
        class="quick-btn bg-brand-50 border border-brand-500 text-brand-700 text-xs font-semibold px-3 py-1.5 rounded-full hover:bg-brand-500 hover:text-white transition-colors">
        📡 Waiver Targets
      </button>
      <button onclick="sendQuick('Give me a start/sit decision breakdown for my flex spots and any close calls this week.')"
        class="quick-btn bg-brand-50 border border-brand-500 text-brand-700 text-xs font-semibold px-3 py-1.5 rounded-full hover:bg-brand-500 hover:text-white transition-colors">
        ⚖️ Start / Sit
      </button>
      <button onclick="sendQuick('Give me a matchup preview for this week — who are the key players and what does this week turn on?')"
        class="quick-btn bg-brand-50 border border-brand-500 text-brand-700 text-xs font-semibold px-3 py-1.5 rounded-full hover:bg-brand-500 hover:text-white transition-colors">
        🔍 Matchup Preview
      </button>
    </div>
  </section>

  <!-- Input bar -->
  <footer class="bg-white border-t border-gray-200 px-3 py-3 max-w-2xl w-full mx-auto sticky bottom-0">
    <form id="chat-form" class="flex gap-2 items-end">
      <textarea
        id="user-input"
        rows="1"
        placeholder="Ask about your roster, waivers, trades…"
        class="flex-1 resize-none rounded-2xl border border-gray-300 focus:border-brand-500 focus:ring-2 focus:ring-brand-500/30 px-4 py-2.5 text-sm outline-none leading-snug max-h-32 overflow-y-auto"
        onkeydown="handleKey(event)"
      ></textarea>
      <button
        type="submit"
        id="send-btn"
        class="flex-shrink-0 bg-brand-600 hover:bg-brand-700 active:scale-95 text-white rounded-2xl p-2.5 transition-all shadow"
        aria-label="Send">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="w-5 h-5">
          <path d="M3.478 2.404a.75.75 0 0 0-.926.941l2.432 7.905H13.5a.75.75 0 0 1 0 1.5H4.984l-2.432 7.905a.75.75 0 0 0 .926.94 60.519 60.519 0 0 0 18.445-8.986.75.75 0 0 0 0-1.218A60.517 60.517 0 0 0 3.478 2.404Z" />
        </svg>
      </button>
    </form>
  </footer>

<script>
  const chatLog   = document.getElementById('chat-log');
  const userInput = document.getElementById('user-input');
  const sendBtn   = document.getElementById('send-btn');
  const chatForm  = document.getElementById('chat-form');

  // Auto-grow textarea
  userInput.addEventListener('input', () => {
    userInput.style.height = 'auto';
    userInput.style.height = Math.min(userInput.scrollHeight, 128) + 'px';
  });

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitQuery(); }
  }

  chatForm.addEventListener('submit', e => { e.preventDefault(); submitQuery(); });

  function sendQuick(text) { userInput.value = text; submitQuery(); }

  function scrollBottom() {
    chatLog.scrollTo({ top: chatLog.scrollHeight, behavior: 'smooth' });
  }

  function appendBubble(role, html, isLoading = false) {
    const wrap = document.createElement('div');
    wrap.className = 'flex gap-2 items-start';

    if (role === 'user') {
      wrap.innerHTML = `
        <div class="ml-auto bg-brand-600 text-white rounded-2xl rounded-tr-sm px-4 py-2.5 shadow-sm max-w-prose text-sm">
          ${escapeHtml(html)}
        </div>
        <span class="text-xl flex-shrink-0 mt-0.5">👤</span>`;
    } else {
      const inner = isLoading
        ? `<div class="dot-flashing"><span></span><span></span><span></span></div>`
        : `<div class="prose-answer text-sm text-gray-800">${html}</div>`;
      wrap.innerHTML = `
        <span class="text-xl flex-shrink-0 mt-0.5">🤖</span>
        <div id="${isLoading ? 'loading-bubble' : ''}" class="bg-white border border-orange-100 rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm max-w-prose min-w-[60px]">
          ${inner}
        </div>`;
    }
    chatLog.appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function escapeHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  async function submitQuery() {
    const q = userInput.value.trim();
    if (!q) return;

    // Reset input
    userInput.value = '';
    userInput.style.height = 'auto';

    // Disable while loading
    sendBtn.disabled = true;
    sendBtn.classList.add('opacity-50');

    // Show user message
    appendBubble('user', q);

    // Show loading indicator
    appendBubble('bot', '', true);

    try {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q }),
      });

      // Remove loading bubble
      const loadingBubble = document.getElementById('loading-bubble');
      if (loadingBubble) loadingBubble.closest('.flex').remove();

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: 'Server error' }));
        appendBubble('bot', `<p class="text-red-600 text-sm">⚠️ ${escapeHtml(err.detail || 'Unknown error')}</p>`);
      } else {
        const data = await resp.json();
        const renderedMd = marked.parse(data.answer || '_(empty response)_');
        appendBubble('bot', renderedMd);
      }
    } catch (err) {
      const loadingBubble = document.getElementById('loading-bubble');
      if (loadingBubble) loadingBubble.closest('.flex').remove();
      appendBubble('bot', `<p class="text-red-600 text-sm">⚠️ Network error: ${escapeHtml(String(err))}</p>`);
    } finally {
      sendBtn.disabled = false;
      sendBtn.classList.remove('opacity-50');
      userInput.focus();
      scrollBottom();
    }
  }

  // Check health on load
  fetch('/health').then(r => {
    const dot = document.getElementById('status-dot');
    dot.className = r.ok
      ? 'ml-auto w-2.5 h-2.5 rounded-full bg-green-300 shadow shadow-green-300/50'
      : 'ml-auto w-2.5 h-2.5 rounded-full bg-red-400 shadow shadow-red-400/50';
    dot.title = r.ok ? 'API online' : 'API offline';
  }).catch(() => {
    const dot = document.getElementById('status-dot');
    dot.className = 'ml-auto w-2.5 h-2.5 rounded-full bg-red-400 shadow shadow-red-400/50';
    dot.title = 'API unreachable';
  });
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
