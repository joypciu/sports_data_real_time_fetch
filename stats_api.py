"""
stats_api.py
===========
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
import threading
import time
from datetime import date, datetime, timezone, timedelta
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
ARCHIVE_DIR = str(_THIS_DIR / "archive")

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
# Archive helpers
# ---------------------------------------------------------------------------

def _load_json_archive(table_name: str) -> list[dict[str, Any]]:
    """Load archived data from JSON file for a given table."""
    archive_path = os.path.join(ARCHIVE_DIR, f"old_{table_name}.json")
    if not os.path.exists(archive_path):
        return []

    try:
        with open(archive_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _get_cutoff_date() -> str:
    """Calculate cutoff date (today - 2 months)."""
    today = datetime.now(timezone.utc)
    cutoff = today - timedelta(days=60)  # 2 months approx
    return cutoff.strftime('%Y-%m-%d')


# ---------------------------------------------------------------------------
# DuckDB helpers — persistent thread-local connections with retry
# ---------------------------------------------------------------------------
#
# Each worker thread keeps ONE read-only connection alive for its lifetime.
# This eliminates the per-request connect() call that races with update_db's
# write lock.  If a connection does go stale (DB file replaced, DuckDB error)
# it is closed and re-opened with exponential backoff, up to _DB_MAX_RETRIES.
#
# FastAPI runs sync `def` endpoint handlers in its default thread-pool
# (starlette.concurrency.run_in_threadpool), so thread-local storage is safe.
# ---------------------------------------------------------------------------

_thread_local: threading.local = threading.local()
_DB_MAX_RETRIES: int   = 5
_DB_RETRY_BASE:  float = 0.1   # seconds; doubles each attempt (0.1 → 3.2 s total)


def _get_thread_conn() -> duckdb.DuckDBPyConnection:  # type: ignore[name-defined]
    """Return this thread's persistent read-only DuckDB connection, creating it if needed."""
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = duckdb.connect(DB_PATH, read_only=True)
        _thread_local.conn = conn
    return conn


