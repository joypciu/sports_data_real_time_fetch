# Realtime Match Fetch

A sports data pipeline that pulls live and historical game data from ESPN's public API, enriches it with secondary sources, and loads it into a queryable DuckDB database — with a unified process manager that runs the API, live monitor, and DB auto-updater concurrently.

## What It Does

- **Fetches** live + historical scores, odds (moneyline / spread / totals), win probabilities, and full per-player box scores from ESPN across 22+ leagues
- **Persists** game records to per-sport JSON files in `historical_data/`
- **Enriches** records with Flashscore data — period/quarter scores, team logos, match-level team stats (possession, shots, rebounds, etc.), and multi-bookmaker odds
- **Loads** everything into a DuckDB database for fast SQL queries
- **Monitors** pregame and live matches in real-time, emitting typed change events (`LINE_MOVE`, `ODDS_MOVE`, `NEW_GAME_DISCOVERED`, `GAME_STARTED`, `SCORE_UPDATE`, `WIN_PROB_SHIFT`, …) and writing a live dashboard
- **Auto-syncs** the database: incremental historical inserts every 5 minutes + a volatile `live_games` table refreshed every 35 seconds from the live monitor's output
- **Runs everything together** via `main.py` — a single supervised entry point for the API, monitor, and DB updater

## Supported Leagues

