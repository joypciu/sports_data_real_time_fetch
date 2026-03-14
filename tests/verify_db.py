"""
verify_db.py
============
Structured health-check for db/sports.db.

Prints PASS / FAIL / WARN / INFO for every check and exits with:
  0  — all checks passed
  1  — one or more checks failed

Usage (from project root or any directory):
    python tests/verify_db.py
    python tests/verify_db.py --db path/to/other.db
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT = os.path.join(_ROOT, "db", "sports.db")

ap = argparse.ArgumentParser(description="Verify db/sports.db health")
ap.add_argument("--db", default=_DEFAULT, help="Path to DuckDB file")
args = ap.parse_args()

# ── ANSI helpers ──────────────────────────────────────────────────────────────
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[36mINFO\033[0m"

passed = failed = warned = 0


def _p(label: str, ok: bool, msg: str = "") -> None:
    global passed, failed
    status = PASS if ok else FAIL
    if ok:
        passed += 1
    else:
        failed += 1
    suffix = f"  → {msg}" if msg else ""
    print(f"  [{status}] {label}{suffix}")


def _w(label: str, msg: str = "") -> None:
    global warned
    warned += 1
    print(f"  [{WARN}] {label}  → {msg}")


def _i(label: str, msg: str = "") -> None:
    print(f"  [{INFO}] {label}  → {msg}")


def section(title: str) -> None:
    print(f"\n{'='*66}")
    print(f"  {title}")
    print(f"{'='*66}")


# ── Open connection ───────────────────────────────────────────────────────────
if not os.path.exists(args.db):
    print(f"\n[{FAIL}] Database not found: {args.db}")
    print("  Run:  python build_db.py --rebuild")
    sys.exit(1)

try:
    con = duckdb.connect(args.db, read_only=True)
except Exception as exc:
    print(f"\n[{FAIL}] Cannot open database: {exc}")
    sys.exit(1)

print(f"\nVerifying  {args.db}\n")

# ─────────────────────────────────────────────────────────────────────────────
# 1. TABLE EXISTENCE
# ─────────────────────────────────────────────────────────────────────────────
section("1. Table Existence")

EXPECTED_TABLES = [
    "teams", "players", "games", "game_teams", "game_players", "player_stats",
]
existing = {
    r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()
}
for tbl in EXPECTED_TABLES:
    _p(f"Table '{tbl}' exists", tbl in existing)

# ─────────────────────────────────────────────────────────────────────────────
# 2. ROW COUNT THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────
section("2. Row Count Thresholds")

thresholds = {
    "games":        500,
    "teams":        100,
    "players":      1_000,
    "game_teams":   1_000,
    "game_players": 10_000,
    "player_stats": 50_000,
}
for tbl, minimum in thresholds.items():
    n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    _p(f"{tbl}: ≥{minimum:,} rows", n >= minimum, f"{n:,}")

# ─────────────────────────────────────────────────────────────────────────────
# 3. SPORT & LEAGUE COVERAGE
# ─────────────────────────────────────────────────────────────────────────────
section("3. Sport & League Coverage")

coverage = [
    ("basketball", "nba",            100),
    ("hockey",     "nhl",             80),
    ("baseball",   "mlb",            200),
    ("soccer",     "esp.1",           10),   # La Liga
    ("soccer",     "eng.1",           10),   # EPL
    ("soccer",     "uefa.champions",   5),   # UCL
    ("cricket",    "8043",             3),   # Sheffield Shield
]
for sport, league, minimum in coverage:
    n = con.execute(
        f"SELECT COUNT(*) FROM games WHERE sport='{sport}' AND league='{league}'"
    ).fetchone()[0]
    _p(f"{sport}/{league}: ≥{minimum} games", n >= minimum, f"{n}")

# Totals by sport (informational)
rows = con.execute("""
    SELECT sport, COUNT(*) n FROM games GROUP BY sport ORDER BY n DESC
