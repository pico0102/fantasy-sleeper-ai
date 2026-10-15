"""Configuration, environment variables, paths, and core templates for v2."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Model defaults
DEFAULT_GEMINI_MODEL: str = "gemini-3.8-flash"
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()

# League & User Configuration
SLEEPER_LEAGUE_ID: str = os.getenv("SLEEPER_LEAGUE_ID", "1389359956657967104").strip()
LEAGUE_ID: str = SLEEPER_LEAGUE_ID
MY_ROSTER_ID: int = int(os.getenv("MY_ROSTER_ID", "4"))
SLEEPER_USER_ID: str = os.getenv("SLEEPER_USER_ID", "pinec0ne").strip()
TEAM_NAME: str = "Tony Romo's DUI Mugshot"

# Survivor Pool Configuration
SLEEPER_SURVIVOR_LEAGUE_ID: str = os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", "1393775027442167808").strip()
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "").strip()

# Email Configuration
GMAIL_SENDER: str = os.getenv("GMAIL_SENDER", "pico2014@gmail.com").strip()
GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "").strip()
SURVIVOR_EMAIL_RECIPIENT: str = os.getenv("SURVIVOR_EMAIL_RECIPIENT", "pico0102@gmail.com").strip()

# API Base Endpoints
SLEEPER_BASE: str = "https://api.sleeper.app/v1"
SLEEPER_PROJ_BASE: str = "https://api.sleeper.app/projections/nfl"
ODDS_API_BASE: str = "https://api.the-odds-api.com/v4"

# Paths
BASE_DIR: Path = Path(__file__).resolve().parent.parent
PLAYER_IDS_PATH: Path = BASE_DIR / "player_ids.json"
BOARD_PATH: Path = BASE_DIR / "board.json"
NEWS_DEPTH_OVERRIDES_PATH: Path = BASE_DIR / "news_depth_overrides.json"
SKILL_DATA_DIR: Path = BASE_DIR / ".agents" / "skills" / "loser-eats-an-egg" / "data"

# Cache TTLs
CACHE_TTL_SECONDS: int = 300       # 5 minutes for general sleeper state
NEWS_CACHE_TTL: int = 900          # 15 minutes for depth charts and news
ODDS_CACHE_TTL: int = 900          # 15 minutes for odds data
PROJECTIONS_CACHE_TTL: int = 1800  # 30 minutes
LLM_CACHE_TTL: int = 1800          # 30 minutes

# Survivor Fail-Closed Refusal Template (exact required deterministic string)
SURVIVOR_FAIL_CLOSED_TEMPLATE: str = (
    "SURVIVOR MODE UNAVAILABLE (FAIL-CLOSED): Verified live NFL odds from The Odds API are currently unavailable "
    "[status={status}, source=the_odds_api_nfl]. To prevent unverified or fabricated point spreads, Survivor pick "
    "generation is disabled until live odds provenance is restored."
)

# Non-negotiable Reasoner Mandates
ANTI_DEFLECTION_MANDATE: str = "If structured data is missing, reason from available context and state what is missing; never deflect."
PREMISE_VERIFICATION_MANDATE: str = "Never accept a user's premise about bye weeks, injuries, depth-chart starter status, or matchups at face value. Verify every premise against `WorldStateSnapshot` first, and if the user's premise is false, correct it explicitly in your first sentence."
NEWS_BENCHING_MANDATE: str = "Sleeper medical status alone does not mean a player is starting. Always check `role_and_news` (`role_status`, `depth_chart_rank`, `supplanted_by`, `recent_news_headlines`). Never recommend picking up or starting a benched player based on historical points or static board tiers."
PROVENANCE_CITATION_MANDATE: str = "Ground every claim in the provided `WorldStateSnapshot` and cite the corresponding `source_id`."
