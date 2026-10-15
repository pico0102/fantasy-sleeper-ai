"""News & Depth-Chart awareness pipeline.

Ingests depth-chart order, starter/benched designations, supplanted_by relationships,
and player news headlines into RoleAndNewsContext.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import NEWS_CACHE_TTL, NEWS_DEPTH_OVERRIDES_PATH, SKILL_DATA_DIR
from core.exceptions import UpstreamSourceError
from core.provenance import build_provenance_record
from core.schemas import InjuryContext, ProvenanceRecord, RoleAndNewsContext, ValidatedPlayer

log = logging.getLogger(__name__)

_NEWS_CACHE: Dict[str, Any] = {}


def _resolve_overrides_path() -> Path:
    if NEWS_DEPTH_OVERRIDES_PATH.exists():
        return NEWS_DEPTH_OVERRIDES_PATH
    fallback = SKILL_DATA_DIR / "news_depth_overrides.json"
    if fallback.exists():
        return fallback
    return NEWS_DEPTH_OVERRIDES_PATH


def load_news_and_depth_chart(
    force_refresh: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], ProvenanceRecord]:
    """Load news and depth-chart overrides/feed with full provenance.

    Returns:
        Tuple of (news_data_dict, ProvenanceRecord).
    """
    now = datetime.now(timezone.utc).timestamp()
    cached = _NEWS_CACHE.get("news_data")
    if (
        not force_refresh
        and cached
        and (now - cached["timestamp"]) < NEWS_CACHE_TTL
    ):
        return cached["data"], cached["provenance"]

    path = _resolve_overrides_path()
    if not path.exists():
        prov = build_provenance_record(
            source_id="nfl_news_and_depth_charts",
            source_type="news_depth_chart",
            endpoint_or_path=str(path),
            status="missing_key",
            record_count=0,
            error_detail=f"News overrides file not found at {path}",
        )
        return {}, prov

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise UpstreamSourceError(
                f"Expected dictionary in {path}, got {type(data)}",
                source_id="nfl_news_and_depth_charts",
            )

        prov = build_provenance_record(
            source_id="nfl_news_and_depth_charts",
            source_type="news_depth_chart",
            endpoint_or_path=str(path),
            status="ok",
            record_count=len(data),
        )
        _NEWS_CACHE["news_data"] = {
            "timestamp": now,
            "data": data,
            "provenance": prov,
        }
        return data, prov

    except Exception as e:
        prov = build_provenance_record(
            source_id="nfl_news_and_depth_charts",
            source_type="news_depth_chart",
            endpoint_or_path=str(path),
            status="failed",
            record_count=0,
            error_detail=str(e),
        )
        return {}, prov


def check_override_sleeper_conflicts(
    player_name: str,
    override_entry: Dict[str, Any],
    sleeper_data: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Detect discrepancies between a news/depth override and Sleeper metadata.

    Flags conflicts when:
    - Team in override differs from Sleeper team (e.g. LV vs ATL).
    - Injury status in override differs from Sleeper injury_status (e.g. Out vs Questionable/Active).
    - Injury body part differs (e.g. Thumb vs Shoulder).
    - Depth chart rank / role differs (e.g. starter in Sleeper vs benched in override).

    Returns:
        List of conflict warning strings.
    """
    conflicts: List[str] = []
    if not sleeper_data or not isinstance(sleeper_data, dict):
        return conflicts

    name = override_entry.get("full_name", player_name)

    # 1. Team check
    ov_team = (override_entry.get("team") or "").strip().upper()
    sl_team = (sleeper_data.get("team") or "").strip().upper()
    if ov_team and sl_team and ov_team != sl_team:
        conflicts.append(
            f"[CONFLICT: TEAM] {name}: override team '{ov_team}' conflicts with Sleeper team '{sl_team}'"
        )

    # 2. Injury status check
    ov_inj = override_entry.get("injury") or {}
    ov_status = ov_inj.get("status") if isinstance(ov_inj, dict) else None
    sl_status = sleeper_data.get("injury_status") or "Active"
    if ov_status:
        norm_ov_status = ov_status.strip().title()
        norm_sl_status = sl_status.strip().title() if sl_status else "Active"
        if norm_ov_status != norm_sl_status:
            conflicts.append(
                f"[CONFLICT: INJURY_STATUS] {name}: override status '{norm_ov_status}' conflicts with Sleeper status '{norm_sl_status}'"
            )

    # 3. Injury body part check
    ov_part = ov_inj.get("body_part") if isinstance(ov_inj, dict) else None
    sl_part = sleeper_data.get("injury_body_part")
    if ov_part and sl_part and ov_part.strip().lower() != sl_part.strip().lower():
        conflicts.append(
            f"[CONFLICT: INJURY_BODY_PART] {name}: override body part '{ov_part}' conflicts with Sleeper body part '{sl_part}'"
        )

    # 4. Depth chart rank / role check
    ov_rank = override_entry.get("depth_chart_rank")
    sl_order = sleeper_data.get("depth_chart_order")
    ov_role = override_entry.get("role_status")
    if ov_rank is not None and sl_order is not None and int(ov_rank) != int(sl_order):
        conflicts.append(
            f"[CONFLICT: DEPTH_CHART] {name}: override rank {ov_rank} ({ov_role}) conflicts with Sleeper depth order {sl_order}"
        )

    return conflicts