""").fetchall()
_i("Games by sport", "  |  ".join(f"{r[0]}:{r[1]}" for r in rows))

# ─────────────────────────────────────────────────────────────────────────────
# 4. SCHEMA VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
section("4. Schema Validation")

# Load all column metadata
cols = {
    (r[0], r[1]): r[2]
    for r in con.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
    """).fetchall()
}


def col_type(tbl: str, col: str) -> str:
    return cols.get((tbl, col), "MISSING")


# game_date must NOT be TIMESTAMPTZ (caused Asia/Dhaka +6h shift + pytz crash)
gd = col_type("games", "game_date")
_p("games.game_date is TIMESTAMP (not TIMESTAMPTZ — no pytz dependency)",
   "tz" not in gd.lower(), f"actual={gd}")

# Critical column presence
key_cols = [
    ("games",        "event_id"),
    ("games",        "sport"),
    ("games",        "league"),
    ("games",        "status"),
    ("games",        "home_score"),
    ("games",        "away_score"),
    ("games",        "home_formation"),
    ("games",        "away_formation"),
    ("games",        "home_win_pct"),
    ("games",        "away_win_pct"),
    ("games",        "draw_odds"),
    ("teams",        "team_id"),
    ("teams",        "team_abbr"),
    ("players",      "player_id"),
    ("players",      "display_name"),
    ("players",      "position"),
    ("game_teams",   "event_id"),
    ("game_teams",   "team_id"),
    ("game_teams",   "home_away"),
    ("game_teams",   "score"),
    ("game_teams",   "is_winner"),
    ("game_teams",   "moneyline"),
    ("game_players", "event_id"),
    ("game_players", "player_id"),
    ("game_players", "starter"),
    ("game_players", "did_not_play"),
    ("game_players", "subbed_in"),
    ("game_players", "subbed_out"),
    ("player_stats", "game_player_id"),
    ("player_stats", "stat_key"),
    ("player_stats", "stat_value"),
]
for tbl, c in key_cols:
    _p(f"{tbl}.{c} column present", (tbl, c) in cols)

# ─────────────────────────────────────────────────────────────────────────────
# 5. PRIMARY KEY UNIQUENESS
# ─────────────────────────────────────────────────────────────────────────────
section("5. Primary Key Uniqueness")

pk_checks = [
    ("games",        "event_id"),
    ("game_players", "id"),
    ("game_teams",   "id"),
    ("player_stats", "id"),
]
for tbl, pk in pk_checks:
    dup = con.execute(
        f"SELECT COUNT(*) FROM (SELECT {pk}, COUNT(*) c FROM {tbl} GROUP BY {pk} HAVING c>1)"
    ).fetchone()[0]
    _p(f"{tbl}.{pk}: no duplicate keys", dup == 0, f"{dup} dupes")

# Composite PK: teams(team_id, sport)
dup_teams = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT team_id, sport, COUNT(*) c FROM teams GROUP BY team_id, sport HAVING c > 1
    )
""").fetchone()[0]
_p("teams.(team_id,sport): no duplicate composite keys", dup_teams == 0, f"{dup_teams} dupes")

# Composite PK: players(player_id, sport)
dup_players = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT player_id, sport, COUNT(*) c FROM players GROUP BY player_id, sport HAVING c > 1
    )
""").fetchone()[0]
_p("players.(player_id,sport): no duplicate composite keys", dup_players == 0,
   f"{dup_players} dupes")

# ─────────────────────────────────────────────────────────────────────────────
# 6. FOREIGN KEY INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────
section("6. Foreign Key Integrity")

simple_fks = [
    ("game_teams",   "event_id",        "games",        "event_id"),
    ("game_players", "event_id",        "games",        "event_id"),
    ("player_stats", "game_player_id",  "game_players", "id"),
]
for child, cc, parent, pc in simple_fks:
    orphans = con.execute(f"""
        SELECT COUNT(*) FROM {child} c
        WHERE NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{pc} = c.{cc})
    """).fetchone()[0]
    _p(f"{child}.{cc} → {parent}: no orphans", orphans == 0, f"{orphans}")

