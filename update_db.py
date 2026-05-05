"""
update_db.py
============
Background database maintenance running as Thread 3 inside main.py.

Two jobs run on separate schedules:

  1. Incremental historical update (default: every 5 minutes)
     Scans every JSON file in historical_data/ and inserts any games whose
     event_id is not yet in the `games` table.  Uses build_db.load_file() so
     the insertion logic is never duplicated.

  2. Live-games sync (default: every 35 seconds, or whenever live_state.json
     changes on disk)
     Reads live/live_state.json written by realtime_monitor.py and replaces
     all rows in the `live_games` table with the current pre/in/post snapshot.
     The table is intentionally volatile — callers see a fresh read every poll.

Thread 3 entry point (used by main.py)::

    import update_db
    t = threading.Thread(
        target=update_db.run_updater_loop,
        args=(db_path, data_dir, live_dir),
        daemon=True,
    )
    t.start()

Standalone usage::

    python update_db.py [--db db/sports.db] [--data-dir historical_data]
                        [--live-dir live] [--hist-interval 300]
                        [--live-interval 35]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import duckdb

import build_db  # reuses load_file(), DDL, and type-coercion helpers

log = logging.getLogger("update_db")

DEFAULT_DB       = os.path.join("db", "sports.db")
DEFAULT_DATA_DIR = "historical_data"
DEFAULT_LIVE_DIR = "live"


# ---------------------------------------------------------------------------
# Type-coercion helpers (mirrors build_db._int / _float)
# ---------------------------------------------------------------------------

def _int(v) -> int | None:          # noqa: ANN001
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:      # noqa: ANN001
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. Incremental historical update
# ---------------------------------------------------------------------------

def _connect_with_retry(db_path: str, retries: int = 6, delay: float = 2.0) -> duckdb.DuckDBPyConnection:  # type: ignore[name-defined]
    """Open a DuckDB connection, retrying if another connection holds the file.

    stats_api.py and update_db both use the default (read-write) mode so DuckDB
    can serialise them, but a brief retry window handles the race on startup or
    during a heavy query.
    """
    for attempt in range(retries):
        try:
            conn = duckdb.connect(db_path)
            conn.execute("SET memory_limit='4GB'")
            conn.execute("SET threads=4")
            return conn
        except Exception as exc:
            if attempt == retries - 1:
                raise
            log.warning("DB connect attempt %d/%d failed (%s) — retrying in %.1fs", attempt + 1, retries, exc, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


# (path -> mtime) of files already fully processed; avoids re-opening the
# write connection on every 5-minute tick for files that haven't changed.
_processed_files: dict[str, float] = {}


def incremental_historical_update(db_path: str, data_dir: str) -> tuple[int, int, int]:
    """Scan all JSON files in *data_dir* and insert any games not yet in the DB.

    Returns (games_added, player_rows_added, stat_rows_added).
    The function is idempotent — re-running it is always safe.

    The write connection is opened ONLY when there are new or modified files,
    minimising the lock window that would block stats_api read-only connections.
    """
    if not os.path.isdir(data_dir):
        return 0, 0, 0

    all_files = sorted(
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.endswith(".json")
    )

    # Pre-scan: identify files that are new or have been modified since last run.
    # This is done WITHOUT opening the DB, so no lock is held during the scan.
    new_files: list[str] = []
    for path in all_files:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if _processed_files.get(path) != mtime:
            new_files.append(path)

    if not new_files:
        return 0, 0, 0  # nothing changed — skip DB entirely

    total_g = total_p = total_s = 0
    processed_this_run: dict[str, float] = {}
    try:
        con = _connect_with_retry(db_path)
        try:
            for path in new_files:
                g, p, s = build_db.load_file(con, path)
                if g:
                    log.info(
                        "Incremental: +%d games, +%d player-rows, +%d stat-rows from %s",
                        g, p, s, os.path.basename(path),
                    )
                total_g += g
                total_p += p
                total_s += s
                try:
                    processed_this_run[path] = os.path.getmtime(path)
                except OSError:
                    pass
        finally:
            con.close()
    except Exception:
        log.exception("Error during incremental historical update")
        return total_g, total_p, total_s

    # Only mark files as processed after a successful DB session
    _processed_files.update(processed_this_run)
    return total_g, total_p, total_s


# ---------------------------------------------------------------------------
# 2. Live-games sync
# ---------------------------------------------------------------------------

def sync_live_games(db_path: str, live_dir: str) -> int:
    """Replace `live_games` table contents from *live_dir*/live_state.json.

    Returns the number of rows written (0 on no-op or error).
    Uses DELETE + bulk INSERT so the table always reflects the latest poll.
    """
    live_path = os.path.join(live_dir, "live_state.json")
    if not os.path.exists(live_path):
        return 0

    try:
        with open(live_path, encoding="utf-8") as fh:
            state: dict = json.load(fh)
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read %s — skipping live sync", live_path)
        return 0

    # Collect all pre/live/finished game state dicts
    all_games: list[dict] = (
        list(state.get("live")     or [])
        + list(state.get("pregame") or [])
        + list(state.get("finished") or [])
    )

    updated_at: str = build_db._ts(state.get("updated_at")) or ""

    rows: list[tuple] = []
    for g in all_games:
        event_id = str(g.get("event_id") or "")
        if not event_id:
            continue

        home = g.get("home") or {}
        away = g.get("away") or {}
        odds = g.get("odds") or {}
        wp   = g.get("win_prob") or {}

        rows.append((
            event_id,
            g.get("league_key", ""),
            g.get("sport", ""),
            g.get("league", ""),
            g.get("name", ""),
            g.get("status", ""),
            g.get("status_detail", ""),
            _int(g.get("period")),
            str(g.get("clock") or ""),
            str(home.get("team_id") or ""),
            home.get("team_name", ""),
            home.get("team_abbr", ""),
            str(home.get("score") or ""),
            str(away.get("team_id") or ""),
            away.get("team_name", ""),
            away.get("team_abbr", ""),
            str(away.get("score") or ""),
            _int(odds.get("home_ml")),
            _int(odds.get("away_ml")),
            _float(odds.get("home_spread")),
            _float(odds.get("game_total")),
            _float(wp.get("home_pct")),
            _float(wp.get("away_pct")),
            json.dumps(g.get("situation") or {}),
            json.dumps(g.get("players")   or []),
            updated_at or None,
        ))

    if not rows:
        # Nothing to write (e.g. file exists but all buckets are empty)
        return 0

    try:
        con = _connect_with_retry(db_path)
        try:
            # DROP + recreate is safer than DELETE for a volatile table —
            # DuckDB can corrupt index state on DELETE when rows were inserted
            # by a previous connection that died uncleanly (FatalException).
            con.execute("DROP TABLE IF EXISTS live_games")
            con.execute(build_db.DDL)  # recreates live_games via CREATE TABLE IF NOT EXISTS
            con.executemany(
                """
                INSERT INTO live_games (
                    event_id, league_key, sport, league, name,
                    status, status_detail, period, clock,
                    home_team_id, home_team_name, home_team_abbr, home_score,
                    away_team_id, away_team_name, away_team_abbr, away_score,
                    home_ml, away_ml, home_spread, game_total,
                    home_win_pct, away_win_pct,
                    situation, players, updated_at
                ) VALUES (
                    ?,?,?,?,?,
                    ?,?,?,?,
                    ?,?,?,?,
                    ?,?,?,?,
                    ?,?,?,?,
                    ?,?,
                    ?,?,?
                )
                """,
                rows,
            )
        finally:
            con.close()
        log.debug("Live sync: wrote %d rows to live_games", len(rows))
    except Exception:
        log.exception("Error syncing live_games table")
        return 0

    return len(rows)


# ---------------------------------------------------------------------------
# 3. Background loop (Thread 3 entry point)
# ---------------------------------------------------------------------------

VACUUM_INTERVAL = 6 * 3600  # VACUUM once every 6 hours to reclaim live_games dead space


def vacuum_db(db_path: str) -> None:
    """Run VACUUM to reclaim space from live_games DELETE/INSERT churn."""
    try:
        con = _connect_with_retry(db_path)
        try:
            log.info("Running VACUUM to reclaim dead space...")
            con.execute("VACUUM")
            log.info("VACUUM complete.")
        finally:
            con.close()
    except Exception:
        log.exception("Error running VACUUM")


# ---------------------------------------------------------------------------
# 4. Redis settlement cache invalidation
# ---------------------------------------------------------------------------

def _flush_settlement_cache() -> int:
    """Delete all Redis keys matching stats_bridge:market:* and stats_bridge:*.
    Called after new games are ingested so stale 'pending' settlements are evicted.
    Returns number of keys deleted.
    """
    try:
        from redis_cache import get_redis_client  # type: ignore[import]
        client = get_redis_client()
        if client is None:
            return 0
        deleted = 0
        for pattern in ("stats_bridge:market:*", "stats_bridge:*"):
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor, match=pattern, count=200)
                if keys:
                    deleted += client.delete(*keys)
                if cursor == 0:
                    break
        if deleted:
            log.info("Flushed %d stale settlement cache keys from Redis.", deleted)
        return deleted
    except Exception as exc:
        log.debug("Redis flush skipped: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# 5. Auto-backfill: fill date gaps from ESPN public scoreboard API
# ---------------------------------------------------------------------------

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# (espn_sport_path, espn_league_path, internal_sport, internal_league)
_BACKFILL_LEAGUES: dict[str, tuple[str, str, str, str]] = {
    "mlb":  ("baseball",    "mlb", "baseball",   "mlb"),
    "nba":  ("basketball",  "nba", "basketball", "nba"),
    "nhl":  ("hockey",      "nhl", "hockey",     "nhl"),
}


def _espn_fetch_scoreboard(sport_path: str, league_path: str, date_str: str) -> list[dict]:
    """Fetch ESPN public scoreboard for a date (YYYYMMDD). Returns raw events list."""
    url = f"{_ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
    params = urllib.parse.urlencode({"dates": date_str, "limit": 100})
    try:
        req = urllib.request.Request(
            f"{url}?{params}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("events", [])
    except Exception as exc:
        log.debug("ESPN fetch failed for %s/%s %s: %s", sport_path, league_path, date_str, exc)
        return []


def _espn_parse_event(event: dict, sport: str, league: str) -> dict | None:
    """Convert a raw ESPN event to the build_db.load_file JSON format."""
    event_id = str(event.get("id", ""))
    if not event_id:
        return None
    comps = event.get("competitions", [])
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors", [])
    home_raw = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away_raw = next((c for c in competitors if c.get("homeAway") == "away"), {})
    if not home_raw or not away_raw:
        return None

    def _team(raw: dict) -> dict:
        t = raw.get("team", {})
        sc = raw.get("score")
        return {
            "team_id":   str(t.get("id", "")),
            "team_name": t.get("displayName", t.get("name", "")),
            "team_abbr": t.get("abbreviation", ""),
            "score":     str(sc) if sc is not None else None,
            "is_winner": raw.get("winner"),
        }

    st  = comp.get("status", {})
    stt = st.get("type", {})
    # Only backfill finished games
    if stt.get("state", "pre") != "post":
        return None

    return {
        "event_id":      event_id,
        "name":          event.get("name", ""),
        "short_name":    event.get("shortName", ""),
        "date":          event.get("date", ""),
        "status":        "post",
        "status_detail": stt.get("description", "Final"),
        "period":        st.get("period", 0),
        "clock":         st.get("displayClock", "0:00"),
        "sport":         sport,
        "league":        league,
        "home":          _team(home_raw),
        "away":          _team(away_raw),
        "players":       [],
    }


def auto_backfill_gaps(db_path: str, data_dir: str) -> int:
    """Detect missing date ranges per sport and fetch from ESPN public API.

    For each tracked league, finds the latest game date in DuckDB and fetches
    all completed games for dates between that date and yesterday (inclusive).
    Writes results to historical_data/backfill_<league>.json so
    incremental_historical_update picks them up on the next tick.

    Returns total new games written across all leagues.
    """
    yesterday = date.today() - timedelta(days=1)
    total_new = 0

    # Get max dates from DB — must NOT use read_only=True in the same process as
    # the read-write connections (update_db write conn + stats_api thread conns);
    # DuckDB treats read_only vs read-write as a "different configuration" and
    # raises ConnectionException.  A plain read-write connection that only runs
    # SELECT is safe here.
    max_dates: dict[str, date] = {}
    try:
        con = duckdb.connect(db_path)
        rows = con.execute(
            "SELECT sport, MAX(CAST(game_date AS DATE)) FROM games GROUP BY sport"
        ).fetchall()
        con.close()
        for sport, max_dt in rows:
            if max_dt:
                max_dates[sport] = max_dt
    except Exception as exc:
        log.warning("auto_backfill: could not read max dates from DB: %s", exc)
        return 0

    for league_key, (sport_path, league_path, sport, league) in _BACKFILL_LEAGUES.items():
        max_dt = max_dates.get(sport)
        if max_dt is None:
            start_dt = yesterday - timedelta(days=7)  # no data at all, go back a week
        else:
            start_dt = max_dt + timedelta(days=1)

        if start_dt > yesterday:
            continue  # DB is current for this sport


        # Collect dates to fetch
        dates_to_fetch = []
        cur = start_dt
        while cur <= yesterday:
            dates_to_fetch.append(cur)
            cur += timedelta(days=1)

        if not dates_to_fetch:
            continue

        log.info(
            "auto_backfill: %s missing %d days (%s → %s), fetching from ESPN...",
            league_key, len(dates_to_fetch),
            dates_to_fetch[0].isoformat(), dates_to_fetch[-1].isoformat(),
        )

        # Load existing backfill file to avoid duplicates
        out_path = os.path.join(data_dir, f"backfill_{league_key}.json")
        existing_ids: set[str] = set()
        existing_games: list[dict] = []
        if os.path.exists(out_path):
            try:
                with open(out_path) as f:
                    existing_games = json.load(f)
                existing_ids = {str(g.get("event_id", "")) for g in existing_games}
            except Exception:
                existing_games = []

        new_games: list[dict] = []
        for dt in dates_to_fetch:
            events = _espn_fetch_scoreboard(sport_path, league_path, dt.strftime("%Y%m%d"))
            for ev in events:
                game = _espn_parse_event(ev, sport, league)
                if game and game["event_id"] not in existing_ids:
                    new_games.append(game)
                    existing_ids.add(game["event_id"])
            time.sleep(0.2)  # polite rate limiting

        if new_games:
            all_games = existing_games + new_games
            tmp = out_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(all_games, f)
            os.replace(tmp, out_path)
            log.info("auto_backfill: wrote %d new games to %s", len(new_games), out_path)
            total_new += len(new_games)

    return total_new


def run_updater_loop(
    db_path:       str = DEFAULT_DB,
    data_dir:      str = DEFAULT_DATA_DIR,
    live_dir:      str = DEFAULT_LIVE_DIR,
    hist_interval: int = 300,   # seconds between historical re-scans
    live_interval: int = 35,    # seconds between live-sync attempts
) -> None:
    """Infinite loop: historical incremental update + live-games sync + periodic VACUUM.

    Designed to run as a daemon thread.  Exits only when the process exits.
    """
    log.info(
        "DB updater started (hist_interval=%ds, live_interval=%ds, vacuum_interval=%ds)",
        hist_interval, live_interval, VACUUM_INTERVAL,
    )

    # Bootstrap: ensure live_games table exists (safe on already-built DBs)
    try:
        con = _connect_with_retry(db_path)
        try:
            con.execute(build_db.DDL)   # all CREATE TABLE IF NOT EXISTS — idempotent
        finally:
            con.close()
        log.info("Schema bootstrap complete.")
    except Exception:
        log.exception("Could not bootstrap schema — updater may fail")

    live_state_path = os.path.join(live_dir, "live_state.json")
    last_live_mtime:  float = 0.0
    last_hist_run:    float = 0.0   # 0 → run immediately on first tick
    last_live_run:    float = 0.0
    last_vacuum_run:  float = 0.0
    last_backfill_run: float = 0.0  # 0 → run immediately on first tick

    BACKFILL_INTERVAL = 6 * 3600   # re-check for gaps every 6 hours

    while True:
        now = time.monotonic()

        # ── Auto-backfill gap detection (on startup + every 6 h) ─────────────
        if now - last_backfill_run >= BACKFILL_INTERVAL:
            try:
                new_games = auto_backfill_gaps(db_path, data_dir)
                if new_games:
                    log.info("auto_backfill wrote %d new games — triggering immediate ingest.", new_games)
                    # Force the incremental updater to run right away
                    last_hist_run = 0.0
            except Exception:
                log.exception("auto_backfill_gaps failed")
            last_backfill_run = now

        # ── Historical incremental update ────────────────────────────────────
        if now - last_hist_run >= hist_interval:
            g, p, s = incremental_historical_update(db_path, data_dir)
            if g:
                log.info(
                    "Inserted/updated %d games, %d player-game rows, %d stat rows.",
                    g, p, s,
                )
                # Evict stale settlement cache so API returns fresh results
                _flush_settlement_cache()
            last_hist_run = now

        # ── Live-games sync (only when live_state.json actually changed) ─────
        if now - last_live_run >= live_interval:
            try:
                mtime = os.path.getmtime(live_state_path)
            except OSError:
                mtime = 0.0

            if mtime != last_live_mtime:
                count = sync_live_games(db_path, live_dir)
                last_live_mtime = mtime
                if count:
                    log.debug("Live sync wrote %d rows.", count)

            last_live_run = now

        # ── Periodic VACUUM — reclaim dead space from live_games churn ───────
        if now - last_vacuum_run >= VACUUM_INTERVAL:
            vacuum_db(db_path)
            last_vacuum_run = now

        time.sleep(5)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="Run the DB updater loop (incremental historical + live sync).",
    )
    ap.add_argument("--db",            default=DEFAULT_DB,       help="DuckDB path")
    ap.add_argument("--data-dir",      default=DEFAULT_DATA_DIR, help="historical_data/ path")
    ap.add_argument("--live-dir",      default=DEFAULT_LIVE_DIR, help="live/ directory path")
    ap.add_argument("--hist-interval", type=int, default=300,    help="Historical re-scan interval (s)")
    ap.add_argument("--live-interval", type=int, default=35,     help="Live-sync check interval (s)")
    args = ap.parse_args()

    run_updater_loop(
        db_path       = args.db,
        data_dir      = args.data_dir,
        live_dir      = args.live_dir,
        hist_interval = args.hist_interval,
        live_interval = args.live_interval,
    )


if __name__ == "__main__":
    main()
