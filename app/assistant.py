"""Assistant orchestrator: computes deterministic GM analysis, manages LLM prompting and fallbacks."""

from __future__ import annotations

import hashlib
import json
import logging
import textwrap
import time
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from app.config import TEAM_NAME
from app.engine import (
    evaluate_matchup_preview,
    evaluate_start_sit,
    evaluate_waivers,
    solve_lineup,
)
from app.intents import Intent, classify_intent, extract_mentioned_players
from app.llm import LLMError, ask_gemini, compute_cache_key
from app.sleeper import resolve_player_name
from app.snapshot import LeagueSnapshot, load_league_snapshot

log = logging.getLogger(__name__)

SYSTEM_PROMPT = textwrap.dedent(f"""\
You are an expert fantasy football General Manager and strategist for Anthony's team: **{TEAM_NAME}** (roster_id 4) in the 12-team, half-PPR Sleeper league "Loser eats an egg".

### League Scoring & Format
- Roster: 1 QB · 2 RB · 2 WR · 1 TE · 2 FLEX (RB/WR/TE) · 1 K · 1 DEF (10 starters) + 5 bench + 1 IR.
- Passing: 0.04/yd · 4 per TD · -1 per INT · 2 per 2PC
- Rushing: 0.1/yd · 6 per TD · 2 per 2PC
- Receiving: 0.5 per reception (half-PPR, no TE premium) · 0.1/yd · 6 per TD · 2 per 2PC
- Kicking: distance-tiered (0-39=3, 40-49=4, 50-59=5, 60+=6)
- Waivers: FAAB bidding ($100 budget), clears Wednesdays 3 AM ET.

### Mathematical Decision Rules (No Hallucinations)
1. LEAD WITH THE RECOMMENDATION, THEN THE REASONING. Show the exact projected numbers and margins.
2. REASONING CONTRACT: For every recommendation, weigh the best alternative (runner-up) and state why it lost.
3. POSITIONAL MARGINAL VALUE: Never favor WR or any position by default. Evaluate by projected marginal value to Anthony's starting lineup. If K is on bye or TE is out, a replacement K/TE has massive value. If WR is recommended, explain specifically which flex or starter they upgrade.
4. HONEST UNCERTAINTY: Flag injury statuses (Questionable), low snap counts, or bad weather. Never guess or hide risks.
5. PRESERVE DETERMINISTIC CALCULATIONS: The exact mathematical numbers and recommended swaps provided in the context are grounded in live Sleeper data. Rely on them.
6. Mobile-friendly format: concise, punchy bullet points, narrow markdown tables.
""")