# game_players → players (sport-scoped)
orphan_pl = con.execute("""
    SELECT COUNT(*) FROM game_players gp
    WHERE NOT EXISTS (
        SELECT 1 FROM players p WHERE p.player_id = gp.player_id AND p.sport = gp.sport
    )
""").fetchone()[0]
_p("game_players → players (sport-scoped): no orphans", orphan_pl == 0, f"{orphan_pl}")

# game_teams → teams (sport-scoped)
orphan_tt = con.execute("""
    SELECT COUNT(*) FROM game_teams gt
    WHERE NOT EXISTS (
        SELECT 1 FROM teams t WHERE t.team_id = gt.team_id AND t.sport = gt.sport
    )
""").fetchone()[0]
_p("game_teams → teams (sport-scoped): no orphans", orphan_tt == 0, f"{orphan_tt}")

# ─────────────────────────────────────────────────────────────────────────────
# 7. HOME / AWAY COVERAGE
# ─────────────────────────────────────────────────────────────────────────────
section("7. Home / Away Coverage")

# Every finished game must have exactly 2 game_team rows
bad_sides = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT g.event_id, COUNT(gt.id) n
        FROM games g
        LEFT JOIN game_teams gt ON gt.event_id = g.event_id
        WHERE g.status = 'post'
        GROUP BY g.event_id HAVING n != 2
    )
""").fetchone()[0]
_p("All finished games have exactly 2 game_team rows", bad_sides == 0,
   f"{bad_sides} exceptions")

# No game has duplicate home or away
dup_side = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT event_id, home_away, COUNT(*) c FROM game_teams
        GROUP BY event_id, home_away HAVING c > 1
    )
""").fetchone()[0]
_p("No game has duplicate home or away side", dup_side == 0, f"{dup_side} violations")

# Soccer finished games have exactly 2 sides (stricter check)
bad_soccer = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT g.event_id, COUNT(gt.id) n
        FROM games g
        LEFT JOIN game_teams gt ON gt.event_id = g.event_id
        WHERE g.sport = 'soccer' AND g.status = 'post'
        GROUP BY g.event_id HAVING n != 2
    )
""").fetchone()[0]
_p("All finished soccer games have exactly 2 sides", bad_soccer == 0, f"{bad_soccer}")

# ─────────────────────────────────────────────────────────────────────────────
# 8. SCORE & WINNER CONSISTENCY
# ─────────────────────────────────────────────────────────────────────────────
section("8. Score & Winner Consistency")

# Non-cricket finished games should have numeric scores
no_score = con.execute("""
    SELECT COUNT(*) FROM games
    WHERE status = 'post' AND sport != 'cricket'
      AND (home_score IS NULL OR away_score IS NULL)
""").fetchone()[0]
_p("Non-cricket finished games have numeric scores", no_score == 0,
   f"{no_score} with NULL scores")

# Cricket: scores are innings-strings → NULL in games table (expected)
cricket_total = con.execute("SELECT COUNT(*) FROM games WHERE sport='cricket'").fetchone()[0]
cricket_null = con.execute(
    "SELECT COUNT(*) FROM games WHERE sport='cricket' AND home_score IS NULL"
).fetchone()[0]
if cricket_total > 0:
    _i("Cricket games have NULL scores (expected — innings-string format)",
       f"{cricket_null}/{cricket_total}")

# is_winner: no game should have BOTH sides marked TRUE (string-bool bug regression)
double_winner = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT event_id FROM game_teams
        WHERE is_winner = TRUE
        GROUP BY event_id HAVING COUNT(*) > 1
    )
""").fetchone()[0]
_p("No game has two winners (is_winner bool-string regression)", double_winner == 0,
   f"{double_winner} games with dual winner")

# Home score matches game_teams.score (home)
score_mm_h = con.execute("""
    SELECT COUNT(*) FROM games g
    JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'home'
    WHERE g.home_score IS NOT NULL AND gt.score IS NOT NULL
      AND g.home_score != gt.score
""").fetchone()[0]
_p("games.home_score consistent with game_teams.score", score_mm_h == 0,
   f"{score_mm_h} mismatches")

