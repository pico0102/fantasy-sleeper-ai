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
    STASH_DROP = "stash_drop"
    FAAB_BID = "faab_bid"
    UNRECOGNIZED = "unrecognized"
    GENERAL = "general"


_FANTASY_DOMAIN_KEYWORDS: Set[str] = {
    "start", "sit", "bench", "lineup", "roster", "team", "waiver", "waivers",
    "wire", "pickup", "pick", "add", "drop", "stash", "hold", "cut", "faab",
    "bid", "claim", "free", "agent", "agents", "matchup", "opponent", "playing",
    "against", "versus", "vs", "trade", "offer", "give", "receive", "standings",
    "record", "points", "leaderboard", "rank", "ranking", "tier", "tiers",
    "qb", "quarterback", "quarterbacks", "rb", "running", "back", "backs",
    "halfback", "wr", "wide", "receiver", "receivers", "te", "tight", "end",
    "ends", "k", "kicker", "kickers", "pk", "def", "dst", "defense", "defenses",
    "flex", "superflex", "ir", "injured", "injury", "status", "hurt", "out",
    "questionable", "doubtful", "bye", "week", "sunday", "monday", "thursday",
    "projection", "projections", "projected", "floor", "ceiling", "target",
    "targets", "touches", "snaps", "red", "zone", "redzone", "touchdown",
    "tds", "ppr", "sleeper", "survivor", "pool", "spread", "odds", "moneyline",
    "healthy", "starter", "starters", "backup", "benched", "depth", "chart",
    "who", "should", "what", "how", "why", "is", "are", "can", "my", "help",
    "analyze", "preview", "breakdown", "advice", "recommend", "recommendation",
}


def normalize_text(text: str) -> str:
    n = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^\w\s]", " ", n.lower())


def extract_target_position(query: str) -> Optional[str]:
    """Deterministically extract the queried fantasy position (QB, RB, WR, TE, K, DEF, FLEX)."""
    q = normalize_text(query)
    tokens = set(q.split())

    if "tight end" in q or "tight ends" in q or "te" in tokens or "tes" in tokens:
        return "TE"
    if (
        "kicker" in q
        or "kickers" in q
        or "placekicker" in q
        or "pk" in tokens
        or ("k" in tokens and any(p in q for p in ["at k", "my k", "start k", "k this", "k slot"]))
    ):
        return "K"
    if (
        "defense" in q
        or "defenses" in q
        or "def" in tokens
        or "dst" in tokens
        or bool(re.search(r"\bd\s+st\b", q))
    ):
        return "DEF"
    if "quarterback" in q or "quarterbacks" in q or "qb" in tokens or "qbs" in tokens:
        return "QB"
    if "running back" in q or "running backs" in q or "halfback" in q or "rb" in tokens or "rbs" in tokens:
        return "RB"
    if (
        "wide receiver" in q
        or "wide receivers" in q
        or "receiver" in q
        or "receivers" in q
        or "wideout" in q
        or "wr" in tokens
        or "wrs" in tokens
    ):
        return "WR"
    if "flex" in tokens:
        return "FLEX"
    return None


def is_stash_or_drop_query(query: str) -> bool:
    """Detect stash-or-drop / keep-or-cut roster management questions."""
    q = normalize_text(query)
    return any(
        phrase in q
        for phrase in [
            "stash or drop",
            "drop or stash",
            "keep or drop",
            "drop or keep",
            "hold or drop",
            "drop or hold",
            "should i stash",
            "should i drop",
            "worth stashing",
            "worth holding",
            "can i drop",
            "safe to drop",
            "cut or keep",
            "stash",
        ]
    )


def is_faab_bid_query(query: str) -> bool:
    """Detect FAAB bid sizing questions (e.g., 'How much FAAB should I bid on C.J. Stroud?')."""
    q = normalize_text(query)
    if "faab" in q and any(w in q for w in ["how much", "bid", "spend", "percent", "budget", "amount", "worth", "put on"]):
        return True
    if "how much" in q and any(w in q for w in ["bid", "faab", "waiver budget"]):
        return True
    if "bid on" in q or "faab on" in q or "faab for" in q:
        return True
    return False


def is_matchup_query(query: str) -> bool:
    """Detect weekly head-to-head matchup preview questions."""
    q = normalize_text(query)
    return any(
        k in q
        for k in [
            "matchup",
            "opponent",
            "playing against",
            "who am i playing",
            "who do i play",
            "facing this week",
            "win probability",
            "projected score",
            "head to head",
        ]
    )


def is_gibberish_or_unrecognized(
    query: str,
    candidate_player_names: Optional[List[str]] = None,
) -> bool:
    """Return True when the user input is gibberish, keyboard mash, or unrelated to fantasy football."""
    if not query or not query.strip():
        return True
    q = normalize_text(query).strip()
    tokens = [t for t in q.split() if t]
    if not tokens:
        return True

    # Check if any known player is mentioned
    if candidate_player_names and extract_mentioned_players(query, candidate_player_names):
        return False

    # Check if any token is a known fantasy/football keyword
    has_domain_word = any(t in _FANTASY_DOMAIN_KEYWORDS for t in tokens)
    if has_domain_word:
        return False

    # Single token with mixed letters+digits or long consonant runs (e.g. "asdfghjkl123")
    if len(tokens) == 1:
        tok = tokens[0]
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            return True
        if re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", tok):
            return True
        return True

    # Multi-token query with zero domain keywords and zero player names
    return True


def classify_intent(
    query: str,
    explicit_intent: Optional[str] = None,
    candidate_player_names: Optional[List[str]] = None,
) -> Intent:
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

    # 2. Specific high-signal sub-intents (Stash/Drop & FAAB Bid & Matchup)
    if is_stash_or_drop_query(query) and "bye" not in q:
        return Intent.STASH_DROP

    if is_faab_bid_query(query):
        return Intent.FAAB_BID

    if is_matchup_query(query):
        return Intent.MATCHUP

    # 3. Targeted keyword classification
    if any(k in q for k in ["start or sit", "start sit", "should i start", "should i bench", "sit or start", "who do i start", "who should i start"]):
        return Intent.START_SIT

    if any(k in q for k in ["waiver", "pickup", "pick up", "faab", "claim", "add drop", "who to drop", "should i drop", "best adds"]):
        return Intent.WAIVERS

    if any(k in q for k in ["trade", "trade offer", "trade target", "give up", "receive in trade"]):
        return Intent.TRADE

    if any(k in q for k in ["standings", "league standing", "record", "points for", "leaderboard"]):
        return Intent.STANDINGS

    if any(k in q for k in ["lineup", "optimal lineup", "roster", "my team", "best lineup"]):
        return Intent.LINEUP

    if any(k in q for k in ["start", "sit", "bench"]) or extract_target_position(query) is not None:
        return Intent.START_SIT

    if is_gibberish_or_unrecognized(query, candidate_player_names=candidate_player_names):
        return Intent.UNRECOGNIZED

    return Intent.GENERAL


def extract_mentioned_players(query: str, candidate_names: List[str]) -> List[str]:
    """Finds any player names mentioned in the query."""
    q = normalize_text(query)
    found = []
    for name in candidate_names:
        clean = normalize_text(name.split(" (")[0])
        parts = clean.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            if f"{first} {last}" in q:
                found.append(name)
            elif len(last) >= 4 and f" {last} " in f" {q} ":
                found.append(name)
    return found