| Sport             | Leagues                                                                                                  |
| ----------------- | -------------------------------------------------------------------------------------------------------- |
| Basketball        | NBA, NCAAB (NCAA Men's)                                                                                  |
| Ice Hockey        | NHL                                                                                                      |
| American Football | NFL, NCAAF (College Football)                                                                            |
| Baseball          | MLB                                                                                                      |
| Soccer            | EPL, La Liga, Bundesliga, Ligue 1, MLS, UCL, UEL                                                         |
| Cricket           | Sheffield Shield, IPL, Big Bash League, T20 World Cup Qualifier, SuperSport Series, Tri-Nation, BPL, BCL |

## Scripts

### `fetch_matches.py`

One-shot fetch for any leagues and date range. Outputs a timestamped JSON file.

```bash
python fetch_matches.py                                # all leagues, 15 days
python fetch_matches.py --leagues nba nhl --days 7
python fetch_matches.py --output results.json
python fetch_matches.py --no-players --summary-only
```

### `daily_ingest.py`

Daily append to `historical_data/` sport files. Deduplicates by event ID.

```bash
python daily_ingest.py                          # yesterday's games, all leagues
python daily_ingest.py --date 2026-03-11        # specific date
python daily_ingest.py --days 21                # backfill last 21 days
python daily_ingest.py --leagues nba nhl        # specific leagues only
```

### `enrich_players.py`

Fills in player box-score data for any game record that has an empty `players` list.

```bash
python enrich_players.py                          # all historical_data/ files
python enrich_players.py --file historical_data/nba.json
```

### `enrich_flashscore.py`

Adds secondary enrichment from the Flashscore API (via sportdb.dev) and Transfermarkt:

- All sports: period/quarter scores, team logos, match stats, bookmaker odds
- Soccer only: Transfermarkt player market values, position, nationality

```bash
python enrich_flashscore.py                  # all files
python enrich_flashscore.py --file nba.json  # one file
python enrich_flashscore.py --no-players     # skip Transfermarkt player lookups
python enrich_flashscore.py --no-match-stats # skip Flashscore match stats/odds
```

Requires a [sportdb.dev](https://sportdb.dev) API key (set `SPORTDB_API_KEY` env var).

### `build_db.py`

Builds or refreshes `db/sports.db` from the JSON files.

```bash
python build_db.py           # incremental refresh
python build_db.py --rebuild # drop & recreate all tables
```

### `realtime_monitor.py`

Polls ESPN every N seconds. Handles both **pregame** and **live** matches with different logic for each:

- **Pregame games**: fetched on every cycle; odds and win-probability API calls are rate-limited (every `PREGAME_ODDS_REFRESH_EVERY` cycles, ~150 s) to avoid hammering the API before tip-off. Emits `NEW_GAME_DISCOVERED` (first sight) and `LINE_MOVE` (betting-line shifts) events.
- **Live games**: full odds + win-probability refresh every cycle; player box scores refreshed every `PLAYER_REFRESH_EVERY` cycles (~90 s). Emits `GAME_STARTED`, `SCORE_UPDATE`, `PERIOD_CHANGE`, `WIN_PROB_SHIFT`, `ODDS_MOVE` events.
- **Finished games**: auto-archived to `historical_data/` on the same cycle the status flips to `post`.

Writes three output files per poll cycle:

| File                         | Contents                                                |
| ---------------------------- | ------------------------------------------------------- |
| `live/live_state.json`       | Combined snapshot: `pregame`, `live`, `finished` arrays |
| `live/live_YYYYMMDD.json`    | In-progress games only                                  |
| `live/pregame_YYYYMMDD.json` | Upcoming games only                                     |
| `live/events_YYYYMMDD.jsonl` | Append-only typed change-event log                      |

```bash
python realtime_monitor.py                    # all leagues, every 30s
python realtime_monitor.py --interval 60
python realtime_monitor.py --leagues nba nhl
python realtime_monitor.py --no-players       # skip player box score pulls
```

### `live_sources.py`

Fetches live games from two external bookmaker feeds — **365scores** and **1xbet** — and generates synthetic market odds from live scores. Used by `realtime_monitor.py` to augment ESPN coverage with games ESPN does not track.

**Sports covered:** Soccer, basketball, ice hockey, baseball (from 365scores); soccer, basketball, ice hockey, cricket, tennis, table tennis, volleyball (from 1xbet).

**Virtual game filter:** Games with team names matching cyber/virtual/esport patterns, or leagues with names like "Cyber", "Virtual", "LFL", or short-form tags (2x2–6x6) are automatically excluded.

**Deduplication:** Games appearing in both sources are merged using a token built from `{sport}:{sorted team name last words}`. Games already tracked by ESPN are also deduplicated and skipped.

**Event ID format:** 365scores games use `365s_{id}` prefix; 1xbet games use `1xb_{id}` prefix — these never collide with ESPN numeric IDs.

#### Odds generation

When a bookmaker feed does not provide market odds, `generate_odds()` derives synthetic moneyline, spread, and total from the live score using sport-specific probability models:

| Sport        | Model                                                                              |
| ------------ | ---------------------------------------------------------------------------------- |
| Soccer       | Sigmoid on score diff scaled by time elapsed; draw probability added to favourite  |
| Basketball   | Score diff / sqrt(minutes remaining × 3)                                           |
| Hockey       | Score diff / sqrt(minutes remaining × 0.5)                                         |
| Baseball     | Score diff × 0.8 × fraction of innings complete                                    |
| Cricket      | Score diff (runs) → sigmoid; displays innings number and overs from 1xbet SC.S     |
| Tennis       | Sets won difference → sigmoid; period = current set number                         |
| Table Tennis | Sets won difference → sigmoid; period = current set number                         |
| Volleyball   | Sets won difference → sigmoid; period = current set number                         |

Win probabilities are converted to American odds (`_prob_to_american`). Spread is set to ±0.5 of the score diff; total is set to the live combined score plus a sport-specific expected remaining total. All generated odds carry `"provider": "generated"`.

**ESPN live fallback:** `realtime_monitor.py` also calls `generate_odds()` for any ESPN-tracked live game that ESPN returns no odds for.

#### External source merge in `realtime_monitor.py`

Every second poll cycle (~60 s at 30 s interval), `run()` calls `fetch_external_live()` and merges results into the in-memory `states` dict:

- Games already tracked by ESPN are skipped (no duplicate entries)
- New external games are added with `event_id` = `365s_*` or `1xb_*`
- Existing external games are refreshed with updated scores and generated odds
- Games that have left the live feed are removed (stale-entry cleanup)

External games are written to `live_state.json` alongside ESPN games and are immediately visible to `stats_api.py`.

```python
from live_sources import fetch_external_live, generate_odds

# Fetch all live external games (deduplicated)
games = fetch_external_live()          # dict[event_id, game_state_dict]

# Generate odds from a live score
odds = generate_odds("soccer", home_score=2, away_score=1,
                     status_text="65'", period=2)
# Returns: {"moneyline": {"home": -220, "away": +350},
#           "spread": {"home": -0.5, "line": -110, "away": +0.5},
#           "total": {"line": 3.5, "over": -115, "under": -105},
#           "provider": "generated"}
```

### `stats_api.py`

Internal FastAPI service (port 8001) that exposes the DuckDB database and live state to other services (used by the Cache API's optional `include_stats` enrichment and `/event/check` market evaluation).

**DuckDB connection mode:** All thread-local connections use read-write mode (`duckdb.connect(DB_PATH)` — no `read_only=True`). This is required because DuckDB does not allow mixing read-only and read-write connections in the same process.

```bash
python stats_api.py                      # start on port 8001
STATS_API_TOKEN=secret python stats_api.py
```

Endpoints:

| Endpoint                                       | Description                                                                      |
| ---------------------------------------------- | -------------------------------------------------------------------------------- |
| `GET /health`                                  | Liveness probe — checks DB connectivity and live_state.json presence             |
| `GET /stats/player?name=Raphinha&sport=soccer` | Per-player recent game stats (ILIKE name search)                                 |
| `GET /stats/team?name=Barcelona&sport=soccer`  | Win/loss record, last 5 results, top scorers                                     |
| `GET /stats/live?team=Barcelona`               | Live / pregame entries from `live/live_state.json`                               |
| `GET /stats/market-check?...`                  | Resolve one event and evaluate `moneyline`, `spread` / `game spread`, or `total` |
| `GET /stats/trends?team=OKC&sport=basketball`  | ATS, O/U, home/away splits + recent form for a team (from DuckDB)                |

Authentication is optional: set `STATS_API_TOKEN` in `.env` to require a bearer token. Leave blank for VPS-internal use.

`/stats/market-check` accepts either `event_id`, or `date` + at least one team name (`team` and/or `opponent` — opponent is optional). When only one team is provided, the endpoint searches for any game on that date involving that team on either side. Plus `market`, `pick`, optional `line`, and optional `sport`. `market=game spread` is treated as `spread`. It searches live state first, then historical DuckDB rows, and returns a normalized payload including `found`, `source`, `settled`, `result`, `outcome`, `event`, `score`, and `pricing`. Invalid dates are rejected with `400` (`YYYY-MM-DD` only).

**Pre-game guard:** ESPN sets home/away scores to `0-0` for games with `status: pre`. The evaluator detects this via `status_norm == "pre"` and returns `outcome: pending` with `score: null` for all three market types (`moneyline`, `spread`, `total`) rather than evaluating the meaningless 0-0 score. A settled result is only computed when `status_norm == "post"`.

`/stats/trends` accepts `team` (required), `sport`, `league`, and `limit` (5–200, default 50). Queries DuckDB directly to compute ATS cover rate, O/U over rate, straight-up win record, and home/away splits for the last N completed games. Returns `recent_form` (last 10 games with per-game result) plus aggregate `overall`, `home`, and `away` blocks.

### `update_db.py`

Background DB maintenance module — runs as Thread 3 inside `main.py`, or standalone.

**Four jobs run on separate schedules:**

| Job                           | Default interval   | What it does                                                                                                                                                              |
| ----------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auto-backfill gaps            | Startup + every 6h | Detects missing date ranges per sport in DuckDB, fetches from ESPN, writes `historical_data/backfill_<league>.json`                                                       |
| Incremental historical update | Every 5 minutes    | Scans `historical_data/*.json`, finds event IDs not in `games`, inserts via `build_db.load_file()` — uses UPSERT logic (skips finalized games, updates stale in-progress) |
| Live-games sync               | Every 35 seconds   | Reads `live/live_state.json`, `DELETE`s old `live_games` rows, `INSERT`s current pre/live/post snapshot                                                                   |
| Vacuum DB                     | Every 6 hours      | Runs `VACUUM ANALYZE` to reclaim space and update statistics                                                                                                              |

The live sync only fires when `live_state.json` mtime has changed, so it adds zero DB pressure when the monitor is idle. After each game ingest, the Redis settlement cache keys (`stats_bridge:market:*`) are flushed so that pending bets re-evaluate against the fresh data.

**Auto-backfill:** On startup, `auto_backfill_gaps()` queries DuckDB for the most recent game date per sport. If there is a gap between that date and today, it fetches ESPN scoreboard data for the missing days and drops the results into `historical_data/` for the incremental updater to pick up on its next cycle. This ensures bets placed on recently completed games settle automatically without any manual intervention.

```bash
python update_db.py                         # run the loop standalone
python update_db.py --hist-interval 600     # slower historical rescans
python update_db.py --live-interval 20      # faster live sync
```

**IMPORTANT — DuckDB connection rules:**

- Never use `read_only=True` connections in the same process as read-write connections. Mixing modes causes a `FatalException: different configuration` crash that corrupts the DB.
- All connections in `stats_api.py` use read-write mode (no `read_only=True` parameter).
- `SET memory_limit='4GB'` and `SET threads=4` are set only on the write connection in `update_db.py`.

### `main.py`

Unified supervised entry point — runs all three components concurrently in daemon threads with automatic crash-restart:

| Thread             | Daemon | What it runs                                           |
| ------------------ | ------ | ------------------------------------------------------ |
| `stats-api`        | ✓      | `uvicorn stats_api:app` on configured port             |
| `realtime-monitor` | ✓      | `realtime_monitor.run()` ESPN poll loop                |
| `db-updater`       | ✓      | `update_db.run_updater_loop()` incremental + live sync |

The main thread health-checks all three every 5 seconds and restarts any that crash. `SIGINT`/`SIGTERM` handled cleanly.

```bash
python main.py                        # all leagues, 30s poll, port 8001
python main.py --interval 60          # slower polling
python main.py --leagues nba nhl      # specific leagues only
python main.py --no-players           # skip player box score pulls
python main.py --port 8002            # different API port
python main.py --api-only             # skip monitor and DB updater
python main.py --monitor-only         # skip API (DB updater still runs)
```

### `schedule_daily.ps1`

PowerShell script to register the daily ingest as a Windows Task Scheduler job.

## Database Schema

```
db/sports.db  (DuckDB)
├── teams         — master team list (team_id, sport, name, abbr)
├── players       — master player list (player_id, sport, display_name, position)
├── games         — one row per finished/historical event (scores, odds, win_prob, Flashscore fields)
├── game_teams    — home/away side per game (moneyline, spread, score, winner)
├── game_players  — per-player per-game appearance (starter/active flags)
├── player_stats  — EAV: one row per (game_player_id, stat_key, stat_value)
└── live_games    — volatile live state: one row per active/upcoming/just-finished game (replaced every poll cycle by update_db)
```

### `live_games` table

Populated every ~35 seconds by `update_db.sync_live_games()`. Always reflects the latest `live_state.json` snapshot. Useful for real-time dashboards that query SQL instead of parsing JSON.

| Column                          | Type       | Description                      |
| ------------------------------- | ---------- | -------------------------------- |
| `event_id`                      | VARCHAR PK | ESPN event ID                    |
| `status`                        | VARCHAR    | `pre` / `in` / `post`            |
| `period`                        | INTEGER    | Current period / half            |
| `clock`                         | VARCHAR    | Display clock e.g. `4:22`        |
| `home_score` / `away_score`     | VARCHAR    | Current score strings            |
| `home_ml` / `away_ml`           | INTEGER    | Current moneyline odds           |
| `home_spread` / `game_total`    | DOUBLE     | Current spread / total           |
| `home_win_pct` / `away_win_pct` | DOUBLE     | Live win probability             |
| `situation`                     | JSON       | Linescores + live play info blob |
| `players`                       | JSON       | Full player-stats array blob     |
| `updated_at`                    | TIMESTAMP  | Poll cycle timestamp             |

The `games` table also holds Flashscore-enriched fields when available:

| Field                     | Description                                                                             |
| ------------------------- | --------------------------------------------------------------------------------------- |
| `fs_id`                   | Flashscore event ID                                                                     |
| `home_logo` / `away_logo` | Team logo URLs                                                                          |
| `fs_tournament`           | Flashscore tournament name                                                              |
| `period_scores`           | Per-period breakdown e.g. `{"home":["38","24","34","32"],"away":["35","27","27","33"]}` |
| `match_stats`             | Team stats per period e.g. possession, shots, rebounds                                  |
| `fs_odds`                 | Multi-bookmaker opening + closing odds                                                  |

Soccer player records also carry Transfermarkt fields: `tm_id`, `tm_position`, `market_value_eur`, `nationality`, `tm_age`.

### KeepBetting market historics

`GET /stats/market/historics?context=...` validates and proxies the signed
KeepBetting market context. It returns per-book American-odds timelines and
the no-vig timeline used by the bet-tracking service for closing-line value.
The endpoint uses the normal optional `STATS_API_TOKEN` bearer authentication.

## Historical Data

`historical_data/` holds one JSON file per sport. Each file is an array of game objects, each containing:

- Game metadata (id, name, date, status, league, sport)
- Home and away team records with scores, odds, spread, totals
- Win probabilities
- Full player list with per-player box-score stats
- Flashscore enrichment fields (where available)

Files are git-tracked so the dataset accumulates across runs.

## Tools

```
tools/
├── audit_data.py    — count games / players per file, detect gaps
├── audit_stats.py   — stat key coverage report
└── check_data.py    — spot-check data integrity
```

The query validation and DB verification scripts live in `tests/` (run independently of the unit test suite):

```bash
python tests/query_tests.py   # 114 PASS — example DuckDB queries, top scorers, standings, advanced analytics
python tests/verify_db.py     # 111 checks — row counts, schema, data integrity regressions
```

## Tests

All test scripts live in `tests/`.

### Unit tests (`tests/test_suite.py`)

**270 self-contained unit tests** across 23 numbered sections covering every module. No live API calls — all ESPN responses are mocked.

| Section | Module             | Tests                                                                                                                                                         |
| ------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1–6     | `enrich_players`   | parse_players, parse_soccer_roster, enrich_game, SPORT_FILE_PREFIX, file helpers, enrich_file                                                                 |
| 7–9     | `daily_ingest`     | load_existing_ids, append_games, parse_game_to_dict                                                                                                           |
| 10      | `fetch_matches`    | helper functions                                                                                                                                              |
| 11–13   | `realtime_monitor` | helper functions, save_live_state (pregame + live + dated files), archive_finished_game                                                                       |
| 14      | Integration        | round-trip enrich_file → data integrity                                                                                                                       |
| 15–16   | `build_db`         | scalar helpers (\_ts, \_int, \_float, \_score), load_file in-memory DuckDB                                                                                    |
| 17      | DB Integration     | live sports.db row counts, schema, sport-specific edge cases                                                                                                  |
| 18      | `realtime_monitor` | detect_changes: GAME_STARTED/FINISHED, SCORE_UPDATE, PERIOD_CHANGE, WIN_PROB_SHIFT (live only), ODDS_MOVE (live) vs LINE_MOVE (pregame), TOTAL_MOVE           |
| 19      | `realtime_monitor` | Pregame rate-limiting: PREGAME_ODDS_REFRESH_EVERY, dated file correctness, refresh_extras param                                                               |
| 20      | `build_db`         | live_games DDL: table exists, required columns, insert/delete, DROP_ALL includes live_games                                                                   |
| 21      | `update_db`        | incremental_historical_update (idempotent, multi-file), sync_live_games (all buckets, stale-row replacement, corrupt JSON, edge cases)                        |
| 22      | `stats_api`        | `/stats/market-check` evaluation for historical/live total, spread, moneyline, invalid-date validation, and pre-game guard (outcome: pending when status=pre) |
| 23      | `main`             | Module structure, thread targets (\_run_api, \_run_monitor, \_run_updater), signatures, signal handling                                                       |

```bash
pytest tests/test_suite.py        # 270 tests
pytest tests/test_suite.py -v     # verbose with test names
```

### DB integrity (`tests/verify_db.py`)

111 checks across 13 sections: row counts, schema, sport-specific logic, regression anchors (Barcelona event, cricket innings scores, is_winner fix).

```bash
python tests/verify_db.py    # exits 0 on all-pass, 1 on any failure
```

### Query validation (`tests/query_tests.py`)

114 checks across 30 sections: standings, top scorers, odds queries, cricket integrity, timezone safety, and advanced analytics (ERA/WHIP/K9, goalkeeper save percentage, shot conversion).

```bash
python tests/query_tests.py
```

## Requirements

```
httpx          # ESPN + sportdb.dev API calls
duckdb         # database engine
pandas         # data loading in build_db.py
fastapi        # stats_api.py service
uvicorn        # stats_api.py ASGI server
python-dotenv  # .env file loading
pytest         # test runner
```

Install:

```bash
pip install -r requirements.txt
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create local env file and set any secrets
cp .env.example .env

# 3. Backfill historical data (21 days all leagues)
python daily_ingest.py --days 21

# 4. Build the database (creates all tables including live_games)
python build_db.py --rebuild

# 5. (Optional) Enrich with Flashscore data
SPORTDB_API_KEY=your_key python enrich_flashscore.py --no-players

# 6. Run everything together (API + monitor + DB auto-updater)
python main.py

# --- Or run components individually ---

# Start only the stats API
python stats_api.py

# Start only the live monitor
python realtime_monitor.py

# Start only the DB updater (keeps live_games and historical tables in sync)
python update_db.py

# 7. Schedule daily ingest (Windows)
powershell -File schedule_daily.ps1
```

## CI/CD

Two GitHub Actions workflows live in `.github/workflows/`.

### `deploy.yml`

Triggered on push to `main`, manual dispatch, or PR targeting `main`.

**`validate` job** (runs on every PR and push):

1. Sets up Python 3.12 and installs `requirements.txt`
2. `py_compile` syntax check on all nine core scripts (`build_db`, `daily_ingest`, `fetch_matches`, `enrich_players`, `enrich_flashscore`, `realtime_monitor`, `stats_api`, `update_db`, `main`)
3. `pytest tests/test_suite.py` — 270 unit tests, all mocked (no live API, no real DB)

**`deploy` job** (push to `main` only, skipped on PRs):

1. SSHes into the VPS and runs `deploy.sh` (up to 2 attempts with a 10-second retry gap)
2. `deploy.sh` pulls code, updates the venv, writes/updates the `sports-stats-api` systemd unit and the `daily-ingest` systemd timer, then restarts the stats API
3. Post-deploy smoke test: SSHes back in and `curl`s `GET /health` — fails the workflow if the service is not responding
4. On failure: sends an email alert via Gmail (configured via repository secrets)

### `cleanup.yml`

Triggered when a pull request is closed (merged or declined). Automatically deletes the head branch (skips `main` and `develop`).

### Required repository secrets

| Secret                | Description                                                          |
| --------------------- | -------------------------------------------------------------------- |
| `VPS_HOST`            | VPS IP address or hostname                                           |
| `VPS_PORT`            | SSH port (usually `22`)                                              |
| `VPS_USERNAME`        | SSH user (expects `ubuntu`)                                          |
| `VPS_SSH_KEY`         | Private SSH key                                                      |
| `DEPLOY_SERVICE_NAME` | systemd service name (e.g. `sports-stats-api`)                       |
| `DEPLOY_DIR`          | Absolute path on VPS (e.g. `/home/ubuntu/services/sports-stats-api`) |
| `DEPLOY_BRANCH`       | Git branch to deploy (e.g. `main`)                                   |
| `DEPLOY_PORT`         | Stats API port (e.g. `8001`)                                         |
| `DEPLOY_REPO_URL`     | Full HTTPS clone URL                                                 |
| `DEPLOY_REPO_SLUG`    | `owner/repo` slug for remote verification                            |
| `GMAIL_SENDER`        | Gmail address for failure alerts                                     |
| `GMAIL_APP_PASSWORD`  | 16-character Gmail App Password                                      |
| `ADMIN_EMAIL`         | Email address to receive alerts                                      |

## VPS deployment

`deploy.sh` handles first-run and incremental deploys:

- Acquires a lock file (`/tmp/<service-name>.deploy.lock`) to prevent parallel deploy races
- Verifies it is running as the `ubuntu` user
- Clones the repo if not already present; otherwise `git pull`
- Creates/updates a Python virtual environment and installs `requirements.txt`
- Writes/updates two systemd units:
  - `<SERVICE_NAME>.service` — runs `uvicorn stats_api:app` continuously on the configured port (replace with `python main.py` to run all three threads under one process)
  - `<SERVICE_NAME>-ingest.timer` + `.service` — fires `daily_ingest.py` every day at 06:00 UTC
- Reloads the systemd daemon and restarts the stats API service
- Verifies the service is active before returning

Useful VPS commands:

```bash
# Watch live logs
sudo journalctl -u sports-stats-api -f

# Check service status
sudo systemctl status sports-stats-api --no-pager

# List timers
sudo systemctl list-timers sports-stats-api-ingest.timer

# Trigger the ingest manually
sudo systemctl start sports-stats-api-ingest

# Health check
curl http://localhost:8001/health
```