# Away score matches game_teams.score (away)
score_mm_a = con.execute("""
    SELECT COUNT(*) FROM games g
    JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'away'
    WHERE g.away_score IS NOT NULL AND gt.score IS NOT NULL
      AND g.away_score != gt.score
""").fetchone()[0]
_p("games.away_score consistent with game_teams.score", score_mm_a == 0,
   f"{score_mm_a} mismatches")

# ─────────────────────────────────────────────────────────────────────────────
# 9. ODDS DATA SANITY
# ─────────────────────────────────────────────────────────────────────────────
section("9. Odds Data Sanity")

bad_ml = con.execute("""
    SELECT COUNT(*) FROM game_teams
    WHERE moneyline IS NOT NULL
      AND (moneyline < -999999 OR moneyline > 999999)
""").fetchone()[0]
_p("Moneylines in sane range (±999999)", bad_ml == 0, f"{bad_ml} outliers")

# Draw odds only on soccer
bad_draw = con.execute(
    "SELECT COUNT(*) FROM games WHERE draw_odds IS NOT NULL AND sport != 'soccer'"
).fetchone()[0]
_p("draw_odds only on soccer games", bad_draw == 0, f"{bad_draw} non-soccer with draw_odds")

# Win probabilities sum to ~1.0 where both are populated
bad_prob = con.execute("""
    SELECT COUNT(*) FROM games
    WHERE home_win_pct IS NOT NULL AND away_win_pct IS NOT NULL
      AND ABS((home_win_pct + away_win_pct) - 1.0) > 0.05
""").fetchone()[0]
_p("Win probabilities sum to ~1.0 (±5%)", bad_prob == 0,
   f"{bad_prob} games where h+a diverges >5%")

n_with_total = con.execute(
    "SELECT COUNT(*) FROM games WHERE game_total IS NOT NULL AND status='post'"
).fetchone()[0]
_i("Finished games with game_total (over/under line)", f"{n_with_total:,}")

# ─────────────────────────────────────────────────────────────────────────────
# 10. STAT DATA COMPLETENESS
# ─────────────────────────────────────────────────────────────────────────────
section("10. Stat Data Completeness")

null_key = con.execute(
    "SELECT COUNT(*) FROM player_stats WHERE stat_key IS NULL OR stat_key = ''"
).fetchone()[0]
_p("player_stats: no NULL/empty stat_key", null_key == 0, f"{null_key} rows")

null_val = con.execute(
    "SELECT COUNT(*) FROM player_stats WHERE stat_value IS NULL"
).fetchone()[0]
_p("player_stats: no NULL stat_value", null_val == 0, f"{null_val} rows")

# Sport-specific key presence
sport_key_checks: list[tuple[str, str | None, str]] = [
    ("basketball", "nba", "PTS"),
    ("basketball", "nba", "REB"),
    ("basketball", "nba", "AST"),
    ("basketball", "nba", "MIN"),
    ("hockey",     "nhl", "TOI"),
    ("hockey",     "nhl", "G"),
    ("hockey",     "nhl", "A"),
    ("baseball",   "mlb", "H"),
    ("baseball",   "mlb", "ERA"),
    ("baseball",   "mlb", "RBI"),
    ("soccer",     None,  "G"),
    ("soccer",     None,  "SV"),
    ("soccer",     None,  "YC"),
]
for sport, league, key in sport_key_checks:
    if league:
        n = con.execute(f"""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            JOIN games g ON g.event_id = gp.event_id
            WHERE gp.sport='{sport}' AND g.league='{league}' AND ps.stat_key='{key}'
        """).fetchone()[0]
        tag = f"{sport}/{league}"
    else:
        n = con.execute(f"""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport='{sport}' AND ps.stat_key='{key}'
        """).fetchone()[0]
        tag = sport
    _p(f"{tag} stat '{key}' present", n > 0, f"{n:,} rows")

