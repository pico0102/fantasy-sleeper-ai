# fantasy-sleeper-ai v2 Rearchitecture Implementation Plan (Revised)

- **Architecture Paradigm:** Deterministic Data Plumbing & Guardrails in Code | Analytical Reasoning in LLM
- **Default Model Generation:** Gemini Flash 3.8 (`gemini-3.8-flash`)
- **Target Branch:** `feat/v2-reasoner-rearchitecture`
- **Legacy Rollback Tag:** `v1-legacy-rules-engine`

---

## 1. Executive Summary & Architectural Invariants

The v2 rearchitecture replaces the legacy v1 rules-based heuristic engine with a unified, verifiable Fantasy GM & Survivor Reasoner built on eight strict architectural invariants:

1. **Code Handles Plumbing, Validation & Guardrails; Model Handles Reasoning:** Python code fetches external data, validates local catalogs, resolves the canonical NFL week, enforces fail-closed pre-LLM gates, and verifies model output against the active snapshot. Gemini (`gemini-3.8-flash`) performs all analytical reasoning across fantasy lineups, trades, waivers, and survivor picks.
2. **Zero Silent Failures:** Every API response and local JSON file is validated through Pydantic v2 schemas. Corrupt files, missing API keys, or upstream HTTP failures raise typed exceptions (`DataValidationError`, `UpstreamSourceError`) or populate explicit `ProvenanceRecord` failure states—never silent `{}` fallbacks.
3. **One Consistent World-Model Per Session & Single-Sourced NFL Week:** Every request is grounded in a single immutable `WorldStateSnapshot` identified by a deterministic SHA-256 `snapshot_id`. The active NFL week (`nfl_week`) is computed in one canonical, unit-tested resolver (`resolve_canonical_nfl_week`) and shared across chat, matchups, survivor mode, and weekly emails—eliminating the v1 Week 5 / Week 6 off-by-one bug and cross-path contradictions.
4. **Full News & Depth-Chart Awareness (Beyond Sleeper Injury Fields):** Because Sleeper's `injury_status` leaves healthy benched players marked `Active`, the pipeline ingests explicit depth-chart rankings, starter/benched role designations, `supplanted_by` relationships, and player news headlines into `RoleAndNewsContext` inside `WorldStateSnapshot`. Healthy benchings (such as Kirk Cousins benched for Fernando Mendoza) carry the same warning priority as major injuries.
5. **Unified Survivor Subsystem with Deterministic Fail-Closed Refusal:** Survivor pick generation is a first-class reasoner path (`core/survivor.py`) shared identically by both the UI Survivor chat toggle and the weekly automated survivor email. It ingests live point spreads and moneylines from The Odds API with full provenance. When odds data is missing, unconfigured (`ODDS_API_KEY` absent), or failed, a deterministic pre-LLM gate short-circuits execution and returns a fixed refusal template (`SURVIVOR_FAIL_CLOSED_TEMPLATE`) so the same query always produces the exact same refusal and never fabricates spreads. In email mode, a hard pre-send safeguard guarantees a failed run can never emit an NFL team name.
6. **v1 Positional Bias Eradication & `board.json` Audit:** All legacy v1 positional multipliers are deleted from code, and `board.json` is audited and sanitized during ingestion to strip any carried-over v1 positional weighting bias.
7. **Anti-Deflection Reasoning Mandate:** The reasoner system prompt enforces the explicit instruction: *"If structured data is missing, reason from available context and state what is missing; never deflect."* And when a user prompt contains a false premise (such as an incorrect bye week or starter status), the reasoner must verify against `WorldStateSnapshot` and correct the premise directly.
8. **Inspectable Provenance & Proactive `/health` Monitoring:** Every response returns inspectable `ProvenanceRecord` metadata rendered in the UI, and `/health` proactively monitors all six subsystems: `player_ids_catalog`, `board_catalog`, `sleeper_api`, `news_depth_chart_feed`, `odds_api`, and `gemini_reasoner`.

---

## 2. Target Repository & Module Layout

