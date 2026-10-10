"""Intent classification and entity extraction for user fantasy football queries."""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import List, Optional, Set, Tuple


class Intent(str, Enum):
    LINEUP = "lineup"
    WAIVERS = "waivers"
    START_SIT = "start_sit"
    MATCHUP = "matchup"
    TRADE = "trade"
    STANDINGS = "standings"
    GENERAL = "general"


def normalize_text(text: str) -> str:
    n = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^\w\s]", " ", n.lower())


def classify_intent(query: str, explicit_intent: Optional[str] = None) -> Intent:
    """Classifies a user query deterministically into a core fantasy GM intent."""
    if explicit_intent:
        try:
            return Intent(explicit_intent.lower().strip())
        except ValueError:
            pass

    q = normalize_text(query)

    # 1. Exact button text matching
    if "analyze my lineup" in q or "tell me who to start" in q:
        return Intent.LINEUP
    if "best waiver wire pickups" in q or "waiver targets" in q:
        return Intent.WAIVERS
    if "start sit decision breakdown" in q or "close calls this week" in q:
        return Intent.START_SIT
    if "matchup preview for this week" in q or "key players and what does this week turn on" in q:
        return Intent.MATCHUP

    # 2. Targeted keyword classification
    if any(k in q for k in ["start or sit", "start sit", "should i start", "should i bench", "sit or start", "who do i start", "who should i start"]):
        return Intent.START_SIT

    if any(k in q for k in ["waiver", "pickup", "pick up", "faab", "claim", "add drop", "who to drop", "should i drop", "best adds"]):
        return Intent.WAIVERS

    if any(k in q for k in ["matchup", "opponent", "playing against", "who am i playing", "win probability", "projected score"]):
        return Intent.MATCHUP

    if any(k in q for k in ["trade", "trade offer", "trade target", "give up", "receive in trade"]):
        return Intent.TRADE

    if any(k in q for k in ["standings", "league standing", "record", "points for", "leaderboard"]):
        return Intent.STANDINGS

    if any(k in q for k in ["lineup", "optimal lineup", "roster", "my team", "best lineup"]):
        return Intent.LINEUP

    if any(k in q for k in ["start", "sit", "bench"]):
        return Intent.START_SIT

    return Intent.GENERAL


def extract_mentioned_players(query: str, candidate_names: List[str]) -> List[str]:
    """Finds any player names mentioned in the query."""
    q = normalize_text(query)
    found = []
    for name in candidate_names:
        clean = normalize_text(name.split(" (")[0])
        # Match if both first and last name appear or last name is distinct (>4 chars)
        parts = clean.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            if f"{first} {last}" in q:
                found.append(name)
            elif len(last) >= 5 and f" {last} " in f" {q} ":
                found.append(name)
    return found