def check_all_override_conflicts(
    overrides: Dict[str, Dict[str, Any]],
    sleeper_players_db: Dict[str, Any],
) -> List[str]:
    """Cross-check all override entries against a Sleeper players dictionary.

    Returns:
        List of all conflict strings detected across all players.
    """
    all_conflicts: List[str] = []
    if not overrides or not sleeper_players_db:
        return all_conflicts

    by_id: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for pid, pdata in sleeper_players_db.items():
        if isinstance(pdata, dict):
            by_id[str(pid)] = pdata
            fname = pdata.get("full_name") or f"{pdata.get('first_name', '')} {pdata.get('last_name', '')}".strip()
            if fname:
                by_name[fname.lower()] = pdata

    for key_name, entry in overrides.items():
        pid = str(entry.get("player_id", ""))
        sl_player = by_id.get(pid) or by_name.get(key_name.lower()) or by_name.get(entry.get("full_name", "").lower())
        if sl_player:
            c = check_override_sleeper_conflicts(key_name, entry, sl_player)
            all_conflicts.extend(c)

    return all_conflicts


def hydrate_player_role_and_news(
    player_name: str,
    sleeper_player_dict: Optional[Dict[str, Any]] = None,
    news_feed: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[RoleAndNewsContext, Optional[InjuryContext]]:
    """Hydrate role and news context for a player from news feed or Sleeper metadata.

    Returns:
        Tuple of (RoleAndNewsContext, optional overridden InjuryContext).
    """
    feed = news_feed
    if feed is None:
        feed, _ = load_news_and_depth_chart()

    # Case-insensitive lookup in feed
    matched_entry: Optional[Dict[str, Any]] = None
    target_norm = player_name.strip().lower()

    for name_key, entry in feed.items():
        if name_key.strip().lower() == target_norm:
            matched_entry = entry
            break

    if matched_entry:
        injury_override = None
        if "injury" in matched_entry and isinstance(matched_entry["injury"], dict):
            injury_override = InjuryContext(**matched_entry["injury"])

        conflicts = check_override_sleeper_conflicts(
            player_name=player_name,
            override_entry=matched_entry,
            sleeper_data=sleeper_player_dict,
        )

        role_ctx = RoleAndNewsContext(
            depth_chart_position=matched_entry.get("depth_chart_position"),
            depth_chart_rank=matched_entry.get("depth_chart_rank"),
            role_status=matched_entry.get("role_status", "unknown"),
            supplanted_by=matched_entry.get("supplanted_by"),
            recent_news_headlines=matched_entry.get("recent_news_headlines", []),
            news_summary=matched_entry.get("news_summary"),
            last_news_updated_utc=matched_entry.get("last_news_updated_utc"),
            override_conflicts=conflicts,
        )
        return role_ctx, injury_override

    # Fallback to Sleeper metadata if available
    if sleeper_player_dict:
        depth_order = sleeper_player_dict.get("depth_chart_order")
        pos = sleeper_player_dict.get("position")
        role_status = "confirmed_starter" if depth_order == 1 else ("backup" if depth_order and depth_order > 1 else "unknown")
        role_ctx = RoleAndNewsContext(
            depth_chart_position=sleeper_player_dict.get("depth_chart_position") or pos,
            depth_chart_rank=depth_order,
            role_status=role_status,
            recent_news_headlines=[],
            news_summary=sleeper_player_dict.get("injury_notes"),
            last_news_updated_utc=sleeper_player_dict.get("news_updated"),
        )
        return role_ctx, None

    return RoleAndNewsContext(), None


def get_notable_role_players(
    news_feed: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[ValidatedPlayer]:
    """Return list of players with active benchings, promotions, or notable news."""
    feed = news_feed
    if feed is None:
        feed, _ = load_news_and_depth_chart()

    players: List[ValidatedPlayer] = []
    for name, entry in feed.items():
        role_ctx, injury_override = hydrate_player_role_and_news(name, news_feed=feed)
        injury = injury_override or InjuryContext()
        player = ValidatedPlayer(
            player_id=str(entry.get("player_id", name)),
            full_name=entry.get("full_name", name),
            position=entry.get("position", entry.get("depth_chart_position", "QB")),
            team=entry.get("team"),
            injury=injury,
            role_and_news=role_ctx,
        )
        players.append(player)

    return players
