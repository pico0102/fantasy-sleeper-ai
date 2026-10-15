"""Unified session WorldStateSnapshot builder and manager.

Assembles the single immutable truth per request turn with deterministic SHA-256 fingerprinting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from core.config import MY_ROSTER_ID, TEAM_NAME
from core.data_pipeline import (
    OddsAPIClient,
    SleeperClient,
    load_board_catalog,
    load_player_ids_catalog,
    normalize_nfl_team,
)
from core.news_pipeline import (
    check_all_override_conflicts,
    get_notable_role_players,
    hydrate_player_role_and_news,
    load_news_and_depth_chart,
)
from core.provenance import now_utc_iso
from core.schemas import (
    InjuryContext,
    LeagueContext,
    NFLGameOdds,
    ProvenanceRecord,
    RosterSlotSummary,
    ValidatedPlayer,
    WorldStateSnapshot,
)
from core.week_resolver import resolve_canonical_nfl_week

log = logging.getLogger(__name__)

# Canonical 2026 NFL Bye Week Schedule
NFL_BYE_WEEKS_2026: Dict[int, List[str]] = {
    1: [],
    2: [],
    3: [],
    4: [],
    5: ["KC", "LAC"],
    6: ["KC", "MIA", "MIN", "LAR"],
    7: ["CHI", "DAL"],
    8: ["ARI", "GB"],
    9: ["CLE", "NYG", "PIT", "TB"],
    10: ["DET", "HOU", "LV", "TEN"],
    11: ["ATL", "BUF", "CIN", "NO", "NYJ", "JAC"],
    12: ["DEN", "IND", "NE", "WAS"],
    13: [],
    14: ["BAL", "CAR", "PHI", "SEA"],
    15: [],
    16: [],
    17: [],
    18: [],
}

# Reverse mapping: team abbreviation -> bye week
TEAM_TO_BYE_WEEK: Dict[str, int] = {}
for wk, teams in NFL_BYE_WEEKS_2026.items():
    for tm in teams:
        TEAM_TO_BYE_WEEK[tm] = wk

# Session cache
_SNAPSHOT_CACHE: Dict[str, Dict[str, Any]] = {}


def scan_mentioned_players(
    query_text: str, player_ids_map: Dict[str, str]
) -> List[Tuple[str, str]]:
    """Scan query for any player names in catalog.

    Returns:
        List of (player_id, full_name) tuples found in the text.
    """
    if not query_text:
        return []

    q_lower = f" {query_text.lower()} "
    found: List[Tuple[str, str]] = []
    seen_ids: Set[str] = set()

    for pid, name in player_ids_map.items():
        name_lower = name.lower()
        # Direct full name match
        if name_lower in q_lower:
            if pid not in seen_ids:
                found.append((pid, name))
                seen_ids.add(pid)
            continue

        # Last name match for distinct names (> 4 chars)
        parts = name_lower.split()
        if len(parts) >= 2:
            last = parts[-1]
            if len(last) >= 5 and f" {last} " in q_lower:
                if pid not in seen_ids:
                    found.append((pid, name))
                    seen_ids.add(pid)

    return found


def filter_relevant_alerts(
    alerts: List[str],
    query: Optional[str],
    user_rostered_names: Optional[Set[str]] = None,
) -> List[str]:
    """Filter active role and injury alerts to only those relevant to the specific question asked.

    Criteria:
    - If no query is provided, return up to 2-3 roster alerts.
    - An alert fires ONLY if the player is:
      (a) on the user's roster,
      (b) a waiver/free-agent candidate under discussion this turn, or
      (c) explicitly named in the user's message.
    - Capped at 2-3 alerts per response, ranked by relevance.
    - Cousins triggers on waiver/pickup questions and never on unrelated start/sit questions.
    """
    if not alerts:
        return []

    if not query or not query.strip():
        roster_alerts = [a for a in alerts if a.startswith("[ROSTER INJURY]") or "ROSTER" in a]
        return roster_alerts[:3] if roster_alerts else alerts[:3]

    q_lower = f" {query.lower()} "

    waiver_terms = [
        "waiver", "waivers", "pickup", "pick up", "pickups", "pick-up",
        "add", "adds", "claim", "claims", "faab", "free agent", "free agents", "wire", "drop"
    ]
    is_waiver_query = any(w in q_lower for w in waiver_terms)

    # Check for position keywords in query
    pos_map = {
        "qb": ["(qb,", " qb ", "quarterback", "quarterbacks"],
        "rb": ["(rb,", " rb ", "running back", "running backs", "halfback"],
        "wr": ["(wr,", " wr ", "wide receiver", "wide receivers", "receiver", "receivers"],
        "te": ["(te,", " te ", "tight end", "tight ends"],
        "k": ["(k,", " kicker ", " k "],
        "def": ["(def,", " defense ", " d/st ", "dst"],
    }

    targeted_positions: Set[str] = set()
    for pos, patterns in pos_map.items():
        for pat in patterns[1:]:
            if pat in q_lower:
                targeted_positions.add(pos)
                break
    if " flex " in q_lower or " flexes " in q_lower:
        targeted_positions.update(["rb", "wr", "te"])

    alert_pattern = re.compile(r"^\[.*?\]\s*([^(]+?)\s*\(([^,]+),\s*([^)]+)\):(.*)$")

    # Detect if any specific player is explicitly named in the query
    has_specific_player = False
    for alert in alerts:
        match = alert_pattern.match(alert)
        if match:
            p_name = match.group(1).strip().lower()
            p_last = p_name.split()[-1] if p_name else ""
            if p_name in q_lower or (len(p_last) >= 4 and f" {p_last} " in q_lower):
                has_specific_player = True
                break
            if "supplanted by " in match.group(4).lower():
                supp = match.group(4).lower().split("supplanted by ")[1].split(")")[0].strip()
                if supp in q_lower or any(len(p) >= 4 and f" {p} " in q_lower for p in supp.split()):
                    has_specific_player = True
                    break

    scored_alerts: List[Tuple[int, str]] = []

    for alert in alerts:
        match = alert_pattern.match(alert)
        if not match:
            if any(len(w) >= 5 and f" {w.lower()} " in q_lower for w in alert.split()):
                scored_alerts.append((40, alert))
            continue

        player_name = match.group(1).strip()
        pos = match.group(2).strip().lower()
        team = match.group(3).strip().lower()
        rest = match.group(4).strip().lower()

        p_name_lower = player_name.lower()
        parts = p_name_lower.split()
        last_name = parts[-1] if parts else ""

        # Condition (c): Explicitly named in user's message
        is_named = False
        if p_name_lower in q_lower:
            is_named = True
        elif len(last_name) >= 4 and f" {last_name} " in q_lower:
            is_named = True

        # Check supplanted_by if referenced
        if "supplanted by " in rest:
            supplanted = rest.split("supplanted by ")[1].split(")")[0].strip().lower()
            if supplanted and (supplanted in q_lower or any(len(p) >= 4 and f" {p} " in q_lower for p in supplanted.split())):
                is_named = True

        if is_named:
            scored_alerts.append((100, alert))
            continue

        # If a specific player is queried, do NOT inject unrelated players
        if has_specific_player:
            continue

        # Condition (a): On the user's roster
        is_roster_alert = alert.startswith("[ROSTER INJURY]") or (
            user_rostered_names is not None and p_name_lower in user_rostered_names
        )
        if is_roster_alert:
            if targeted_positions:
                if pos in targeted_positions:
                    scored_alerts.append((85, alert))
                # Unrelated position on roster: do not fire
            else:
                # General query without specific player or position: only fire on roster/team keywords
                general_keywords = ["team", "roster", "lineup", "all", "overview", "rate", "preview", "standings"]
                if any(f" {k} " in q_lower for k in general_keywords):
                    scored_alerts.append((60, alert))
            continue

        # Condition (b): Waiver/free-agent candidate under discussion this turn
        # MUST be a waiver query; NEVER fires on unrelated start/sit questions
        if is_waiver_query:
            if targeted_positions:
                if pos in targeted_positions:
                    scored_alerts.append((75, alert))
            else:
                scored_alerts.append((70, alert))
            continue

    # Sort by relevance descending, deduplicate while preserving order, cap at 3
    scored_alerts.sort(key=lambda x: x[0], reverse=True)
    seen_texts = set()
    result: List[str] = []
    for score, alt in scored_alerts:
        if alt not in seen_texts:
            seen_texts.add(alt)
            result.append(alt)
            if len(result) >= 3:
                break

    return result


class WorldModelBuilder:
    """Builds unified, immutable WorldStateSnapshot instances."""

    def __init__(
        self,
        sleeper_client: Optional[SleeperClient] = None,
        odds_client: Optional[OddsAPIClient] = None,
    ):
        self.sleeper_client = sleeper_client or SleeperClient()
        self.odds_client = odds_client or OddsAPIClient()

    async def build_snapshot(
        self,
        session_id: str = "default",
        query: Optional[str] = None,
        force_refresh: bool = False,
    ) -> WorldStateSnapshot:
        """Assemble a complete, single-sourced WorldStateSnapshot."""
        # 1. Sleeper NFL State & Canonical Week
        nfl_state, nfl_state_prov = await self.sleeper_client.fetch_nfl_state(
            force_refresh=force_refresh
        )
        canonical_week = resolve_canonical_nfl_week(nfl_state)
        nfl_season = str(nfl_state.get("season", "2026"))

        # 2. Bye weeks for canonical week
        teams_on_bye = NFL_BYE_WEEKS_2026.get(canonical_week, [])

        # 3. Catalogs
        player_ids_map, player_ids_prov = load_player_ids_catalog(force_refresh=force_refresh)
        board_data, board_summary, board_prov = load_board_catalog(force_refresh=force_refresh)
        board_by_name = {b["name"].strip().lower(): b for b in board_data if "name" in b}

        # 4. News & Depth Chart Feed
        news_feed, news_prov = load_news_and_depth_chart(force_refresh=force_refresh)

        # 5. Live NFL Odds
        odds_list, odds_prov = await self.odds_client.fetch_nfl_odds(
            nfl_week=canonical_week, force_refresh=force_refresh
        )

        # 6. Sleeper League Data
        try:
            league_raw = await self.sleeper_client.fetch_league(force_refresh=force_refresh)
            league_ctx = LeagueContext(
                league_id=league_raw.get("league_id", ""),
                name=league_raw.get("name", "Loser eats an egg"),
                season=league_raw.get("season", nfl_season),
                total_rosters=int(league_raw.get("total_rosters", 12)),
                scoring_settings=league_raw.get("scoring_settings", {}),
                roster_positions=league_raw.get("roster_positions", []),
            )
        except Exception as e:
            log.warning("Failed to load league context: %s", e)
            league_ctx = LeagueContext(
                league_id="",
                name="Loser eats an egg",
                season=nfl_season,
                total_rosters=12,
            )

        # 7. Users map
        try:
            users_list = await self.sleeper_client.fetch_users(force_refresh=force_refresh)
            users_map = {u.get("user_id"): u.get("display_name", "") for u in users_list}
        except Exception:
            users_map = {}

        # 8. Rosters & Matchups
        try:
            rosters_raw, rosters_prov = await self.sleeper_client.fetch_rosters(
                force_refresh=force_refresh
            )
        except Exception as e:
            rosters_raw = []
            rosters_prov = ProvenanceRecord(
                source_id="sleeper_rosters",
                source_type="sleeper_api",
                endpoint_or_path="sleeper/rosters",
                fetched_at_utc=now_utc_iso(),
                status="failed",
                error_detail=str(e),
            )

        try:
            matchups_raw, matchups_prov = await self.sleeper_client.fetch_matchups(
                week=canonical_week, force_refresh=force_refresh
            )
        except Exception as e:
            matchups_raw = []
            matchups_prov = ProvenanceRecord(
                source_id="sleeper_matchups",
                source_type="sleeper_api",
                endpoint_or_path=f"sleeper/matchups/{canonical_week}",
                fetched_at_utc=now_utc_iso(),
                status="failed",
                error_detail=str(e),
            )

        try:
            trending_raw, trending_prov = await self.sleeper_client.fetch_trending(
                trend_type="add", force_refresh=force_refresh
            )
        except Exception as e:
            trending_raw = []
            trending_prov = ProvenanceRecord(
                source_id="sleeper_trending",
                source_type="sleeper_api",
                endpoint_or_path="sleeper/trending/add",
                fetched_at_utc=now_utc_iso(),
                status="failed",
                error_detail=str(e),
            )

        sleeper_db: Dict[str, Any] = {}
        try:
            from app.sleeper import get_all_players_sync
            sleeper_db = get_all_players_sync() or {}
        except Exception:
            sleeper_db = {}

        # Helper to construct a ValidatedPlayer
        def build_player(
            pid: str,
            name_hint: Optional[str] = None,
            pos_hint: Optional[str] = None,
            team_hint: Optional[str] = None,
            injury_hint: Optional[str] = None,
        ) -> ValidatedPlayer:
            sl_info = sleeper_db.get(pid, {}) if isinstance(sleeper_db, dict) else {}
            sl_name = (
                sl_info.get("full_name")
                or f"{sl_info.get('first_name', '')} {sl_info.get('last_name', '')}".strip()
                or None
            )
            full_name = name_hint or player_ids_map.get(pid) or sl_name or f"Player {pid}"
            role_ctx, inj_override = hydrate_player_role_and_news(
                full_name, sleeper_player_dict=sl_info, news_feed=news_feed
            )

            # Determine medical status
            status_lit = "Active"
            if inj_override:
                inj_ctx = inj_override
            else:
                sl_inj = sl_info.get("injury_status")
                raw_inj = injury_hint or sl_inj
                if raw_inj:
                    status_lit = raw_inj if raw_inj in {
                        "Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended", "COV"
                    } else "Active"
                body_part = sl_info.get("injury_body_part")
                inj_ctx = InjuryContext(status=status_lit, body_part=body_part)

            # Board lookup
            board_entry = board_by_name.get(full_name.strip().lower(), {})
            position = pos_hint or board_entry.get("pos") or role_ctx.depth_chart_position or sl_info.get("position") or "FLEX"
            team = team_hint or board_entry.get("team") or sl_info.get("team")
            bye_wk = TEAM_TO_BYE_WEEK.get(team) if team else None

            return ValidatedPlayer(
                player_id=pid,
                full_name=full_name,
                position=position,
                team=team,
                bye_week=bye_wk,
                injury=inj_ctx,
                role_and_news=role_ctx,
                board_tier=board_entry.get("tier"),
                board_rank=board_entry.get("rank"),
                board_raw_value=board_entry.get("pts") or board_entry.get("vorp"),
                board_bias_adjusted=board_entry.get("board_bias_adjusted", False),
            )

        # Process all rosters
        rostered_player_ids: Set[str] = set()
        all_roster_summaries: List[RosterSlotSummary] = []
        user_roster: Optional[RosterSlotSummary] = None
        user_matchup_id: Optional[int] = None

        # Build matchup map
        roster_to_matchup: Dict[int, int] = {}
        for m in matchups_raw:
            rid = m.get("roster_id")
            mid = m.get("matchup_id")
            if rid is not None and mid is not None:
                roster_to_matchup[int(rid)] = int(mid)

        for r in rosters_raw:
            rid = int(r.get("roster_id", 0))
            owner_id = str(r.get("owner_id", ""))
            owner_name = users_map.get(owner_id, f"Team {rid}")
            settings = r.get("settings", {})
            wins = int(settings.get("wins", 0))
            losses = int(settings.get("losses", 0))
            fpts = float(settings.get("fpts", 0.0)) + float(settings.get("fpts_decimal", 0)) / 100.0

            raw_starters = r.get("starters", []) or []
            raw_players = r.get("players", []) or []
            raw_reserve = r.get("reserve", []) or []

            starter_set = set(raw_starters)
            reserve_set = set(raw_reserve)

            starters: List[ValidatedPlayer] = []
            bench: List[ValidatedPlayer] = []
            ir: List[ValidatedPlayer] = []

            for pid in raw_players:
                if not pid:
                    continue
                rostered_player_ids.add(str(pid))
                p_obj = build_player(str(pid))
                if pid in starter_set:
                    starters.append(p_obj)
                elif pid in reserve_set:
                    ir.append(p_obj)
                else:
                    bench.append(p_obj)

            summary = RosterSlotSummary(
                roster_id=rid,
                owner_id=owner_id,
                owner_name=owner_name,
                starters=starters,
                bench=bench,
                ir=ir,
                wins=wins,
                losses=losses,
                fpts=round(fpts, 2),
            )
            all_roster_summaries.append(summary)

            if rid == MY_ROSTER_ID:
                user_roster = summary
                user_matchup_id = roster_to_matchup.get(rid)

        # Identify opponent roster
        opponent_roster: Optional[RosterSlotSummary] = None
        if user_matchup_id is not None:
            for r in all_roster_summaries:
                if r.roster_id != MY_ROSTER_ID and roster_to_matchup.get(r.roster_id) == user_matchup_id:
                    opponent_roster = r
                    break

        # Process top available waivers
        top_waivers: List[ValidatedPlayer] = []
        for b in board_data:
            b_name = b.get("name", "")
            # Reverse lookup ID
            matched_id: Optional[str] = None
            for pid, p_name in player_ids_map.items():
                if p_name.strip().lower() == b_name.strip().lower():
                    matched_id = pid
                    break

            if matched_id and matched_id not in rostered_player_ids:
                p_obj = build_player(matched_id, name_hint=b_name, pos_hint=b.get("pos"), team_hint=b.get("team"))
                top_waivers.append(p_obj)
                if len(top_waivers) >= 30:
                    break

        # Process trending adds
        trending_players: List[ValidatedPlayer] = []
        for t in trending_raw[:20]:
            pid = str(t.get("player_id", ""))
            count = int(t.get("count", 0))
            p_obj = build_player(pid)
            p_obj.trending_adds_24h = count
            trending_players.append(p_obj)

        # Notable role changes (seed from news feed)
        raw_notable_role_changes: List[ValidatedPlayer] = get_notable_role_players(news_feed=news_feed)

        # Entity pre-lookup: scan query for mentioned players and hydrate into raw_notable_role_changes
        if query:
            mentioned = scan_mentioned_players(query, player_ids_map)
            existing_names = {p.full_name.lower() for p in raw_notable_role_changes}
            for pid, name in mentioned:
                if name.lower() not in existing_names:
                    p_obj = build_player(pid, name_hint=name)
                    raw_notable_role_changes.append(p_obj)
                    existing_names.add(name.lower())

        user_rostered_names: Set[str] = set()
        if user_roster:
            for p in user_roster.starters + user_roster.bench + user_roster.ir:
                user_rostered_names.add(p.full_name.lower())

        # Compile active injury and role alerts in clean natural language
        raw_alerts: List[str] = []
        for p in raw_notable_role_changes:
            if p.role_and_news.is_role_risk:
                supplanted_str = f" behind starter {p.role_and_news.supplanted_by}" if p.role_and_news.supplanted_by else ""
                raw_alerts.append(
                    f"[BENCHING/ROLE RISK] {p.full_name} ({p.position}, {p.team or 'FA'}): "
                    f"Demoted to backup{supplanted_str}"
                )
            if p.injury.is_medical_risk:
                raw_alerts.append(
                    f"[INJURY ALERT] {p.full_name} ({p.position}, {p.team or 'FA'}): "
                    f"{p.injury.status} ({p.injury.body_part or 'Medical issue'})"
                )

        if user_roster:
            for p in user_roster.starters + user_roster.bench:
                if p.injury.is_medical_risk:
                    raw_alerts.append(
                        f"[ROSTER INJURY] {p.full_name} ({p.position}, {p.team or 'FA'}): "
                        f"{p.injury.status} ({p.injury.body_part or 'Unspecified'})"
                    )

        # Gate alerts by relevance:
        # fires only if player is (a) on user's roster, (b) waiver candidate under discussion, (c) explicitly named.
        # Cap at 2-3 alerts.
        filtered_alerts = filter_relevant_alerts(
            raw_alerts, query=query, user_rostered_names=user_rostered_names
        )

        # Gate notable_role_changes at the pipeline level so irrelevant players don't pollute prompt/context
        notable_role_changes: List[ValidatedPlayer] = []
        if query and query.strip():
            alert_names_in_filtered = set()
            for a in filtered_alerts:
                m = re.match(r"^\[.*?\]\s*([^(]+?)\s*\(", a)
                if m:
                    alert_names_in_filtered.add(m.group(1).strip().lower())

            q_lower_query = f" {query.lower()} "
            for p in raw_notable_role_changes:
                p_last = p.full_name.split()[-1].lower() if p.full_name else ""
                if (
                    p.full_name.lower() in alert_names_in_filtered
                    or p.full_name.lower() in q_lower_query
                    or (len(p_last) >= 4 and f" {p_last} " in q_lower_query)
                ):
                    notable_role_changes.append(p)
        else:
            notable_role_changes = raw_notable_role_changes

        # Cross-check overrides against Sleeper database for conflict warnings
        warnings: List[str] = []
        try:
            from app.sleeper import get_all_players_sync
            sleeper_db = get_all_players_sync()
            if news_feed and sleeper_db:
                warnings.extend(check_all_override_conflicts(news_feed, sleeper_db))
        except Exception as e:
            log.debug("Override conflict cross-check: %s", e)

        # Consolidated provenance
        provenance = [
            nfl_state_prov,
            rosters_prov,
            matchups_prov,
            trending_prov,
            player_ids_prov,
            board_prov,
            news_prov,
            odds_prov,
        ]

        # Deterministic 16-char SHA-256 snapshot_id
        fingerprint_content = (
            f"{nfl_season}:{canonical_week}:{session_id}:"
            f"{user_roster.roster_id if user_roster else 0}:"
            f"{len(top_waivers)}:{odds_prov.status}:{len(notable_role_changes)}"
        )
        snapshot_id = hashlib.sha256(fingerprint_content.encode("utf-8")).hexdigest()[:16]

        snapshot = WorldStateSnapshot(
            snapshot_id=snapshot_id,
            session_id=session_id,
            created_at_utc=now_utc_iso(),
            nfl_season=nfl_season,
            nfl_week=canonical_week,
            nfl_teams_on_bye=teams_on_bye,
            league=league_ctx,
            user_roster=user_roster,
            opponent_roster=opponent_roster,
            all_rosters=all_roster_summaries,
            top_available_waivers=top_waivers,
            trending_players=trending_players,
            notable_role_changes=notable_role_changes,
            current_week_odds=odds_list,
            board_audit_summary=board_summary,
            provenance=provenance,
            active_injury_and_role_alerts=filtered_alerts,
            warnings=warnings,
        )

        return snapshot
