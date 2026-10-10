---
name: loser-eats-an-egg
description: Manage Anthony's 2026 fantasy football team "Tony Romo's DUI Mugshot" in the Sleeper league "Loser eats an egg" — weekly lineup calls, waiver targets, trade evaluation, and matchup previews. Use whenever he asks about his fantasy team, his lineup, who to start or sit, waiver wire pickups, a trade offer, or his league standings.
---

# Loser Eats an Egg — season manager

Anthony's 12-team half-PPR Sleeper league. This skill carries his league constants,
his roster, the valuation method built during the draft, and the weekly workflows.

## League constants — do not re-derive these

| | |
|---|---|
| League ID | `1389359956657967104` |
| Draft ID | `1389359956657967105` |
| League name | Loser eats an egg |
| His username | `pinec0ne` · team **Tony Romo's DUI Mugshot** |
| His roster_id | **4** · draft slot **7** |
| Format | 12-team, half-PPR, snake, 15 rounds |
| Starters | 1 QB · 2 RB · 2 WR · 1 TE · 2 FLEX (W/R/T) · 1 K · 1 DEF |
| Bench | 5 · plus 1 IR |
| Playoffs | 6 teams, start week 15 |
| Waivers | clear Wednesday 3 AM ET, 1-day process |
| Trade deadline | week 11 |

### Exact scoring (verified from the league's settings page — do not assume)

- Passing: 0.04/yd · **4 per TD** · **−1 per INT** · 2 per 2PC
- Rushing: 0.1/yd · 6 per TD · 2 per 2PC
- Receiving: **0.5 per reception** (no TE premium) · 0.1/yd · 6 per TD · 2 per 2PC
- Kicking: FG 0–39 = 3, **40–49 = 4, 50–59 = 5, 60+ = 6** (distance-tiered)
- No yardage bonuses, no first-down scoring

## ⚠️ There is no current roster in this file — always pull it

**His current roster is never written down here.** Call
`/league/1389359956657967104/rosters` and use what it returns. That one call gives **all
twelve rosters live**, so opponent rosters are current too, no matter who traded or
added whom without involving him.

Resolve names via `data/player_ids.json` first, then `/players/nfl/{id}` for anything
missing (see *Resolving player IDs* below).

Before pulling live, read `fantasy-football/current-state.md` (repo root) for the
last-known picture (team shape, standing risks, open trades) — it's a fast orientation,
not a substitute for the live call above.

What follows is **draft history** — a permanent record of what he selected on 23 Aug
2026. It does not go stale because it describes an event, not a roster. Use it for
context ("he spent his 1st on McCaffrey"), never as an answer to "what's on my team."

## Draft results — 23 Aug 2026 (historical, immutable)

| Rd.Pick | Player | Pos |
|---|---|---|
| 1.07 | Christian McCaffrey (SF) | RB |
| 2.18 | Omarion Hampton (LAC) | RB |
| 3.31 | Jeremiyah Love (ARI) | RB |
| 4.42 | Bucky Irving (TB) | RB |
| 5.55 | Davante Adams (LAR) | WR |
| 6.66 | Harold Fannin Jr. (CLE) | TE |
| 7.79 | J.K. Dobbins (DEN) | RB |
| 8.90 | Jaxson Dart (NYG) | QB |
| 9.103 | Xavier Worthy (KC) | WR |
| 10.114 | Jakobi Meyers (JAX) | WR |
| 11.127 | Khalil Shakir (BUF) | WR |
| 12.138 | Seattle | DEF |
| 13.151 | Tyjae Spears (TEN) | RB |
| 14.162 | Wil Lutz (DEN) | K |
| 15.175 | Kaelon Black (SF) | RB |

**Team shape at draft close** (starting point only — for the current numbers, see
`fantasy-football/current-state.md`): 1st of 12 in projected starting lineup, 1st in RB-in-lineup, last in
WR-in-lineup. His RB surplus fills both FLEX slots, which is *why* WR-in-lineup is last
even though his rostered WRs aren't individually bad — they never see a FLEX spot.

⚠️ **Metric warning, learned the hard way:** when computing his RB/WR standing, use the
**lineup-allocation method** (`scripts/league.py` / the `best_lineup` function,
position-summed post-FLEX) — never a raw "top-N points owned" depth stat. The two can
disagree by several ranks (a real case: depth stat said 9th, lineup method said 12th —
last). The lineup method is the one that matches what actually plays out on Sundays.

## Where the current numbers live — not in this file

**Team shape, standing risks, and live trade activity go stale fast and are tracked in
`fantasy-football/current-state.md` at the repo root, not here.** Update that file
directly whenever team shape, risks, or trade offers change — read it first, edit it,
save it. There is no separate project-memory system in this setup; that file *is* the
persistent state layer (it played the role a Claude Project doc played previously).

**This file only needs a refresh when the stable stuff changes:** league settings,
scoring rules, roster structure, or the trade-partner *methodology* itself (not the
specific teams it currently points to — that list lives in `current-state.md` too).
That's rare. Day-to-day roster/trade churn should never require editing this file —
it belongs in `current-state.md`.

## Live data — always pull, never recall

Base `https://api.sleeper.app/v1` — public, no auth. Append `?cb=<random>` to defeat
fetch caching when polling.

