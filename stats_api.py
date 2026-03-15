"""
stats_api.py
============
Internal read-only FastAPI service that exposes historical and live sports
statistics from the DuckDB database (db/sports.db) and the live state file
written by realtime_monitor.py.

Endpoints
---------
GET /health                                  → liveness probe
GET /stats/player?name=Raphinha&sport=soccer → per-player recent game stats
GET /stats/team?name=Barcelona&sport=soccer  → team record + recent results
GET /stats/live?team=Barcelona               → live / pregame entries

Authentication
--------------
Optional bearer-token auth via STATS_API_TOKEN env var.
If the env var is not set, all requests are allowed (internal VPS use).

Usage
-----
    python stats_api.py                     # port 8001
    STATS_API_TOKEN=secret python stats_api.py
    uvicorn stats_api:app --port 8001
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent.resolve()
DB_PATH   = str(_THIS_DIR / "db" / "sports.db")
LIVE_DIR  = str(_THIS_DIR / "live")

_TOKEN    = os.getenv("STATS_API_TOKEN", "").strip()
_PORT     = int(os.getenv("STATS_API_PORT", "8001"))

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Sports Stats API",
    description="Internal service — exposes DuckDB + live state stats",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

_security = HTTPBearer(auto_error=False)


def _verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_security),
) -> None:
    """If STATS_API_TOKEN is set, require a matching Bearer token."""
    if not _TOKEN:
        return  # auth disabled — internal VPS use
    if credentials is None or credentials.credentials != _TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------------------------
# DuckDB helpers (read-only, thread-per-request)
# ---------------------------------------------------------------------------

def _conn() -> duckdb.DuckDBPyConnection:  # type: ignore[name-defined]
    """Return a fresh read-only DuckDB connection for one request."""
    return duckdb.connect(DB_PATH, read_only=True)


def _query(sql: str, params: list | None = None) -> list[dict[str, Any]]:
    """Execute *sql* and return rows as a list of dicts."""
    con = _conn()
    try:
        rel = con.execute(sql, params or [])
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Live state helpers
# ---------------------------------------------------------------------------

def _read_live_state() -> dict[str, list]:
    """Load live_state.json; return empty sections on any error."""
    path = os.path.join(LIVE_DIR, "live_state.json")
    empty: dict[str, list] = {"pregame": [], "live": [], "finished": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "pregame":  data.get("pregame", []),
            "live":     data.get("live", []),
            "finished": data.get("finished", []),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return empty


def _matches_filter(entry: dict, team: str | None, player: str | None) -> bool:
    """True if the live entry matches the given team or player filter."""
    if team:
        t = team.lower()
        home = (entry.get("home") or {}).get("team_name", "").lower()
        away = (entry.get("away") or {}).get("team_name", "").lower()
        short = entry.get("short_name", "").lower()
        if t not in home and t not in away and t not in short:
            return False
    if player:
        p = player.lower()
        found = False
        for pl in entry.get("players", []):
            dn = pl.get("display_name", "").lower()
            if p in dn:
                found = True
                break
        if not found:
            return False
    return True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health(_: None = Depends(_verify_token)) -> JSONResponse:
    """Liveness probe — verifies DB is accessible and live dir exists."""
    db_ok   = False
    live_ok = os.path.isfile(os.path.join(LIVE_DIR, "live_state.json"))
    try:
        rows = _query("SELECT COUNT(*) AS n FROM games")
        db_ok = rows[0]["n"] > 0
    except Exception:
        pass
    return JSONResponse({"status": "ok", "db": db_ok, "live_state": live_ok})


@app.get("/stats/player")
def stats_player(
    name:  str           = Query(..., description="Player display_name (partial match)"),
    sport: Optional[str] = Query(None, description="Sport filter (e.g. soccer, basketball)"),
    limit: int           = Query(10, ge=1, le=100, description="Max games to return"),
    _:     None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Return recent per-game statistics for a player matched by display_name.
    Searches case-insensitively; returns up to *limit* games, most recent first.
    """
    sql = """
        SELECT
            g.event_id,
            g.game_date              AS date,
            g.sport,
            g.league,
            COALESCE(th.team_name, '') AS home_team,
            COALESCE(ta.team_name, '') AS away_team,
            g.home_score,
            g.away_score,
            g.status,
            p.display_name,
            p.team_name,
            p.position,
            gp.starter               AS is_starter,
            ps.stat_key              AS stat_name,
            ps.stat_value
        FROM players    p
        JOIN game_players gp ON gp.player_id = p.player_id AND gp.sport = p.sport
        JOIN games        g  ON g.event_id   = gp.event_id
        JOIN player_stats ps ON ps.game_player_id = gp.id
        LEFT JOIN (
            SELECT gt.event_id, t.team_name
            FROM   game_teams gt
            JOIN   teams      t ON t.team_id = gt.team_id AND t.sport = gt.sport
            WHERE  gt.home_away = 'home'
        ) th ON th.event_id = g.event_id
        LEFT JOIN (
            SELECT gt.event_id, t.team_name
            FROM   game_teams gt
            JOIN   teams      t ON t.team_id = gt.team_id AND t.sport = gt.sport
            WHERE  gt.home_away = 'away'
        ) ta ON ta.event_id = g.event_id
        WHERE LOWER(p.display_name) LIKE LOWER(?)
        {sport_filter}
        ORDER BY g.game_date DESC, g.event_id, ps.stat_key
        LIMIT ?
    """
    sport_filter = "AND LOWER(g.sport) = LOWER(?)" if sport else ""
    sql = sql.format(sport_filter=sport_filter)

    pattern = f"%{name}%"
    params: list = [pattern]
    if sport:
        params.append(sport)
    params.append(limit * 50)  # over-fetch rows; group by game below

    try:
        rows = _query(sql, params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Group into games → player → stats
    games_map: dict[str, dict] = {}
    for row in rows:
        eid  = str(row["event_id"])
        pname = row["display_name"]
        key  = f"{eid}:{pname}"
        if eid not in games_map:
            games_map[eid] = {
                "event_id":  eid,
                "date":      str(row["date"]) if row["date"] else None,
                "sport":     row["sport"],
                "league":    row["league"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "home_score": row["home_score"],
                "away_score": row["away_score"],
                "status":    row["status"],
                "players":   {},
            }
        gm = games_map[eid]
        if pname not in gm["players"]:
            gm["players"][pname] = {
                "display_name":  pname,
                "team":          row["team_name"],
                "position":      row["position"],
                "is_starter":    bool(row["is_starter"]) if row["is_starter"] is not None else None,
                "stats":         {},
            }
        gm["players"][pname]["stats"][row["stat_name"]] = row["stat_value"]

    # Convert to list, collapse players dict to list, cap at limit games
    result = []
    for gm in list(games_map.values())[:limit]:
        gm["players"] = list(gm["players"].values())
        result.append(gm)

    return JSONResponse({"found": bool(result), "count": len(result), "games": result})


@app.get("/stats/team")
def stats_team(
    name:  str           = Query(..., description="Team name (partial match)"),
    sport: Optional[str] = Query(None, description="Sport filter"),
    limit: int           = Query(5, ge=1, le=50, description="Max recent games"),
    _:     None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Return win/loss record, last *limit* results, and top 3 scorers for a team.
    Matches team name case-insensitively against home_team and away_team.
    """
    sport_filter = "AND LOWER(g.sport) = LOWER(?)" if sport else ""

    # Shared subqueries for resolving home/away team names from game_teams + teams
    _home_sub = """
        SELECT gt.event_id, t.team_name
        FROM   game_teams gt
        JOIN   teams      t ON t.team_id = gt.team_id AND t.sport = gt.sport
        WHERE  gt.home_away = 'home'
    """
    _away_sub = """
        SELECT gt.event_id, t.team_name
        FROM   game_teams gt
        JOIN   teams      t ON t.team_id = gt.team_id AND t.sport = gt.sport
        WHERE  gt.home_away = 'away'
    """

    # --- Record ---
    record_sql = f"""
        WITH tg AS (
            SELECT
                g.home_score, g.away_score, g.status,
                COALESCE(th.team_name, '') AS home_team,
                COALESCE(ta.team_name, '') AS away_team
            FROM games g
            LEFT JOIN ({_home_sub}) th ON th.event_id = g.event_id
            LEFT JOIN ({_away_sub}) ta ON ta.event_id = g.event_id
            WHERE (LOWER(COALESCE(th.team_name,'')) LIKE LOWER(?) OR LOWER(COALESCE(ta.team_name,'')) LIKE LOWER(?))
              AND g.status = 'post'
              {sport_filter}
        )
        SELECT
            COUNT(*) FILTER (WHERE (LOWER(home_team) LIKE LOWER(?) AND home_score > away_score)
                                OR (LOWER(away_team) LIKE LOWER(?) AND away_score > home_score)) AS wins,
            COUNT(*) FILTER (WHERE (LOWER(home_team) LIKE LOWER(?) AND home_score < away_score)
                                OR (LOWER(away_team) LIKE LOWER(?) AND away_score < home_score)) AS losses,
            COUNT(*) FILTER (WHERE home_score IS NOT NULL AND away_score IS NOT NULL
                              AND home_score = away_score
                              AND (LOWER(home_team) LIKE LOWER(?) OR LOWER(away_team) LIKE LOWER(?))) AS draws,
            COUNT(*) AS total_games
        FROM tg
    """
    pattern = f"%{name}%"
    rec_params: list = [pattern, pattern]  # CTE WHERE
    if sport:
        rec_params.append(sport)
    rec_params += [pattern] * 6  # FILTER clauses (2 per wins/losses/draws)

    # --- Recent games ---
    recent_sql = f"""
        SELECT
            g.event_id, g.game_date AS date, g.sport, g.league,
            COALESCE(th.team_name, '') AS home_team,
            COALESCE(ta.team_name, '') AS away_team,
            g.home_score, g.away_score, g.status
        FROM games g
        LEFT JOIN ({_home_sub}) th ON th.event_id = g.event_id
        LEFT JOIN ({_away_sub}) ta ON ta.event_id = g.event_id
        WHERE (LOWER(COALESCE(th.team_name,'')) LIKE LOWER(?) OR LOWER(COALESCE(ta.team_name,'')) LIKE LOWER(?))
          {sport_filter}
        ORDER BY g.game_date DESC
        LIMIT ?
    """
    recent_params: list = [pattern, pattern]
    if sport:
        recent_params.append(sport)
    recent_params.append(limit)

    # --- Top scorers (goals / points per player) ---
    scorers_sql = f"""
        SELECT p.display_name, SUM(TRY_CAST(ps.stat_value AS DOUBLE)) AS total
        FROM players    p
        JOIN game_players gp ON gp.player_id = p.player_id AND gp.sport = p.sport
        JOIN games        g  ON g.event_id   = gp.event_id
        JOIN player_stats ps ON ps.game_player_id = gp.id
        WHERE LOWER(p.team_name) LIKE LOWER(?)
          AND LOWER(ps.stat_key) IN ('goals', 'points', 'runs scored', 'runs')
          {sport_filter}
        GROUP BY p.display_name
        ORDER BY total DESC NULLS LAST
        LIMIT 3
    """
    scorers_params: list = [pattern]
    if sport:
        scorers_params.append(sport)

    try:
        rec_rows    = _query(record_sql, rec_params)
        recent_rows = _query(recent_sql, recent_params)
        scorer_rows = _query(scorers_sql, scorers_params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    record  = rec_rows[0] if rec_rows else {}
    recent  = [
        {
            "event_id":   str(r["event_id"]),
            "date":       str(r["date"]) if r["date"] else None,
            "sport":      r["sport"],
            "league":     r["league"],
            "home_team":  r["home_team"] or None,
            "away_team":  r["away_team"] or None,
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "status":     r["status"],
        }
        for r in recent_rows
    ]
    scorers = [
        {"player": r["display_name"], "total": r["total"]}
        for r in scorer_rows
    ]

    found = bool(recent) or (record.get("total_games", 0) or 0) > 0
    return JSONResponse({
        "found":       found,
        "team_filter": name,
        "record":      record,
        "recent":      recent,
        "top_scorers": scorers,
    })


@app.get("/stats/live")
def stats_live(
    team:   Optional[str] = Query(None, description="Team name filter (partial match)"),
    player: Optional[str] = Query(None, description="Player name filter (partial match)"),
    _:      None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Return current live / pregame entries from live_state.json.
    Pass team or player to narrow results; omit both to return all live games.
    """
    if not team and not player:
        # Return everything that is currently live
        state  = _read_live_state()
        return JSONResponse({
            "live_count":    len(state["live"]),
            "pregame_count": len(state["pregame"]),
            "live":          state["live"],
            "pregame":       state["pregame"],
        })

    state   = _read_live_state()
    matches = {
        "live":    [e for e in state["live"]    if _matches_filter(e, team, player)],
        "pregame": [e for e in state["pregame"] if _matches_filter(e, team, player)],
    }
    return JSONResponse({
        "found":         bool(matches["live"] or matches["pregame"]),
        "live_count":    len(matches["live"]),
        "pregame_count": len(matches["pregame"]),
        **matches,
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=_PORT)