# DNP players should have no meaningful (>0) numeric stats
dnp_with_stats = con.execute("""
    SELECT COUNT(*) FROM game_players gp
    WHERE gp.did_not_play = TRUE
      AND EXISTS (
          SELECT 1 FROM player_stats ps
          WHERE ps.game_player_id = gp.id
            AND TRY_CAST(ps.stat_value AS DOUBLE) > 0
      )
""").fetchone()[0]
_p("DNP players have no meaningful (>0) stats", dnp_with_stats == 0,
   f"{dnp_with_stats} violations")

# NHL TOI must be MM:SS format (not a raw float)
bad_toi = con.execute("""
    SELECT COUNT(*) FROM player_stats ps
    JOIN game_players gp ON gp.id = ps.game_player_id
    WHERE gp.sport = 'hockey' AND ps.stat_key = 'TOI'
      AND NOT REGEXP_MATCHES(ps.stat_value, '^\d+:\d{2}$')
""").fetchone()[0]
_p("NHL TOI stats in MM:SS format", bad_toi == 0, f"{bad_toi} non-conforming values")

# Slash-format stats are present (validates composite stat storage)
slash_n = con.execute("""
    SELECT COUNT(*) FROM player_stats
    WHERE stat_key IN ('FG','3PT','FT','H-AB','PC-ST') AND stat_value LIKE '%-%'
""").fetchone()[0]
_p("Slash-format stats present (FG, 3PT, etc.)", slash_n > 0, f"{slash_n:,} rows")

# Soccer subbed-in ≈ subbed-out (within 20%)
sub_in  = con.execute("SELECT COUNT(*) FROM game_players WHERE subbed_in  = TRUE").fetchone()[0]
sub_out = con.execute("SELECT COUNT(*) FROM game_players WHERE subbed_out = TRUE").fetchone()[0]
diff = abs(sub_in - sub_out)
_p("Soccer subbed_in ≈ subbed_out (within 20%)",
   diff <= max(sub_in, sub_out) * 0.2 if max(sub_in, sub_out) > 0 else True,
   f"in={sub_in:,}  out={sub_out:,}  diff={diff}")

# ─────────────────────────────────────────────────────────────────────────────
# 11. DATE & TIMESTAMP INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────
section("11. Date & Timestamp Integrity")

# No epoch dates (1970-01-01 = timestamp parsing failure)
epoch_n = con.execute(
    "SELECT COUNT(*) FROM games WHERE CAST(game_date AS DATE) = '1970-01-01'"
).fetchone()[0]
_p("No epoch-default (1970-01-01) game dates", epoch_n == 0, f"{epoch_n}")

# No dates implausibly far in the future
future_n = con.execute(
    "SELECT COUNT(*) FROM games WHERE game_date > NOW() + INTERVAL '365' DAY"
).fetchone()[0]
_p("No game dates > 1 year in future", future_n == 0, f"{future_n}")

# La Liga kickoff hours must be 12-23 UTC (typical evening kickoffs, rules out midnight
# artifacts from date-only parsing and +6h Dhaka shift)
laliga_bad_hr = con.execute("""
    SELECT COUNT(*) FROM games
    WHERE league = 'esp.1' AND status = 'post'
      AND (EXTRACT(HOUR FROM game_date) < 12 OR EXTRACT(HOUR FROM game_date) > 23)
""").fetchone()[0]
_p("La Liga kickoff hours are 12-23 UTC (no tz corruption)", laliga_bad_hr == 0,
   f"{laliga_bad_hr} out of range")

# Date range query executes without crash (old TIMESTAMPTZ caused pytz crash here)
try:
    n = con.execute(
        "SELECT COUNT(*) FROM games WHERE game_date >= '2024-01-01' AND game_date < '2027-01-01'"
    ).fetchone()[0]
    _p("Date range query on TIMESTAMP column succeeds (no pytz crash)", n > 0,
       f"{n:,} games in 2024-2026")
