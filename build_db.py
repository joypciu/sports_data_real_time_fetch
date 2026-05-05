"""
build_db.py
===========
Builds (or rebuilds) a DuckDB database from all JSON files in historical_data/.

Schema
------
  teams        — master team list (team_id, sport, name, abbr)
  players      — master player list (player_id, sport, display_name, position)
  games        — one row per event (scores, odds, win_prob, etc.)
  game_teams   — home/away side per game (moneyline, spread, score, winner)
  game_players — per-player per-game appearance (starter, active, sub flags)
  player_stats — EAV: one row per (game_player_id, stat_key, stat_value)

Usage:
    python build_db.py                          # build/refresh db/sports.db
    python build_db.py --db path/to/other.db    # custom db path
    python build_db.py --rebuild                # drop & recreate all tables
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import duckdb
import pandas as pd

DATA_DIR = "historical_data"
DEFAULT_DB = "db/sports.db"


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS teams (
    team_id     VARCHAR NOT NULL,
    sport       VARCHAR NOT NULL,
    team_name   VARCHAR,
    team_abbr   VARCHAR,
    PRIMARY KEY (team_id, sport)
);

CREATE TABLE IF NOT EXISTS players (
    player_id    VARCHAR NOT NULL,
    sport        VARCHAR NOT NULL,
    display_name VARCHAR,
    position     VARCHAR,
    team_id      VARCHAR,
    team_name    VARCHAR,
    PRIMARY KEY (player_id, sport)
);

CREATE TABLE IF NOT EXISTS games (
    event_id        VARCHAR PRIMARY KEY,
    sport           VARCHAR NOT NULL,
    league          VARCHAR,
    name            VARCHAR,
    short_name      VARCHAR,
    game_date       TIMESTAMP,          -- stored as UTC, no tz conversion
    status          VARCHAR,           -- pre / in / post
    status_detail   VARCHAR,
    period          INTEGER,
    clock           VARCHAR,
    home_score      INTEGER,
    away_score      INTEGER,
    -- odds / totals
    provider        VARCHAR,
    game_total      DOUBLE,
    over_odds       INTEGER,
    under_odds      INTEGER,
    open_spread     DOUBLE,
    open_total      DOUBLE,
    draw_odds       INTEGER,
    -- win probability
    home_win_pct    DOUBLE,
    away_win_pct    DOUBLE,
    -- soccer extras
    home_formation  VARCHAR,
    away_formation  VARCHAR
);

CREATE TABLE IF NOT EXISTS game_teams (
    id              VARCHAR PRIMARY KEY,   -- event_id || '_' || home_away
    event_id        VARCHAR NOT NULL REFERENCES games(event_id),
    team_id         VARCHAR NOT NULL,
    sport           VARCHAR NOT NULL,
    home_away       VARCHAR NOT NULL,      -- home / away
    score           INTEGER,
    is_winner       BOOLEAN,
    moneyline       INTEGER,
    spread          DOUBLE,
    spread_odds     INTEGER,
    team_total      DOUBLE
);

CREATE TABLE IF NOT EXISTS game_players (
    id              VARCHAR PRIMARY KEY,   -- event_id || '_' || player_id
    event_id        VARCHAR NOT NULL REFERENCES games(event_id),
    player_id       VARCHAR NOT NULL,
    sport           VARCHAR NOT NULL,
    team_id         VARCHAR,
    home_away       VARCHAR,
    starter         BOOLEAN,
    active          BOOLEAN,
    did_not_play    BOOLEAN,
    dnp_reason      VARCHAR,
    subbed_in       BOOLEAN,
    subbed_out      BOOLEAN,
    formation_place VARCHAR,
    stats_json      VARCHAR            -- JSON dict of {stat_key: stat_value}
);

CREATE TABLE IF NOT EXISTS player_stats (
    id              VARCHAR PRIMARY KEY,   -- game_player_id || '_' || stat_key
    game_player_id  VARCHAR NOT NULL REFERENCES game_players(id),
    stat_key        VARCHAR NOT NULL,
    stat_value      VARCHAR               -- kept as text; cast at query time
);

-- Useful indexes
CREATE INDEX IF NOT EXISTS idx_players_name      ON players(display_name);
CREATE INDEX IF NOT EXISTS idx_teams_name        ON teams(team_name);
CREATE INDEX IF NOT EXISTS idx_games_sport       ON games(sport);
CREATE INDEX IF NOT EXISTS idx_games_league      ON games(league);
CREATE INDEX IF NOT EXISTS idx_games_date        ON games(game_date);
CREATE INDEX IF NOT EXISTS idx_game_teams_event  ON game_teams(event_id);
CREATE INDEX IF NOT EXISTS idx_game_teams_team   ON game_teams(team_id, sport);
CREATE INDEX IF NOT EXISTS idx_game_players_event  ON game_players(event_id);
CREATE INDEX IF NOT EXISTS idx_game_players_player ON game_players(player_id, sport);
CREATE INDEX IF NOT EXISTS idx_player_stats_gp     ON player_stats(game_player_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_key    ON player_stats(stat_key);

-- Highly-volatile live game state (replaced every poll cycle by update_db.py)
CREATE TABLE IF NOT EXISTS live_games (
    event_id        VARCHAR PRIMARY KEY,
    league_key      VARCHAR,
    sport           VARCHAR,
    league          VARCHAR,
    name            VARCHAR,
    status          VARCHAR,        -- 'pre' | 'in' | 'post'
    status_detail   VARCHAR,
    period          INTEGER,
    clock           VARCHAR,
    home_team_id    VARCHAR,
    home_team_name  VARCHAR,
    home_team_abbr  VARCHAR,
    home_score      VARCHAR,
    away_team_id    VARCHAR,
    away_team_name  VARCHAR,
    away_team_abbr  VARCHAR,
    away_score      VARCHAR,
    home_ml         INTEGER,
    away_ml         INTEGER,
    home_spread     DOUBLE,
    game_total      DOUBLE,
    home_win_pct    DOUBLE,
    away_win_pct    DOUBLE,
    situation       JSON,           -- linescores / play-by-play blob
    players         JSON,           -- full player-stats array blob
    updated_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_live_games_status ON live_games(status);
CREATE INDEX IF NOT EXISTS idx_live_games_sport  ON live_games(sport);
"""

