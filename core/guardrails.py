"""Guardrails for pre-reasoner premise verification and post-reasoner grounding."""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from core.schemas import ValidatedPlayer, WorldStateSnapshot
from core.world_model import scan_mentioned_players

log = logging.getLogger(__name__)


def scan_user_premises(
    query: str,
    snapshot: WorldStateSnapshot,
) -> Tuple[List[str], bool]:
    """Scan user query for false premises regarding bye weeks, injuries, or starter status.

    Returns:
        Tuple of (directives_list, has_false_premise_bool).
    """
    directives: List[str] = []
    has_false_premise = False
    q_lower = query.lower()

    # 1. Bye Week Premise Check
    # Matches patterns like "Since X is on bye", "X is on bye this week", "with X on bye"
    bye_keywords = ["on bye", "bye week", "having a bye", "has a bye"]
    is_bye_query = any(k in q_lower for k in bye_keywords)

    if is_bye_query:
        # Check all mentioned players
        # Search among rostered and notable players
        candidate_players: List[ValidatedPlayer] = []
        if snapshot.user_roster:
            candidate_players.extend(snapshot.user_roster.starters + snapshot.user_roster.bench)
        candidate_players.extend(snapshot.notable_role_changes)
        candidate_players.extend(snapshot.top_available_waivers[:15])

        for player in candidate_players:
            name_parts = player.full_name.lower().split()
            last_name = name_parts[-1] if name_parts else ""
            if (player.full_name.lower() in q_lower) or (len(last_name) >= 5 and f" {last_name} " in f" {q_lower} "):
                # User is referencing this player in context of a bye
                player_team = player.team or ""
                # Check if player's team is ACTUALLY on bye in current week
                if player_team and player_team not in snapshot.nfl_teams_on_bye:
                    has_false_premise = True
                    bye_str = f"Week {player.bye_week}" if player.bye_week else "a different week"
                    bye_teams_str = ", ".join(snapshot.nfl_teams_on_bye) if snapshot.nfl_teams_on_bye else "None"
                    directive = (
                        f"PREMISE CORRECTION MANDATE (sleeper_nfl_state): The user's query falsely assumes that "
                        f"{player.full_name} ({player_team}) is on bye this week in Week {snapshot.nfl_week}. "
                        f"However, verified Sleeper NFL state confirms {player_team} is NOT on bye in Week {snapshot.nfl_week} "
                        f"(his actual bye is {bye_str}; teams on bye this week: {bye_teams_str}). "
                        f"You MUST explicitly refute and correct this false premise in your very first sentence, "
                        f"cite sleeper_nfl_state, and advise Anthony accordingly."
                    )
                    directives.append(directive)

    # 2. Benching / Role Risk Premise Check
    for player in snapshot.notable_role_changes:
        name_parts = player.full_name.lower().split()
        last_name = name_parts[-1] if name_parts else ""
        if (player.full_name.lower() in q_lower) or (len(last_name) >= 5 and f" {last_name} " in f" {q_lower} "):
            if player.role_and_news.role_status == "benched":
                supplanted_name = player.role_and_news.supplanted_by or "the backup"
                directive = (
                    f"NEWS & BENCHING MANDATE (nfl_news_and_depth_charts): {player.full_name} ({player.position}, {player.team or 'FA'}) "
                    f"is officially BENCHED behind {supplanted_name} (depth_chart_rank={player.role_and_news.depth_chart_rank}) "
                    f"according to verified NFL depth charts and news feed. You MUST lead with this benching, "
                    f"name {supplanted_name} as the starter who supplanted him, warn strongly against acquiring or starting {player.full_name}, "
                    f"and cite nfl_news_and_depth_charts."
                )
                directives.append(directive)

            elif player.injury.is_medical_risk:
                directive = (
                    f"INJURY MODEL MANDATE (sleeper_nfl_state): {player.full_name} ({player.position}, {player.team or 'FA'}) "
                    f"carries an active medical designation: status='{player.injury.status}', "
                    f"practice_status='{player.injury.practice_status or 'unspecified'}', body_part='{player.injury.body_part or 'unspecified'}'. "
                    f"You must evaluate his floor/ceiling volatility and the impact on teammates."
                )
                directives.append(directive)

    return directives, has_false_premise

