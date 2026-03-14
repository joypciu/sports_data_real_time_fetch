# Realtime Match Fetch

A sports data pipeline that pulls live and historical game data from ESPN's public API, enriches it with secondary sources, and loads it into a queryable DuckDB database.

## What It Does

- **Fetches** live + historical scores, odds (moneyline / spread / totals), win probabilities, and full per-player box scores from ESPN across 22+ leagues
- **Persists** game records to per-sport JSON files in `historical_data/`
- **Enriches** records with Flashscore data — period/quarter scores, team logos, match-level team stats (possession, shots, rebounds, etc.), and multi-bookmaker odds
- **Loads** everything into a DuckDB database for fast SQL queries
- **Monitors** live matches in real-time, emitting a change-event log and a live dashboard

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

Polls ESPN every N seconds. Prints a live terminal dashboard, writes a `live_state.json` snapshot, and appends a `events_YYYYMMDD.jsonl` change log.

```bash
python realtime_monitor.py                    # all leagues, every 30s
python realtime_monitor.py --interval 60
python realtime_monitor.py --leagues nba nhl
```

### `stats_api.py`

Internal read-only FastAPI service (port 8001) that exposes the DuckDB database and live state to other services (used by the Cache API's optional `include_stats` enrichment).

```bash
python stats_api.py                      # start on port 8001
STATS_API_TOKEN=secret python stats_api.py
```

Endpoints:

| Endpoint                                       | Description                                                          |
| ---------------------------------------------- | -------------------------------------------------------------------- |
| `GET /health`                                  | Liveness probe — checks DB connectivity and live_state.json presence |
| `GET /stats/player?name=Raphinha&sport=soccer` | Per-player recent game stats (ILIKE name search)                     |
| `GET /stats/team?name=Barcelona&sport=soccer`  | Win/loss record, last 5 results, top scorers                         |
| `GET /stats/live?team=Barcelona`               | Live / pregame entries from `live/live_state.json`                   |

Authentication is optional: set `STATS_API_TOKEN` in `.env` to require a bearer token. Leave blank for VPS-internal use.

### `schedule_daily.ps1`

PowerShell script to register the daily ingest as a Windows Task Scheduler job.

## Database Schema

```
db/sports.db  (DuckDB)
├── teams         — master team list (team_id, sport, name, abbr)
├── players       — master player list (player_id, sport, display_name, position)
├── games         — one row per event (scores, odds, win_prob, Flashscore fields)
├── game_teams    — home/away side per game (moneyline, spread, score, winner)
├── game_players  — per-player per-game appearance (starter/active flags)
└── player_stats  — EAV: one row per (game_player_id, stat_key, stat_value)
```

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

206 self-contained unit tests covering parsing, deduplication, enrichment logic, and edge cases across all sports. No live API calls — all ESPN responses are mocked.

```bash
python tests/test_suite.py   # runs via unittest, verbose output
pytest tests/test_suite.py   # alternative runner
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

# 4. Build the database
python build_db.py --rebuild

# 5. (Optional) Enrich with Flashscore data
SPORTDB_API_KEY=your_key python enrich_flashscore.py --no-players

# 6. Start the stats API (internal service, port 8001)
python stats_api.py

# 7. Start the live monitor
python realtime_monitor.py

# 8. Schedule daily ingest (Windows)
powershell -File schedule_daily.ps1
```

## CI/CD

Two GitHub Actions workflows live in `.github/workflows/`.

### `deploy.yml`

Triggered on push to `main`, manual dispatch, or PR targeting `main`.

**`validate` job** (runs on every PR and push):

1. Sets up Python 3.12 and installs `requirements.txt`
2. `py_compile` syntax check on all seven core scripts
3. `pytest tests/test_suite.py` — 206 unit tests, all mocked (no live API, no real DB)

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
  - `<SERVICE_NAME>.service` — runs `uvicorn stats_api:app` continuously on the configured port
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