def _drop_thread_conn() -> None:
    """Close and discard this thread's connection so the next call reconnects."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.conn = None


def _query(sql: str, params: list | None = None) -> list[dict[str, Any]]:
    """Execute *sql* on this thread's persistent connection, retrying on lock errors."""
    last_exc: Exception | None = None
    for attempt in range(_DB_MAX_RETRIES):
        try:
            conn = _get_thread_conn()
            rel  = conn.execute(sql, params or [])
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row)) for row in rel.fetchall()]
        except Exception as exc:
            _drop_thread_conn()  # force reconnect on next attempt
            last_exc = exc
            if attempt < _DB_MAX_RETRIES - 1:
                time.sleep(_DB_RETRY_BASE * (2 ** attempt))  # 0.1 0.2 0.4 0.8 1.6 s
    assert last_exc is not None
    raise last_exc


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


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _date_only(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _team_matches(query: str | None, team_name: str | None, team_abbr: str | None) -> bool:
    if not query:
        return False
    query_norm = _normalize_text(query)
    if not query_norm:
        return False
    name_norm = _normalize_text(team_name)
    abbr_norm = _normalize_text(team_abbr)
    return (
        query_norm == name_norm
        or query_norm == abbr_norm
        or query_norm in name_norm
        or name_norm in query_norm
        or query_norm in abbr_norm
    )


def _matchup_matches(event: dict[str, Any], team: str | None, opponent: str | None) -> bool:
    if not team and not opponent:
        return True

    if team and opponent:
        forward = (
            _team_matches(team, event.get("home_team"), event.get("home_abbr"))
            and _team_matches(opponent, event.get("away_team"), event.get("away_abbr"))
        )
        reverse = (
            _team_matches(team, event.get("away_team"), event.get("away_abbr"))
            and _team_matches(opponent, event.get("home_team"), event.get("home_abbr"))
        )
        return forward or reverse

    # Single team provided — match whichever side it's on
    single = team or opponent
    return (
        _team_matches(single, event.get("home_team"), event.get("home_abbr"))
        or _team_matches(single, event.get("away_team"), event.get("away_abbr"))
    )


def _resolve_pick_side(pick: str, event: dict[str, Any]) -> str | None:
    pick_norm = _normalize_text(pick)
    if pick_norm in {"home", "away", "draw", "tie"}:
        return "draw" if pick_norm == "tie" else pick_norm
    if _team_matches(pick, event.get("home_team"), event.get("home_abbr")):
        return "home"
    if _team_matches(pick, event.get("away_team"), event.get("away_abbr")):
        return "away"
    return None


def _historical_event_candidates(
    event_id: str | None,
    game_date: str | None,
    sport: str | None,
) -> list[dict[str, Any]]:
    home_team_subquery = """
        SELECT gt.event_id, t.team_name, t.team_abbr
        FROM game_teams gt
        JOIN teams t ON t.team_id = gt.team_id AND t.sport = gt.sport
        WHERE gt.home_away = 'home'
    """
    away_team_subquery = """
        SELECT gt.event_id, t.team_name, t.team_abbr
        FROM game_teams gt
        JOIN teams t ON t.team_id = gt.team_id AND t.sport = gt.sport
        WHERE gt.home_away = 'away'
    """

    sql = f"""
        SELECT
            g.event_id,
            g.game_date AS date,
            g.sport,
            g.league,
            g.name,
            g.short_name,
            g.status,
            COALESCE(ht.team_name, '') AS home_team,
            COALESCE(ht.team_abbr, '') AS home_abbr,
            COALESCE(away_t.team_name, '') AS away_team,
            COALESCE(away_t.team_abbr, '') AS away_abbr,
            g.home_score,
            g.away_score,
            g.provider,
            g.game_total,
            g.over_odds,
            g.under_odds,
            g.draw_odds,
            home_line.moneyline AS home_ml,
            away_line.moneyline AS away_ml,
            home_line.spread AS home_spread,
            away_line.spread AS away_spread,
            home_line.spread_odds AS home_spread_odds,
            away_line.spread_odds AS away_spread_odds
        FROM games g
        LEFT JOIN ({home_team_subquery}) ht ON ht.event_id = g.event_id
        LEFT JOIN ({away_team_subquery}) away_t ON away_t.event_id = g.event_id
        LEFT JOIN game_teams home_line
            ON home_line.event_id = g.event_id AND home_line.home_away = 'home'
        LEFT JOIN game_teams away_line
            ON away_line.event_id = g.event_id AND away_line.home_away = 'away'
        WHERE {{where_clause}}
        ORDER BY g.game_date DESC
        LIMIT 50
    """

    if event_id:
        return _query(sql.format(where_clause="g.event_id = ?"), [event_id])

    if not game_date:
        return []

    params: list[Any] = [game_date]
    where_clause = "CAST(g.game_date AS DATE) = CAST(? AS DATE)"
    if sport:
        where_clause += " AND LOWER(g.sport) = LOWER(?)"
        params.append(sport)
    return _query(sql.format(where_clause=where_clause), params)


def _live_event_candidates(
    event_id: str | None,
    game_date: str | None,
    sport: str | None,
) -> list[dict[str, Any]]:
    state = _read_live_state()
    candidates: list[dict[str, Any]] = []
    for bucket_name in ("live", "pregame", "finished"):
        for entry in state.get(bucket_name, []):
            if event_id and str(entry.get("event_id") or "") != event_id:
                continue
            if not event_id:
                if game_date and _date_only(entry.get("date")) != game_date:
                    continue
                if sport and _normalize_text(entry.get("sport")) != _normalize_text(sport):
                    continue

            odds = entry.get("odds") or {}
            home = entry.get("home") or {}
            away = entry.get("away") or {}
            candidates.append({
                "event_id": str(entry.get("event_id") or ""),
                "date": entry.get("date"),
                "sport": entry.get("sport"),
                "league": entry.get("league"),
                "name": entry.get("name"),
                "short_name": entry.get("short_name"),
                "status": entry.get("status"),
                "home_team": home.get("team_name"),
                "home_abbr": home.get("team_abbr"),
                "away_team": away.get("team_name"),
                "away_abbr": away.get("team_abbr"),
                "home_score": _as_int(home.get("score")),
                "away_score": _as_int(away.get("score")),
                "provider": odds.get("provider"),
                "game_total": _as_float(odds.get("game_total")),
                "over_odds": _as_int(odds.get("over_odds")),
                "under_odds": _as_int(odds.get("under_odds")),
                "draw_odds": _as_int(odds.get("draw_odds")),
                "home_ml": _as_int(odds.get("home_ml")),
                "away_ml": _as_int(odds.get("away_ml")),
                "home_spread": _as_float(odds.get("home_spread")),
                "away_spread": _as_float(odds.get("away_spread")),
                "home_spread_odds": _as_int(odds.get("home_spread_odds")),
                "away_spread_odds": _as_int(odds.get("away_spread_odds")),
                "source": "live",
            })
    return candidates


def _resolve_event(
    event_id: str | None,
    game_date: str | None,
    sport: str | None,
    team: str | None,
    opponent: str | None,
) -> dict[str, Any] | None:
    live_candidates = _live_event_candidates(event_id, game_date, sport)
    for event in live_candidates:
        if _matchup_matches(event, team, opponent):
            return event

    try:
        historical_candidates = _historical_event_candidates(event_id, game_date, sport)
    except Exception as exc:
        # DuckDB unavailable or temporarily locked — treat as not found so
        # callers receive a clean 404 rather than an unhandled 500.
        import logging
        logging.getLogger(__name__).warning("DuckDB query failed in _resolve_event: %s", exc)
        return None

    for event in historical_candidates:
        if _matchup_matches(event, team, opponent):
            event["source"] = "historical"
            return event

    return None


def _evaluate_market(
    event: dict[str, Any],
    market: str,
    pick: str,
    line: float | None,
) -> dict[str, Any]:
    market_norm = _normalize_text(market)
    if market_norm == "game spread":
        market_norm = "spread"
    pick_norm = _normalize_text(pick)
    home_score = _as_int(event.get("home_score"))
    away_score = _as_int(event.get("away_score"))
    settled = _normalize_text(str(event.get("status") or "")) == "post"
    total_score = None if home_score is None or away_score is None else home_score + away_score

    result: bool | None = None
    outcome = "pending"
    resolved_pick = pick
    resolved_line = line

    if market_norm == "moneyline":
        side = _resolve_pick_side(pick, event)
        if side is None:
            raise HTTPException(
                status_code=400,
                detail="For moneyline, pick must be home, away, draw, or a matching team name",
            )
        resolved_pick = side
        if home_score is not None and away_score is not None:
            if side == "draw":
                result = home_score == away_score
            elif side == "home":
                result = home_score > away_score
            else:
                result = away_score > home_score
            outcome = "win" if result else "loss"

    elif market_norm == "spread":
        side = _resolve_pick_side(pick, event)
        if side not in {"home", "away"}:
            raise HTTPException(
                status_code=400,
                detail="For spread, pick must be home, away, or a matching team name",
            )
        resolved_pick = side
        if resolved_line is None:
            resolved_line = _as_float(event.get("home_spread" if side == "home" else "away_spread"))
        if resolved_line is None:
            raise HTTPException(
                status_code=400,
                detail="Spread line is required when the event does not have a stored spread",
            )
        if home_score is not None and away_score is not None:
            adjusted = (home_score if side == "home" else away_score) + resolved_line
            opponent_score = away_score if side == "home" else home_score
            if adjusted > opponent_score:
                result = True
                outcome = "win"
            elif adjusted < opponent_score:
                result = False
                outcome = "loss"
            else:
                result = None
                outcome = "push"

    elif market_norm == "total":
        if pick_norm not in {"over", "under"}:
            raise HTTPException(
                status_code=400,
                detail="For total, pick must be over or under",
            )
        resolved_pick = pick_norm
        if resolved_line is None:
            resolved_line = _as_float(event.get("game_total"))
        if resolved_line is None:
            raise HTTPException(
                status_code=400,
                detail="Total line is required when the event does not have a stored total",
            )
        if total_score is not None:
            if total_score > resolved_line:
                result = pick_norm == "over"
                outcome = "win" if result else "loss"
            elif total_score < resolved_line:
                result = pick_norm == "under"
                outcome = "win" if result else "loss"
            else:
                result = None
                outcome = "push"

    else:
        raise HTTPException(
            status_code=400,
            detail="market must be one of moneyline, spread, game spread, or total",
        )

    return {
        "found": True,
        "source": event.get("source"),
        "settled": settled,
        "market": market_norm,
        "pick": resolved_pick,
        "line": resolved_line,
        "result": result,
        "outcome": outcome,
        "event": {
            "event_id": str(event.get("event_id") or ""),
            "date": str(event.get("date")) if event.get("date") is not None else None,
            "sport": event.get("sport"),
            "league": event.get("league"),
            "name": event.get("name"),
            "short_name": event.get("short_name"),
            "status": event.get("status"),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
        },
        "score": {
            "home": home_score,
            "away": away_score,
            "total": total_score,
        },
        "pricing": {
            "provider": event.get("provider"),
            "game_total": _as_float(event.get("game_total")),
            "over_odds": _as_int(event.get("over_odds")),
            "under_odds": _as_int(event.get("under_odds")),
            "draw_odds": _as_int(event.get("draw_odds")),
            "home_ml": _as_int(event.get("home_ml")),
            "away_ml": _as_int(event.get("away_ml")),
            "home_spread": _as_float(event.get("home_spread")),
            "away_spread": _as_float(event.get("away_spread")),
        },
    }


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
            gp.stats_json
        FROM players    p
        JOIN game_players gp ON gp.player_id = p.player_id AND gp.sport = p.sport
        JOIN games        g  ON g.event_id   = gp.event_id
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
        ORDER BY g.game_date DESC, g.event_id
        LIMIT ?
    """
    sport_filter = "AND LOWER(g.sport) = LOWER(?)" if sport else ""
    sql = sql.format(sport_filter=sport_filter)

    pattern = f"%{name}%"
    params: list = [pattern]
    if sport:
        params.append(sport)
    params.append(limit)  # one row per game-player now

    try:
        rows = _query(sql, params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    result = []
    source = "database"

    if rows:
        # Group into games → players; one DB row per game-player now, stats in JSON blob
        games_map: dict[str, dict] = {}
        for row in rows:
            eid   = str(row["event_id"])
            pname = row["display_name"]
            if eid not in games_map:
                games_map[eid] = {
                    "event_id":   eid,
                    "date":       str(row["date"]) if row["date"] else None,
                    "sport":      row["sport"],
                    "league":     row["league"],
                    "home_team":  row["home_team"],
                    "away_team":  row["away_team"],
                    "home_score": row["home_score"],
                    "away_score": row["away_score"],
                    "status":     row["status"],
                    "players":    {},
                }
            gm = games_map[eid]
            if pname not in gm["players"]:
                gm["players"][pname] = {
                    "display_name": pname,
                    "team":         row["team_name"],
                    "position":     row["position"],
                    "is_starter":   bool(row["is_starter"]) if row["is_starter"] is not None else None,
                    "stats":        json.loads(row["stats_json"] or "{}"),
                }

        # Convert to list, collapse players dict to list, cap at limit games
        result = []
        for gm in list(games_map.values())[:limit]:
            gm["players"] = list(gm["players"].values())
            result.append(gm)
    else:
        # Fallback to archive
        # First, find player_id from name and sport
        player_sql = """
            SELECT player_id FROM players
            WHERE LOWER(display_name) LIKE LOWER(?)
        """
        player_params = [f"%{name}%"]
        if sport:
            player_sql += " AND LOWER(sport) = LOWER(?)"
            player_params.append(sport)

        player_rows = _query(player_sql, player_params)
        if player_rows:
            player_id = player_rows[0]["player_id"]
            # Load archive
            archive_data = _load_json_archive("player_stats")
            # Filter by player_id
            player_archive = [stat for stat in archive_data if stat.get("player_id") == player_id]
            if player_archive:
                source = "archive"
                # Group by game_id, similar to DB logic
                games_map: dict[str, dict] = {}
                for stat in player_archive[:limit]:  # limit to prevent too much data
                    game_id = stat.get("game_id")
                    if game_id and str(game_id) not in games_map:
                        # Get game info from archive or DB
                        game_sql = "SELECT * FROM games WHERE id = ?"
                        game_rows = _query(game_sql, [game_id])
                        if game_rows:
                            game = game_rows[0]
                            games_map[str(game_id)] = {
                                "event_id": str(game["event_id"]),
                                "date": str(game["game_date"]) if game["game_date"] else None,
                                "sport": game["sport"],
                                "league": game["league"],
                                "home_team": None,  # Would need to join, simplify
                                "away_team": None,
                                "home_score": game["home_score"],
                                "away_score": game["away_score"],
                                "status": game["status"],
                                "players": {
                                    name: {  # Assuming name matches
                                        "display_name": name,
                                        "team": None,  # Would need to get from players table
                                        "position": None,
                                        "is_starter": None,
                                        "stats": stat  # Simplified, just the stat dict
                                    }
                                }
                            }
                result = list(games_map.values())

    return JSONResponse({"found": bool(result), "count": len(result), "source": source, "games": result})


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
        SELECT p.display_name,
               SUM(TRY_CAST(
                   COALESCE(
                       json_extract_string(gp.stats_json, '$.goals'),
                       json_extract_string(gp.stats_json, '$.G'),
                       json_extract_string(gp.stats_json, '$.points'),
                       json_extract_string(gp.stats_json, '$.PTS'),
                       json_extract_string(gp.stats_json, '$.runs'),
                       json_extract_string(gp.stats_json, '$.R'),
                       json_extract_string(gp.stats_json, '$.runs scored')
                   ) AS DOUBLE
               )) AS total
        FROM players    p
        JOIN game_players gp ON gp.player_id = p.player_id AND gp.sport = p.sport
        JOIN games        g  ON g.event_id   = gp.event_id
        WHERE LOWER(p.team_name) LIKE LOWER(?)
          AND gp.stats_json IS NOT NULL
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

    found = bool(recent_rows) or bool(rec_rows and rec_rows[0].get("total_games", 0) > 0)
    source = "database"

    if found:
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
    else:
        # Fallback to archive
        # Find team_id from name
        team_sql = """
            SELECT team_id FROM teams
            WHERE LOWER(team_name) LIKE LOWER(?)
        """
        team_params = [f"%{name}%"]
        if sport:
            team_sql += " AND LOWER(sport) = LOWER(?)"
            team_params.append(sport)

        team_rows = _query(team_sql, team_params)
        if team_rows:
            team_id = team_rows[0]["team_id"]
            # Load archived game_teams
            archive_data = _load_json_archive("game_teams")
            team_archive = [gt for gt in archive_data if gt.get("team_id") == team_id]
            if team_archive:
                source = "archive"
                # Simplified: just return recent archived games
                recent = []
                for gt in team_archive[:limit]:
                    game_id = gt.get("game_id")
                    if game_id:
                        game_sql = "SELECT * FROM games WHERE id = ?"
                        game_rows = _query(game_sql, [game_id])
                        if game_rows:
                            game = game_rows[0]
                            recent.append({
                                "event_id": str(game["event_id"]),
                                "date": str(game["game_date"]) if game["game_date"] else None,
                                "sport": game["sport"],
                                "league": game["league"],
                                "home_team": None,
                                "away_team": None,
                                "home_score": game["home_score"],
                                "away_score": game["away_score"],
                                "status": game["status"],
                            })
                record = {}  # Can't compute record from archive easily
                scorers = []  # Can't compute scorers from archive easily

    return JSONResponse({
        "found":       bool(recent),
        "source":      source,
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


@app.get("/stats/market-check")
def stats_market_check(
    event_id: Optional[str] = Query(None, description="Exact event_id when known"),
    date: Optional[str] = Query(None, description="Game date in YYYY-MM-DD format"),
    sport: Optional[str] = Query(None, description="Optional sport filter"),
    team: Optional[str] = Query(None, description="One team in the matchup"),
    opponent: Optional[str] = Query(None, description="The opposing team in the matchup"),
    market: str = Query(..., description="moneyline, spread, or total"),
    pick: str = Query(..., description="Pick to evaluate: team/home/away/draw for moneyline or spread, over/under for total"),
    line: Optional[float] = Query(None, description="Optional custom line; if omitted, stored line is used"),
    _: None = Depends(_verify_token),
) -> JSONResponse:
    if not event_id and not (date and (team or opponent)):
        raise HTTPException(
            status_code=400,
            detail="Provide either event_id, or date + at least one team name",
        )

    if date and _date_only(date) is None:
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format")

    event = _resolve_event(event_id, _date_only(date), sport, team, opponent)
    if event is None:
        return JSONResponse(
            status_code=404,
            content={
                "found": False,
                "message": "No matching event found",
                "query": {
                    "event_id": event_id,
                    "date": _date_only(date),
                    "sport": sport,
                    "team": team,
                    "opponent": opponent,
                    "market": market,
                    "pick": pick,
                    "line": line,
                },
            },
        )

    return JSONResponse(_evaluate_market(event, market, pick, line))


# ---------------------------------------------------------------------------
# Event log helpers
# ---------------------------------------------------------------------------

def _iter_event_files(date_from: str | None, date_to: str | None):
    """Yield (date_str, path) for every events_YYYYMMDD.jsonl in LIVE_DIR."""
    try:
        names = sorted(
            f for f in os.listdir(LIVE_DIR)
            if f.startswith("events_") and f.endswith(".jsonl")
        )
    except OSError:
        return
    for name in names:
        date_str = name[len("events_"):-len(".jsonl")]  # "20260415"
        if date_from and date_str < date_from.replace("-", ""):
            continue
        if date_to and date_str > date_to.replace("-", ""):
            continue
        yield date_str, os.path.join(LIVE_DIR, name)


def _event_matches(ev: dict[str, Any], sport: str | None, league: str | None,
                   event_type: str | None, team: str | None,
                   event_id: str | None) -> bool:
    if sport and _normalize_text(ev.get("sport")) != _normalize_text(sport):
        return False
    if league:
        ev_league = _normalize_text(ev.get("league", ""))
        if _normalize_text(league) not in ev_league and ev_league not in _normalize_text(league):
            return False
    if event_type and _normalize_text(ev.get("type", ev.get("event_type", ""))) != _normalize_text(event_type):
        return False
    if event_id and str(ev.get("event_id", "")) != str(event_id):
        return False
    if team:
        game_label = _normalize_text(ev.get("game", ev.get("short_name", "")))
        home_abbr  = _normalize_text(ev.get("home_abbr", ev.get("home", "")))
        away_abbr  = _normalize_text(ev.get("away_abbr", ev.get("away", "")))
        t = _normalize_text(team)
        if t not in game_label and t not in home_abbr and t not in away_abbr:
            return False
    return True


# ---------------------------------------------------------------------------
# Event log endpoints
# ---------------------------------------------------------------------------

@app.get("/stats/events")
def stats_events(
    sport:      Optional[str] = Query(None, description="Sport filter: basketball, baseball, soccer, hockey"),
    league:     Optional[str] = Query(None, description="League filter: nba, mlb, nhl, eng.1, esp.1 …"),
    event_type: Optional[str] = Query(None, alias="type", description="Event type: LINE_MOVE, SCORE_UPDATE, TOTAL_MOVE, PERIOD_CHANGE, ODDS_MOVE, WIN_PROB_SHIFT, GAME_STARTED, GAME_FINISHED, NEW_GAME_DISCOVERED"),
    team:       Optional[str] = Query(None, description="Team abbreviation or partial name (matched against game label)"),
    event_id:   Optional[str] = Query(None, description="Exact ESPN event_id to filter a specific game"),
    date_from:  Optional[str] = Query(None, description="Start date inclusive (YYYY-MM-DD)"),
    date_to:    Optional[str] = Query(None, description="End date inclusive (YYYY-MM-DD)"),
    limit:      int           = Query(50, ge=1, le=500, description="Max events to return"),
    offset:     int           = Query(0, ge=0, description="Skip this many matching events (for pagination)"),
    _:          None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Query the historical event log (LINE_MOVE, SCORE_UPDATE, etc.) with filters.

    Events are stored in live/events_YYYYMMDD.jsonl files, one per day.
    All 37,757 events from 2026-03-15 onwards are available.
    """
    if date_from and _date_only(date_from) is None:
        raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
    if date_to and _date_only(date_to) is None:
        raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")

    matched: list[dict[str, Any]] = []
    total_scanned = 0

    for _date_str, path in _iter_event_files(date_from, date_to):
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    total_scanned += 1
                    if _event_matches(ev, sport, league, event_type, team, event_id):
                        matched.append(ev)
        except OSError:
            continue

    total_matched = len(matched)
    page = matched[offset: offset + limit]

    return JSONResponse({
        "found":         total_matched > 0,
        "total_matched": total_matched,
        "returned":      len(page),
        "offset":        offset,
        "limit":         limit,
        "filters": {
            "sport": sport, "league": league, "type": event_type,
            "team": team, "event_id": event_id,
            "date_from": date_from, "date_to": date_to,
        },
        "events": page,
    })


@app.get("/stats/events/summary")
def stats_events_summary(
    _: None = Depends(_verify_token),
) -> JSONResponse:
    """
    Return aggregate counts across all event log files.
    Shows total events broken down by type, sport, and league.
    Useful for understanding what activity has been recorded.
    """
    from collections import Counter
    by_type:   Counter = Counter()
    by_sport:  Counter = Counter()
    by_league: Counter = Counter()
    by_date:   Counter = Counter()
    games: dict[str, dict[str, Any]] = {}   # event_id -> {game, sport, league, count}

    for date_str, path in _iter_event_files(None, None):
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype  = ev.get("type", ev.get("event_type", "UNKNOWN"))
                    sport  = ev.get("sport", "unknown")
                    league = ev.get("league", "unknown")
                    eid    = str(ev.get("event_id", ""))
                    by_type[etype]               += 1
                    by_sport[sport]              += 1
                    by_league[f"{sport}/{league}"] += 1
                    by_date[date_str]            += 1
                    if eid:
                        if eid not in games:
                            games[eid] = {
                                "event_id": eid,
                                "game":     ev.get("game", ev.get("short_name", "")),
                                "sport":    sport,
                                "league":   league,
                                "count":    0,
                            }
                        games[eid]["count"] += 1
        except OSError:
            continue

    total = sum(by_type.values())
    top_games = sorted(games.values(), key=lambda g: -g["count"])[:20]
    date_keys = sorted(by_date.keys())

    return JSONResponse({
        "total_events":   total,
        "unique_games":   len(games),
        "date_range": {
            "from": date_keys[0] if date_keys else None,
            "to":   date_keys[-1] if date_keys else None,
            "days": len(date_keys),
        },
        "by_type":   dict(sorted(by_type.items(),   key=lambda x: -x[1])),
        "by_sport":  dict(sorted(by_sport.items(),  key=lambda x: -x[1])),
        "by_league": dict(sorted(by_league.items(), key=lambda x: -x[1])),
        "top_games_by_activity": top_games,
    })


# ---------------------------------------------------------------------------
# Game Timeline endpoint
# ---------------------------------------------------------------------------

@app.get("/stats/game/timeline")
def stats_game_timeline(
    event_id: str  = Query(..., description="ESPN event ID"),
    _:        None = Depends(_verify_token),
) -> JSONResponse:
    """
    Return every recorded event for a single game in chronological order.
    Includes score updates, line moves, period changes, win probability
    shifts, and the final result — all sorted oldest to newest.
    """
    matched: list[dict[str, Any]] = []

    for _date_str, path in _iter_event_files(None, None):
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if str(ev.get("event_id", "")) == event_id:
                        matched.append(ev)
        except OSError:
            continue

    matched.sort(key=lambda e: e.get("timestamp", ""))

    game_meta: dict[str, Any] = {}
    if matched:
        first = matched[0]
        game_meta = {
            "event_id": event_id,
            "game":     first.get("game", first.get("short_name", "")),
            "sport":    first.get("sport"),
            "league":   first.get("league"),
        }

    return JSONResponse({
        "found":       bool(matched),
        "event_id":    event_id,
        "game":        game_meta.get("game"),
        "sport":       game_meta.get("sport"),
        "league":      game_meta.get("league"),
        "event_count": len(matched),
        "events":      matched,
    })


# ---------------------------------------------------------------------------
# Head-to-Head History endpoint
# ---------------------------------------------------------------------------

@app.get("/stats/matchups")
def stats_matchups(
    team:     str           = Query(..., description="One team name or abbreviation"),
    opponent: str           = Query(..., description="Opposing team name or abbreviation"),
    sport:    Optional[str] = Query(None, description="Sport filter"),
    limit:    int           = Query(10, ge=1, le=50, description="Max games to return"),
    _:        None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Return historical head-to-head games between two teams, most recent first.
    Also checks the live state for any upcoming/live matchup between them.
    """
    sport_filter = "AND LOWER(g.sport) = LOWER(?)" if sport else ""

    _home_sub = """
        SELECT gt.event_id, t.team_name, t.team_abbr
        FROM   game_teams gt
        JOIN   teams      t ON t.team_id = gt.team_id AND t.sport = gt.sport
        WHERE  gt.home_away = 'home'
    """
    _away_sub = """
        SELECT gt.event_id, t.team_name, t.team_abbr
        FROM   game_teams gt
        JOIN   teams      t ON t.team_id = gt.team_id AND t.sport = gt.sport
        WHERE  gt.home_away = 'away'
    """

    sql = f"""
        SELECT
            g.event_id,
            g.game_date     AS date,
            g.sport,
            g.league,
            g.short_name,
            COALESCE(ht.team_name, '') AS home_team,
            COALESCE(ht.team_abbr, '') AS home_abbr,
            COALESCE(awt.team_name, '') AS away_team,
            COALESCE(awt.team_abbr, '') AS away_abbr,
            g.home_score,
            g.away_score,
            g.status
        FROM games g
        LEFT JOIN ({_home_sub}) ht ON ht.event_id = g.event_id
        LEFT JOIN ({_away_sub}) awt ON awt.event_id = g.event_id
        WHERE g.status = 'post'
          {sport_filter}
        ORDER BY g.game_date DESC
        LIMIT ?
    """

    params: list[Any] = []
    if sport:
        params.append(sport)
    params.append(500)  # fetch wide, filter client-side for both-team match

    try:
        rows = _query(sql, params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    results = []
    for r in rows:
        if not (
            _team_matches(team, r["home_team"], r["home_abbr"]) or
            _team_matches(team, r["away_team"], r["away_abbr"])
        ):
            continue
        if not (
            _team_matches(opponent, r["home_team"], r["home_abbr"]) or
            _team_matches(opponent, r["away_team"], r["away_abbr"])
        ):
            continue

        home_score = _as_int(r["home_score"])
        away_score = _as_int(r["away_score"])
        if home_score is not None and away_score is not None:
            if home_score > away_score:
                winner = r["home_team"]
            elif away_score > home_score:
                winner = r["away_team"]
            else:
                winner = "draw"
        else:
            winner = None

        results.append({
            "event_id":   str(r["event_id"]),
            "date":       str(r["date"]) if r["date"] else None,
            "sport":      r["sport"],
            "league":     r["league"],
            "short_name": r["short_name"],
            "home_team":  r["home_team"] or None,
            "away_team":  r["away_team"] or None,
            "home_score": home_score,
            "away_score": away_score,
            "winner":     winner,
            "status":     r["status"],
        })
        if len(results) >= limit:
            break

    # Tally head-to-head record
    team_norm = _normalize_text(team)
    team_wins = sum(
        1 for g in results
        if g["winner"] and _normalize_text(g["winner"]) != "draw"
        and team_norm in _normalize_text(g["winner"])
    )
    opp_wins = len([g for g in results if g["winner"] and not (
        _normalize_text(g["winner"]) == "draw" or team_norm in _normalize_text(g["winner"])
    )])
    draws = sum(1 for g in results if g["winner"] == "draw")

    return JSONResponse({
        "found":    bool(results),
        "team":     team,
        "opponent": opponent,
        "sport":    sport,
        "h2h_record": {
            "team_wins":     team_wins,
            "opponent_wins": opp_wins,
            "draws":         draws,
            "total_games":   len(results),
        },
        "games": results,
    })


# ---------------------------------------------------------------------------
# Odds History endpoint
# ---------------------------------------------------------------------------

@app.get("/stats/game/odds-history")
def stats_game_odds_history(
    event_id: str  = Query(..., description="ESPN event ID"),
    _:        None = Depends(_verify_token),
) -> JSONResponse:
    """
    Return the full timeline of how odds moved for one game —
    moneyline shifts, total line changes, spread updates —
    from opening line to close, in chronological order.
    """
    odds_types = {"LINE_MOVE", "TOTAL_MOVE", "ODDS_MOVE"}
    matched: list[dict[str, Any]] = []

    for _date_str, path in _iter_event_files(None, None):
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if str(ev.get("event_id", "")) == event_id:
                        etype = ev.get("type", ev.get("event_type", ""))
                        if etype in odds_types:
                            matched.append(ev)
        except OSError:
            continue

    matched.sort(key=lambda e: e.get("timestamp", ""))

    game_meta: dict[str, Any] = {}
    if matched:
        first = matched[0]
        game_meta = {
            "game":   first.get("game", first.get("short_name", "")),
            "sport":  first.get("sport"),
            "league": first.get("league"),
        }

    # Build a summary of opening vs closing values by field
    opening: dict[str, Any] = {}
    closing: dict[str, Any] = {}
    for ev in matched:
        field = ev.get("field")
        if not field:
            continue
        val = ev.get("new_value")
        if field not in opening and ev.get("old_value") is not None:
            opening[field] = ev["old_value"]
        closing[field] = val

    return JSONResponse({
        "found":       bool(matched),
        "event_id":    event_id,
        "game":        game_meta.get("game"),
        "sport":       game_meta.get("sport"),
        "league":      game_meta.get("league"),
        "move_count":  len(matched),
        "opening":     opening,
        "closing":     closing,
        "history":     matched,
    })


# ---------------------------------------------------------------------------
# Injuries endpoint
# ---------------------------------------------------------------------------

@app.get("/stats/injuries")
def stats_injuries(
    sport:  Optional[str] = Query(None, description="Sport filter: basketball, soccer, hockey, baseball, football"),
    team:   Optional[str] = Query(None, description="Team name or abbreviation (partial match)"),
    player: Optional[str] = Query(None, description="Player name (partial match)"),
    _:      None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Return current injury and availability status for players.
    Data is fetched from ESPN every ~10 minutes and stored in live/injuries.json.
    """
    path = os.path.join(LIVE_DIR, "injuries.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return JSONResponse({"found": False, "injuries": [], "error": "Injury data not yet available — refreshes every 10 minutes"})

    injuries: list[dict[str, Any]] = data.get("injuries", [])

    if sport:
        injuries = [i for i in injuries if _normalize_text(i.get("sport", "")) == _normalize_text(sport)]
    if team:
        t = _normalize_text(team)
        injuries = [
            i for i in injuries
            if t in _normalize_text(i.get("team_name", ""))
            or t in _normalize_text(i.get("team_abbr", ""))
        ]
    if player:
        p = _normalize_text(player)
        injuries = [i for i in injuries if p in _normalize_text(i.get("player_name", ""))]

    return JSONResponse({
        "found":      bool(injuries),
        "count":      len(injuries),
        "fetched_at": data.get("fetched_at"),
        "injuries":   injuries,
    })


# ---------------------------------------------------------------------------
# Team Trends endpoint  (ATS / O-U / home-away splits)
# ---------------------------------------------------------------------------

@app.get("/stats/trends")
def stats_trends(
    team:   str           = Query(..., description="Team name or abbreviation"),
    sport:  Optional[str] = Query(None, description="Sport filter: basketball, soccer, hockey, baseball"),
    league: Optional[str] = Query(None, description="League filter: nba, mlb, nhl, eng.1 …"),
    limit:  int           = Query(50, ge=5, le=200, description="Number of recent completed games to analyse (default 50)"),
    _:      None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Return ATS (against the spread), over/under, and home/away performance
    trends for a team, computed from the historical DuckDB database.

    ATS  — team covered the spread (team_score + spread > opponent_score).
    O/U  — combined final score vs the posted game total line.
    Splits include home-only and away-only breakdowns.
    """
    sport_clause  = "AND LOWER(g.sport)  = LOWER(?)" if sport  else ""
    league_clause = "AND LOWER(g.league) = LOWER(?)" if league else ""

    sql = f"""
        SELECT
            g.event_id,
            g.game_date,
            g.sport,
            g.league,
            CAST(g.home_score  AS DOUBLE) AS home_score,
            CAST(g.away_score  AS DOUBLE) AS away_score,
            CAST(g.game_total  AS DOUBLE) AS game_total,
            my_gt.home_away                AS my_side,
            CAST(my_gt.spread  AS DOUBLE)  AS my_spread,
            CAST(my_gt.moneyline AS DOUBLE) AS my_ml
        FROM games g
        JOIN game_teams my_gt  ON my_gt.event_id = g.event_id
        JOIN teams      my_t   ON my_t.team_id   = my_gt.team_id
                               AND my_t.sport     = my_gt.sport
        WHERE g.status = 'post'
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND (
              LOWER(my_t.team_name) LIKE LOWER(?)
              OR LOWER(my_t.team_abbr) = LOWER(?)
          )
          {sport_clause}
          {league_clause}
        ORDER BY g.game_date DESC
        LIMIT ?
    """

    team_like = f"%{team}%"
    params: list[Any] = [team_like, team.lower()]
    if sport:  params.append(sport)
    if league: params.append(league)
    params.append(limit)

    try:
        rows = _query(sql, params)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if not rows:
        return JSONResponse({
            "found": False,
            "team":  team,
            "sport": sport,
            "games_analysed": 0,
            "message": "No completed games found for this team in the database.",
        })

    # ---- compute record accumulators ----
    def _blank():
        return {"covers": 0, "losses": 0, "pushes": 0,
                "overs": 0, "unders": 0, "ou_pushes": 0,
                "wins": 0, "defeats": 0, "draws": 0, "games": 0}

    overall = _blank()
    home    = _blank()
    away    = _blank()
    recent_form: list[dict[str, Any]] = []

    for r in rows:
        hs = r["home_score"]
        as_ = r["away_score"]
        gt  = r["game_total"]
        side = r["my_side"]          # "home" or "away"
        spd  = r["my_spread"]        # spread from DB for this team's side

        bucket = home if side == "home" else away
        overall["games"] += 1
        bucket["games"]  += 1

        # --- win / loss / draw (straight up) ---
        if hs is not None and as_ is not None:
            if side == "home":
                my_score, opp_score = hs, as_
            else:
                my_score, opp_score = as_, hs

            if my_score > opp_score:
                overall["wins"]    += 1
                bucket["wins"]     += 1
                su = "W"
            elif my_score < opp_score:
                overall["defeats"] += 1
                bucket["defeats"]  += 1
                su = "L"
            else:
                overall["draws"]   += 1
                bucket["draws"]    += 1
                su = "D"

            # --- ATS ---
            ats = "N/A"
            if spd is not None:
                covered_score = my_score + spd
                if covered_score > opp_score:
                    overall["covers"] += 1
                    bucket["covers"]  += 1
                    ats = "cover"
                elif covered_score < opp_score:
                    overall["losses"] += 1
                    bucket["losses"]  += 1
                    ats = "loss"
                else:
                    overall["pushes"] += 1
                    bucket["pushes"]  += 1
                    ats = "push"
        else:
            su, ats, my_score, opp_score = "?", "N/A", None, None

        # --- O/U ---
        ou = "N/A"
        if gt is not None and hs is not None and as_ is not None:
            total = hs + as_
            if total > gt:
                overall["overs"]  += 1
                bucket["overs"]   += 1
                ou = "over"
            elif total < gt:
                overall["unders"] += 1
                bucket["unders"]  += 1
                ou = "under"
            else:
                overall["ou_pushes"] += 1
                bucket["ou_pushes"]  += 1
                ou = "push"

        if len(recent_form) < 10:
            recent_form.append({
                "event_id":  str(r["event_id"]),
                "date":      str(r["game_date"]) if r["game_date"] else None,
                "side":      side,
                "score":     f"{int(my_score)}-{int(opp_score)}" if my_score is not None else None,
                "su":        su,
                "ats":       ats,
                "ou":        ou,
                "spread":    spd,
            })

    def _pct(n, d):
        return round(n / d * 100, 1) if d else None

    def _fmt(b):
        ats_games = b["covers"] + b["losses"] + b["pushes"]
        ou_games  = b["overs"]  + b["unders"]  + b["ou_pushes"]
        return {
            "games": b["games"],
            "su_record": f"{b['wins']}-{b['defeats']}-{b['draws']}",
            "ats": {
                "covers": b["covers"], "losses": b["losses"], "pushes": b["pushes"],
                "cover_pct": _pct(b["covers"], ats_games),
            },
            "ou": {
                "overs": b["overs"], "unders": b["unders"], "pushes": b["ou_pushes"],
                "over_pct": _pct(b["overs"], ou_games),
            },
        }

    return JSONResponse({
        "found":          True,
        "team":           team,
        "sport":          sport,
        "league":         league,
        "games_analysed": overall["games"],
        "overall":        _fmt(overall),
        "home":           _fmt(home),
        "away":           _fmt(away),
        "recent_form":    recent_form,
    })


# ---------------------------------------------------------------------------
# Player Prop Settlement
# ---------------------------------------------------------------------------
#
# PROP_STAT_MAP: maps our market name → ESPN stat_key candidates (tried left→right).
# Keys are confirmed present in ESPN data (see tools/verify_db.py sport-key checks).
#
# When a market is NOT in this map, or when the player/stat is absent from a
# specific game's data, we return espn_limitation=True with a plain-English
# explanation so callers know this is ESPN's data coverage, not a system error.
# ---------------------------------------------------------------------------

PROP_STAT_MAP: dict[str, list[str]] = {
    # ── Basketball (NBA) ────────────────────────────────────────────────────
    # Confirmed keys: PTS, REB, AST, MIN; slash-format: FG, 3PT, FT
    "player_points":      ["PTS"],
    "player_rebounds":    ["REB"],
    "player_assists":     ["AST"],
    "player_threes":      ["3PT"],          # slash "made-att" → first number used
    "player_steals":      ["STL"],          # present in ESPN, not in verify_db checks
    "player_blocks":      ["BLK"],          # present in ESPN, not in verify_db checks
    "player_turnovers":   ["TOV", "TO"],
    "player_minutes":     ["MIN"],
    "player_fg_made":     ["FG"],           # slash "made-att" → first number
    "player_ft_made":     ["FT"],           # slash "made-att" → first number
    # ── Soccer ──────────────────────────────────────────────────────────────
    # Confirmed keys: G, SV, YC
    "player_goals":       ["G"],
    "player_saves":       ["SV"],
    "player_yellow_cards":["YC"],
    # ── Hockey (NHL) ────────────────────────────────────────────────────────
    # Confirmed keys: G, A, TOI
    "player_goals_hockey":   ["G"],
    "player_assists_hockey": ["A"],
    # ── Baseball (MLB) ──────────────────────────────────────────────────────
    # Confirmed keys: H, RBI, ERA
    "player_hits":        ["H"],
    "player_rbis":        ["RBI"],
    # ── Cricket ─────────────────────────────────────────────────────────────
    "player_runs_cricket":    ["BAT_INN1_RUNS", "BAT_INN2_RUNS"],
    "player_wickets_cricket": ["BWL_INN1_WICKETS", "BWL_INN2_WICKETS"],
}

# Stat keys where ESPN stores the value as "made-attempted" (e.g. "8-18").
# We extract the made count (first number) for prop comparison.
_SLASH_STAT_KEYS: frozenset[str] = frozenset({"FG", "3PT", "FT", "H-AB", "PC-ST"})

# Markets ESPN definitively does not provide in any game data.
# Returning this list in the limitation response proves the gap is ESPN's, not ours.
ESPN_UNSUPPORTED_PROPS: frozenset[str] = frozenset({
    "anytime_scorer", "first_scorer", "last_scorer",
    "first_td", "last_td", "first_basket",
    "player_double_double", "player_triple_double", "player_hat_trick",
    # American-football props — ESPN tracks NFL but our ingest does not collect them
    "player_pass_yards", "player_rush_yards", "player_receiving_yards",
    "player_pass_tds", "player_rush_tds", "player_receiving_tds",
    "player_receptions", "player_pass_attempts",
})

SUPPORTED_PROP_MARKETS: list[str] = sorted(PROP_STAT_MAP.keys())


def _extract_stat_value(raw: Any, stat_key: str) -> float | None:
    """Parse a raw stat string to float, handling slash format (e.g. '8-18' → 8.0)."""
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if stat_key in _SLASH_STAT_KEYS and "-" in s:
        try:
            return float(s.split("-")[0])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


@app.get("/stats/prop-check")
def stats_prop_check(
    player:   str           = Query(...,  description="Player display name (partial match, case-insensitive)"),
    market:   str           = Query(...,  description="Prop market: player_points | player_rebounds | player_assists | player_threes | player_goals | player_hits | player_rbis | … (see espn_supported_markets in response)"),
    pick:     str           = Query(...,  description="over | under"),
    line:     float         = Query(...,  description="The prop line to evaluate (e.g. 25.5)"),
    event_id: Optional[str] = Query(None, description="ESPN event_id — most precise"),
    date:     Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport:    Optional[str] = Query(None, description="Sport filter"),
    team:     Optional[str] = Query(None, description="Team filter (helps narrow player search)"),
    _:        None          = Depends(_verify_token),
) -> JSONResponse:
    """
    Evaluate a player prop bet against ESPN historical data.

    Returns outcome (win/loss/push/pending) and a settlement explanation.

    When ESPN does not carry the requested stat, the response includes
    ``espn_limitation: true`` and a plain-English ``espn_limitation_reason``
    listing exactly what IS available — so it is clear the gap is in
    ESPN's data coverage, not in this system.

    Supported markets: player_points, player_rebounds, player_assists,
    player_threes, player_steals, player_blocks, player_turnovers,
    player_goals, player_saves, player_yellow_cards, player_goals_hockey,
    player_assists_hockey, player_hits, player_rbis, player_runs_cricket,
    player_wickets_cricket, player_fg_made, player_ft_made, player_minutes.
    """
    market_norm = market.strip().lower().replace(" ", "_")
    pick_norm   = pick.strip().lower()

    # ── 1. Reject picks that aren't over/under ──────────────────────────────
    if pick_norm not in {"over", "under"}:
        raise HTTPException(
            status_code=400,
            detail="pick must be 'over' or 'under' for player prop bets.",
        )

    # ── 2. Definitively unsupported by ESPN ────────────────────────────────
    if market_norm in ESPN_UNSUPPORTED_PROPS:
        return JSONResponse(status_code=200, content={
            "found":                  False,
            "outcome":                "pending",
            "settled":                False,
            "espn_limitation":        True,
            "espn_limitation_reason": (
                f"'{market_norm}' is not available in ESPN data. "
                "ESPN does not provide this stat type for any game in our database."
            ),
            "requested_market":       market_norm,
            "requested_player":       player,
            "espn_supported_markets": SUPPORTED_PROP_MARKETS,
        })

    # ── 3. Unknown market (not supported and not known-unsupported) ─────────
    if market_norm not in PROP_STAT_MAP:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown prop market '{market_norm}'. "
                f"Supported: {SUPPORTED_PROP_MARKETS}. "
                f"Known ESPN limitations (always pending): {sorted(ESPN_UNSUPPORTED_PROPS)}."
            ),
        )

    stat_keys = PROP_STAT_MAP[market_norm]

    # ── 4. Require at least one game locator ────────────────────────────────
    if not event_id and not date:
        raise HTTPException(
            status_code=400,
            detail="Provide event_id or date (YYYY-MM-DD) to locate the game.",
        )

    # ── 5. Find the game ────────────────────────────────────────────────────
    try:
        event = _resolve_event(event_id, _date_only(date) if date else None, sport, team, None)
    except Exception as exc:
        _log = __import__("logging").getLogger(__name__)
        _log.warning("prop-check _resolve_event error: %s", exc)
        event = None

    if event is None:
        return JSONResponse(status_code=404, content={
            "found":   False,
            "outcome": "pending",
            "settled": False,
            "note":    "Game not found — it may be in the future or not yet tracked.",
        })

    resolved_event_id = str(event.get("event_id") or "")
    game_status       = _normalize_text(str(event.get("status") or ""))
    game_settled      = (game_status == "post")

    # ── 6. Look up player in game_players for this event ────────────────────
    sql = """
        SELECT
            gp.stats_json,
            gp.did_not_play,
            gp.dnp_reason,
            p.display_name  AS player_name
        FROM game_players gp
        JOIN players p ON p.player_id = gp.player_id
                      AND p.sport     = gp.sport
        WHERE gp.event_id = ?
          AND LOWER(p.display_name) LIKE LOWER(?)
        LIMIT 5
    """
    try:
        rows = _query(sql, [resolved_event_id, f"%{player}%"])
    except Exception as exc:
        return JSONResponse(status_code=503, content={
            "found":   False,
            "outcome": "pending",
            "settled": False,
            "error":   f"Database error: {exc}",
        })

    if not rows:
        return JSONResponse(status_code=200, content={
            "found":                  False,
            "outcome":                "pending",
            "settled":                False,
            "espn_limitation":        True,
            "espn_limitation_reason": (
                f"Player '{player}' was not found in ESPN data for this game "
                f"(event_id={resolved_event_id}). "
                "ESPN may not have tracked individual stats for this player, "
                "or the player did not participate."
            ),
            "requested_market":       market_norm,
            "requested_player":       player,
            "game_status":            game_status,
            "espn_supported_markets": SUPPORTED_PROP_MARKETS,
        })

    # Use the first match (closest name)
    row         = rows[0]
    player_name = row["player_name"]
    did_not_play = bool(row.get("did_not_play"))

    # ── 7. Parse stats_json ─────────────────────────────────────────────────
    raw_stats: dict[str, Any] = {}
    try:
        raw_stats = json.loads(row["stats_json"] or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        pass

    available_keys = sorted(raw_stats.keys())

    # DNP player → cannot settle
    if did_not_play:
        return JSONResponse(status_code=200, content={
            "found":                  True,
            "player":                 player_name,
            "outcome":                "pending",
            "settled":                False,
            "espn_limitation":        True,
            "espn_limitation_reason": (
                f"ESPN marks {player_name} as Did Not Play for this game"
                + (f" ({row.get('dnp_reason')})" if row.get("dnp_reason") else "")
                + ". Prop bets on DNP players cannot be settled automatically."
            ),
            "requested_market":  market_norm,
            "espn_available_stats": available_keys,
        })

    # ── 8. Try each candidate key until one has a value ────────────────────
    stat_value: float | None = None
    matched_key: str | None  = None
    for key in stat_keys:
        val = _extract_stat_value(raw_stats.get(key), key)
        if val is not None:
            stat_value  = val
            matched_key = key
            break

    if stat_value is None:
        keys_tried = stat_keys
        return JSONResponse(status_code=200, content={
            "found":                  True,
            "player":                 player_name,
            "outcome":                "pending",
            "settled":                False,
            "espn_limitation":        True,
            "espn_limitation_reason": (
                f"ESPN data for {player_name} in this game does not include "
                f"the stat needed for '{market_norm}' "
                f"(keys tried: {keys_tried}). "
                "This stat may not be tracked by ESPN for this sport/game, "
                "or the player's stats were not reported for this fixture."
            ),
            "requested_market":       market_norm,
            "espn_stat_keys_tried":   keys_tried,
            "espn_available_stats":   available_keys,
            "espn_supported_markets": SUPPORTED_PROP_MARKETS,
        })

    # ── 9. Evaluate over/under ──────────────────────────────────────────────
    if not game_settled:
        outcome = "pending"
        settled = False
    elif stat_value > line:
        outcome = "win"   if pick_norm == "over"  else "loss"
        settled = True
    elif stat_value < line:
        outcome = "win"   if pick_norm == "under" else "loss"
        settled = True
    else:
        outcome = "push"
        settled = True

    return JSONResponse(status_code=200, content={
        "found":         True,
        "player":        player_name,
        "market":        market_norm,
        "pick":          pick_norm,
        "line":          line,
        "stat_key":      matched_key,
        "stat_value":    stat_value,
        "outcome":       outcome,
        "settled":       settled,
        "source":        event.get("source", "historical"),
        "game_status":   game_status,
        "event_id":      resolved_event_id,
        "espn_limitation": False,
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=_PORT)
