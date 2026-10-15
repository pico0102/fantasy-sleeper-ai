"""Pydantic v2 schemas for all external payloads, internal state, and reasoner contracts."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator


# ── 1. Provenance Records ──────────────────────────────────────────────────
ProvenanceSourceId = Literal[
    "sleeper_nfl_state",
    "sleeper_rosters",
    "sleeper_matchups",
    "sleeper_trending",
    "local_player_ids",
    "local_board_catalog",
    "nfl_news_and_depth_charts",
    "the_odds_api_nfl",
]

ProvenanceSourceType = Literal[
    "sleeper_api",
    "local_catalog",
    "news_depth_chart",
    "odds_api",
]

ProvenanceStatus = Literal["ok", "stale", "degraded", "failed", "missing_key"]


class ProvenanceRecord(BaseModel):
    """Inspectable provenance record for data inputs."""
    source_id: ProvenanceSourceId
    source_type: ProvenanceSourceType
    endpoint_or_path: str
    fetched_at_utc: str
    age_seconds: float = Field(default=0.0, ge=0.0)
    status: ProvenanceStatus
    record_count: int = Field(default=0, ge=0)
    error_detail: Optional[str] = None


# ── 2. Medical & Injury Context ───────────────────────────────────────────
InjuryStatusLiteral = Literal[
    "Active", "Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended", "COV", "Unknown"
]

MEDICAL_RISK_SET = {"Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended", "COV"}
UNSTARTABLE_SET = {"Doubtful", "Out", "IR", "PUP", "Suspended", "COV"}


class InjuryContext(BaseModel):
    """Medical designation context computed strictly from verified status."""
    status: InjuryStatusLiteral = "Active"
    body_part: Optional[str] = None
    practice_status: Optional[str] = None
    injury_notes: Optional[str] = None
    is_medical_risk: bool = False
    is_unstartable_medical: bool = False

    @model_validator(mode="after")
    def compute_risk_flags(self) -> "InjuryContext":
        self.is_medical_risk = self.status in MEDICAL_RISK_SET
        self.is_unstartable_medical = self.status in UNSTARTABLE_SET
        return self


# ── 3. Depth-Chart, Role & News Context ──────────────────────────────────
RoleStatusLiteral = Literal[
    "confirmed_starter", "benched", "backup", "committee", "position_battle", "unknown"
]

ROLE_RISK_SET = {"benched", "backup", "position_battle"}


class RoleAndNewsContext(BaseModel):
    """Depth chart rank, coaching decisions, benchings, and news headlines."""
    depth_chart_position: Optional[str] = None
    depth_chart_rank: Optional[int] = None
    role_status: RoleStatusLiteral = "unknown"
    supplanted_by: Optional[str] = None
    recent_news_headlines: List[str] = Field(default_factory=list)
    news_summary: Optional[str] = None
    last_news_updated_utc: Optional[str] = None
    is_role_risk: bool = False

    @model_validator(mode="after")
    def compute_role_risk(self) -> "RoleAndNewsContext":
        is_risk = self.role_status in ROLE_RISK_SET
        if not is_risk and self.depth_chart_rank is not None:
            # For QBs, rank >= 2 is always an acute role risk in 1QB fantasy
            if self.depth_chart_position == "QB" and self.depth_chart_rank >= 2:
                is_risk = True
            elif self.role_status in {"unknown", "backup"} and self.depth_chart_rank >= 2:
                is_risk = True
        self.is_role_risk = is_risk
        return self


# ── 4. Validated Player Schema ───────────────────────────────────────────
class ValidatedPlayer(BaseModel):
    """Unified player entity validated against catalogs, depth-charts, and news."""
    player_id: str
    full_name: str
    position: str
    team: Optional[str] = None
    bye_week: Optional[int] = None
    injury: InjuryContext = Field(default_factory=InjuryContext)
    role_and_news: RoleAndNewsContext = Field(default_factory=RoleAndNewsContext)
    board_tier: Optional[int] = None
    board_rank: Optional[int] = None
    board_raw_value: Optional[float] = None
    board_bias_adjusted: bool = False
    trending_adds_24h: int = 0
    trending_drops_24h: int = 0


# ── 5. Odds & Survivor Models ─────────────────────────────────────────────
class NFLGameOdds(BaseModel):
    """Live NFL point spread and moneyline from The Odds API."""
    game_id: str
    nfl_week: int
    commence_time_utc: str
    home_team: str
    away_team: str
    favorite_team: str
    underdog_team: str
    spread_points: float
    favorite_moneyline: Optional[int] = None
    underdog_moneyline: Optional[int] = None
    implied_win_prob: float = Field(ge=0.0, le=1.0)
    bookmaker_count: int = Field(default=1, ge=0)


SurvivorPickStatus = Literal["verified_pick", "fail_closed_no_odds", "guardrail_rejected"]


class SurvivorPickResult(BaseModel):
    """Survivor pick recommendation contract shared by chat and weekly email."""
    status: SurvivorPickStatus
    nfl_week: int
    recommended_team: Optional[str] = None
    opponent_team: Optional[str] = None
    spread: Optional[float] = None
    implied_win_prob: Optional[float] = None
    reasoning_markdown: str
    future_value_notes: Optional[str] = None
    odds_provenance: ProvenanceRecord
    snapshot_id: str

    @model_validator(mode="after")
    def enforce_fail_closed_invariants(self) -> "SurvivorPickResult":
        # Hard invariant: recommended_team MUST be None when status != "verified_pick"
        if self.status != "verified_pick" and self.recommended_team is not None:
            self.recommended_team = None
        return self


# ── 6. League & Roster Summaries ──────────────────────────────────────────
class LeagueContext(BaseModel):
    """Sleeper league metadata and scoring rules."""
    league_id: str
    name: str
    season: str
    total_rosters: int
    scoring_settings: Dict[str, Any] = Field(default_factory=dict)
    roster_positions: List[str] = Field(default_factory=list)


class RosterSlotSummary(BaseModel):
    """Aggregated team roster with validated player objects."""
    roster_id: int
    owner_id: str
    owner_name: str
    starters: List[ValidatedPlayer] = Field(default_factory=list)
    bench: List[ValidatedPlayer] = Field(default_factory=list)
    ir: List[ValidatedPlayer] = Field(default_factory=list)
    wins: int = 0
    losses: int = 0
    fpts: float = 0.0


# ── 7. Unified WorldStateSnapshot ─────────────────────────────────────────
class WorldStateSnapshot(BaseModel):
    """Immutable, single-sourced state snapshot for an entire session turn."""
    snapshot_id: str
    session_id: str = "default"
    created_at_utc: str
    nfl_season: str
    nfl_week: int = Field(ge=1, le=18)
    nfl_teams_on_bye: List[str] = Field(default_factory=list)
    league: Optional[LeagueContext] = None
    user_roster: Optional[RosterSlotSummary] = None
    opponent_roster: Optional[RosterSlotSummary] = None
    all_rosters: List[RosterSlotSummary] = Field(default_factory=list)
    top_available_waivers: List[ValidatedPlayer] = Field(default_factory=list)
    trending_players: List[ValidatedPlayer] = Field(default_factory=list)
    notable_role_changes: List[ValidatedPlayer] = Field(default_factory=list)
    current_week_odds: List[NFLGameOdds] = Field(default_factory=list)
    board_audit_summary: Dict[str, Any] = Field(default_factory=dict)
    provenance: List[ProvenanceRecord] = Field(default_factory=list)
    active_injury_and_role_alerts: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ── 8. Request & Response Schemas ─────────────────────────────────────────
class ChatRequest(BaseModel):
    """Incoming user chat query."""
    message: str
    session_id: Optional[str] = "default"
    mode: Literal["fantasy", "survivor"] = "fantasy"
    used_teams: List[str] = Field(default_factory=list)
    force_refresh: bool = False


class ChatResponse(BaseModel):
    """Grounded reasoner output contract."""
    response: str
    mode: str
    snapshot_id: str
    nfl_week: int
    provenance: List[ProvenanceRecord] = Field(default_factory=list)
    injury_alerts: List[str] = Field(default_factory=list)
    citations: List[str] = Field(default_factory=list)
    survivor_result: Optional[SurvivorPickResult] = None