except Exception as exc:
    _p("Date range query on TIMESTAMP column succeeds", False, str(exc))

# Date grouping (GROUP BY DATE)
try:
    n2 = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT CAST(game_date AS DATE) dt, COUNT(*) n
            FROM games WHERE status = 'post'
            GROUP BY dt HAVING n >= 1
        )
    """).fetchone()[0]
    _p("GROUP BY DATE on game_date works", n2 > 0, f"{n2} distinct game dates")
except Exception as exc:
    _p("GROUP BY DATE on game_date works", False, str(exc))

# ─────────────────────────────────────────────────────────────────────────────
# 12. REGRESSION ANCHORS (KNOWN DATA POINTS)
# ─────────────────────────────────────────────────────────────────────────────
section("12. Regression Anchors (Known Data Points)")

# ── Barcelona (original user bug: missing from DB + query crash) ──────────────
barca = con.execute(
    "SELECT team_id, team_name, team_abbr FROM teams WHERE team_abbr='BAR' AND sport='soccer'"
).fetchone()
_p("Barcelona (BAR) in teams table", barca is not None,
   f"id={barca[0]}" if barca else "NOT FOUND")

if barca:
    barca_id = barca[0]

    laliga_n = con.execute(f"""
        SELECT COUNT(*) FROM game_teams gt
        JOIN games g ON g.event_id = gt.event_id
        WHERE gt.team_id='{barca_id}' AND g.league='esp.1'
    """).fetchone()[0]
    _p("Barcelona has ≥3 La Liga games (was 0 before TIMESTAMPTZ fix)", laliga_n >= 3,
       f"{laliga_n}")

    ucl_n = con.execute(f"""
        SELECT COUNT(*) FROM game_teams gt
        JOIN games g ON g.event_id = gt.event_id
        WHERE gt.team_id='{barca_id}' AND g.league='uefa.champions'
    """).fetchone()[0]
    _p("Barcelona has ≥1 UCL game", ucl_n >= 1, f"{ucl_n}")

    # Score anchor: event 748391 — Barca 3-0 Levante
    ev1 = con.execute(
        "SELECT home_score, away_score FROM games WHERE event_id='748391'"
    ).fetchone()
    if ev1:
        _p("Event 748391 (Barca 3-0 Levante): correct score",
           ev1[0] == 3 and ev1[1] == 0, f"home={ev1[0]}  away={ev1[1]}")
    else:
        _w("Event 748391 (Barca vs Levante) not found in DB", "")

    # Timezone anchor: 748391 was at 15:00 UTC; TIMESTAMPTZ bug stored as 21:00
    h1 = con.execute(
        "SELECT EXTRACT(HOUR FROM game_date) FROM games WHERE event_id='748391'"
    ).fetchone()
    if h1:
        _p("Event 748391 game_date hour=15 UTC (not 21 — Dhaka +6h regression fixed)",
           int(h1[0]) == 15, f"hour={int(h1[0])}")

    # UCL Barca vs Newcastle: 20:00 UTC
    h2 = con.execute(
        "SELECT EXTRACT(HOUR FROM game_date) FROM games WHERE event_id='401862577'"
    ).fetchone()
    if h2:
        _p("Event 401862577 (Barca UCL vs Newcastle) hour=20 UTC", int(h2[0]) == 20,
           f"hour={int(h2[0])}")

    # Key players present
    for pname in ["Raphinha", "Pedri", "Lamine Yamal"]:
        found = con.execute(f"""
            SELECT COUNT(*) FROM players
            WHERE sport='soccer' AND display_name ILIKE '%{pname}%'
        """).fetchone()[0]
        _p(f"Barcelona player '{pname}' in players table", found > 0, f"{found} match(es)")

# ── Cricket regression (was 0 games before pandas import-inside-function fix) ──
cricket_n = con.execute("SELECT COUNT(*) FROM games WHERE sport='cricket'").fetchone()[0]
_p("Cricket: 5 Sheffield Shield games present (was 0 before pandas import fix)",
   cricket_n == 5, f"{cricket_n}")

# ── is_winner regression: bool("false") == True in Python ─────────────────────
dbl_win = con.execute("""
    SELECT COUNT(*) FROM (
        SELECT event_id FROM game_teams WHERE is_winner = TRUE
        GROUP BY event_id HAVING COUNT(*) > 1
    )