| Need | Endpoint |
|---|---|
| Current week / season state | `/state/nfl` |
| All 12 rosters (players, starters, W-L) | `/league/1389359956657967104/rosters` |
| Manager names → roster_id | `/league/1389359956657967104/users` |
| Weekly matchups & scores | `/league/1389359956657967104/matchups/{week}` |
| Waivers, adds/drops, trades | `/league/1389359956657967104/transactions/{week}` |
| Draft picks (historical) | `/draft/1389359956657967105/picks` |
| **Resolve any single player ID** | `/players/nfl/{player_id}` |
| Waiver-wire trending adds | `/players/nfl/trending/add?lookback_hours=24&limit=25` |

### Resolving player IDs

Roster, matchup, and transaction payloads give **player IDs, not names.**

`data/player_ids.json` covers the 174 players drafted in 2026 plus 9 waiver adds
resolved since (183 total, refreshed 25 Aug 2026) — fast lookup, no fetch.
For **anything not in it** — a waiver pickup, a rookie called up, a player another
manager added — call `/players/nfl/{player_id}`. It returns one small object with
`first_name`, `last_name`, `team`, `position`, and `injury_status`. Verified working on
IDs that were never drafted in this league.

Never fetch the bare `/players/nfl` — it is ~5 MB and will blow up the fetch. Always
resolve IDs one at a time through the single-player endpoint.

`injury_status` on that endpoint is a genuine live field — use it as a first pass on
start/sit, then confirm with news (web search).

The `sleeper.app` web page is a JavaScript app and returns nothing useful. Always use
`api.sleeper.app`.

## Bundled files

- `data/player_ids.json` — Sleeper player ID → name, 183 entries; needed to read any `/rosters` response
- `data/board.json` — full VORP board: every player, projected half-PPR points, position rank
- `data/blended.json` — same, plus blended preseason ADP and market-edge figures
- `data/allpicks.txt` — all 180 draft picks as `pick|roster_id|name|position`
- `scripts/vorp.py` — rebuilds the board from projections; edit the projection tables to refresh
- `scripts/league.py` — scores all 12 rosters by optimal lineup and ranks them
- `scripts/oncall.py` — best-available engine (draft-day tool, still useful for waivers)

Projections are **preseason**. By week 4 they are stale — weight recent usage, snap
counts, and target share over anything in these files, and say so when you do.

## Weekly workflows

### Start/sit (run Thursday, confirm Sunday morning)
1. `/state/nfl` for the week, then `/league/.../rosters` for his current 15.
2. Search for injury news and snap-count trends on every starter and any bench player
   with a case.
3. Check opponent defense and, for K, weather and dome status — his distance-tiered
   kicker scoring rewards leg strength and clear conditions.
4. Recommend a lineup with a one-line reason each. Flag only the genuinely close calls;
   do not narrate the obvious starts.

### Waivers (run Tuesday, claims process Wednesday 3 AM ET)
1. `/players/nfl/trending/add?lookback_hours=24&limit=25` for what the wider Sleeper
   population is adding, then `/league/.../transactions/{week}` for what happened in
   *this* league. Cross-reference: a player trending nationally who is still unrostered
   here is the best claim available.
2. Evaluate every position by **projected marginal value to Anthony's starting lineup**
   across a multi-week horizon (factoring in byes, injuries, and bench drops).
   Never default to WR or any single position — if a K or TE is on bye or out,
   a waiver replacement carries massive marginal points. If a WR genuinely provides
   the greatest marginal gain over existing flex/starters, explain why.
3. Factor in actual remaining FAAB ($100 season budget).

### Trade evaluation
Evaluate by **change in optimal starting lineup**, never by raw player value. A player
who does not crack his lineup is worth roughly nothing to him regardless of name value.

Method: build both teams' best legal lineups before and after, and report the delta for
each side. `scripts/league.py` has a working `lineup()` function for this. A trade worth
doing is positive for both sides — those are the ones that actually get accepted.

His structural edge: surplus RB, deficit WR (see the metric warning above — use
WR-in-lineup, not raw depth). **Current mirror-image trade partners and any live trade
offers are tracked in `fantasy-football/current-state.md`, not here** — read that first,
then re-verify against live rosters before acting, since manager-to-manager moves can
shift the picture without touching his own roster.

### Matchup preview
Pull `/matchups/{week}`, identify the opponent's roster_id, project both lineups, and
name the two or three players the week actually turns on.

## How to work with Anthony

- **Lead with the recommendation, then the reasoning.** He acts on a short answer and
  reads the justification after.
- **Show the numbers.** Every claim gets a figure. He notices when one is missing.
- **Say when you are guessing.** During the draft the useful calls were the ones labeled
  as uncertain — kicker rankings, defense, the RB-heavy tilt in the projections.
- **Be fast when he is on a clock.** If he says he is deciding now, give the name first
  and explain second.
- He is on mobile. Keep tables narrow and answers scannable.

## Migration note (moved from Claude/Cowork, Sept 2026)

This skill was originally maintained as a Claude Cowork "skill" with a companion Claude
Project doc (`current-state.md`) that Claude wrote to automatically. There is no
Cowork Project here, so:

- `current-state.md` now lives at `fantasy-football/current-state.md` in this repo/workspace.
  Read and edit it directly with normal file tools — it is a plain markdown file, not a
  special memory system.
- Everything else (scoring, endpoints, workflows) is unchanged and works the same way
  under any model that can call web/HTTP tools.