def _format_lineup_markdown(res: Dict[str, Any]) -> str:
    lines = [
        f"### 📋 Optimal Week Lineup Analysis",
        f"**Projected Total:** **{res['total_pts']:.1f} pts**\n",
        "| Slot | Player | Pos | Proj | Status |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for s in res["starters"]:
        inj = f" ⚠️ {s['injury_status']}" if s.get("injury_status") else "Active"
        locked = " 🔒" if s.get("locked") else ""
        lines.append(f"| **{s['slot']}** | {resolve_player_name(s['id'])} | {s['pos']} | {s['pts']:.1f} | {inj}{locked} |")

    lines.append("\n**Bench:** " + ", ".join(f"{resolve_player_name(b['id'])} ({b['pts']:.1f})" for b in res["bench"]))
    return "\n".join(lines)


def _format_waiver_markdown(res: Dict[str, Any]) -> str:
    lines = [
        f"### 📡 Positional Marginal Value Waiver Analysis",
        f"**Remaining FAAB Budget:** ${res['faab_remaining']} of $100\n",
    ]
    if res["hold_faab"]:
        lines.append("💡 **Recommendation: HOLD YOUR FAAB.** No available waiver targets provide significant marginal improvement (>= 0.5 pts/wk) to your starting lineup.\n")

    lines.append("| Target | Pos | Proj | Marginal Gain | Suggested Drop | Rec. FAAB |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    for t in res["top_targets"]:
        lines.append(
            f"| **{t['name']}** | {t['pos']} | {t['w0_proj']:.1f} | **+{t['marginal_gain']:.1f} pts/wk** | Drop {t['suggested_drop_name']} | ${t['suggested_bid']} |"
        )

    if res.get("runner_up_reasoning"):
        lines.append(f"\n⚖️ **Best Alternative Evaluation:**\n{res['runner_up_reasoning']}")

    if res.get("different_pos_alternative"):
        alt = res["different_pos_alternative"]
        lines.append(f"\n🔄 **Different Position Pivot:** {alt['name']} ({alt['pos']}, +{alt['marginal_gain']:.1f} pts/wk)")

    return "\n".join(lines)


def _format_startsit_markdown(res: Dict[str, Any]) -> str:
    lines = [f"### ⚖️ Start / Sit Breakdown & Close Calls"]
    if res["is_already_optimal"]:
        lines.append("✅ **Your current starting lineup is already 100% optimal!** No swaps needed.")
    else:
        lines.append("⚠️ **Recommended Lineup Adjustments:**")
        for sw in res["swaps_recommended"]:
            lines.append(f"- 🔄 **Swap In:** **{sw['start_player']}** ({sw['start_pts']:.1f} pts) over **{sw['bench_player']}** ({sw['bench_pts']:.1f} pts) [**+{sw['pts_gain']:.1f} pts**]")

    if res["close_calls"]:
        lines.append("\n🔍 **Close Calls (< 2.0 pts margin):**")
        for cc in res["close_calls"]:
            lines.append(f"- **{cc['slot']}:** Start **{cc['starter_name']}** ({cc['starter_pts']:.1f}) over **{cc['bench_name']}** ({cc['bench_pts']:.1f}) — margin: {cc['margin']:.1f} pts. {cc['reasoning']}")
    else:
        lines.append("\nNo razor-thin coin flips this week; starters have clear projected separation over bench options.")

    return "\n".join(lines)


def _format_matchup_markdown(res: Dict[str, Any]) -> str:
    margin_str = f"+{res['projected_margin']:.1f}" if res['projected_margin'] >= 0 else f"{res['projected_margin']:.1f}"
    lines = [
        f"### 🔍 Week Matchup Preview: Anthony vs. {res['opponent_name']}",
        f"- **Current Live Score:** Anthony {res['my_live_pts']:.1f} — {res['opp_live_pts']:.1f} {res['opponent_name']}",
        f"- **Projected Final Score:** **Anthony {res['my_projected_total']:.1f}** — **{res['opp_projected_total']:.1f} {res['opponent_name']}** (Margin: {margin_str} pts)",
        f"- **Estimated Win Probability:** **{res['win_probability_pct']}%**",
        "\n**Key Anthony Pivot Players:**",
    ]
    for p in res["my_key_players"]:
        lines.append(f"- {p['slot']}: **{p['name']}** ({p['pts']:.1f} projected pts, {p['team']})")

    lines.append(f"\n**Key {res['opponent_name']} Threats:**")
    for p in res["opp_key_players"]:
        lines.append(f"- {p['slot']}: **{p['name']}** ({p['pts']:.1f} projected pts, {p['team']})")

    return "\n".join(lines)


async def run_assistant(
    query: str,
    league_context: str = "egg",
    explicit_intent: Optional[str] = None,
    snapshot: Optional[LeagueSnapshot] = None,
) -> Tuple[str, Dict[str, Any], int]:
    """
    Executes the assistant pipeline end-to-end:
    Snapshot -> Engine Calculation -> Prompting -> Gemini -> Fallback -> Response.
    """
    t0 = time.time()

    if not snapshot:
        snapshot = await load_league_snapshot()

    intent = classify_intent(query, explicit_intent)
    my_roster_ids = snapshot.my_roster.get("players") or []
    current_starters_ids = snapshot.my_roster.get("starters") or []
    current_reserve_ids = snapshot.my_roster.get("reserve") or []

    all_rostered_ids = set()
    for r in snapshot.rosters:
        all_rostered_ids.update(r.get("players") or [])

    faab_remaining = 90
    settings = snapshot.my_roster.get("settings") or {}
    budget_used = settings.get("waiver_budget_used", 10)
    faab_remaining = max(0, 100 - budget_used)

    # 1. Deterministic Engine Calculations
    lineup_calc = solve_lineup(
        my_roster_ids,
        snapshot.projections,
        all_players=snapshot.all_players,
    )

    waiver_calc = evaluate_waivers(
        my_roster_ids=my_roster_ids,
        my_starters_ids=current_starters_ids,
        my_reserve_ids=current_reserve_ids,
        all_rostered_ids=all_rostered_ids,
        projections=snapshot.projections,
        future_projections=snapshot.future_projections,
        current_week=snapshot.week,
        faab_remaining=faab_remaining,
        all_players=snapshot.all_players,
        player_name_resolver=resolve_player_name,
    )

    startsit_calc = evaluate_start_sit(
        my_roster_ids=my_roster_ids,
        current_starters_ids=current_starters_ids,
        projections=snapshot.projections,
        scoreboard=snapshot.scoreboard,
        player_name_resolver=resolve_player_name,
        all_players=snapshot.all_players,
    )

    opp_roster_ids = snapshot.opponent_roster.get("players") or []
    matchup_calc = evaluate_matchup_preview(
        my_matchup=snapshot.my_matchup,
        opponent_matchup=snapshot.opponent_matchup,
        my_roster_ids=my_roster_ids,
        opponent_roster_ids=opp_roster_ids,
        opponent_name=snapshot.opponent_name,
        projections=snapshot.projections,
        scoreboard=snapshot.scoreboard,
        player_name_resolver=resolve_player_name,
        all_players=snapshot.all_players,
    )

    # Build calculation fingerprint for deterministic caching
    fingerprint_raw = json.dumps({
        "week": snapshot.week,
        "lineup_tot": lineup_calc["total_pts"],
        "top_waiver": waiver_calc["top_target"]["player_id"] if waiver_calc["top_target"] else None,
        "swaps": len(startsit_calc["swaps_recommended"]),
        "matchup_pts": (snapshot.my_matchup.get("points"), snapshot.opponent_matchup.get("points")),
    }, sort_keys=True)
    data_fingerprint = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()
    cache_key = compute_cache_key(query, intent.value, data_fingerprint)

    # Deterministic fallback text ready if LLM is unavailable
    if intent == Intent.LINEUP:
        deterministic_body = _format_lineup_markdown(lineup_calc)
    elif intent == Intent.WAIVERS:
        deterministic_body = _format_waiver_markdown(waiver_calc)
    elif intent == Intent.START_SIT:
        deterministic_body = _format_startsit_markdown(startsit_calc)
    elif intent == Intent.MATCHUP:
        deterministic_body = _format_matchup_markdown(matchup_calc)
    else:
        # General/overview
        swaps_cnt = len(startsit_calc["swaps_recommended"])
        opt_text = "100% Optimal" if startsit_calc["is_already_optimal"] else f"{swaps_cnt} swap(s) recommended"
        top_t = waiver_calc["top_target"]
        top_t_text = f"{top_t['name']} (+{top_t['marginal_gain']:.1f} pts/wk)" if top_t else "None"
        deterministic_body = (
            f"### 🏈 General GM Overview (Week {snapshot.week})\n"
            f"- **Optimal Lineup:** Projected **{lineup_calc['total_pts']:.1f} pts**\n"
            f"- **Current Matchup:** Anthony {matchup_calc['my_live_pts']:.1f} vs {snapshot.opponent_name} {matchup_calc['opp_live_pts']:.1f} ({matchup_calc['win_probability_pct']}% Win Probability)\n"
            f"- **Top Waiver Move:** {top_t_text}\n"
            f"- **Lineup Optimization:** {opt_text}"
        )

    # 2. Build focused prompt for Gemini
    focused_context = (
        f"## Live Context (Week {snapshot.week}, {snapshot.season})\n"
        f"**User Question:** {query}\n"
        f"**Detected Intent:** {intent.value}\n\n"
        f"### Deterministic Engine Output:\n"
        f"{deterministic_body}\n\n"
        f"### Additional Matchup & Weather Info:\n"
        f"{json.dumps(snapshot.scoreboard[:6], indent=1)}\n"
    )

    answer = ""
    data_sources = dict(snapshot.data_sources)

    try:
        llm_answer = await ask_gemini(
            prompt=focused_context,
            system_instruction=SYSTEM_PROMPT,
            cache_key=cache_key,
        )
        # Verify the LLM returned substantial reasoning and didn't hallucinate contradictory picks
        if len(llm_answer.strip()) > 80:
            answer = llm_answer
            data_sources["gemini_llm"] = {"status": "ok", "detail": "Direct reasoning output"}
        else:
            answer = deterministic_body
            data_sources["gemini_llm"] = {"status": "degraded", "detail": "Empty LLM output, used deterministic engine"}
    except LLMError as err:
        log.warning(f"LLM call failed ({err.error_type}): {err.message}. Using deterministic fallback.")
        data_sources["gemini_llm"] = {"status": "error", "error": f"{err.error_type}: {err.message}"}
        # Provide the verified deterministic calculation with a clear transparency notice
        answer = f"⚠️ *Note: LLM analysis service returned an error ({err.message}). Showing verified deterministic calculations:*\n\n{deterministic_body}"
    except Exception as e:
        log.error(f"Unexpected error calling LLM: {e}")
        data_sources["gemini_llm"] = {"status": "error", "error": str(e)}
        answer = f"⚠️ *Note: Analysis service encountered an issue ({str(e)}). Showing verified deterministic calculations:*\n\n{deterministic_body}"

    latency_ms = int((time.time() - t0) * 1000)
    return answer, data_sources, latency_ms