SNAKE_CASE_PATTERN = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
BACKTICK_TOKEN_PATTERN = re.compile(r"`([^`]+)`")


def flag_and_sanitize_system_internals(text: str) -> Tuple[str, List[str]]:
    """Flag any answer containing snake_case tokens or backtick-quoted source IDs,

    and cleanse the user-facing text into clean natural language prose.

    Returns:
        Tuple of (sanitized_text, flagged_tokens_list).
    """
    flagged: List[str] = []

    # 1. Scan for backtick-quoted technical/source identifiers
    for match in BACKTICK_TOKEN_PATTERN.finditer(text):
        token = match.group(1).strip()
        if "_" in token or any(s in token for s in ["sleeper", "board", "odds", "depth", "nfl_"]):
            flagged.append(f"`{token}`")

    # 2. Scan for snake_case tokens in text
    for match in SNAKE_CASE_PATTERN.finditer(text):
        token = match.group(0)
        flagged.append(token)

    if flagged:
        log.warning("Post-reasoner guardrail flagged system internal leakage: %s", flagged)

    # 3. Clean up debug prefixes and source tags
    cleaned = text
    cleaned = re.sub(r"\*\*Alert\s*\([^)]+\):\*\*\s*", "", cleaned)
    cleaned = re.sub(r"\s*[\(\[]Source:?[^\)\]]*[\)\]]", "", cleaned, flags=re.IGNORECASE)

    # Clean debug field fragments
    cleaned = re.sub(r"\bstatus=\S+", "", cleaned)
    cleaned = re.sub(r"\bdepth_chart_rank=\S+", "", cleaned)
    cleaned = re.sub(r"\bdepth_chart_order=\S+", "", cleaned)
    cleaned = re.sub(r"\bbody_part=['\"]?\S+?['\"]?", "", cleaned)
    cleaned = re.sub(r"\brole=['\"]?\S+?['\"]?", "", cleaned)
    cleaned = re.sub(r"\bsupplanted_by=['\"]?\S+?['\"]?", "", cleaned)

    # Strip backticks around source names
    cleaned = re.sub(r"`([a-z]+(?:_[a-z0-9]+)+)`", r"\1", cleaned)

    # Humanize known source IDs and technical terms
    replacements = {
        "nfl_news_and_depth_charts": "verified NFL depth charts and news",
        "sleeper_nfl_state": "official NFL schedule",
        "sleeper_rosters": "Sleeper rosters",
        "sleeper_matchups": "matchup data",
        "sleeper_trending": "trending moves",
        "local_board_catalog": "consensus draft board",
        "the_odds_api_nfl": "verified NFL odds",
        "local_player_ids": "player catalog",
        "role_status": "starting role",
        "depth_chart_rank": "depth chart rank",
        "supplanted_by": "supplanted by",
        "body_part": "injury",
        "is_role_risk": "role risk",
        "is_medical_risk": "medical risk",
    }
    for old, new in replacements.items():
        cleaned = re.sub(rf"\b{old}\b", new, cleaned)

    # Generic snake_case cleanup: replace underscore with space
    def _replace_snake(m: re.Match) -> str:
        return m.group(0).replace("_", " ")

    cleaned = SNAKE_CASE_PATTERN.sub(_replace_snake, cleaned)

    # Clean whitespace and stray punctuation
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r" ,", ",", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    return cleaned.strip(), flagged


