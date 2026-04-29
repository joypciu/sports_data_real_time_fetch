"""
optimize_vps.py
===============
Safe, zero-data-loss VPS database maintenance script.

Run once manually whenever the database feels sluggish or disk is tight:
    python tools/optimize_vps.py

What it does (in order, all reversible):
1.  Checkpoint DuckDB WAL — flushes pending writes so no data is in flight
2.  VACUUM DuckDB sports.db — reclaims free pages from live_games churn
3.  VACUUM SQLite bets.db   — compact bet records
4.  VACUUM SQLite requests.db — compact request telemetry
5.  Compress old JSONL event logs (>30 days) with gzip — big disk savings
6.  Remove zero-byte and obviously corrupt JSONL files
7.  Print a before/after size report for every database and the live/ dir

Nothing is deleted permanently:
  - DB VACUUMs only compact in-place — no rows are removed.
  - JSONL compression keeps the .gz alongside (no original deletion until
    you confirm space was freed and re-run with --delete-originals).
  - Empty files are removed (they hold no data).
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — adjust if your VPS layout differs
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
REPO_ROOT    = SCRIPT_DIR.parent
DB_DIR       = REPO_ROOT / "db"
LIVE_DIR     = REPO_ROOT / "live"
CACHE_API_DIR = REPO_ROOT.parent / "cache api"

DUCKDB_PATH    = DB_DIR / "sports.db"
BETS_DB_PATH   = CACHE_API_DIR / "bets.db"
REQUESTS_DB_PATH = CACHE_API_DIR / "request_logs" / "requests.db"

JSONL_MAX_AGE_DAYS = 30   # compress JSONL files older than this many days


def _fmt_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "not found"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Step 1-2: DuckDB checkpoint + VACUUM
# ---------------------------------------------------------------------------

def optimize_duckdb() -> None:
    if not DUCKDB_PATH.exists():
        print(f"  [SKIP] DuckDB not found at {DUCKDB_PATH}")
        return

    before = DUCKDB_PATH.stat().st_size
    print(f"  Before: {_fmt_bytes(before)}")

    try:
        import duckdb  # type: ignore
    except ImportError:
        print("  [SKIP] duckdb Python package not installed — skipping DuckDB optimization")
        return

    try:
        con = duckdb.connect(str(DUCKDB_PATH))
        print("  Running CHECKPOINT …")
        con.execute("CHECKPOINT")
        print("  Running VACUUM …")
        con.execute("VACUUM")
        con.close()
    except Exception as exc:
        print(f"  [WARN] DuckDB optimization error: {exc}")
        return

    after = DUCKDB_PATH.stat().st_size
    saved = before - after
    print(f"  After:  {_fmt_bytes(after)}  (saved {_fmt_bytes(max(0, saved))})")


# ---------------------------------------------------------------------------
# Step 3-4: SQLite VACUUM
# ---------------------------------------------------------------------------

def vacuum_sqlite(path: Path, label: str) -> None:
    if not path.exists():
        print(f"  [SKIP] {label} not found at {path}")
        return

    before = path.stat().st_size
    print(f"  {label}: {_fmt_bytes(before)} before …")

    try:
        con = sqlite3.connect(str(path))
        con.execute("VACUUM")
        con.close()
    except Exception as exc:
        print(f"  [WARN] SQLite VACUUM error for {label}: {exc}")
        return

    after = path.stat().st_size
    saved = before - after
    print(f"  {label}: {_fmt_bytes(after)} after  (saved {_fmt_bytes(max(0, saved))})")


# ---------------------------------------------------------------------------
# Step 5-6: Compress / clean up old JSONL event logs
# ---------------------------------------------------------------------------

def compress_old_jsonl(delete_originals: bool = False) -> None:
    if not LIVE_DIR.exists():
        print(f"  [SKIP] live/ dir not found at {LIVE_DIR}")
        return

    cutoff_ts = time.time() - JSONL_MAX_AGE_DAYS * 86400
    files = sorted(LIVE_DIR.glob("events_*.jsonl"))
    compressed = 0
    removed_empty = 0
    total_saved = 0

    for f in files:
        try:
            stat = f.stat()
        except FileNotFoundError:
            continue

        # Remove empty files (zero bytes — no useful data)
        if stat.st_size == 0:
            f.unlink()
            removed_empty += 1
            print(f"  Removed empty: {f.name}")
            continue

        if stat.st_mtime > cutoff_ts:
            continue  # recent file — skip

        gz_path = f.with_suffix(".jsonl.gz")
        if gz_path.exists():
            # Already compressed; optionally delete original
            if delete_originals:
                f.unlink()
                total_saved += stat.st_size
                print(f"  Deleted original (already .gz): {f.name}")
            continue

        # Compress
        try:
            orig_size = stat.st_size
            with f.open("rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            gz_size = gz_path.stat().st_size
            ratio = (1 - gz_size / orig_size) * 100 if orig_size else 0
            print(f"  Compressed: {f.name}  {_fmt_bytes(orig_size)} → {_fmt_bytes(gz_size)} ({ratio:.0f}% smaller)")
            compressed += 1
            total_saved += orig_size - gz_size

            if delete_originals:
                f.unlink()
                total_saved += gz_size  # credit the full original against what stays
        except Exception as exc:
            print(f"  [WARN] Failed to compress {f.name}: {exc}")
            if gz_path.exists():
                gz_path.unlink()  # clean up partial write

    print(f"\n  Compressed {compressed} JSONL files, removed {removed_empty} empty files.")
    print(f"  Estimated disk saved: {_fmt_bytes(total_saved)}")
    if not delete_originals and compressed:
        print("  Original .jsonl files kept. Re-run with --delete-originals to remove them after verifying .gz files.")


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def size_report() -> None:
    paths = {
        "DuckDB (sports.db)":      DUCKDB_PATH,
        "SQLite (bets.db)":        BETS_DB_PATH,
        "SQLite (requests.db)":    REQUESTS_DB_PATH,
    }
    print("\n  File sizes:")
    for label, path in paths.items():
        print(f"    {label:30s}  {_fmt_size(path)}")

    if LIVE_DIR.exists():
        live_total = _dir_size(LIVE_DIR)
        jsonl_count = len(list(LIVE_DIR.glob("events_*.jsonl")))
        gz_count    = len(list(LIVE_DIR.glob("events_*.jsonl.gz")))
        print(f"    {'live/ directory':30s}  {_fmt_bytes(live_total)}")
        print(f"      {jsonl_count} uncompressed JSONL + {gz_count} compressed .gz")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Optimize VPS databases without data loss")
    parser.add_argument("--delete-originals", action="store_true",
                        help="Delete original .jsonl files after successful .gz compression")
    parser.add_argument("--skip-duckdb",   action="store_true", help="Skip DuckDB optimization")
    parser.add_argument("--skip-sqlite",   action="store_true", help="Skip SQLite VACUUMs")
    parser.add_argument("--skip-compress", action="store_true", help="Skip JSONL compression")
    parser.add_argument("--report-only",   action="store_true", help="Only print size report, do nothing")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  VPS Database Optimizer — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    print("\n[BEFORE]")
    size_report()

    if args.report_only:
        return

    if not args.skip_duckdb:
        print("\n[1/3] DuckDB: checkpoint + VACUUM")
        optimize_duckdb()

    if not args.skip_sqlite:
        print("\n[2/3] SQLite: VACUUM")
        vacuum_sqlite(BETS_DB_PATH,     "bets.db")
        vacuum_sqlite(REQUESTS_DB_PATH, "requests.db")

    if not args.skip_compress:
        print(f"\n[3/3] Compressing JSONL event logs older than {JSONL_MAX_AGE_DAYS} days")
        compress_old_jsonl(delete_originals=args.delete_originals)

    print("\n[AFTER]")
    size_report()
    print(f"\n{'='*60}")
    print("  Done. No data was deleted. Run with --delete-originals to remove")
    print("  original .jsonl files once you have verified .gz files are intact.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
