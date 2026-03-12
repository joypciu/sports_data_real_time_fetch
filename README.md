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
├── check_data.py    — spot-check data integrity
├── query_tests.py   — example DuckDB queries (top scorers, standings, etc.)
└── verify_db.py     — assert row counts and schema after a build
```

## Tests

```bash
python tests/test_suite.py   # 96 unit tests covering parsing, dedup, enrichment
```

## Requirements

```
httpx
duckdb
```

Install:

```bash
pip install httpx duckdb
```

## Setup

```bash
# 1. Backfill historical data (21 days all leagues)
python daily_ingest.py --days 21

# 2. Build the database
python build_db.py --rebuild

# 3. (Optional) Enrich with Flashscore data
SPORTDB_API_KEY=your_key python enrich_flashscore.py --no-players

# 4. Start the live monitor
python realtime_monitor.py

# 5. Schedule daily ingest (Windows)
powershell -File schedule_daily.ps1
```