Preserve the existing Cloud Run / Uvicorn entrypoint (`main.py` or `app/main.py` matching the `Dockerfile` CMD) while organizing the v2 architecture into focused modules:

    .
    ├── IMPLEMENTATION_PLAN.md
    ├── main.py                      # FastAPI entrypoint (/, /api/chat, /api/survivor, /api/world-state, /health)
    ├── requirements.txt             # Pinned dependencies (fastapi, uvicorn, pydantic>=2.0, requests, google-genai, pytest, httpx)
    ├── Dockerfile                   # Cloud Run container specification
    ├── player_ids.json              # Local player identity catalog
    ├── board.json                   # Local valuation/tier catalog (audited for v1 positional bias)
    ├── news_depth_overrides.json    # Structured depth-chart/benching & breaking news feed catalog/cache
    ├── core/
    │   ├── __init__.py
    │   ├── config.py                # Env vars, GEMINI_MODEL="gemini-3.8-flash", ODDS_API_KEY, TTLs
    │   ├── exceptions.py            # Typed exceptions (DataValidationError, UpstreamSourceError, SurvivorGateError)
    │   ├── schemas.py               # Pydantic v2 models for WorldState, News/DepthChart, Odds, Survivor, Chat
    │   ├── provenance.py            # ProvenanceRecord builders and staleness evaluators
    │   ├── week_resolver.py         # Single-sourced, unit-tested NFL week determination (fixes Week 5/6 off-by-one)
    │   ├── board_auditor.py         # Audits and strips v1 positional bias from board.json at load time
    │   ├── data_pipeline.py         # Validated loaders for Sleeper API, catalogs, and The Odds API
    │   ├── news_pipeline.py         # Depth-chart rank, benching/starter transitions, and news headline ingestion
    │   ├── world_model.py           # Deterministic WorldStateSnapshot builder & session cache
    │   ├── guardrails.py            # Entity grounding, false-premise detection, injury/benching risk checks
    │   ├── reasoner.py              # Gemini Flash 3.8 analytical reasoner with anti-deflection mandate
    │   └── survivor.py              # Unified Survivor pick engine (chat toggle + weekly email) & fail-closed gate
    ├── static/                      # Mobile-first Tailwind CSS + marked.js UI with Provenance & Survivor toggle
    └── tests/
        ├── __init__.py
        ├── test_schemas.py          # Pydantic v2 schema, injury, and role-risk validation tests
        ├── test_week_resolver.py    # Unit tests for single-sourced week boundary & Week 5/6 regression
        ├── test_world_model.py      # Snapshot determinism, board bias audit, and provenance tests
        ├── test_survivor.py         # Deterministic fail-closed refusal & email zero-team-leak safeguard tests
        ├── test_guardrails.py       # Hallucination, benching risk, and false-premise correction tests
        ├── test_qa_probes.py        # Dedicated regression suite encoding all 7 QA Probes from the addendum
        └── test_api.py              # FastAPI /health (including odds_api), /api/chat, and UI integration tests

---

## 3. Core Data Contracts & Pydantic v2 Schemas (`core/schemas.py`)

All external inputs and internal state transitions are enforced via strict Pydantic v2 models:

1. **`ProvenanceRecord`**
   - `source_id`: `Literal["sleeper_nfl_state", "sleeper_rosters", "sleeper_matchups", "sleeper_trending", "local_player_ids", "local_board_catalog", "nfl_news_and_depth_charts", "the_odds_api_nfl"]`
   - `source_type`: `Literal["sleeper_api", "local_catalog", "news_depth_chart", "odds_api"]`
   - `endpoint_or_path`: `str`
   - `fetched_at_utc`: `str` (ISO-8601 UTC timestamp)
   - `age_seconds`: `float`
   - `status`: `Literal["ok", "stale", "degraded", "failed", "missing_key"]`
   - `record_count`: `int`
   - `error_detail`: `Optional[str] = None`

2. **`InjuryContext`** (Medical Designations)
   - `status`: `Literal["Active", "Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended", "COV", "Unknown"] = "Active"`
   - `body_part`: `Optional[str] = None`
   - `practice_status`: `Optional[str] = None`
   - `injury_notes`: `Optional[str] = None`
   - `is_medical_risk`: `bool` (Computed: `True` when `status` in `{"Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended", "COV"}`)
   - `is_unstartable_medical`: `bool` (Computed: `True` when `status` in `{"Doubtful", "Out", "IR", "PUP", "Suspended", "COV"}`)

3. **`RoleAndNewsContext`** (Depth Chart, Benching & Coaching Decisions)
   - `depth_chart_position`: `Optional[str] = None`
   - `depth_chart_rank`: `Optional[int] = None` (`1` = primary starter, `2` = backup, `3+` = depth)
   - `role_status`: `Literal["confirmed_starter", "benched", "backup", "committee", "position_battle", "unknown"] = "unknown"`
   - `supplanted_by`: `Optional[str] = None` (e.g., `"Fernando Mendoza"` when Kirk Cousins is benched)
   - `recent_news_headlines`: `list[str] = []`
   - `news_summary`: `Optional[str] = None`
   - `last_news_updated_utc`: `Optional[str] = None`
   - `is_role_risk`: `bool` (Computed: `True` when `role_status in {"benched", "backup", "position_battle"}` or when a single-starter position like QB has `depth_chart_rank >= 2`)

