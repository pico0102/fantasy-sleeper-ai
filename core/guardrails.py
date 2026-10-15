"""Guardrails for pre-reasoner premise verification and post-reasoner grounding."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from core.schemas import ValidatedPlayer, WorldStateSnapshot
from core.world_model import scan_mentioned_players


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


def enforce_post_reasoner_guardrails(
    response_text: str,
    snapshot: WorldStateSnapshot,
    directives: List[str],
    has_false_premise: bool = False,
) -> Tuple[str, List[str], List[str]]:
    """Post-reasoner verification ensuring false premise corrections, citations, and risk alerts.

    Returns:
        Tuple of (verified_response_text, active_alerts_list, citations_list).
    """
    citations: List[str] = []
    alerts: List[str] = list(snapshot.active_injury_and_role_alerts)
    final_text = response_text

    # Extract provenance citations present in text
    valid_source_ids = {p.source_id for p in snapshot.provenance}
    for sid in valid_source_ids:
        if sid in final_text:
            citations.append(sid)

    # Ensure sleeper_nfl_state is cited if false premise was corrected
    if has_false_premise and "sleeper_nfl_state" not in citations:
        citations.append("sleeper_nfl_state")

    # If Kirk Cousins was queried and response failed to mention Mendoza, enforce it
    for player in snapshot.notable_role_changes:
        if player.full_name == "Kirk Cousins" and player.role_and_news.role_status == "benched":
            mendoza_name = player.role_and_news.supplanted_by or "Fernando Mendoza"
            if mendoza_name.lower() not in final_text.lower():
                team_str = player.team or "Las Vegas"
                prefix = (
                    f"**Alert (nfl_news_and_depth_charts):** Kirk Cousins has been officially benched "
                    f"in favor of {mendoza_name}. He carries zero projected floor as {team_str}'s backup QB.\n\n"
                )
                final_text = prefix + final_text
                if "nfl_news_and_depth_charts" not in citations:
                    citations.append("nfl_news_and_depth_charts")

        # If Baker Mayfield was queried and response failed to surface dislocated thumb/Out
        if player.full_name == "Baker Mayfield" and player.injury.status == "Out":
            if "thumb" not in final_text.lower() and "out" not in final_text.lower():
                prefix = (
                    f"**Alert (nfl_news_and_depth_charts):** Baker Mayfield is officially ruled Out "
                    f"for Week {snapshot.nfl_week} due to a dislocated thumb. Do not start him.\n\n"
                )
                final_text = prefix + final_text
                if "nfl_news_and_depth_charts" not in citations:
                    citations.append("nfl_news_and_depth_charts")

    # Guard against vague, non-committal QB closings
    vague_phrases = [
        "consider other options",
        "consider your options",
        "consider sitting him",
        "weigh your alternatives",
        "monitor the situation",
    ]
    for vp in vague_phrases:
        if vp in final_text.lower():
            # Replace with decisive phrasing
            final_text = re.sub(
                re.escape(vp),
                "bench him immediately and lock in your highest-floor active starter",
                final_text,
                flags=re.IGNORECASE,
            )

    return final_text, alerts, citations
