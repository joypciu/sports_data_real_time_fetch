"""
main.py
=======
Unified entry point that runs both components side-by-side:

  1. Stats API     — FastAPI on port 8001 (historical DuckDB + live state reads)
  2. Realtime Monitor — ESPN polling loop (writes live_state.json every N seconds)

The monitor continuously updates live/live_state.json which the Stats API reads
on every /stats/live request, giving callers a seamless historical + realtime view.

Architecture
------------
  main.py (this file)
     ├── Thread 1: uvicorn  → stats_api:app  (port 8001, non-blocking)
     └── Thread 2: realtime_monitor.run()    (ESPN poll loop)

Both threads share the same filesystem:
  db/sports.db        ← read by stats_api (read-only DuckDB connections)
  live/live_state.json ← written by monitor, read by stats_api /stats/live
  historical_data/    ← read by build_db.py; monitor appends finished games here

Usage
-----
    python main.py                        # default: all leagues, 30s poll, port 8001
    python main.py --interval 60          # slower polling
    python main.py --leagues nba nhl      # only specific leagues
    python main.py --no-players           # skip player box score pulls
    python main.py --port 8002            # different API port
    python main.py --api-only             # skip monitor (useful for debugging)
    python main.py --monitor-only         # skip API   (useful for debugging)

Environment variables (all optional)
-------------------------------------
    STATS_API_TOKEN=secret   bearer token gate on the API
    STATS_API_PORT=8001      overrides --port
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

# ── resolve imports (project root) ───────────────────────────────────────────
_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# API thread
# ---------------------------------------------------------------------------

def _run_api(port: int) -> None:
    """Start uvicorn in the current thread (blocking)."""
    import uvicorn
    import stats_api  # noqa: F401 — registers routes on import

    uvicorn.run(
        "stats_api:app",
        host="0.0.0.0",
        port=port,
        workers=1,           # single worker so we share memory cleanly
        log_level="warning", # suppress per-request noise; errors still shown
        access_log=False,
    )


# ---------------------------------------------------------------------------
# Monitor thread
# ---------------------------------------------------------------------------

def _run_updater(db_path: str, data_dir: str, live_dir: str) -> None:
    """Run the DB updater loop in the current thread (blocking)."""
    import update_db
    update_db.run_updater_loop(db_path=db_path, data_dir=data_dir, live_dir=live_dir)


def _run_monitor(args: argparse.Namespace) -> None:
    """Run the ESPN polling loop in the current thread (blocking)."""
    import realtime_monitor as rm

    # Build a namespace that rm.run() understands
    monitor_args = argparse.Namespace(
        leagues    = args.leagues,          # None → all leagues
        interval   = args.interval,
        output_dir = args.output_dir,
        data_dir   = args.data_dir,
        no_players = args.no_players,
    )
    rm.run(monitor_args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Stats API + Realtime Monitor together.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Monitor options ──────────────────────────────────────────────────────
    from realtime_monitor import LEAGUES  # local import to avoid circular issues

    parser.add_argument(
        "--leagues", nargs="+", metavar="LEAGUE",
        choices=list(LEAGUES.keys()),
        default=None,
        help="Leagues to monitor (default: all)",
    )
    parser.add_argument(
        "--interval", type=int, default=30, metavar="SECONDS",
        help="Monitor poll interval in seconds",
    )
    parser.add_argument(
        "--output-dir", default="live", metavar="DIR",
        help="Directory for live_state.json and event logs",
    )
    parser.add_argument(
        "--data-dir", default="historical_data", metavar="DIR",
        help="Directory for archiving finished games",
    )
    parser.add_argument(
        "--no-players", action="store_true",
        help="Skip fetching player box scores (faster polling)",
    )

    # ── API options ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--port", type=int,
        default=int(os.getenv("STATS_API_PORT", "8001")),
        metavar="PORT",
        help="Stats API port",
    )

    # ── Mode flags ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--api-only", action="store_true",
        help="Run only the Stats API (no ESPN polling)",
    )
    parser.add_argument(
        "--monitor-only", action="store_true",
        help="Run only the realtime monitor (no API server)",
    )

    args = parser.parse_args()

    # Sanity check
    if args.api_only and args.monitor_only:
        parser.error("--api-only and --monitor-only are mutually exclusive.")

    # ── DB check ─────────────────────────────────────────────────────────────
    db_path = _ROOT / "db" / "sports.db"
    if not db_path.exists():
        print(
            f"[main] WARNING: {db_path} not found.\n"
            "       Run  python build_db.py  first to populate the database.\n"
            "       The API will start but /stats/player and /stats/team will return empty results.",
            file=sys.stderr,
        )

    # ── Banner ───────────────────────────────────────────────────────────────
    print("=" * 50)
    print(" Sports Data Pipeline — Unified Entry Point")
    print("=" * 50)
    if not args.monitor_only:
        print(f"  Stats API      : http://0.0.0.0:{args.port}")
        print(f"  API docs       : http://localhost:{args.port}/docs")
    if not args.api_only:
        leagues_str = ", ".join(args.leagues) if args.leagues else "all"
        print(f"  Monitor leagues: {leagues_str}")
        print(f"  Poll interval  : {args.interval}s")
        print(f"  Live output    : {os.path.abspath(args.output_dir)}/")
        print(f"  Archive dir    : {os.path.abspath(args.data_dir)}/")
    print()

    threads: list[threading.Thread] = []
    stop_event = threading.Event()

    # ── Launch API thread ─────────────────────────────────────────────────────
    if not args.monitor_only:
        api_thread = threading.Thread(
            target=_run_api,
            args=(args.port,),
            name="stats-api",
            daemon=True,   # dies automatically if main thread exits
        )
        api_thread.start()
        threads.append(api_thread)
        print(f"[main] Stats API started on port {args.port}")
        # Give uvicorn a moment to bind the port before starting the monitor
        time.sleep(1.5)

    # ── Launch monitor thread ─────────────────────────────────────────────────
    if not args.api_only:
        monitor_thread = threading.Thread(
            target=_run_monitor,
            args=(args,),
            name="realtime-monitor",
            daemon=True,
        )
        monitor_thread.start()
        threads.append(monitor_thread)
        print(f"[main] Realtime monitor started (interval={args.interval}s)")

    # ── Launch DB updater thread ──────────────────────────────────────────────
    updater_thread = threading.Thread(
        target=_run_updater,
        args=(str(db_path), args.data_dir, args.output_dir),
        name="db-updater",
        daemon=True,
    )
    updater_thread.start()
    threads.append(updater_thread)
    print("[main] DB updater started (historical every 5m, live sync every 35s)")

    # ── Graceful shutdown on SIGINT / SIGTERM ─────────────────────────────────
    def _handle_signal(sig, frame):  # noqa: ANN001
        print(f"\n[main] Received signal {sig} — shutting down …")
        stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Keep main thread alive; restart crashed threads ───────────────────────
    print("[main] Running. Press Ctrl+C to stop.\n")
    while not stop_event.is_set():
        for t in threads:
            if not t.is_alive():
                print(f"[main] WARNING: thread '{t.name}' exited unexpectedly — restarting …",
                      file=sys.stderr)
                if t.name == "stats-api" and not args.monitor_only:
                    new_t = threading.Thread(
                        target=_run_api, args=(args.port,),
                        name="stats-api", daemon=True,
                    )
                elif t.name == "realtime-monitor" and not args.api_only:
                    new_t = threading.Thread(
                        target=_run_monitor, args=(args,),
                        name="realtime-monitor", daemon=True,
                    )
                elif t.name == "db-updater":
                    new_t = threading.Thread(
                        target=_run_updater,
                        args=(str(db_path), args.data_dir, args.output_dir),
                        name="db-updater", daemon=True,
                    )
                else:
                    continue
                new_t.start()
                threads[threads.index(t)] = new_t
        time.sleep(5)


if __name__ == "__main__":
    main()
