import os
import json
import logging
import textwrap
from google import genai
import time
import httpx
import asyncio
from datetime import datetime, timezone

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
    
    ### Anti-Hallucination Rules
    - If odds data is unavailable or missing, say so explicitly.
    - Never invent spread numbers, moneyline odds, or win probabilities.
    - Always cite the source of odds data provided in the context.
    - Ensure your recommendations strictly avoid teams the user has already picked in previous weeks (if previous picks are provided).
    
    ### Task
    - Recommend the single best pick for this week.
    - Give an alternate contrarian pick.
    - Include a confidence percentage for each pick (based on Vegas odds).
    - Keep formatting clean, scannable, and mobile-friendly.
""")

async def get_survivor_state(league_id: str) -> dict:
    url = f"https://api.sleeper.app/v1/league/{league_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            league_data = resp.json()
            current_leg = league_data.get("settings", {}).get("leg", 1)
            return {"current_week": current_leg, "league_name": league_data.get("name", "Survivor")}
    except Exception as e:
        log.warning(f"Failed to fetch survivor state: {e}")
        return {"current_week": 1, "league_name": "Survivor"}

async def get_previous_picks(league_id: str, current_week: int) -> list:
    picks = []
    async with httpx.AsyncClient() as client:
        for w in range(1, current_week):
            url = f"https://api.sleeper.app/v1/league/{league_id}/matchups/{w}"
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    matchups = resp.json()
                    week_picks = []
                    for m in matchups:
                        if m.get("pick"):
                            week_picks.append({"roster_id": m.get("roster_id"), "pick": m.get("pick")})
                    if week_picks:
                        picks.append({"week": w, "picks": week_picks})
            except Exception as e:
                log.warning(f"Failed to fetch previous picks for week {w}: {e}")
    return picks

async def get_vegas_odds() -> dict:
    data_source = {
        "status": "ok",
        "error": None,
        "fetched_at": datetime.now(timezone.utc).isoformat()
    }
    games = []

    if not ODDS_API_KEY or ODDS_API_KEY == "your-odds-api-key-here":
        data_source["status"] = "error"
        data_source["error"] = "ODDS_API_KEY not set"
        return {"games": games, "data_source": data_source}
    
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads&oddsFormat=american"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            
            for g in data:
                home = g["home_team"]
                away = g["away_team"]
                start = g["commence_time"]
                
                spread = None
                moneyline_home = None
                moneyline_away = None
                
                bookmakers = g.get("bookmakers", [])
                for bm in bookmakers:
                    for m in bm.get("markets", []):
                        if m["key"] == "spreads" and spread is None:
                            for out in m.get("outcomes", []):
                                if out["name"] == home:
                                    spread = out.get("point")
                        elif m["key"] == "h2h" and (moneyline_home is None or moneyline_away is None):
                            for out in m.get("outcomes", []):
                                if out["name"] == home:
                                    moneyline_home = out.get("price")
                                elif out["name"] == away:
                                    moneyline_away = out.get("price")
                
                home_prob = None
                away_prob = None
                
                if moneyline_home is not None:
                    if moneyline_home < 0:
                        home_prob = abs(moneyline_home) / (abs(moneyline_home) + 100)
                    else:
                        home_prob = 100 / (moneyline_home + 100)
                
                if moneyline_away is not None:
                    if moneyline_away < 0:
                        away_prob = abs(moneyline_away) / (abs(moneyline_away) + 100)
                    else:
                        away_prob = 100 / (moneyline_away + 100)
                
                # fallback to spread for probability if moneyline is missing
                if home_prob is None and spread is not None:
                    home_prob = 1 / (1 + 10**(spread/7))
                    away_prob = 1 - home_prob

                games.append({
                    "home": home,
                    "away": away,
                    "start": start,
                    "spread_home": spread,
                    "moneyline_home": moneyline_home,
                    "moneyline_away": moneyline_away,
                    "implied_prob_home": round(home_prob, 3) if home_prob is not None else None,
                    "implied_prob_away": round(away_prob, 3) if away_prob is not None else None
                })
                
    except Exception as e:
        log.warning(f"Failed to fetch odds: {e}")
        data_source["status"] = "error"
        data_source["error"] = str(e)
        
    return {"games": games, "data_source": data_source}

async def build_survivor_recommendation(api_key: str, custom_prompt: str = None) -> dict:
    state = await get_survivor_state(SLEEPER_SURVIVOR_LEAGUE_ID)
    current_week = state.get("current_week", 1)
    
    odds_task = asyncio.create_task(get_vegas_odds())
    picks_task = asyncio.create_task(get_previous_picks(SLEEPER_SURVIVOR_LEAGUE_ID, current_week))
    
    odds_data, previous_picks = await asyncio.gather(odds_task, picks_task)
    
    context = (
        f"League: {state.get('league_name')}\n"
        f"Current Week: {current_week}\n"
        f"Data Sources: {json.dumps(odds_data.get('data_source'))}\n"
        f"Odds/Matchups: {json.dumps(odds_data.get('games'), indent=2)}\n"
        f"Previous Picks in League: {json.dumps(previous_picks, indent=2)}\n"
    )
    
    client = genai.Client(api_key=api_key)
    
    question = custom_prompt if custom_prompt else "Please provide your survivor recommendation for this week."
    
    prompt = (
        f"Live Survivor Data:\n<data>\n{context}\n</data>\n\n"
        f"Answer this request:\n<user_question>\n{question}\n</user_question>"
    )
    
    models = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    last_error = None
    output_text = ""
    
    for model_name in models:
        try:
            interaction = client.interactions.create(
                model=model_name,
                input=prompt,
                system_instruction=SURVIVOR_PROMPT,
                store=False,
            )
            if interaction.output_text:
                output_text = interaction.output_text
                break
        except Exception as e:
            last_error = e
            await asyncio.sleep(1.0)
            
    if not output_text:
        output_text = f"Survivor engine error: {last_error}"
        
    return {
        "recommendation": output_text,
        "data_sources": odds_data.get("data_source"),
        "week": current_week
    }

async def ask_survivor_gemini(query: str, api_key: str) -> str:
    result = await build_survivor_recommendation(api_key, custom_prompt=query)
    return result["recommendation"]