4. **`ValidatedPlayer`**
   - `player_id`: `str`
   - `full_name`: `str`
   - `position`: `str`
   - `team`: `Optional[str] = None`
   - `bye_week`: `Optional[int] = None`
   - `injury`: `InjuryContext`
   - `role_and_news`: `RoleAndNewsContext`
   - `board_tier`: `Optional[int] = None`
   - `board_rank`: `Optional[int] = None`
   - `board_raw_value`: `Optional[float] = None`
   - `board_bias_adjusted`: `bool = False`
   - `trending_adds_24h`: `int = 0`
   - `trending_drops_24h`: `int = 0`

5. **`NFLGameOdds` & `SurvivorPickResult`**
   - `NFLGameOdds`: `game_id: str`, `nfl_week: int`, `commence_time_utc: str`, `home_team: str`, `away_team: str`, `favorite_team: str`, `underdog_team: str`, `spread_points: float` (negative for favorite), `favorite_moneyline: Optional[int]`, `underdog_moneyline: Optional[int]`, `implied_win_prob: float`, `bookmaker_count: int`.
   - `SurvivorPickResult`:
     - `status`: `Literal["verified_pick", "fail_closed_no_odds", "guardrail_rejected"]`
     - `nfl_week`: `int`
     - `recommended_team`: `Optional[str] = None` (Strictly `None` whenever `status != "verified_pick"`)
     - `opponent_team`: `Optional[str] = None`
     - `spread`: `Optional[float] = None`
     - `implied_win_prob`: `Optional[float] = None`
     - `reasoning_markdown`: `str` (Contains fixed `SURVIVOR_FAIL_CLOSED_TEMPLATE` when `status == "fail_closed_no_odds"`)
     - `future_value_notes`: `Optional[str] = None`
     - `odds_provenance`: `ProvenanceRecord`
     - `snapshot_id`: `str`

6. **`WorldStateSnapshot`**
   - `snapshot_id`: `str` (16-char SHA-256 fingerprint)
   - `session_id`: `str`
   - `created_at_utc`: `str`
   - `nfl_season`: `str`
   - `nfl_week`: `int` (Populated exclusively via `resolve_canonical_nfl_week`)
   - `nfl_teams_on_bye`: `list[str]` (Canonical list of NFL team abbreviations on bye in `nfl_week`)
   - `league`: `Optional[LeagueContext] = None`
   - `user_roster`: `Optional[RosterSlotSummary] = None`
   - `opponent_roster`: `Optional[RosterSlotSummary] = None`
   - `all_rosters`: `list[RosterSlotSummary] = []`
   - `top_available_waivers`: `list[ValidatedPlayer] = []`
   - `trending_players`: `list[ValidatedPlayer] = []`
   - `notable_role_changes`: `list[ValidatedPlayer] = []` (Players with active benchings/promotions, e.g., Kirk Cousins / Fernando Mendoza, always available in snapshot even if unrostered)
   - `current_week_odds`: `list[NFLGameOdds] = []`
   - `board_audit_summary`: `dict[str, Any]`
   - `provenance`: `list[ProvenanceRecord]`
   - `active_injury_and_role_alerts`: `list[str] = []`
   - `warnings`: `list[str] = []`

---

## 4. Detailed Design of the Revised Subsystems

### 4.1 Single-Sourced NFL Week Determination (`core/week_resolver.py`)
- **Root Cause of the v1 Week 5 / Week 6 Bug:** v1 mixed `leg`, `display_week`, and 0-indexed array offsets or stale local calculations across different modules, causing Survivor mode to label Week 6 picks as Week 5 and creating cross-path contradictions.
- **v2 Specification:**
  - `resolve_canonical_nfl_week(sleeper_state_payload: dict, reference_utc: Optional[datetime] = None) -> int` is the **sole source of truth** for the current NFL week across the entire application.
  - It validates that `season_type == "regular"` (or postseason), reconciles `week` and `leg` from Sleeper `/v1/state/nfl` against the official NFL Tuesday 08:00 UTC weekly advancement boundary when `display_week` lags `week`, and validates `1 <= nfl_week <= 18`.
  - Every consumer (`WorldStateSnapshot.nfl_week`, league matchup fetcher, bye-week validator, `SurvivorEngine`, and `SurvivorEmailDispatcher`) reads this single integer from `WorldStateSnapshot.nfl_week`. Direct week math anywhere else in the codebase is prohibited.

