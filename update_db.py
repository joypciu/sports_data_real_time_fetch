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

def incremental_historical_update(db_path: str, data_dir: str) -> tuple[int, int, int]:
    """Scan all JSON files in *data_dir* and insert any games not yet in the DB.

    Returns (games_added, player_rows_added, stat_rows_added).
    The function is idempotent — re-running it is always safe.
    """
    if not os.path.isdir(data_dir):
        return 0, 0, 0

    files = sorted(
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.endswith(".json")
    )
    if not files:
        return 0, 0, 0

    total_g = total_p = total_s = 0
    try:
        con = duckdb.connect(db_path)
        for path in files:
            g, p, s = build_db.load_file(con, path)
            if g:
                log.info(
                    "Incremental: +%d games, +%d player-rows, +%d stat-rows from %s",
                    g, p, s, os.path.basename(path),
                )
            total_g += g
            total_p += p
            total_s += s
        con.close()
    except Exception:
        log.exception("Error during incremental historical update")

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
        con = duckdb.connect(db_path)
        con.execute("DELETE FROM live_games")
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
        con.close()
        log.debug("Live sync: wrote %d rows to live_games", len(rows))
    except Exception:
        log.exception("Error syncing live_games table")
        return 0

    return len(rows)


# ---------------------------------------------------------------------------
# 3. Background loop (Thread 3 entry point)
# ---------------------------------------------------------------------------

def run_updater_loop(
    db_path:       str = DEFAULT_DB,
    data_dir:      str = DEFAULT_DATA_DIR,
    live_dir:      str = DEFAULT_LIVE_DIR,
    hist_interval: int = 300,   # seconds between historical re-scans
    live_interval: int = 35,    # seconds between live-sync attempts
) -> None:
    """Infinite loop: historical incremental update + live-games sync.

    Designed to run as a daemon thread.  Exits only when the process exits.
    """
    log.info(
        "DB updater started (hist_interval=%ds, live_interval=%ds)",
        hist_interval, live_interval,
    )

    # Bootstrap: ensure live_games table exists (safe on already-built DBs)
    try:
        con = duckdb.connect(db_path)
        con.execute(build_db.DDL)   # all CREATE TABLE IF NOT EXISTS — idempotent
        con.close()
        log.info("Schema bootstrap complete.")
    except Exception:
        log.exception("Could not bootstrap schema — updater may fail")

    live_state_path = os.path.join(live_dir, "live_state.json")
    last_live_mtime: float = 0.0
    last_hist_run:   float = 0.0   # 0 → run immediately on first tick
    last_live_run:   float = 0.0

    while True:
        now = time.monotonic()

        # ── Historical incremental update ────────────────────────────────────
        if now - last_hist_run >= hist_interval:
            g, p, s = incremental_historical_update(db_path, data_dir)
            if g:
                print(
                    f"[update_db] Inserted {g} new games, "
                    f"{p} player-game rows, {s} stat rows."
                )
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
