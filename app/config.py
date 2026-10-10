"""Configuration and constants for Fantasy Football AI Assistant."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# League & User Configuration
SLEEPER_LEAGUE_ID: str = os.getenv("SLEEPER_LEAGUE_ID", "1389359956657967104").strip()
LEAGUE_ID: str = SLEEPER_LEAGUE_ID
MY_ROSTER_ID: int = 4  # pinec0ne / Anthony's roster_id
SLEEPER_USER_ID: str = os.getenv("SLEEPER_USER_ID", "pinec0ne").strip()
TEAM_NAME: str = "Tony Romo's DUI Mugshot"

# Survivor Pool Configuration
SLEEPER_SURVIVOR_LEAGUE_ID: str = os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", "1393775027442167808").strip()

# API Keys & Models
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "").strip()

# Email Configuration
GMAIL_SENDER: str = os.getenv("GMAIL_SENDER", "pico2014@gmail.com").strip()
GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "").strip()
SURVIVOR_EMAIL_RECIPIENT: str = "pico0102@gmail.com"

# API Base Endpoints
SLEEPER_BASE: str = "https://api.sleeper.app/v1"
SLEEPER_PROJ_BASE: str = "https://api.sleeper.app/projections/nfl"
GEMINI_INTERACTIONS_URL: str = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Paths
BASE_DIR: Path = Path(__file__).resolve().parent.parent
SKILL_DATA_DIR: Path = BASE_DIR / ".agents" / "skills" / "loser-eats-an-egg" / "data"

# Cache TTLs
CACHE_TTL_SECONDS: int = 300  # 5 minutes
PROJECTIONS_CACHE_TTL: int = 1800  # 30 minutes
LLM_CACHE_TTL: int = 1800  # 30 minutes