### 4.2 News & Depth-Chart Awareness (`core/news_pipeline.py`)
- **Why Sleeper Injury Fields Are Insufficient:** Sleeper's `injury_status` only reflects physical injuries. When a healthy veteran quarterback like Kirk Cousins is benched for rookie Fernando Mendoza, Sleeper still reports `injury_status: null` (`Active`).
- **Input Sources:**
  1. **Sleeper Depth Chart & News Metadata:** Extract `depth_chart_order`, `depth_chart_position`, and `news_updated` from the player catalog.
  2. **Live NFL Depth-Chart & Transactions/News Feed (`news_pipeline.py`):** Ingests public NFL team depth-chart/transaction updates (via ESPN/NFL public JSON endpoints with fallback/augmentation from a validated `news_depth_overrides.json` transaction feed) capturing coaching switches, benchings, promotions, and practice/role blurbs.
- **Update Cadence:**
  - Polled and cached with a 15-minute TTL (`900s`) during normal operation, and refreshed immediately when `force_refresh=True` is passed.
  - Tracked with its own `ProvenanceRecord` (`source_id="nfl_news_and_depth_charts"`, `source_type="news_depth_chart"`).
- **Integration into `WorldStateSnapshot`:**
  - Hydrates `ValidatedPlayer.role_and_news` (`RoleAndNewsContext`) for all rostered players, top waiver candidates, and any player queried by name in the user's message (entity pre-lookup scans `player_ids.json` for any player mentioned in `ChatRequest.message` and injects their full `ValidatedPlayer` profile into `WorldStateSnapshot.notable_role_changes` so even deep-bench or unrostered players have full news/role context).
- **How the Reasoner Must Weigh It:**
  - `RoleAndNewsContext.is_role_risk == True` (especially `role_status == "benched"` or `depth_chart_rank >= 2` at QB) is weighted with **higher priority than historical season points or static `board.json` tier**.
  - The reasoner is instructed: *"A healthy benched player (`role_status='benched'`) has near-zero projected floor. You must lead with the benching/depth-chart change, name the player who supplanted them (`supplanted_by`), and never recommend acquiring or starting them in standard 1QB formats based on outdated board ranks or past stats."*
  - **Mandatory Acceptance Test:** Asking `"Should I pick up Kirk Cousins?"` must identify that Kirk Cousins is benched (`role_status="benched"`, `depth_chart_rank=2`) behind Fernando Mendoza (`supplanted_by="Fernando Mendoza"`), warn against the pickup, and cite `nfl_news_and_depth_charts`.

### 4.3 `board.json` Positional Bias Audit (`core/board_auditor.py`)
- At startup and catalog reload, `board_auditor.py` inspects `board.json` for carried-over v1 positional bias:
  - Checks position distribution across Tiers 1–5 and detects hard-coded v1 positional multipliers or skewed synthetic value weights (e.g., blanket RB/WR/QB multipliers baked into raw values).
  - Strips any synthetic v1 multiplier fields, normalizes rankings to transparent baseline tier/positional ranks, flags `board_bias_adjusted = True` where normalized, and attaches a `board_audit_summary` to `WorldStateSnapshot` and `/health`.
  - Ensures the LLM evaluates positional value dynamically from the active league's actual `scoring_settings` (PPR vs Half-PPR vs Standard) and `roster_positions` rather than v1 static weights.

### 4.4 Survivor Subsystem, Fail-Closed Refusal & Unified Weekly Email (`core/survivor.py`)
- **(a) Odds API Ingestion with Full Provenance:**
  - `OddsAPIClient` fetches `/v4/sports/americanfootball_nfl/odds/?regions=us&markets=spreads,h2h&oddsFormat=american` using `ODDS_API_KEY`.
  - Filters games strictly to the single-sourced `WorldStateSnapshot.nfl_week` window.
  - Emits `ProvenanceRecord(source_id="the_odds_api_nfl", source_type="odds_api", ...)`.
- **(b) Explicit Survivor Reasoner Path (`SurvivorEngine`):**
  - Invoked when `/api/chat` receives `mode="survivor"` (from the UI Survivor toggle) or when the query is routed to `/api/survivor`, AND invoked directly by the weekly email job.
  - Prompts Gemini Flash 3.8 with the validated `current_week_odds` table, `nfl_week`, `used_teams` list, key quarterback injury/benching alerts from `WorldStateSnapshot`, and future schedule value.
