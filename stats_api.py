<<<<<<< HEAD
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
    if not team or not opponent:
        return True

    forward = (
        _team_matches(team, event.get("home_team"), event.get("home_abbr"))
        and _team_matches(opponent, event.get("away_team"), event.get("away_abbr"))
    )
    reverse = (
        _team_matches(team, event.get("away_team"), event.get("away_abbr"))
        and _team_matches(opponent, event.get("home_team"), event.get("home_abbr"))
    )
    return forward or reverse


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

    historical_candidates = _historical_event_candidates(event_id, game_date, sport)
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
            detail="market must be one of moneyline, spread, or total",
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
    if not event_id and not (date and team and opponent):
        raise HTTPException(
            status_code=400,
            detail="Provide either event_id or date + team + opponent",
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
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=_PORT)
=======
import os
import json
import duckdb
from fastapi import FastAPI, HTTPException, Query
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

app = FastAPI(title="Sports Stats API")

# Configuration
DB_PATH = "db/sports.db"
ARCHIVE_DIR = "archive"
CUTOFF_MONTHS = 2  # Archive data older than 2 months


def get_db_connection():
    """Get a read-only connection to the DuckDB database."""
    return duckdb.connect(DB_PATH, read_only=True)


def load_json_archive(table_name: str) -> List[Dict[str, Any]]:
    """Load archived data from JSON file for a given table."""
    archive_path = os.path.join(ARCHIVE_DIR, f"old_{table_name}.json")
    if not os.path.exists(archive_path):
        return []

    try:
        with open(archive_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading archive {archive_path}: {e}")
        return []


def get_player_stats_from_db(
    player_id: int, start_date: Optional[str] = None, end_date: Optional[str] = None
) -> List[Dict]:
    """Fetch player stats from the database."""
    conn = get_db_connection()
    try:
        query = """
            SELECT ps.*, g.game_date
            FROM player_stats ps
            JOIN games g ON ps.game_id = g.id
            WHERE ps.player_id = ?
        """
        params = [player_id]

        if start_date:
            query += " AND g.game_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND g.game_date <= ?"
            params.append(end_date)

        query += " ORDER BY g.game_date"

        result = conn.execute(query, params).fetchall()
        # Get column names
        columns = [desc[0] for desc in conn.description]
        # Convert to list of dicts
        return [dict(zip(columns, row)) for row in result]
    finally:
        conn.close()


def get_team_stats_from_db(
    team_id: int, start_date: Optional[str] = None, end_date: Optional[str] = None
) -> List[Dict]:
    """Fetch team stats from the database."""
    conn = get_db_connection()
    try:
        query = """
            SELECT gt.*, g.game_date
            FROM game_teams gt
            JOIN games g ON gt.game_id = g.id
            WHERE gt.team_id = ?
        """
        params = [team_id]

        if start_date:
            query += " AND g.game_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND g.game_date <= ?"
            params.append(end_date)

        query += " ORDER BY g.game_date"

        result = conn.execute(query, params).fetchall()
        columns = [desc[0] for desc in conn.description]
        return [dict(zip(columns, row)) for row in result]
    finally:
        conn.close()


@app.get("/stats/player")
async def get_player_stats(
    player_id: int = Query(..., description="ID of the player"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """
    Get statistics for a specific player.
    First tries to fetch from the database (recent data).
    If no data found in DB, falls back to JSON archive (old data).
    """
    # Try database first
    db_stats = get_player_stats_from_db(player_id, start_date, end_date)
    if db_stats:
        return {"source": "database", "data": db_stats, "count": len(db_stats)}

    # Fallback to JSON archive
    archive_stats = load_json_archive("player_stats")
    if not archive_stats:
        raise HTTPException(
            status_code=404,
            detail=f"No stats found for player_id {player_id} in database or archive",
        )

    # Filter archive data by date if specified
    filtered_stats = archive_stats
    if start_date or end_date:
        filtered_stats = []
        for stat in archive_stats:
            game_date = stat.get("game_date")
            if game_date:
                if start_date and game_date < start_date:
                    continue
                if end_date and game_date > end_date:
                    continue
                filtered_stats.append(stat)

    if not filtered_stats:
        raise HTTPException(
            status_code=404,
            detail=f"No stats found for player_id {player_id} in archive matching date criteria",
        )

    return {"source": "archive", "data": filtered_stats, "count": len(filtered_stats)}


@app.get("/stats/team")
async def get_team_stats(
    team_id: int = Query(..., description="ID of the team"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """
    Get statistics for a specific team.
    First tries to fetch from the database (recent data).
    If no data found in DB, falls back to JSON archive (old data).
    """
    # Try database first
    db_stats = get_team_stats_from_db(team_id, start_date, end_date)
    if db_stats:
        return {"source": "database", "data": db_stats, "count": len(db_stats)}

    # Fallback to JSON archive
    archive_stats = load_json_archive("game_teams")
    if not archive_stats:
        raise HTTPException(
            status_code=404,
            detail=f"No stats found for team_id {team_id} in database or archive",
        )

    # Filter archive data by date if specified
    filtered_stats = archive_stats
    if start_date or end_date:
        filtered_stats = []
        for stat in archive_stats:
            game_date = stat.get("game_date")
            if game_date:
                if start_date and game_date < start_date:
                    continue
                if end_date and game_date > end_date:
                    continue
                filtered_stats.append(stat)

    if not filtered_stats:
        raise HTTPException(
            status_code=404,
            detail=f"No stats found for team_id {team_id} in archive matching date criteria",
        )

    return {"source": "archive", "data": filtered_stats, "count": len(filtered_stats)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
>>>>>>> 63ab153 (Add database archiving system and update stats API with fallback to JSON archives)
