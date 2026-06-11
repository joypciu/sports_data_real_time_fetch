import sqlite3
import os
import json
from datetime import datetime, timezone, timedelta
import difflib
import unicodedata
import re
from typing import Any

DB_PATH = os.path.join(os.path.dirname(__file__), "db", "sofascore_cache.db")


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sofascore_events (
                event_id          TEXT PRIMARY KEY,
                sport             TEXT NOT NULL,
                game_date         TEXT NOT NULL,
                home_name         TEXT NOT NULL,
                away_name         TEXT NOT NULL,
                home_name_norm    TEXT,
                away_name_norm    TEXT,
                status_type       TEXT,
                status_desc       TEXT,
                is_finished       INTEGER NOT NULL DEFAULT 0,
                home_score_current INTEGER,
                away_score_current INTEGER,
                home_score_raw    TEXT,
                away_score_raw    TEXT,
                tournament_name   TEXT,
                tournament_slug   TEXT,
                start_timestamp   INTEGER,
                scraped_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                event_payload     TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_events_date_sport ON sofascore_events (game_date, sport)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_events_home_norm ON sofascore_events (game_date, sport, home_name_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_events_away_norm ON sofascore_events (game_date, sport, away_name_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_events_finished ON sofascore_events (is_finished, game_date)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sofascore_event_details (
                event_id      TEXT NOT NULL,
                detail_type   TEXT NOT NULL,
                payload       TEXT NOT NULL,
                scraped_at    TEXT NOT NULL,
                PRIMARY KEY (event_id, detail_type)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sofascore_ingest_log (
                run_id        TEXT PRIMARY KEY,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                sport         TEXT,
                game_date     TEXT,
                events_seen   INTEGER DEFAULT 0,
                events_upserted INTEGER DEFAULT 0,
                details_fetched INTEGER DEFAULT 0,
                proxy_used    INTEGER,
                status        TEXT,
                error_message TEXT,
                duration_sec  REAL
            )
        """)


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = str(s).lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    an = _norm(a)
    bn = _norm(b)
    if not an or not bn:
        return 0.0
    if an == bn:
        return 1.0

    an_parts = set(an.split())
    bn_parts = set(bn.split())
    if an_parts and an_parts.issubset(bn_parts):
        return 0.95
    if bn_parts and bn_parts.issubset(an_parts):
        return 0.95

    return difflib.SequenceMatcher(None, an, bn).ratio()


def lookup_event(sport: str, game_date: str, team_hint: str | None = None, opponent_hint: str | None = None) -> dict[str, Any] | None:
    """Find the best matching event from the local cache."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sofascore_events WHERE sport = ? AND game_date = ?",
            (sport, game_date)
        ).fetchall()

    if not rows:
        return None

    best_match = None
    best_score = 0.0

    for row in rows:
        event = json.loads(row["event_payload"])
        if not team_hint:
            # If no team hint is provided, we can't fuzzy match. Just return the first one (rare).
            return event

        score_h = _name_score(team_hint, row["home_name"])
        score_a = _name_score(team_hint, row["away_name"])
        candidate_score = max(score_h, score_a)

        if opponent_hint:
            opp_h = _name_score(opponent_hint, row["home_name"])
            opp_a = _name_score(opponent_hint, row["away_name"])
            candidate_score += max(opp_h, opp_a)

        if candidate_score > best_score and candidate_score > 0.6:
            best_score = candidate_score
            best_match = event

    return best_match


def lookup_details(event_id: str, detail_type: str) -> dict[str, Any] | None:
    """Fetch stored details (incidents, statistics, lineups)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT payload FROM sofascore_event_details WHERE event_id = ? AND detail_type = ?",
            (str(event_id), detail_type)
        ).fetchone()

    if row:
        return json.loads(row["payload"])
    return None


def upsert_event(sport: str, game_date: str, event: dict[str, Any]) -> None:
    event_id = str(event.get("id", ""))
    if not event_id:
        return

    home_team = event.get("homeTeam", {})
    away_team = event.get("awayTeam", {})
    home_name = home_team.get("name", "")
    away_name = away_team.get("name", "")
    
    status = event.get("status", {})
    status_type = status.get("type", "")
    status_desc = status.get("description", "")
    is_finished = 1 if status_type == "finished" else 0
    
    home_score_raw = event.get("homeScore", {})
    away_score_raw = event.get("awayScore", {})
    home_score_current = home_score_raw.get("current")
    away_score_current = away_score_raw.get("current")

    tournament = event.get("tournament", {})
    tournament_name = tournament.get("name", "")
    tournament_slug = tournament.get("slug", "")

    start_timestamp = event.get("startTimestamp")
    now = datetime.now(timezone.utc).isoformat()
    
    payload_json = json.dumps(event)
    home_raw_json = json.dumps(home_score_raw)
    away_raw_json = json.dumps(away_score_raw)

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO sofascore_events (
                event_id, sport, game_date, home_name, away_name, 
                home_name_norm, away_name_norm, status_type, status_desc, is_finished,
                home_score_current, away_score_current, home_score_raw, away_score_raw,
                tournament_name, tournament_slug, start_timestamp,
                scraped_at, updated_at, event_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                status_type = excluded.status_type,
                status_desc = excluded.status_desc,
                is_finished = excluded.is_finished,
                home_score_current = excluded.home_score_current,
                away_score_current = excluded.away_score_current,
                home_score_raw = excluded.home_score_raw,
                away_score_raw = excluded.away_score_raw,
                updated_at = excluded.updated_at,
                event_payload = excluded.event_payload
        """, (
            event_id, sport, game_date, home_name, away_name,
            _norm(home_name), _norm(away_name), status_type, status_desc, is_finished,
            home_score_current, away_score_current, home_raw_json, away_raw_json,
            tournament_name, tournament_slug, start_timestamp,
            now, now, payload_json
        ))


def upsert_details(event_id: str, detail_type: str, payload: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO sofascore_event_details (event_id, detail_type, payload, scraped_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, detail_type) DO UPDATE SET
                payload = excluded.payload,
                scraped_at = excluded.scraped_at
        """, (str(event_id), detail_type, payload_json, now))

def prune_old_events(days: int = 30) -> None:
    """Delete events and details older than `days` days from the database."""
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_connection() as conn:
        # Delete details for old events first
        conn.execute("""
            DELETE FROM sofascore_event_details
            WHERE event_id IN (
                SELECT event_id FROM sofascore_events WHERE game_date < ?
            )
        """, (cutoff_date,))
        # Delete old events
        conn.execute("DELETE FROM sofascore_events WHERE game_date < ?", (cutoff_date,))
        # Optionally, delete old ingest logs
        conn.execute("DELETE FROM sofascore_ingest_log WHERE started_at < ?", (cutoff_date,))

def get_ingest_logs(limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recent ingest logs."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT run_id, started_at, finished_at, status, events_seen, events_upserted, details_fetched, duration_sec, error_message
            FROM sofascore_ingest_log
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]

# Initialize on import
init_db()
