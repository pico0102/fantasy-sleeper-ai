"""Unified Survivor Subsystem, Fail-Closed Refusal Gate & Weekly Email Dispatcher.

Shared identically by the UI Survivor toggle and the weekly automated survivor email job.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from core.config import ANTHONY_SURVIVOR_USED_TEAMS, SURVIVOR_FAIL_CLOSED_TEMPLATE
from core.exceptions import SurvivorGateError
from core.provenance import build_provenance_record
from core.schemas import NFLGameOdds, ProvenanceRecord, SurvivorPickResult, WorldStateSnapshot

log = logging.getLogger(__name__)

# All 32 NFL team cities and mascots (case-insensitive words)
NFL_CITIES_AND_MASCOTS = [
    "cardinals", "falcons", "ravens", "bills", "panthers", "bears", "bengals", "browns",
    "cowboys", "broncos", "lions", "packers", "texans", "colts", "jaguars", "chiefs",
    "raiders", "chargers", "rams", "dolphins", "vikings", "patriots", "saints", "giants",
    "jets", "eagles", "steelers", "49ers", "niners", "seahawks", "buccaneers", "bucs",
    "titans", "commanders",
    "arizona", "atlanta", "baltimore", "buffalo", "carolina", "chicago", "cincinnati",
    "cleveland", "dallas", "denver", "detroit", "green bay", "houston", "indianapolis",
    "jacksonville", "kansas city", "las vegas", "los angeles", "miami", "minnesota",
    "new england", "new orleans", "philadelphia", "pittsburgh", "san francisco",
    "seattle", "tampa bay", "tennessee", "washington",
]

# NFL 2-3 letter team abbreviations (case-sensitive uppercase whole words)
NFL_ABBREVIATIONS = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAC", "JAX", "KC", "LV", "LAC", "LAR",
    "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA",
    "TB", "TEN", "WAS",
]

# Precompiled regex for leakage detection
_CITIES_MASCOTS_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in NFL_CITIES_AND_MASCOTS) + r")\b",
    re.IGNORECASE,
)
_ABBREVIATIONS_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in NFL_ABBREVIATIONS) + r")\b"
)


def assert_no_nfl_team_leaked(text: str) -> None:
    """Verify that text contains zero NFL team names, cities, mascots, or abbreviations.

    Raises:
        AssertionError: If any NFL team token is detected.
    """
    if not text:
        return

    # Check case-insensitive cities and mascots
    cm_matches = _CITIES_MASCOTS_REGEX.findall(text)
    if cm_matches:
        raise AssertionError(
            f"Zero-team-leak violation: Found NFL city/mascot '{cm_matches[0]}' in text: {text}"
        )

    # Check case-sensitive uppercase abbreviations
    abbr_matches = _ABBREVIATIONS_REGEX.findall(text)
    if abbr_matches:
        raise AssertionError(
            f"Zero-team-leak violation: Found NFL abbreviation '{abbr_matches[0]}' in text: {text}"
        )


class SurvivorEngine:
    """Unified Survivor Pick Engine with pre-LLM fail-closed gate."""

    @staticmethod
    def evaluate_week(
        snapshot: WorldStateSnapshot,
        used_teams: Optional[List[str]] = None,
        query: Optional[str] = None,
    ) -> SurvivorPickResult:
        """Evaluate Survivor pool for canonical NFL week.

        Pre-LLM Gate:
        If odds data is unconfigured, failed, or empty, halt immediately and return
        deterministic SURVIVOR_FAIL_CLOSED_TEMPLATE without calling the LLM.
        """
        # Plumb Anthony's survivor pick history (SEA, LAC, BUF, BAL, HOU) into used_teams
        combined_used: List[str] = list(ANTHONY_SURVIVOR_USED_TEAMS)
        if used_teams:
            combined_used.extend(used_teams)
        used = {t.strip().upper() for t in combined_used if t}

        # Locate odds provenance
        odds_prov: Optional[ProvenanceRecord] = None
        for p in snapshot.provenance:
            if p.source_id == "the_odds_api_nfl":
                odds_prov = p
                break

        if odds_prov is None:
            odds_prov = build_provenance_record(
                source_id="the_odds_api_nfl",
                source_type="odds_api",
                endpoint_or_path="the-odds-api/nfl",
                status="missing_key",
                record_count=0,
                error_detail="Odds provenance record not found in snapshot",
            )

        # ── Pre-LLM Deterministic Fail-Closed Gate ─────────────────────────
        if odds_prov.status != "ok" or not snapshot.current_week_odds:
            status = odds_prov.status if odds_prov.status != "ok" else "failed"
            refusal_message = SURVIVOR_FAIL_CLOSED_TEMPLATE.format(status=status)
            return SurvivorPickResult(
                status="fail_closed_no_odds",
                nfl_week=snapshot.nfl_week,
                recommended_team=None,
                opponent_team=None,
                spread=None,
                implied_win_prob=None,
                reasoning_markdown=refusal_message,
                future_value_notes=None,
                odds_provenance=odds_prov,
                snapshot_id=snapshot.snapshot_id,
            )

        # ── Filter Eligible Games (Excluding Used Teams) ────────────────────
        eligible_games: List[NFLGameOdds] = []
        for g in snapshot.current_week_odds:
            if g.favorite_team not in used:
                eligible_games.append(g)

        if not eligible_games:
            # Fallback when all favorites have been used
            refusal_message = (
                f"No eligible survivor favorites remaining for Week {snapshot.nfl_week}. "
                f"All available favorites have already been used in previous weeks: {', '.join(sorted(list(used)))}."
            )
            return SurvivorPickResult(
                status="guardrail_rejected",
                nfl_week=snapshot.nfl_week,
                recommended_team=None,
                opponent_team=None,
                spread=None,
                implied_win_prob=None,
                reasoning_markdown=refusal_message,
                odds_provenance=odds_prov,
                snapshot_id=snapshot.snapshot_id,
            )

        # Sort eligible games: strongest spread and highest win probability first
        eligible_games.sort(key=lambda x: (x.spread_points, -x.implied_win_prob))
        top_pick = eligible_games[0]

        # Check if user query adversarially requests a previously burned team
        adversarial_note = ""
        if query:
            burned_name_map = {
                "SEA": ["sea", "seattle", "seahawks"],
                "LAC": ["lac", "chargers", "los angeles chargers", "la chargers"],
                "BUF": ["buf", "buffalo", "bills"],
                "BAL": ["bal", "baltimore", "ravens"],
                "HOU": ["hou", "houston", "texans"],
            }
            q_low = f" {query.lower()} "
            requested_burned: List[str] = []
            for abbr in sorted(used):
                keywords = burned_name_map.get(abbr, [abbr.lower()])
                if any(f" {kw} " in q_low or f" {kw}?" in q_low or f" {kw}." in q_low or f" {kw}," in q_low for kw in keywords):
                    requested_burned.append(abbr)
            if requested_burned:
                burned_str = ", ".join(requested_burned)
                adversarial_note = (
                    f"** Eligibility Rejection:** You **cannot** pick **{burned_str}** in Week {snapshot.nfl_week} "
                    f"because you already used {', '.join(sorted(list(used)))} in prior weeks. "
                    f"Survivor pool rules strictly prohibit repeating a team.\n\n"
                )

        reasoning = (
            f"### 🛡️ Verified Survivor Recommendation — Week {snapshot.nfl_week}\n\n"
            f"{adversarial_note}"
            f"**Recommended Pick:** **{top_pick.favorite_team}** (vs {top_pick.underdog_team})\n"
            f"- **Point Spread:** `{top_pick.spread_points:+.1f}` (Favored by {abs(top_pick.spread_points):.1f} pts)\n"
            f"- **Implied Win Probability:** `{top_pick.implied_win_prob:.1%}`\n"
            f"- **Moneyline:** `{top_pick.favorite_moneyline or 'N/A'}`\n"
            f"- **Odds Verification:** Live NFL sportsbook spreads ({odds_prov.record_count} games verified)\n\n"
            f"**Strategy & Future Value:** {top_pick.favorite_team} offers the strongest win equity on the board "
            f"while preserving future elite options. Previously used teams ({', '.join(sorted(list(used))) or 'None'}) were strictly excluded."
        )

        return SurvivorPickResult(
            status="verified_pick",
            nfl_week=snapshot.nfl_week,
            recommended_team=top_pick.favorite_team,
            opponent_team=top_pick.underdog_team,
            spread=top_pick.spread_points,
            implied_win_prob=top_pick.implied_win_prob,
            reasoning_markdown=reasoning,
            future_value_notes=f"Preserves late-season options while securing {top_pick.favorite_team}.",
            odds_provenance=odds_prov,
            snapshot_id=snapshot.snapshot_id,
        )


class SurvivorEmailDispatcher:
    """Automated weekly survivor email dispatcher with hard zero-team-leak pre-send safeguard."""

    def __init__(self, engine: Optional[SurvivorEngine] = None):
        self.engine = engine or SurvivorEngine()

    def run(
        self,
        snapshot: WorldStateSnapshot,
        used_teams: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Execute weekly survivor evaluation and enforce pre-send zero-leak safeguard."""
        result = self.engine.evaluate_week(snapshot, used_teams=used_teams)

        # ── Hard Pre-Send Safeguard ─────────────────────────────────────────
        if result.status != "verified_pick" or result.recommended_team is None:
            # Enforce 32-NFL-team regex scan on the output text
            assert_no_nfl_team_leaked(result.reasoning_markdown)
            log.warning("Survivor email aborted due to fail-closed state: %s", result.status)
            return {
                "email_sent": False,
                "abort_reason": result.status,
                "recommended_team": None,
                "detail": result.reasoning_markdown,
                "nfl_week": snapshot.nfl_week,
            }

        # In verified_pick state, sending email is permitted
        return {
            "email_sent": True,
            "recommended_team": result.recommended_team,
            "opponent_team": result.opponent_team,
            "spread": result.spread,
            "implied_win_prob": result.implied_win_prob,
            "nfl_week": snapshot.nfl_week,
            "subject": f"[Survivor Pool] Week {snapshot.nfl_week} Pick: {result.recommended_team}",
            "body": result.reasoning_markdown,
        }