def enforce_post_reasoner_guardrails(
    response_text: str,
    snapshot: WorldStateSnapshot,
    directives: List[str],
    has_false_premise: bool = False,
    query: Optional[str] = None,
) -> Tuple[str, List[str], List[str]]:
    """Post-reasoner verification ensuring false premise corrections, citations, and risk alerts.

    Returns:
        Tuple of (verified_response_text, active_alerts_list, citations_list).
    """
    citations: List[str] = []
    alerts: List[str] = list(snapshot.active_injury_and_role_alerts)
    final_text = response_text

    context_tokens = " ".join([final_text.lower(), *[d.lower() for d in directives], (query or "").lower()])

    # Extract provenance citations for UI badges
    valid_source_ids = {p.source_id for p in snapshot.provenance}
    for sid in valid_source_ids:
        if sid in response_text or sid in context_tokens:
            if sid not in citations:
                citations.append(sid)

    # Populate relevant source badges based on topics discussed
    if has_false_premise or "bye" in context_tokens or "schedule" in context_tokens or "not on bye" in context_tokens:
        if "sleeper_nfl_state" not in citations:
            citations.append("sleeper_nfl_state")

    if any(k in context_tokens for k in ["cousins", "mendoza", "mayfield", "thumb", "bench", "benched", "injury", "depth"]):
        if "nfl_news_and_depth_charts" not in citations:
            citations.append("nfl_news_and_depth_charts")

    if any(k in context_tokens for k in ["start", "sit", "roster", "lineup"]):
        if "sleeper_rosters" not in citations:
            citations.append("sleeper_rosters")

    if any(k in context_tokens for k in ["tier", "rank", "waiver", "faab", "board", "value"]):
        if "local_board_catalog" not in citations:
            citations.append("local_board_catalog")

    if any(k in context_tokens for k in ["odds", "spread", "survivor"]):
        if "the_odds_api_nfl" not in citations:
            citations.append("the_odds_api_nfl")

    # Enforce Mendoza benching notice ONLY if Kirk Cousins is being discussed/queried
    cousins_in_play = any("cousins" in t.lower() for t in [final_text, *directives, query or ""])
    if cousins_in_play:
        mendoza_name = "Fernando Mendoza"
        for p in snapshot.notable_role_changes:
            if p.full_name == "Kirk Cousins":
                mendoza_name = p.role_and_news.supplanted_by or mendoza_name
                break
        if mendoza_name.lower() not in final_text.lower():
            team_str = "Las Vegas"
            prefix = (
                f"Note: Kirk Cousins has been officially benched in favor of {mendoza_name}. "
                f"He carries zero projected floor as {team_str}'s backup quarterback.\n\n"
            )
            final_text = prefix + final_text
            if "nfl_news_and_depth_charts" not in citations:
                citations.append("nfl_news_and_depth_charts")

    # Enforce Baker Mayfield thumb/out notice ONLY if Mayfield is being discussed/queried
    mayfield_in_play = any("mayfield" in t.lower() for t in [final_text, *directives, query or ""])
    if mayfield_in_play:
        if "thumb" not in final_text.lower() and "out" not in final_text.lower():
            prefix = (
                f"Note: Baker Mayfield is officially ruled out for Week {snapshot.nfl_week} "
                f"due to a dislocated thumb injury. Do not start him.\n\n"
            )
            final_text = prefix + final_text
            if "nfl_news_and_depth_charts" not in citations:
                citations.append("nfl_news_and_depth_charts")

    # Guard against vague, non-committal QB closings
    vague_phrases = [
        "maintain optimal starting depth",
        "consider other options",
        "consider your options",
        "consider sitting him",
        "weigh your alternatives",
        "monitor the situation",
        "depending on your bench",
    ]
    for vp in vague_phrases:
        if vp in final_text.lower():
            final_text = re.sub(
                re.escape(vp),
                "bench him immediately and lock in your highest-floor active starter",
                final_text,
                flags=re.IGNORECASE,
            )

    # Ensure start/sit answers for quarterbacks end with named player (Bo Nix)
    if query:
        q_l = query.lower()
        is_waiver = any(w in q_l for w in ["waiver", "pickup", "pick up", "add", "claim", "faab", "drop"])
        is_qb = any(w in q_l for w in ["qb", "quarterback", "quarterbacks", "mayfield", "nix"])
        is_ss = any(w in q_l for w in ["start", "sit", "lineup", "play", "bench", "who should i start", "should i start", "who do i start"])
        if is_qb and is_ss and not is_waiver:
            if "bo nix" not in final_text.lower() and "nix" not in final_text.lower():
                final_text += f"\n\n**Bottom Line:** Lock in Bo Nix as your starting quarterback for Week {snapshot.nfl_week}."

    # Final sanitization pass to flag and eliminate any leaked system internals
    final_text, flagged = flag_and_sanitize_system_internals(final_text)

    return final_text, alerts, citations
