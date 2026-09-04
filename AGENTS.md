# Fantasy Football — Loser Eats an Egg

This workspace manages Anthony's team in the Sleeper league "Loser eats an egg"
(12-team, half-PPR). His team is **Tony Romo's DUI Mugshot**, roster_id 4,
username `pinec0ne`.

**Read `.agents/skills/loser-eats-an-egg/SKILL.md` first, for anything related to
this league** — lineup calls, waiver targets, trade evaluation, matchup previews,
or league standings. It has the exact scoring rules, live Sleeper API endpoints,
bundled draft/projection data, and the weekly workflows. Don't ask Anthony to
re-explain the league; it's all in that file.

Fast-changing state (current roster shape, standing risks, live trade/waiver
notes) lives in `fantasy-football/current-state.md`, not in the skill file.
Read it for orientation, but always re-verify against the live Sleeper API
before answering a real question — see the skill file for endpoints. **Edit
`fantasy-football/current-state.md` directly** whenever team shape, risks, or
trade offers change; there's no separate memory system here, that file is the
persistent layer.

## Style
- Lead with the recommendation, then the reasoning.
- Show the numbers behind every claim.
- Say when you're guessing.
- Anthony is usually on his phone — keep answers scannable, tables narrow.
- Preseason projections (in `.agents/skills/loser-eats-an-egg/data/`) go stale by
  week 4 — weight recent usage, snap counts, and target share over the bundled
  board after that, and say so when you do.

## Automated Deployment Workflow
When you make changes to the backend codebase or the frontend (in `Unified Apps/piconeprojects`):
1. **Git:** Automatically run `git add .`, `git commit -m "..."`, and `git push` on Anthony's behalf without him asking.
2. **GCP:** After modifying backend Python code in this workspace, always ask Anthony if he wants you to deploy the updates to Google Cloud Run. If he says yes, use your terminal tools to run the following exact PowerShell command:
   ```powershell
   gcloud run deploy fantasy-ai-service `
     --source . `
     --region us-east1 `
     --platform managed `
     --allow-unauthenticated `
     --set-secrets "GEMINI_API_KEY=gemini-api-key:latest" `
     --set-env-vars "SLEEPER_LEAGUE_ID=1389359956657967104" `
     --set-env-vars "SLEEPER_USER_ID=pinec0ne"
   ```