DROP_ALL = """
DROP TABLE IF EXISTS live_games;
DROP TABLE IF EXISTS player_stats;
DROP TABLE IF EXISTS game_players;
DROP TABLE IF EXISTS game_teams;
DROP TABLE IF EXISTS games;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS teams;
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _score(v) -> int | None:
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return None


def _ts(v: str | None) -> str | None:
    """Normalise ESPN ISO-8601 date strings to a plain UTC TIMESTAMP string.

    Strips any timezone offset so DuckDB TIMESTAMP stores the bare UTC value
    without session-timezone conversion (avoids the pytz dependency entirely).
    """
    if not v:
        return None
    # Normalise 'Z' → '+00:00' then pad missing seconds
    v = v.replace("Z", "+00:00")
    v = re.sub(r"T(\d{2}:\d{2})([\+\-])", r"T\1:00\2", v)
    # Strip timezone offset — all ESPN dates are UTC, store as plain timestamp
    v = re.sub(r"[\+\-]\d{2}:\d{2}$", "", v)
    return v


# ---------------------------------------------------------------------------
# Load one JSON file into the DB
# ---------------------------------------------------------------------------

def load_file(con: duckdb.DuckDBPyConnection, path: str) -> tuple[int, int, int]:
    """Insert all games from a JSON file. Returns (games_added, players_added, stats_added)."""
    with open(path, encoding="utf-8") as f:
        games: list[dict] = json.load(f)

    if not games:
        return 0, 0, 0

    # Only skip games already finalized (status='post' with real scores).
    # Games archived mid-play (status='pre'/'in', or scores still null) must
    # be re-processed so their final score gets written via the UPSERT below.
    finalized_events: set[str] = {
        r[0] for r in con.execute(
            "SELECT event_id FROM games WHERE status='post' AND home_score IS NOT NULL"
        ).fetchall()
    }

    rows_games:        list[tuple] = []
    rows_game_teams:   list[tuple] = []
    rows_teams:        list[tuple] = []
    rows_game_players: list[tuple] = []
    rows_players:      list[tuple] = []

    for g in games:
        event_id = str(g.get("event_id", ""))
        if not event_id or event_id in finalized_events:
            continue

        sport  = g.get("sport", "")
        league = g.get("league", "")

        formations = g.get("formations") or {}

        rows_games.append((
            event_id,
            sport,
            league,
            g.get("name", ""),
            g.get("short_name", ""),
            _ts(g.get("date")),
            g.get("status", ""),
            g.get("status_detail", ""),
            _int(g.get("period")),
            str(g.get("clock", "")),
            _score((g.get("home") or {}).get("score")),
            _score((g.get("away") or {}).get("score")),
            g.get("provider"),
            _float(g.get("game_total")),
            _int(g.get("over_odds")),
            _int(g.get("under_odds")),
            _float(g.get("open_spread")),
            _float(g.get("open_total")),
            _int(g.get("draw_odds")),
            _float(g.get("home_win_pct")),
            _float(g.get("away_win_pct")),
            formations.get("home"),
            formations.get("away"),
        ))

        for side in ("home", "away"):
            t = g.get(side) or {}
            team_id = str(t.get("team_id", ""))
            if not team_id:
                continue
            rows_teams.append((team_id, sport, t.get("team_name", ""), t.get("team_abbr", "")))
            rows_game_teams.append((
                f"{event_id}_{side}",
                event_id,
                team_id,
                sport,
                side,
                _score(t.get("score")),
                (str(t.get("is_winner")).strip().lower() == "true") if t.get("is_winner") is not None else None,
                _int(t.get("moneyline")),
                _float(t.get("spread")),
                _int(t.get("spread_odds")),
                _float(t.get("team_total")),
            ))

        for p in g.get("players") or []:
            player_id = str(p.get("player_id", ""))
            if not player_id:
                continue

            # Resolve team_id via team_abbr
            side       = p.get("home_away", "away")
            team_dict  = g.get(side) or {}
            team_id    = str(team_dict.get("team_id", ""))

            rows_players.append((
                player_id,
                sport,
                p.get("display_name", ""),
                p.get("position", p.get("position_name", "")),
                team_id,
                team_dict.get("team_name", ""),
            ))

            gp_id = f"{event_id}_{player_id}"
            stats_dict = {k: str(v) for k, v in (p.get("stats") or {}).items()}
            rows_game_players.append((
                gp_id,
                event_id,
                player_id,
                sport,
                team_id,
                side,
                bool(p.get("starter", False)),
                bool(p.get("active", True)),
                bool(p.get("did_not_play", False)),
                p.get("dnp_reason", ""),
                bool(p.get("subbed_in", False)),
                bool(p.get("subbed_out", False)),
                p.get("formation_place"),
                json.dumps(stats_dict) if stats_dict else None,
            ))

    if not rows_games:
        return 0, 0, 0

    # DuckDB fastest bulk path: register pandas DataFrame, then INSERT SELECT.
    # teams: safe to ignore duplicates (team metadata never changes meaningfully).
    def _bulk_ignore(table: str, cols: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        df = pd.DataFrame(rows, columns=cols)
        con.register("_df_tmp", df)
        con.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM _df_tmp")
        con.unregister("_df_tmp")

    _bulk_ignore("teams", ["team_id","sport","team_name","team_abbr"], rows_teams)

    # games: UPSERT — update status/scores for games that were previously
    # ingested while still in-progress (status='pre'/'in' or scores null).
    if rows_games:
        df = pd.DataFrame(rows_games, columns=[
            "event_id","sport","league","name","short_name","game_date",
            "status","status_detail","period","clock",
            "home_score","away_score","provider",
            "game_total","over_odds","under_odds","open_spread","open_total",
            "draw_odds","home_win_pct","away_win_pct","home_formation","away_formation",
        ])
        con.register("_df_tmp", df)
        con.execute("""
            INSERT INTO games SELECT * FROM _df_tmp
            ON CONFLICT (event_id) DO UPDATE SET
                status        = EXCLUDED.status,
                status_detail = EXCLUDED.status_detail,
                period        = EXCLUDED.period,
                clock         = EXCLUDED.clock,
                home_score    = COALESCE(EXCLUDED.home_score, games.home_score),
                away_score    = COALESCE(EXCLUDED.away_score, games.away_score),
                home_win_pct  = COALESCE(EXCLUDED.home_win_pct, games.home_win_pct),
                away_win_pct  = COALESCE(EXCLUDED.away_win_pct, games.away_win_pct)
        """)
        con.unregister("_df_tmp")

    # game_teams: UPSERT scores and winner flag.
    if rows_game_teams:
        df = pd.DataFrame(rows_game_teams, columns=[
            "id","event_id","team_id","sport","home_away",
            "score","is_winner","moneyline","spread","spread_odds","team_total",
        ])
        con.register("_df_tmp", df)
        con.execute("""
            INSERT INTO game_teams SELECT * FROM _df_tmp
            ON CONFLICT (id) DO UPDATE SET
                score      = COALESCE(EXCLUDED.score, game_teams.score),
                is_winner  = COALESCE(EXCLUDED.is_winner, game_teams.is_winner),
                moneyline  = COALESCE(EXCLUDED.moneyline, game_teams.moneyline),
                spread     = COALESCE(EXCLUDED.spread, game_teams.spread),
                spread_odds = COALESCE(EXCLUDED.spread_odds, game_teams.spread_odds)
        """)
        con.unregister("_df_tmp")

    if rows_players:
        df = pd.DataFrame(rows_players, columns=["player_id","sport","display_name","position","team_id","team_name"])
        con.register("_df_tmp", df)
        con.execute("""
            INSERT INTO players SELECT * FROM _df_tmp
            ON CONFLICT (player_id, sport) DO UPDATE SET
                display_name = COALESCE(EXCLUDED.display_name, players.display_name),
                position     = COALESCE(EXCLUDED.position,     players.position),
                team_id      = COALESCE(EXCLUDED.team_id,      players.team_id),
                team_name    = COALESCE(EXCLUDED.team_name,    players.team_name)
        """)
        con.unregister("_df_tmp")

    _bulk_ignore("game_players", [
        "id","event_id","player_id","sport","team_id","home_away",
        "starter","active","did_not_play","dnp_reason",
        "subbed_in","subbed_out","formation_place","stats_json",
    ], rows_game_players)

    stats_written = sum(1 for r in rows_game_players if r[-1] is not None)
    # rows_games includes both new inserts and score-updates; caller treats
    # non-zero return as "something changed" (triggers log + Redis flush).
    return len(rows_games), len(rows_players), stats_written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Build DuckDB from historical_data/ JSON files")
    ap.add_argument("--db",      default=DEFAULT_DB, help=f"Database path (default: {DEFAULT_DB})")
    ap.add_argument("--rebuild", action="store_true", help="Drop and recreate all tables before loading")
    ap.add_argument("--data-dir", default=DATA_DIR,   help=f"JSON data directory (default: {DATA_DIR})")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    con = duckdb.connect(args.db)
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    if args.rebuild:
        print("Dropping existing tables...")
        con.execute(DROP_ALL)

    print("Creating schema...")
    con.execute(DDL)

    files = sorted(
        os.path.join(args.data_dir, f)
        for f in os.listdir(args.data_dir)
        if f.endswith(".json")
    )

    total_games = total_players = total_stats = 0
    for path in files:
        sport = os.path.basename(path).replace(".json", "")
        g, p, s = load_file(con, path)
        print(f"  {sport:<12} {g:>4} games  {p:>5} player-rows  {s:>7} stat rows")
        total_games   += g
        total_players += p
        total_stats   += s

    con.close()

    print()
    print(f"Done — {total_games} games, {total_players} player-game rows, {total_stats} stat rows")
    print(f"Database: {os.path.abspath(args.db)}")


if __name__ == "__main__":
    main()
