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

from core.board_auditor import normalize_player_name
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
    run_startup_catalog_sanity_check,
)
from core.provenance import now_utc_iso
from core.schemas import (
    InjuryContext,
    LeagueContext,
    NFLGameOdds,
    ProvenanceRecord,
    RoleAndNewsContext,
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

VALID_FANTASY_POSITIONS: Set[str] = {"QB", "RB", "WR", "TE", "K", "DEF"}

SEASON_ENDING_OR_IR_MARKERS = (
    "out for season",
    "out for the season",
    "season-ending",
    "season ending",
    "season over",
    "torn acl",
    "placed on ir",
    "placed on injured reserve",
    "injured reserve",
    "sidelined until",
)


def is_season_ending_or_ir_injury(
    injury_or_player: Any,
    role_and_news: Optional[RoleAndNewsContext] = None,
) -> bool:
    """Return True if player has an unstartable/IR/Out designation or season-ending injury."""
    if isinstance(injury_or_player, ValidatedPlayer):
        injury = injury_or_player.injury
        role_and_news = role_and_news or injury_or_player.role_and_news
    else:
        injury = injury_or_player

    if injury.is_unstartable_medical or injury.status in {
        "Doubtful", "Out", "IR", "PUP", "Suspended", "COV"
    }:
        return True

    text_fragments: List[str] = []
    if getattr(injury, "injury_notes", None):
        text_fragments.append(injury.injury_notes.lower())
    if getattr(injury, "details", None):
        text_fragments.append(injury.details.lower())
    if role_and_news:
        if role_and_news.news_summary:
            text_fragments.append(role_and_news.news_summary.lower())
        for hl in role_and_news.recent_news_headlines:
            text_fragments.append(hl.lower())

    combined = " ".join(text_fragments)
    if any(marker in combined for marker in SEASON_ENDING_OR_IR_MARKERS):
        return True
    return False


def is_eligible_waiver_candidate(
    player: ValidatedPlayer,
    rostered_player_ids: Set[str],
    rostered_player_names: Optional[Set[str]] = None,
) -> bool:
    """Hard deterministic pipeline filter for waiver/FAAB candidates (modeled on SurvivorEngine).

    Excludes:
    1. Non-fantasy positions (IDP/OL/invalid).
    2. Any player on any league roster (by player_id or normalized full_name).
    3. Any player with IR, Out, PUP, Doubtful, Suspended, or season-ending injury status.
    4. Any player who has been officially benched/demoted (role_status == 'benched').
    """
    if player.position not in VALID_FANTASY_POSITIONS:
        return False

    if str(player.player_id) in rostered_player_ids:
        return False

    norm_name = normalize_player_name(player.full_name)
    if not norm_name or norm_name.startswith("player "):
        return False

    if rostered_player_names and norm_name in rostered_player_names:
        return False

    if is_season_ending_or_ir_injury(player.injury, player.role_and_news):
        return False

    if player.role_and_news.role_status == "benched":
        return False

    return True


def filter_waiver_candidates(
    candidates: List[ValidatedPlayer],
    rostered_player_ids: Set[str],
    rostered_player_names: Optional[Set[str]] = None,
) -> List[ValidatedPlayer]:
    """Filter and deduplicate candidate players through the hard waiver eligibility gate."""
    eligible: List[ValidatedPlayer] = []
    seen_ids: Set[str] = set()
    seen_names: Set[str] = set()

    for p in candidates:
        pid = str(p.player_id)
        norm = normalize_player_name(p.full_name)
        if pid in seen_ids or (norm and norm in seen_names):
            continue
        if is_eligible_waiver_candidate(p, rostered_player_ids, rostered_player_names):
            eligible.append(p)
            seen_ids.add(pid)
            if norm:
                seen_names.add(norm)
    return eligible


def filter_eligible_starters(
    players: List[ValidatedPlayer],
    position: Optional[str] = None,
    teams_on_bye: Optional[List[str]] = None,
) -> List[ValidatedPlayer]:
    """Hard deterministic pipeline filter for starting lineup candidates on a roster.

    Excludes unstartable medical statuses (Out, IR, PUP, Doubtful, Suspended) and benched players,
    filters by position (or RB/WR/TE for FLEX), and ranks active non-bye players first.
    """
    bye_set = {t.upper() for t in (teams_on_bye or [])}
    eligible: List[ValidatedPlayer] = []
    seen_ids: Set[str] = set()

    for p in players:
        if str(p.player_id) in seen_ids:
            continue
        if is_season_ending_or_ir_injury(p.injury, p.role_and_news):
            continue
        if p.role_and_news.role_status == "benched":
            continue
        if position:
            pos_upper = position.upper()
            if pos_upper == "FLEX":
                if p.position not in {"RB", "WR", "TE"}:
                    continue
            elif p.position != pos_upper:
                continue
        eligible.append(p)
        seen_ids.add(str(p.player_id))

    eligible.sort(
        key=lambda p: (
            1 if (p.team and p.team.upper() in bye_set) else 0,
            p.board_tier if p.board_tier is not None else 99,
            -(p.board_raw_value if p.board_raw_value is not None else 0.0),
        )
    )
    return eligible


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
    q_norm = f" {normalize_player_name(query_text)} "
    found: List[Tuple[str, str]] = []
    seen_ids: Set[str] = set()
    seen_names: Set[str] = set()

    for pid, name in player_ids_map.items():
        name_lower = name.lower()
        name_norm = normalize_player_name(name)
        if not name_norm:
            continue

        # Direct full name match (raw or normalized)
        if name_lower in q_lower or f" {name_norm} " in q_norm:
            if pid not in seen_ids and name_norm not in seen_names:
                found.append((pid, name))
                seen_ids.add(pid)
                seen_names.add(name_norm)
            continue

        # Last name match for distinct names (>= 5 chars)
        parts = name_norm.split()
        if len(parts) >= 2:
            last = parts[-1]
            if len(last) >= 5 and (f" {last} " in q_lower or f" {last} " in q_norm):
                if pid not in seen_ids and name_norm not in seen_names:
                    found.append((pid, name))
                    seen_ids.add(pid)
                    seen_names.add(name_norm)

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
        board_by_name: Dict[str, Dict[str, Any]] = {}
        for b in board_data:
            if "name" in b:
                board_by_name[b["name"].strip().lower()] = b
                board_by_name[normalize_player_name(b["name"])] = b

        # Build reverse name -> all matching player_ids map (normalized + raw)
        name_to_pids: Dict[str, List[str]] = {}
        for pid, p_name in player_ids_map.items():
            for key in {p_name.strip().lower(), normalize_player_name(p_name)}:
                if key:
                    name_to_pids.setdefault(key, []).append(str(pid))

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

        # 7. Users map (include team_name if configured so matchup previews name both team & owner)
        try:
            users_list = await self.sleeper_client.fetch_users(force_refresh=force_refresh)
            users_map: Dict[str, str] = {}
            for u in users_list:
                uid = u.get("user_id")
                if not uid:
                    continue
                disp = (u.get("display_name") or "").strip()
                meta = u.get("metadata") or {}
                t_name = (meta.get("team_name") or "").strip() if isinstance(meta, dict) else ""
                if t_name and disp and t_name.lower() != disp.lower():
                    users_map[uid] = f"{t_name} ({disp})"
                else:
                    users_map[uid] = t_name or disp
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
            raw_db = get_all_players_sync()
            if isinstance(raw_db, dict):
                sleeper_db = dict(raw_db)
        except Exception:
            sleeper_db = {}

        # Pre-collect all rostered player IDs across every league roster (starters, bench, reserve/IR, taxi)
        rostered_player_ids: Set[str] = set()
        for r in rosters_raw:
            for slot_key in ("players", "starters", "reserve", "taxi"):
                for pid in (r.get(slot_key) or []):
                    if pid and str(pid) != "0":
                        rostered_player_ids.add(str(pid))

        # Hydrate live Sleeper metadata for candidate & roster players if sleeper_db is missing entries
        if hasattr(self.sleeper_client, "fetch_players_batch"):
            ids_to_hydrate: List[str] = []
            for r in rosters_raw:
                if int(r.get("roster_id", 0)) == MY_ROSTER_ID:
                    ids_to_hydrate.extend(str(p) for p in (r.get("players") or []) if p)
            for b in board_data[:75]:
                b_norm = normalize_player_name(b.get("name", ""))
                for pid in name_to_pids.get(b_norm, []):
                    if pid not in rostered_player_ids:
                        ids_to_hydrate.append(pid)
            for t in trending_raw[:25]:
                t_pid = str(t.get("player_id", ""))
                if t_pid:
                    ids_to_hydrate.append(t_pid)
            if query:
                for pid, _ in scan_mentioned_players(query, player_ids_map):
                    ids_to_hydrate.append(str(pid))
            missing_ids = [
                pid for pid in dict.fromkeys(ids_to_hydrate)
                if pid and pid.isdigit() and pid not in sleeper_db
            ]
            if missing_ids:
                try:
                    fetched_batch = await self.sleeper_client.fetch_players_batch(
                        missing_ids[:60], force_refresh=force_refresh
                    )
                    if isinstance(fetched_batch, dict):
                        sleeper_db.update(fetched_batch)
                except Exception as e:
                    log.debug("Batch player hydration skipped: %s", e)

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

            # Determine medical status (combine Sleeper live status with overrides)
            status_lit = "Active"
            sl_inj = sl_info.get("injury_status")
            sl_body = sl_info.get("injury_body_part")
            sl_notes = sl_info.get("injury_notes")
            if inj_override:
                inj_ctx = inj_override
                # If Sleeper reports a more severe status (IR/Out/PUP) than an Active override, honor Sleeper
                if inj_ctx.status == "Active" and sl_inj in {"IR", "Out", "PUP", "Suspended", "Doubtful", "Questionable"}:
                    inj_ctx = InjuryContext(
                        status=sl_inj,
                        body_part=sl_body or inj_ctx.body_part,
                        details=sl_notes or inj_ctx.details,
                        source="sleeper_api",
                    )
            else:
                raw_inj = injury_hint or sl_inj
                if raw_inj:
                    status_lit = raw_inj if raw_inj in {
                        "Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended", "COV"
                    } else "Active"
                inj_ctx = InjuryContext(
                    status=status_lit,
                    body_part=sl_body,
                    details=sl_notes,
                    source="sleeper_api" if sl_inj else "default",
                )

            # Board lookup (support normalized name matching)
            norm_full = normalize_player_name(full_name)
            board_entry = (
                board_by_name.get(full_name.strip().lower())
                or board_by_name.get(norm_full)
                or {}
            )
            position = (
                pos_hint
                or board_entry.get("pos")
                or role_ctx.depth_chart_position
                or sl_info.get("position")
                or "FLEX"
            )
            team = team_hint or board_entry.get("team") or sl_info.get("team")
            known_pos_overrides = {
                "matt gay": ("K", "IND"),
                "devaughn vele": ("WR", "DEN"),
                "harold fannin": ("TE", "CLE"),
                "dallas cowboys": ("DEF", "DAL"),
                "seattle seahawks": ("DEF", "SEA"),
            }
            if position == "FLEX" and full_name.strip().lower() in known_pos_overrides:
                override_pos, override_team = known_pos_overrides[full_name.strip().lower()]
                position = override_pos
                team = team or override_team
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
        rostered_player_names: Set[str] = set()
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

            all_slot_pids = list(dict.fromkeys(list(raw_players) + list(raw_starters) + list(raw_reserve)))
            for pid in all_slot_pids:
                if not pid or str(pid) == "0":
                    continue
                pid_str = str(pid)
                rostered_player_ids.add(pid_str)
                p_obj = build_player(pid_str)
                rostered_player_names.add(normalize_player_name(p_obj.full_name))
                rostered_player_names.add(p_obj.full_name.strip().lower())
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

        # Process top available waivers with hard pipeline filter (analogous to SurvivorEngine)
        raw_board_waivers: List[ValidatedPlayer] = []
        for b in board_data:
            b_name = b.get("name", "")
            b_norm = normalize_player_name(b_name)
            if not b_norm or b_norm in rostered_player_names:
                continue

            candidate_pids = name_to_pids.get(b_norm) or name_to_pids.get(b_name.strip().lower()) or []
            if not candidate_pids:
                continue
            # If ANY mapped ID for this player is rostered in the league, exclude immediately
            if any(pid in rostered_player_ids for pid in candidate_pids):
                continue

            matched_id = candidate_pids[0]
            p_obj = build_player(
                matched_id,
                name_hint=b_name,
                pos_hint=b.get("pos"),
                team_hint=b.get("team"),
            )
            raw_board_waivers.append(p_obj)

        top_waivers: List[ValidatedPlayer] = filter_waiver_candidates(
            raw_board_waivers,
            rostered_player_ids=rostered_player_ids,
            rostered_player_names=rostered_player_names,
        )[:30]

        # Process trending adds and apply the exact same hard waiver eligibility filter
        raw_trending_players: List[ValidatedPlayer] = []
        for t in trending_raw[:25]:
            pid = str(t.get("player_id", ""))
            if not pid:
                continue
            count = int(t.get("count", 0))
            p_obj = build_player(pid)
            p_obj.trending_adds_24h = count
            raw_trending_players.append(p_obj)

        trending_players: List[ValidatedPlayer] = filter_waiver_candidates(
            raw_trending_players,
            rostered_player_ids=rostered_player_ids,
            rostered_player_names=rostered_player_names,
        )[:20]

        # Merge high-signal unrostered trending players (e.g. C.J. Stroud) into top_waivers if not already present
        existing_waiver_ids = {p.player_id for p in top_waivers}
        existing_waiver_names = {normalize_player_name(p.full_name) for p in top_waivers}
        for tp in trending_players:
            tp_norm = normalize_player_name(tp.full_name)
            if (
                tp.player_id not in existing_waiver_ids
                and tp_norm not in existing_waiver_names
                and not tp.full_name.startswith("Player ")
                and tp.position in {"QB", "RB", "WR", "TE", "K", "DEF"}
            ):
                top_waivers.append(tp)
                existing_waiver_ids.add(tp.player_id)
                existing_waiver_names.add(tp_norm)

        # Notable role changes (seed from news feed)
        raw_notable_role_changes: List[ValidatedPlayer] = get_notable_role_players(news_feed=news_feed)

        # Entity pre-lookup: scan query for mentioned players and hydrate into raw_notable_role_changes
        mentioned_names_lower: Set[str] = set()
        if query:
            mentioned = scan_mentioned_players(query, player_ids_map)
            existing_names = {normalize_player_name(p.full_name) for p in raw_notable_role_changes}
            for pid, name in mentioned:
                mentioned_names_lower.add(name.lower())
                mentioned_names_lower.add(normalize_player_name(name))
                if normalize_player_name(name) not in existing_names:
                    p_obj = build_player(pid, name_hint=name)
                    raw_notable_role_changes.append(p_obj)
                    existing_names.add(normalize_player_name(name))

        user_rostered_names: Set[str] = set()
        if user_roster:
            for p in user_roster.starters + user_roster.bench + user_roster.ir:
                user_rostered_names.add(p.full_name.lower())
                user_rostered_names.add(normalize_player_name(p.full_name))

        # Compile active injury and role alerts in clean natural language
        raw_alerts: List[str] = []
        for p in raw_notable_role_changes:
            p_norm = normalize_player_name(p.full_name)
            if p.role_and_news.is_role_risk:
                supplanted_str = f" behind starter {p.role_and_news.supplanted_by}" if p.role_and_news.supplanted_by else ""
                raw_alerts.append(
                    f"[BENCHING/ROLE RISK] {p.full_name} ({p.position}, {p.team or 'FA'}): "
                    f"Demoted to backup{supplanted_str}"
                )
            if p.injury.is_medical_risk and (
                p_norm in user_rostered_names
                or p.full_name.lower() in user_rostered_names
                or p_norm in mentioned_names_lower
                or p.full_name.lower() in mentioned_names_lower
                or not query
            ):
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

        # Cross-check overrides and catalogs against Sleeper database for sanity & conflict warnings
        warnings: List[str] = []
        try:
            sanity_report = run_startup_catalog_sanity_check(
                player_ids_map=player_ids_map,
                board_data=board_data,
                news_feed=news_feed,
                sleeper_players=sleeper_db,
            )
            board_summary = dict(board_summary)
            board_summary["catalog_sanity"] = {
                "status": sanity_report["status"],
                "duplicate_player_id_names_count": sanity_report["duplicate_player_id_names_count"],
                "unmapped_board_players_count": sanity_report["unmapped_board_players_count"],
                "board_entries_missing_injury_metadata_count": sanity_report["board_entries_missing_injury_metadata_count"],
                "overrides_missing_injury_metadata_count": sanity_report["overrides_missing_injury_metadata_count"],
                "sleeper_override_conflicts_count": sanity_report["sleeper_override_conflicts_count"],
            }
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
