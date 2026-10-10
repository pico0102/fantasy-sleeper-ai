# Current State — Tony Romo's DUI Mugshot

Last refreshed: **October 9, 2026** (Week 5 live per Sleeper API).
Record: **2-2** (467.8 Points For).
Current Matchup: Week 5 vs **Big Daddy** (roster 3, eleenavolk). Live score: Anthony leads 30.5 – 0.0.

## Live Verified Roster (Week 5)
- **QB:** Bo Nix (DEN) · Baker Mayfield (TB, Out)
- **RB:** Christian McCaffrey (SF), Jeremiyah Love (ARI), Bucky Irving (TB), Jaylen Warren (PIT)
- **WR:** Davante Adams (LAR), Xavier Worthy (KC), Jakobi Meyers (JAX), Khalil Shakir (BUF), Devaughn Vele (NO), Dohnte Meyers (CIN) · A.J. Brown (NE, IR slot)
- **TE:** Harold Fannin Jr. (CLE)
- **K:** Matt Gay (LV)
- **DEF:** Seattle Seahawks (SEA)
- **FAAB Budget:** **$90 remaining** of $100 ($10 used).

## Waiver & Lineup Methodology
- **Dynamic Positional Marginal Value:** Evaluates every position by net marginal points added to starting lineup across current + next 2 weeks. Never hardcodes WR preference.
- If K or TE is on bye or out, replacement adds carry massive marginal gain. If WR is recommended, reasons explicitly by showing which bench/flex player is upgraded.

## Full league roster map (roster_id → manager → team) — from 1 Sept 2026 pull, will drift as other managers make moves

| RID | Manager | Team |
|---|---|---|
| 1 | notgeoff ("Geoff") | 'Lik my Nabers |
| 2 | BenjaminReydel ("Ben") | Big Ballz No Breakz |
| 3 | eleenavolk | Big Daddy |
| 4 | pinec0ne (Anthony) | Tony Romo's DUI Mugshot |
| 5 | elliegance | Sad-a-boo Bears |
| 6 | sfahy13 | Duke's Maye-O |
| 7 | devindragon1 ("Devin") | Dragon Deez Balls |
| 8 | gwintastico | Veal McMillanese |
| 9 | abby421 | (unnamed) |
| 10 | JRad2727 | Commish is Corrupt |
| 11 | talsballs | Jet2 holiday |
| 12 | Lior420 | (unnamed) |

## Update log
- 1 Sept 2026 (roster move): Anthony picked up Tre Tucker (LV WR) as a free agent and dropped Kaelon Black — matched the #1 waiver recommendation from the same day's check. Not a FAAB waiver claim since Tucker was apparently unrostered/free-agent rather than on the waiver list. Flagged roster section as unverified-until-next-live-pull; still need to re-run the lineup-delta sweep post-move.
- 1 Sept 2026 (waiver check): Re-pulled live state/rosters/trending/transactions. Confirmed post-trade roster landed exactly as projected (15/15, full). Re-ran lineup-delta sweep live: #1 overall, #2 RB, #11 WR of 12. Corrected season start date (Sept 9, not Sept 13). Identified 3 WR waiver targets (Tucker, Thomas, Vele) with Kaelon Black as the recommended drop. New finding: Allgeier is ARI's RB1 ahead of Love on the season's first depth chart, capping Love's floor independent of the ankle. Confirmed via web search Love is still uncertain for Week 1 (high ankle sprain, no team commitment yet).
- 28 Aug 2026 (trade close): Geoff trade accepted (Dobbins+Hampton+Dart for Brown+Warren+Nix). Recorded outcome, killed all other Dobbins-based outreach as moot, logged injury/role/bye-week findings gathered during negotiation.
- 28 Aug 2026 (earlier): Re-pulled all 12 live rosters, ran full trade-partner sweep by lineup-delta method, identified Big Ballz/Dragon Deez/Jet2/Commish/abby421 as other viable Dobbins-for-WR partners.
- 25 Aug 2026: initial write.
- 3 Sept 2026: Migrated this state file over from Claude Cowork Project doc to a plain repo file for use in Google Antigravity. Content unchanged.