- **(c) Deterministic Fail-Closed Refusal (Pre-LLM Gate):**
  - Before calling the LLM, `SurvivorEngine.evaluate_week()` executes a deterministic gate:
    - If `ODDS_API_KEY` is missing, `odds_provenance.status != "ok"`, or `len(current_week_odds) == 0`, execution halts immediately **without calling Gemini**.
    - Returns `SurvivorPickResult(status="fail_closed_no_odds", recommended_team=None, ...)` populated with the exact fixed template:
          SURVIVOR MODE UNAVAILABLE (FAIL-CLOSED): Verified live NFL odds from The Odds API are currently unavailable [status={status}, source=the_odds_api_nfl]. To prevent unverified or fabricated point spreads, Survivor pick generation is disabled until live odds provenance is restored.
    - Because the LLM is never invoked when odds fail, asking the same survivor question repeatedly under missing/failed odds is guaranteed to return the exact same refusal string 100% of the time.
- **(d) Unified Weekly Survivor Email & Zero-Team-Leak Safeguard (`SurvivorEmailDispatcher`):**
  - The weekly email generator calls the exact same `SurvivorEngine.evaluate_week(snapshot, used_teams)` method used by the chat toggle.
  - **Hard Pre-Send Safeguard:**
    1. If `result.status != "verified_pick"` or `result.recommended_team is None`, the email dispatcher immediately aborts sending any pick email (`email_sent=False`, `abort_reason=result.status`).
    2. Even in failure notifications or fallback logs/payloads, a deterministic regex sanitizer (`assert_no_nfl_team_leaked(text)`) scans against all 32 NFL team names, cities, Mascots, and 2/3-letter abbreviations. If a run has `status != "verified_pick"`, it is structurally impossible for the email subsystem to emit an NFL team name.
- **(e) Odds API Registered in `/health`:**
  - `/health` includes `subsystems["odds_api"]` reporting `status` (`"ok"`, `"missing_key"`, `"degraded"`, `"failed"`), `fetched_at_utc`, `games_loaded_for_week`, and `nfl_week`.

### 4.5 Explicit Reasoner System Rules (`core/reasoner.py`)
- Default model is set to `GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")`.
- The system prompt MUST contain these four explicit rules verbatim:
  1. **Anti-Deflection Rule:** *"If structured data is missing, reason from available context and state what is missing; never deflect."*
  2. **Premise Verification Rule:** *"Never accept a user's premise about bye weeks, injuries, depth-chart starter status, or matchups at face value. Verify every premise against `WorldStateSnapshot` first, and if the user's premise is false, correct it explicitly in your first sentence."*
  3. **News & Benching Priority Rule:** *"Sleeper medical status alone does not mean a player is starting. Always check `role_and_news` (`role_status`, `depth_chart_rank`, `supplanted_by`, `recent_news_headlines`). Never recommend picking up or starting a benched player based on historical points or static board tiers."*
  4. **Provenance Citation Rule:** *"Ground every claim in the provided `WorldStateSnapshot` and cite the corresponding `source_id`."*

---

## 5. The 7 QA Probes Regression Matrix (`tests/test_qa_probes.py`)

All 7 failure modes from the v1 post-mortem addendum MUST be encoded as automated, blocking regression tests in `tests/test_qa_probes.py`:

| Probe ID | Addendum Failure Mode | Test Scenario / Input Probe | Required v2 Pass Criteria |
| :--- | :--- | :--- | :--- |
| **Probe 1** | **Stale Awareness on Benched Players** | Prompt: `"Should I pick up Kirk Cousins?"` (where Sleeper `injury_status` is `"Active"`, but depth-chart/news feed shows Cousins benched for Fernando Mendoza) | `WorldStateSnapshot` hydrates Cousins with `role_status="benched"`, `depth_chart_rank=2`, `supplanted_by="Fernando Mendoza"`. Response explicitly warns of the Mendoza benching risk, advises against pickup, and cites `nfl_news_and_depth_charts`. |
| **Probe 2** | **Deflection When Structured Data is Missing** | Prompt asks for trade/start-sit advice when an optional structured sub-field (e.g., red-zone target share or secondary stat) is missing from the snapshot | Response does NOT deflect or tell the user to look elsewhere. It states specifically which data point is missing and completes a reasoned recommendation using available roster, scoring, tier, and matchup context. |
| **Probe 3** | **Lack of Injury Model (Baker Mayfield Probe)** | Prompt asks to evaluate/start Baker Mayfield (or a roster starter/QB carrying an active injury designation/practice limitation) | `InjuryContext` is populated with `status`, `body_part`, `practice_status`, and `is_medical_risk=True`. Response surfaces the injury in `injury_alerts` and models its impact on floor/ceiling and dependent pass-catchers. |
| **Probe 4** | **Acceptance of Unverified/False User Premises** | Prompt contains a false bye-week or starter premise (e.g., `"Since Player X is on bye this week in Week 6, should I drop him?"` when Player X's team is NOT on bye in `nfl_week`) | Guardrail/Reasoner cross-checks `WorldStateSnapshot.nfl_teams_on_bye` and `ValidatedPlayer.bye_week`, explicitly rejects and corrects the false premise in the response, and cites `sleeper_nfl_state`. |
| **Probe 5** | **Cross-Path Contradictions & Week 5/6 Off-By-One** | Query current week, bye weeks, and matchups across (a) standard `/api/chat`, (b) `/api/world-state`, (c) Survivor chat toggle, and (d) Survivor email preview during Week 6 | `resolve_canonical_nfl_week` returns `6` everywhere (never `5`). All four paths report the exact same `nfl_week=6`, `snapshot_id`, and bye-week teams with zero contradiction. |
| **Probe 6** | **Unverifiable Data Provenance for Live Odds & Claims** | Execute both a standard fantasy `/api/chat` query and a verified Survivor query with mocked live odds | Both responses return a non-empty `provenance` list containing `source_id`, `fetched_at_utc`, `age_seconds`, `record_count`, and `status="ok"`, rendered as inspectable provenance badges in the UI payload. |
| **Probe 7** | **Survivor Mode Non-Determinism & Email Team Leak on Missing Odds** | Unset `ODDS_API_KEY` (or simulate Odds API HTTP 500) and run: (a) two identical Survivor chat queries; (b) `SurvivorEmailDispatcher.run()` | (a) Both chat calls short-circuit pre-LLM and return the exact same `SURVIVOR_FAIL_CLOSED_TEMPLATE` byte-for-byte with zero fabricated spreads. (b) Email run aborts (`email_sent=False`, `recommended_team=None`) and passes the 32-NFL-team regex scan proving zero team names were emitted. |

---

## 6. Phased Execution Roadmap & Atomic Git Checkpoints

Execute Phases 0 through 6 sequentially without pausing for human confirmation. At the end of each phase, run `pytest`, update the checkboxes in this file from `[ ]` to `[x]`, and create the specified Conventional Commit.

### Phase 0: Repository Audit, Dependency Hygiene & Git Baseline
- [x] **0.1 Audit Workspace:** Inspect existing files (`main.py`, `requirements.txt`, `Dockerfile`, `player_ids.json`, `board.json`, static/templates) to preserve deployment entrypoints.
- [x] **0.2 Tag Legacy v1 Baseline & Create Branch:**
  - Ensure clean working tree, tag current HEAD as `v1-legacy-rules-engine`, and checkout `feat/v2-reasoner-rearchitecture`.
  - Write this `IMPLEMENTATION_PLAN.md` to `./IMPLEMENTATION_PLAN.md` in the repository root.
- [x] **0.3 Update `requirements.txt`:**
  - Ensure `fastapi`, `uvicorn[standard]`, `pydantic>=2.0.0`, `requests`, `google-genai`, `pytest`, and `httpx` are present and installed.
- [x] **0.4 Commit Phase 0:**
  - Commit message: `chore(v2): initialize v2 rearchitecture branch and tag v1 legacy baseline`

### Phase 1: Schemas, Single-Sourced Week Resolver & `board.json` Bias Auditor
- [x] **1.1 Implement `core/config.py` and `core/exceptions.py`:**
  - Set `DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"`.
  - Define `FantasyAIError`, `DataValidationError`, `UpstreamSourceError`, `ReasonerExecutionError`, and `SurvivorGateError`.
- [x] **1.2 Implement `core/schemas.py` and `core/provenance.py`:**
  - Implement all Pydantic v2 schemas from Section 3 (`ProvenanceRecord`, `InjuryContext`, `RoleAndNewsContext`, `ValidatedPlayer`, `NFLGameOdds`, `SurvivorPickResult`, `WorldStateSnapshot`, `ChatRequest`, `ChatResponse`).
- [x] **1.3 Implement `core/week_resolver.py`:**
  - Implement `resolve_canonical_nfl_week` as the single-sourced NFL week function fixing the Week 5 / Week 6 off-by-one bug.
- [x] **1.4 Implement `core/board_auditor.py`:**
  - Implement startup audit of `board.json` to detect and strip v1 positional bias multipliers and produce `board_audit_summary`.
- [x] **1.5 Create `tests/test_schemas.py` and `tests/test_week_resolver.py`:**
  - Test schema validation, `InjuryContext` and `RoleAndNewsContext` risk flags, `board.json` bias stripping, and Week 5 / Week 6 rollover determinism.
- [x] **1.6 Verify & Commit Phase 1:**
  - Run `pytest tests/test_schemas.py tests/test_week_resolver.py`.
  - Commit message: `feat(core): add Pydantic v2 schemas, canonical week resolver, and board bias auditor`

### Phase 2: Data Plumbing, News/Depth-Chart Pipeline & Odds API Client
- [x] **2.1 Implement `core/data_pipeline.py`:**
  - Validated loaders for `player_ids.json` and `board.json` (piped through `board_auditor`).
  - `SleeperClient` with TTL cache and `ProvenanceRecord` generation for NFL state, league, rosters, matchups (using `resolve_canonical_nfl_week`), and trending players.
  - `OddsAPIClient` fetching live NFL spreads/moneylines from The Odds API (`ODDS_API_KEY`) with full provenance (`source_id="the_odds_api_nfl"`, timestamp, status) and explicit `missing_key` / `failed` states.
- [x] **2.2 Implement `core/news_pipeline.py` & `news_depth_overrides.json`:**
  - Implement depth-chart order, starter/benched role status, `supplanted_by`, and news headline ingestion (`source_id="nfl_news_and_depth_charts"`).
  - Seed/validate baseline transaction & depth-chart records (including Kirk Cousins benched (`role_status="benched"`, `depth_chart_rank=2`, `supplanted_by="Fernando Mendoza"`) and Baker Mayfield injury context) merged with live feed updates.
- [x] **2.3 Remove Legacy v1 Rules Engine:**
  - Remove all v1 static trade calculators, positional multipliers, and regex intent routers.
- [x] **2.4 Verify & Commit Phase 2:**
  - Run `pytest`.
  - Commit message: `feat(pipeline): add Sleeper client, news/depth-chart pipeline, and Odds API ingestion`

### Phase 3: Unified Session `WorldStateSnapshot` Builder
- [x] **3.1 Implement `core/world_model.py`:**
  - Assemble immutable `WorldStateSnapshot` combining single-sourced `nfl_week`, league rosters/matchups, `board_audit_summary`, hydrated `InjuryContext` + `RoleAndNewsContext`, `notable_role_changes` (including query-mentioned player lookup from `player_ids.json`), `current_week_odds`, and all `ProvenanceRecord` entries.
  - Compute deterministic 16-char SHA-256 `snapshot_id` and cache per session.
- [x] **3.2 Create `tests/test_world_model.py`:**
  - Verify snapshot determinism, query-entity hydration (ensuring unrostered players like Kirk Cousins are hydrated into the snapshot when asked about), and provenance aggregation.
- [x] **3.3 Verify & Commit Phase 3:**
  - Run `pytest tests/test_world_model.py`.
  - Commit message: `feat(world-model): assemble unified WorldStateSnapshot with news, odds, and provenance`

### Phase 4: Analytical Reasoner, Guardrails & Unified Survivor Engine
- [x] **4.1 Implement `core/guardrails.py`:**
  - Pre-reasoner premise scanner (detecting bye-week, injury, and benching claims in user prompts and attaching explicit ground-truth verification directives).
  - Post-reasoner entity grounding check, medical risk check, and benching/role-risk alert enforcement.
- [x] **4.2 Implement `core/reasoner.py`:**
  - Configure `google-genai` SDK defaulting to `gemini-3.8-flash`.
  - Embed the verbatim system instructions:
    - *"If structured data is missing, reason from available context and state what is missing; never deflect."*
    - Premise verification, benching/depth-chart priority over static board ranks, and provenance citation rules.
- [x] **4.3 Implement `core/survivor.py`:**
  - Implement `SurvivorEngine.evaluate_week()` as an explicit reasoner path.
  - Enforce the pre-LLM deterministic fail-closed gate returning `SURVIVOR_FAIL_CLOSED_TEMPLATE` whenever `odds_provenance.status != "ok"` or `current_week_odds` is empty.
  - Implement `SurvivorEmailDispatcher` running on the exact same `SurvivorEngine.evaluate_week()` engine with the 32-NFL-team regex safeguard (`assert_no_nfl_team_leaked`) guaranteeing a failed run can never emit a team name.
- [x] **4.4 Create `tests/test_survivor.py` and `tests/test_guardrails.py`:**
  - Verify deterministic fail-closed refusal identity across repeated runs, verified survivor pick path when odds are present, and the email zero-team-leak safeguard.
- [x] **4.5 Verify & Commit Phase 4:**
  - Run `pytest tests/test_survivor.py tests/test_guardrails.py`.
  - Commit message: `feat(reasoner-survivor): implement Gemini 3.8 Flash reasoner, guardrails, and fail-closed survivor engine`

### Phase 5: FastAPI Endpoints, Deep `/health` (with Odds API) & Mobile UI
- [ ] **5.1 Refactor `main.py`:**
  - `GET /health`: Check and report detailed status, timestamps, and record counts for all 6 subsystems: `player_ids_catalog`, `board_catalog` (including bias audit status), `sleeper_api`, `news_depth_chart_feed`, `odds_api`, and `gemini_reasoner`, plus `canonical_nfl_week`.
  - `POST /api/chat`: Support both Fantasy GM mode and Survivor toggle mode (`mode: "fantasy" | "survivor"`), returning `ChatResponse` with full `provenance`, `injury_alerts`, `citations`, and `snapshot_id`.
  - `POST /api/survivor` & `POST /api/survivor/email-preview`: Expose explicit survivor evaluation and weekly email preview endpoints powered by `SurvivorEngine`.
  - `GET /api/world-state`: Return current session `WorldStateSnapshot` summary and provenance records.
- [ ] **5.2 Update Mobile-First UI (`static/` or inline template):**
  - Keep responsive Tailwind CSS + `marked.js` interface.
  - Wire the **Survivor Mode Toggle** directly to the unified survivor engine path.
  - Render inspectable **Provenance Badges** (showing source, timestamp, status), **Injury & Benching Risk Alerts**, and live `/health` subsystem indicators (including Odds API and News feed status).
- [ ] **5.3 Verify & Commit Phase 5:**
  - Verify all routes mount and respond cleanly.
  - Commit message: `feat(api-ui): register odds_api in /health and wire unified survivor and provenance UI`

### Phase 6: The 7 QA Probes Regression Suite & End-to-End Verification
- [ ] **6.1 Implement `tests/test_qa_probes.py`:**
  - Implement all 7 QA Probe regression tests specified in Section 5 (`test_probe_1_kirk_cousins_mendoza_benching`, `test_probe_2_no_deflection_on_missing_structured_data`, `test_probe_3_baker_mayfield_injury_model`, `test_probe_4_reject_false_bye_week_premise`, `test_probe_5_no_cross_path_contradiction_or_week_5_6_bug`, `test_probe_6_inspectable_provenance_on_chat_and_odds`, `test_probe_7_survivor_fail_closed_and_email_zero_team_leak`).
- [ ] **6.2 Implement `tests/test_api.py` & Run Full Suite:**
  - Test `/health`, `/api/chat`, `/api/survivor`, `/api/world-state`, and `/` via FastAPI `TestClient`.
  - Run `pytest -v` across the entire repository and ensure 100% pass rate with 0 errors.
- [ ] **6.3 Finalize Plan & Commit Phase 6:**
  - Mark all checkboxes `[x]` in `IMPLEMENTATION_PLAN.md`.
  - Commit message: `test(qa-probes): encode all 7 QA probes as regression tests and finalize v2 rebuild`

---

## 7. Definition of Done & Verification Matrix

The autonomous execution loop is complete only when every check below passes with exit code 0:

| Verification Gate | Command / Target | Required Outcome |
| :--- | :--- | :--- |
| **Git Rollback Tag** | `git tag -l v1-legacy-rules-engine` | Tag exists on pre-v2 commit |
| **Git Active Branch** | `git branch --show-current` | `feat/v2-reasoner-rearchitecture` |
| **Default Gemini Model** | `core/config.py` | Defaults to `gemini-3.8-flash` |
| **Board Bias Audit** | `core/board_auditor.py` | v1 positional multipliers stripped; audit summary in `/health` |
| **Single-Sourced NFL Week** | `pytest tests/test_week_resolver.py` | Week 5 / Week 6 boundary unit tests pass; single source across all paths |
| **Kirk Cousins Acceptance Test** | `pytest tests/test_qa_probes.py -k probe_1` | Surfaces Fernando Mendoza benching risk & cites `nfl_news_and_depth_charts` |
| **Survivor Fail-Closed & Email Guard** | `pytest tests/test_qa_probes.py -k probe_7` | Fixed refusal template on missing odds; failed email run emits zero NFL team names |
| **All 7 QA Probes Suite** | `pytest tests/test_qa_probes.py -v` | All 7 QA probe tests pass 100% |
| **Deep `/health` Check** | `GET /health` | Reports status & provenance for Sleeper, Catalogs, News/Depth-Chart, Odds API, and Gemini |