import os
import requests
import json
import logging
import textwrap
from google import genai
import time

log = logging.getLogger(__name__)

SLEEPER_SURVIVOR_LEAGUE_ID = os.getenv("SLEEPER_SURVIVOR_LEAGUE_ID", "1393775027442167808")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

SURVIVOR_PROMPT = textwrap.dedent("""\
    You are an expert game theory analyst and NFL sports bettor managing a Sleeper Survivor pool ("Fat Crew Survivor").
    The user's goal is to win the entire pool. They need to survive this week, but also keep elite teams available for future weeks.
    
    ### Strategy
    - If there is an obvious safe pick with low future value, take them.
    - If the public is heavily heavily concentrated on one team (high ownership), suggest a pivot to a team with similar win probability to gain +EV.
    - Warn the user if they are considering picking a team that they will desperately need in a future week.
    
    ### Task
    - Recommend the single best pick for this week.
    - Give an alternate contrarian pick.
    - Include a confidence percentage for each pick (based on Vegas odds).
    - Keep formatting clean, scannable, and mobile-friendly.
""")

def get_survivor_state(league_id: str) -> dict:
    url = f"https://api.sleeper.app/v1/league/{league_id}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        league_data = resp.json()
        current_leg = league_data.get("settings", {}).get("leg", 1)
        return {"current_week": current_leg, "league_name": league_data.get("name", "Survivor")}
    except Exception as e:
        log.warning(f"Failed to fetch survivor state: {e}")
        return {"current_week": "Unknown", "league_name": "Survivor"}

def get_vegas_odds() -> list:
    if not ODDS_API_KEY or ODDS_API_KEY == "your-odds-api-key-here":
        return [{"home": "BUF", "away": "JAX", "spread": "-5.5", "home_win_prob": "72%"},
                {"home": "CIN", "away": "WAS", "spread": "-7.5", "home_win_prob": "78%"},
                {"home": "SEA", "away": "MIA", "spread": "-4.5", "home_win_prob": "68%"}]
    
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads&oddsFormat=american"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        games = []
        for g in data[:16]:
            games.append({
                "home": g["home_team"],
                "away": g["away_team"],
                "start": g["commence_time"]
            })
        return games
    except Exception as e:
        log.warning(f"Failed to fetch odds: {e}")
        return []

def build_survivor_context() -> str:
    state = get_survivor_state(SLEEPER_SURVIVOR_LEAGUE_ID)
    odds = get_vegas_odds()
    return f"Week: {state['current_week']}\nOdds/Matchups: {json.dumps(odds, indent=2)}\n"

def ask_survivor_gemini(query: str, api_key: str) -> str:
    client = genai.Client(api_key=api_key)
    context = build_survivor_context()
    prompt = (
        f"Live Survivor Data:\n<data>\n{context}\n</data>\n\n"
        f"Answer this question for the user:\n<user_question>\n{query}\n</user_question>"
    )
    models = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    last_error = None
    for model_name in models:
        try:
            interaction = client.interactions.create(
                model=model_name,
                input=prompt,
                system_instruction=SURVIVOR_PROMPT,
                store=False,
            )
            if interaction.output_text:
                return interaction.output_text
        except Exception as e:
            last_error = e
            time.sleep(1.0)
    return f"Survivor engine error: {last_error}"