""").fetchone()[0]
_p("No game has two winners (is_winner string-bool fix: str.lower()=='true')",
   dbl_win == 0, f"{dbl_win}")

# ─────────────────────────────────────────────────────────────────────────────
# 13. QUERY PERFORMANCE SMOKE-TEST
# ─────────────────────────────────────────────────────────────────────────────
section("13. Query Performance Smoke-Test")

import time

perf_queries = [
    ("5-table NBA box-score join", """
        SELECT p.display_name, CAST(pts.stat_value AS INTEGER) pts
        FROM games g
        JOIN game_players gp  ON gp.event_id = g.event_id
        JOIN players p        ON p.player_id = gp.player_id AND p.sport = gp.sport
        JOIN player_stats pts ON pts.game_player_id = gp.id AND pts.stat_key = 'PTS'
        JOIN player_stats reb ON reb.game_player_id = gp.id AND reb.stat_key = 'REB'
        WHERE g.league = 'nba' AND g.status = 'post' AND gp.did_not_play = FALSE
        ORDER BY pts DESC LIMIT 10
    """),
    ("CTE + win streak window", """
        WITH ordered AS (
            SELECT gt.team_id, g.game_date, gt.is_winner,
                   ROW_NUMBER() OVER (PARTITION BY gt.team_id ORDER BY g.game_date DESC) rn
            FROM game_teams gt JOIN games g ON g.event_id = gt.event_id
            WHERE g.league = 'nba' AND g.status = 'post'
        )
        SELECT team_id, COUNT(*) streak FROM ordered WHERE rn <= 5 AND is_winner = TRUE
        GROUP BY team_id ORDER BY streak DESC LIMIT 5
    """),
    ("Soccer goalkeeper clean sheets", """
        WITH sv AS (SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key='SV'),
             ga AS (SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key='GA')
        SELECT p.display_name, COUNT(*) cs
        FROM sv s JOIN ga ON ga.game_player_id = s.game_player_id
        JOIN game_players gp ON gp.id = s.game_player_id
        JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
        JOIN games g ON g.event_id = gp.event_id
        WHERE g.status='post' AND s.v > 0 AND ga.v = 0
        GROUP BY p.display_name ORDER BY cs DESC LIMIT 5
    """),
    ("Per-league batch aggregate", """
        SELECT g.league, COUNT(DISTINCT g.event_id) gms,
               ROUND(AVG(g.home_score + g.away_score), 2) avg_total
        FROM games g
        WHERE g.status='post' AND g.home_score IS NOT NULL
        GROUP BY g.league ORDER BY gms DESC
    """),
]

THRESHOLD_SEC = 5.0
for label, sql in perf_queries:
    t0 = time.perf_counter()
    try:
        rows = con.execute(sql).fetchall()
        elapsed = time.perf_counter() - t0
        _p(f"'{label}' completes < {THRESHOLD_SEC}s",
           elapsed < THRESHOLD_SEC, f"{elapsed:.3f}s  ({len(rows)} rows)")
    except Exception as exc:
        _p(f"'{label}' executes without error", False, str(exc))

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
section("SUMMARY")

total = passed + failed + warned
print(f"  Passed  : {passed}")
print(f"  Failed  : {failed}")
print(f"  Warned  : {warned}")
print(f"  Total   : {total}")

con.close()

if failed == 0:
    print(f"\n  \033[32mAll checks passed.\033[0m\n")
    sys.exit(0)
else:
    print(f"\n  \033[31m{failed} check(s) FAILED — see above for details.\033[0m\n")
    sys.exit(1)
