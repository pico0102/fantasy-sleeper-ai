# Migrating "Loser Eats an Egg" from Claude Cowork to Google Antigravity

This bundle is everything Cowork had for your fantasy football workflow, repackaged
for Antigravity. Antigravity reads two things automatically, both included here:

1. **`AGENTS.md`** (repo root) — the cross-tool instructions file Antigravity loads
   at session start. This replaces the Cowork Project's "instructions" field.
2. **`.agents/skills/loser-eats-an-egg/`** — an Agent Skill in the same `SKILL.md`
   format Claude uses. Antigravity looks for skills under `.agents/skills/`, so this
   should activate the same way it did in Cowork (triggered by its `description`
   whenever you ask about your team, lineup, waivers, trades, or standings).

## Setup (5 minutes)

1. Unzip this into a folder — ideally a small git repo, e.g. `fantasy-football-2026/`.
   Open that folder as your workspace in Antigravity.
2. That's it for the skill and instructions — no import step, Antigravity reads
   `AGENTS.md` and `.agents/skills/` directly from the filesystem.
3. Optional, if you also use Claude Code somewhere and want the same skill there:
   `ln -s .agents/skills .claude/skills`

## What moved and what changed

| Cowork piece | Antigravity equivalent | Notes |
|---|---|---|
| Skill (`loser-eats-an-egg`) | `.agents/skills/loser-eats-an-egg/SKILL.md` | Same content; one line at the bottom notes the memory-system swap below. Same bundled `data/` (player IDs, VORP board, blended board, draft picks) and `scripts/` (`vorp.py`, `league.py`, `oncall.py`) — copied byte-for-byte. |
| Project instructions | `AGENTS.md` | Condensed from the Project's instructions field. |
| Project doc `current-state.md` (Claude auto-wrote to this) | `fantasy-football/current-state.md` | Plain file now — no auto-write mechanism exists in Antigravity, so just ask the agent to update it (or do it yourself) after a lineup/waiver/trade session. It's a normal file, so you can also just edit it by hand. |
| "Draft Board" project doc (a Claude Artifact link) | Not migrated — see below | It was a hosted, view-only HTML page (`https://claude.ai/code/artifact/fc930a02-db4f-473d-86b4-339d1107d3f2`), not source data. The data behind it is already in `data/board.json` and `data/blended.json`. The link still works if you just want to look at it (it's private to your Claude account) — it doesn't need to move, and Antigravity can't read a Claude-hosted URL anyway. |

## One thing to sanity-check on day one

Ask the new agent something like *"what's the state of my team right now"* and
confirm it (a) reads `fantasy-football/current-state.md` for orientation, and
(b) actually calls the live Sleeper API (`https://api.sleeper.app/v1/league/1389359956657967104/rosters`)
before giving you a real answer, rather than trusting stale bundled data. The
skill file says to do both, but it's worth checking once that Gemini follows the
same discipline Claude did.

Everything else — league ID, scoring, endpoints, weekly workflows — is unchanged
and doesn't depend on which model is running it.
