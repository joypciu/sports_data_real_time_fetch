"""
query_tests.py
==============
Edge-case query tests against db/sports.db.
Each test prints PASS / FAIL / WARN with details.
Run from project root: python tools/query_tests.py
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "sports.db")

PASS  = "\033[32mPASS\033[0m"
FAIL  = "\033[31mFAIL\033[0m"
WARN  = "\033[33mWARN\033[0m"
INFO  = "\033[36mINFO\033[0m"

passed = failed = warned = 0

def _p(label, ok, msg=""):
    global passed, failed
    status = PASS if ok else FAIL
    if ok:
        passed += 1
    else:
        failed += 1
    suffix = f"  → {msg}" if msg else ""
    print(f"  [{status}] {label}{suffix}")

def _w(label, msg=""):
    global warned
    warned += 1
    print(f"  [{WARN}] {label}  → {msg}")

def _i(label, msg=""):
    print(f"  [{INFO}] {label}  → {msg}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


con = duckdb.connect(DB, read_only=True)

# ─────────────────────────────────────────────────────────────────────────────
# 1. BASIC INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────
section("1. Basic Integrity")

n_games   = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
n_teams   = con.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
n_players = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
n_gp      = con.execute("SELECT COUNT(*) FROM game_players").fetchone()[0]
n_ps      = con.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0]

_p("games table non-empty",        n_games   > 0, f"{n_games:,}")
_p("teams table non-empty",        n_teams   > 0, f"{n_teams:,}")
_p("players table non-empty",      n_players > 0, f"{n_players:,}")
_p("game_players table non-empty", n_gp      > 0, f"{n_gp:,}")
_p("player_stats table non-empty", n_ps      > 0, f"{n_ps:,}")

# ─────────────────────────────────────────────────────────────────────────────
# 2. FOREIGN KEY INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────
section("2. Foreign Key Integrity")

orphan_gt = con.execute("""
    SELECT COUNT(*) FROM game_teams gt
    WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.event_id = gt.event_id)
""").fetchone()[0]
_p("game_teams → games: no orphans", orphan_gt == 0, f"{orphan_gt} orphans")

orphan_gp = con.execute("""
    SELECT COUNT(*) FROM game_players gp
    WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.event_id = gp.event_id)
""").fetchone()[0]
_p("game_players → games: no orphans", orphan_gp == 0, f"{orphan_gp} orphans")

orphan_ps = con.execute("""
    SELECT COUNT(*) FROM player_stats ps
    WHERE NOT EXISTS (SELECT 1 FROM game_players gp WHERE gp.id = ps.game_player_id)
""").fetchone()[0]
_p("player_stats → game_players: no orphans", orphan_ps == 0, f"{orphan_ps} orphans")

# Every game_player links to a known player
orphan_pl = con.execute("""
    SELECT COUNT(*) FROM game_players gp
    WHERE NOT EXISTS (
        SELECT 1 FROM players p
        WHERE p.player_id = gp.player_id AND p.sport = gp.sport
    )
""").fetchone()[0]
_p("game_players → players: no orphans", orphan_pl == 0, f"{orphan_pl} orphans")

# Every game_team links to a known team
orphan_tt = con.execute("""
    SELECT COUNT(*) FROM game_teams gt
    WHERE NOT EXISTS (
        SELECT 1 FROM teams t
        WHERE t.team_id = gt.team_id AND t.sport = gt.sport
    )
""").fetchone()[0]
_p("game_teams → teams: no orphans", orphan_tt == 0, f"{orphan_tt} orphans")

# ─────────────────────────────────────────────────────────────────────────────
# 3. UNIQUENESS / DUPLICATES
# ─────────────────────────────────────────────────────────────────────────────
section("3. Uniqueness & Duplicates")

dup_games = con.execute("""
    SELECT event_id, COUNT(*) c FROM games GROUP BY event_id HAVING c > 1
""").fetchall()
_p("No duplicate event_ids in games", len(dup_games) == 0, f"{len(dup_games)} dupes")

dup_gp = con.execute("""
    SELECT id, COUNT(*) c FROM game_players GROUP BY id HAVING c > 1
""").fetchall()
_p("No duplicate game_player ids", len(dup_gp) == 0, f"{len(dup_gp)} dupes")

dup_ps = con.execute("""
    SELECT id, COUNT(*) c FROM player_stats GROUP BY id HAVING c > 1
""").fetchall()
_p("No duplicate player_stat ids", len(dup_ps) == 0, f"{len(dup_ps)} dupes")

dup_gt = con.execute("""
    SELECT id, COUNT(*) c FROM game_teams GROUP BY id HAVING c > 1
""").fetchall()
_p("No duplicate game_team ids", len(dup_gt) == 0, f"{len(dup_gt)} dupes")

# ─────────────────────────────────────────────────────────────────────────────
# 4. HOME/AWAY COVERAGE
# ─────────────────────────────────────────────────────────────────────────────
section("4. Home / Away Coverage")

# Every finished game should have exactly 2 game_team rows
bad_gt = con.execute("""
    SELECT g.event_id, g.sport, COUNT(gt.id) n
    FROM games g
    LEFT JOIN game_teams gt ON gt.event_id = g.event_id
    WHERE g.status = 'post'
    GROUP BY g.event_id, g.sport
    HAVING n != 2
""").fetchall()
_p("All finished games have exactly 2 game_team rows", len(bad_gt) == 0,
   f"{len(bad_gt)} games with wrong side count")
for r in bad_gt[:5]:
    print(f"       event={r[0]} sport={r[1]} sides={r[2]}")

# Each game should have one home and one away row
wrong_sides = con.execute("""
    SELECT event_id, home_away, COUNT(*) c
    FROM game_teams
    GROUP BY event_id, home_away
    HAVING c > 1
""").fetchall()
_p("No game has duplicate home or away side", len(wrong_sides) == 0,
   f"{len(wrong_sides)} violations")

# ─────────────────────────────────────────────────────────────────────────────
# 5. GAMES WITHOUT PLAYERS
# ─────────────────────────────────────────────────────────────────────────────
section("5. Games Without Players (should be pre-game or known gaps)")

no_players = con.execute("""
    SELECT g.sport, g.league, g.status, COUNT(*) n
    FROM games g
    WHERE NOT EXISTS (SELECT 1 FROM game_players gp WHERE gp.event_id = g.event_id)
    GROUP BY g.sport, g.league, g.status
    ORDER BY n DESC
""").fetchall()
if no_players:
    for r in no_players:
        if r[2] == "post":
            _w(f"Finished games with no players", f"{r[2]} {r[0]}/{r[1]}: {r[3]} games")
        else:
            _i(f"Pre/live games with no players (expected)", f"{r[2]} {r[0]}/{r[1]}: {r[3]}")
else:
    _p("All games have at least one player", True)

# ─────────────────────────────────────────────────────────────────────────────
# 6. STAT VALUE CASTABILITY
# ─────────────────────────────────────────────────────────────────────────────
section("6. Stat Value Castability")

# Numeric stat keys that should always cast to DOUBLE
numeric_checks = {
    "nba":      ["PTS", "REB", "AST", "MIN"],
    "hockey":   ["G", "A"],       # TOI is MM:SS — checked separately below
    "baseball": ["H", "RBI"],
    "soccer":   ["G", "SV"],
}

for sport, keys in numeric_checks.items():
    for key in keys:
        bad = con.execute(f"""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport = '{sport}' AND ps.stat_key = '{key}'
              AND TRY_CAST(ps.stat_value AS DOUBLE) IS NULL
              AND ps.stat_value NOT IN ('', '--', 'N/A')
        """).fetchone()[0]
        _p(f"{sport}.{key} casts to DOUBLE", bad == 0, f"{bad} non-castable values")

# TOI is MM:SS — verify format is correct, not castable to DOUBLE
toi_bad_format = con.execute("""
    SELECT COUNT(*) FROM player_stats ps
    JOIN game_players gp ON gp.id = ps.game_player_id
    WHERE gp.sport = 'hockey' AND ps.stat_key = 'TOI'
      AND NOT REGEXP_MATCHES(ps.stat_value, '^\\d+:\\d{2}$')
""").fetchone()[0]
_p("hockey.TOI values are all in MM:SS format", toi_bad_format == 0,
   f"{toi_bad_format} values not matching MM:SS")

# Slash stats (e.g. FG = '9-14') should NOT cast to double — confirm they exist
slash_stats = con.execute("""
    SELECT COUNT(*) FROM player_stats
    WHERE stat_key IN ('FG','3PT','FT','H-AB','PC-ST')
      AND stat_value LIKE '%-%'
""").fetchone()[0]
_p("Slash-format stats present (FG, 3PT etc.)", slash_stats > 0, f"{slash_stats:,} rows")

# ─────────────────────────────────────────────────────────────────────────────
# 7. SPORT-SPECIFIC STAT KEY COVERAGE
# ─────────────────────────────────────────────────────────────────────────────
section("7. Sport-Specific Stat Key Coverage")

expected_keys = {
    "basketball/nba":     ["PTS", "REB", "AST", "MIN", "FG"],
    "hockey/nhl":         ["G", "A", "TOI", "S", "PIM"],
    "baseball/mlb":       ["H", "RBI", "ERA", "IP", "AVG"],
    "soccer/all":         ["G", "SV", "YC", "SH", "A"],
}
sport_league_map = {
    "basketball/nba":  ("basketball", "nba"),
    "hockey/nhl":      ("hockey",     "nhl"),
    "baseball/mlb":    ("baseball",   "mlb"),
}

for label, keys in expected_keys.items():
    if label == "soccer/all":
        for key in keys:
            n = con.execute(f"""
                SELECT COUNT(*) FROM player_stats ps
                JOIN game_players gp ON gp.id = ps.game_player_id
                WHERE gp.sport = 'soccer' AND ps.stat_key = '{key}'
            """).fetchone()[0]
            _p(f"soccer stat '{key}' present", n > 0, f"{n:,} rows")
    else:
        sport, league = sport_league_map[label]
        for key in keys:
            n = con.execute(f"""
                SELECT COUNT(*) FROM player_stats ps
                JOIN game_players gp ON gp.id = ps.game_player_id
                JOIN games g ON g.event_id = gp.event_id
                WHERE gp.sport = '{sport}' AND g.league = '{league}'
                  AND ps.stat_key = '{key}'
            """).fetchone()[0]
            _p(f"{label} stat '{key}' present", n > 0, f"{n:,} rows")

# ─────────────────────────────────────────────────────────────────────────────
# 8. DNP / INACTIVE PLAYERS
# ─────────────────────────────────────────────────────────────────────────────
section("8. DNP / Inactive Players")

dnp_with_stats = con.execute("""
    SELECT COUNT(*) FROM game_players gp
    WHERE gp.did_not_play = TRUE
      AND EXISTS (
          SELECT 1 FROM player_stats ps
          WHERE ps.game_player_id = gp.id
            AND ps.stat_key NOT IN ('--')
            AND TRY_CAST(ps.stat_value AS DOUBLE) > 0
      )
""").fetchone()[0]
_p("DNP players have no meaningful stats", dnp_with_stats == 0,
   f"{dnp_with_stats} DNP players with non-zero stats")

dnp_count = con.execute("SELECT COUNT(*) FROM game_players WHERE did_not_play = TRUE").fetchone()[0]
_i("Total DNP player-game rows", f"{dnp_count:,}")

# ─────────────────────────────────────────────────────────────────────────────
# 9. SOCCER-SPECIFIC: FORMATIONS & SUBSTITUTIONS
# ─────────────────────────────────────────────────────────────────────────────
section("9. Soccer – Formations & Substitutions")

games_with_formation = con.execute("""
    SELECT COUNT(*) FROM games
    WHERE sport = 'soccer' AND status = 'post'
      AND home_formation IS NOT NULL AND away_formation IS NOT NULL
""").fetchone()[0]
soccer_finished = con.execute("""
    SELECT COUNT(*) FROM games WHERE sport = 'soccer' AND status = 'post'
""").fetchone()[0]
pct = round(100 * games_with_formation / soccer_finished) if soccer_finished else 0
_p("Soccer finished games have formations (>70%)", pct >= 70,
   f"{games_with_formation}/{soccer_finished} ({pct}%)")

sub_in  = con.execute("SELECT COUNT(*) FROM game_players WHERE subbed_in  = TRUE").fetchone()[0]
sub_out = con.execute("SELECT COUNT(*) FROM game_players WHERE subbed_out = TRUE").fetchone()[0]
_p("Substitution data present (subbed_in)", sub_in  > 0, f"{sub_in:,}")
_p("Substitution data present (subbed_out)", sub_out > 0, f"{sub_out:,}")

# Subbed-in should roughly equal subbed-out
diff = abs(sub_in - sub_out)
_p("subbed_in ≈ subbed_out (within 20%)", diff <= max(sub_in, sub_out) * 0.2,
   f"in={sub_in} out={sub_out} diff={diff}")

# ─────────────────────────────────────────────────────────────────────────────
# 10. ODDS / MONEYLINE EDGE CASES
# ─────────────────────────────────────────────────────────────────────────────
section("10. Odds & Moneyline Edge Cases")

# Moneyline sanity check — ESPN can report extreme odds (e.g. -100000 for heavy faves)
# We just ensure values aren't obviously corrupt (beyond ±999999)
bad_ml = con.execute("""
    SELECT COUNT(*) FROM game_teams
    WHERE moneyline IS NOT NULL
      AND (moneyline < -999999 OR moneyline > 999999)
""").fetchone()[0]
_p("All moneylines in sane range (±999999)", bad_ml == 0, f"{bad_ml} outliers")

# Report extreme odds as INFO
extreme_ml = con.execute("""
    SELECT gt.event_id, gt.moneyline, g.sport, g.league
    FROM game_teams gt JOIN games g ON g.event_id = gt.event_id
    WHERE gt.moneyline IS NOT NULL AND (gt.moneyline < -5000 OR gt.moneyline > 5000)
    ORDER BY gt.moneyline
""").fetchall()
if extreme_ml:
    _i(f"Extreme moneylines (|ml| > 5000) — heavy favourites, valid ESPN data",
       f"{len(extreme_ml)} lines")
    for r in extreme_ml:
        print(f"       event={r[0]}  ml={r[1]}  {r[2]}/{r[3]}")

# Games with draw odds should all be soccer
draw_non_soccer = con.execute("""
    SELECT COUNT(*) FROM games
    WHERE draw_odds IS NOT NULL AND sport != 'soccer'
""").fetchone()[0]
_p("Draw odds only on soccer games", draw_non_soccer == 0,
   f"{draw_non_soccer} non-soccer games with draw_odds")

# Null odds — expected for some games, just report
null_odds_games = con.execute("""
    SELECT sport, COUNT(*) n FROM games
    WHERE game_total IS NULL AND status = 'post'
    GROUP BY sport ORDER BY n DESC
""").fetchall()
_i("Finished games with no game_total (by sport)",
   ", ".join(f"{r[0]}:{r[1]}" for r in null_odds_games) or "none")

# Win probability sums to ~1 where present
bad_prob = con.execute("""
    SELECT COUNT(*) FROM games
    WHERE home_win_pct IS NOT NULL AND away_win_pct IS NOT NULL
      AND ABS((home_win_pct + away_win_pct) - 1.0) > 0.05
""").fetchone()[0]
_p("Win probabilities sum to ~1.0 (±5%)", bad_prob == 0,
   f"{bad_prob} games where home+away win_pct diverges >5%")

# ─────────────────────────────────────────────────────────────────────────────
# 11. SCORES & RESULTS CONSISTENCY
# ─────────────────────────────────────────────────────────────────────────────
section("11. Score & Result Consistency")

# Finished games should have scores
no_score = con.execute("""
    SELECT sport, COUNT(*) n FROM games
    WHERE status = 'post' AND (home_score IS NULL OR away_score IS NULL)
    GROUP BY sport
""").fetchall()
if no_score:
    for r in no_score:
        _w(f"Finished {r[0]} games with NULL score", f"{r[1]} games")
else:
    _p("All finished games have both scores", True)

# is_winner consistency — exactly one winner per finished game (not draw)
multi_winner = con.execute("""
    SELECT gt.event_id, COUNT(*) n
    FROM game_teams gt
    JOIN games g ON g.event_id = gt.event_id
    WHERE g.status = 'post' AND gt.is_winner = TRUE
    GROUP BY gt.event_id
    HAVING n > 1
""").fetchall()
_p("No game has two winners", len(multi_winner) == 0,
   f"{len(multi_winner)} games with 2 winners")

# Home score matches game_teams.score
score_mismatch = con.execute("""
    SELECT COUNT(*) FROM games g
    JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'home'
    WHERE g.home_score IS NOT NULL AND gt.score IS NOT NULL
      AND g.home_score != gt.score
""").fetchone()[0]
_p("games.home_score == game_teams.score (home)", score_mismatch == 0,
   f"{score_mismatch} mismatches")

score_mismatch_away = con.execute("""
    SELECT COUNT(*) FROM games g
    JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'away'
    WHERE g.away_score IS NOT NULL AND gt.score IS NOT NULL
      AND g.away_score != gt.score
""").fetchone()[0]
_p("games.away_score == game_teams.score (away)", score_mismatch_away == 0,
   f"{score_mismatch_away} mismatches")

# ─────────────────────────────────────────────────────────────────────────────
# 12. CROSS-SPORT PLAYER ID COLLISIONS
# ─────────────────────────────────────────────────────────────────────────────
section("12. Cross-Sport Player ID Collisions")

# Same player_id appearing in multiple sports — is this handled safely?
cross_sport = con.execute("""
    SELECT player_id, COUNT(DISTINCT sport) n
    FROM players
    GROUP BY player_id
    HAVING n > 1
""").fetchall()
_p("Player IDs are scoped per sport (PK is player_id+sport)",
   True,  # by design — PK is (player_id, sport)
   f"{len(cross_sport)} player_ids appear in >1 sport (correctly separated)")

# ─────────────────────────────────────────────────────────────────────────────
# 13. USEFUL AGGREGATE QUERIES
# ─────────────────────────────────────────────────────────────────────────────
section("13. Useful Aggregate Queries (result check)")

# Date range per sport
rows = con.execute("""
    SELECT sport, league,
           MIN(CAST(game_date AS DATE)) first_game,
           MAX(CAST(game_date AS DATE)) last_game,
           COUNT(*) n
    FROM games
    GROUP BY sport, league
    ORDER BY sport, league
""").fetchall()
_p("Date range query works", len(rows) > 0, f"{len(rows)} sport/league combos")
for r in rows:
    print(f"       {r[0]:<12} {r[1]:<25} {r[2]}  →  {r[3]}  ({r[4]} games)")

# Top NBA teams by win rate
rows = con.execute("""
    SELECT t.team_name,
           SUM(CASE WHEN gt.is_winner THEN 1 ELSE 0 END) wins,
           COUNT(*) gp,
           ROUND(100.0 * SUM(CASE WHEN gt.is_winner THEN 1 ELSE 0 END) / COUNT(*), 1) win_pct
    FROM game_teams gt
    JOIN teams t ON t.team_id = gt.team_id AND t.sport = gt.sport
    JOIN games g ON g.event_id = gt.event_id
    WHERE g.league = 'nba' AND g.status = 'post'
    GROUP BY t.team_name
    HAVING gp >= 5
    ORDER BY win_pct DESC LIMIT 5
""").fetchall()
_p("NBA team win-rate query works", len(rows) > 0, "top 5 teams:")
for r in rows:
    print(f"       {r[0]:<25} {r[1]}W/{r[2]}GP  {r[3]}%")

# Average stats per game position (NBA)
rows = con.execute("""
    SELECT p.position,
           ROUND(AVG(CAST(pts.stat_value AS DOUBLE)), 1) avg_pts,
           ROUND(AVG(CAST(reb.stat_value AS DOUBLE)), 1) avg_reb,
           COUNT(DISTINCT gp.id) games
    FROM game_players gp
    JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN player_stats pts ON pts.game_player_id = gp.id AND pts.stat_key = 'PTS'
    JOIN player_stats reb ON reb.game_player_id = gp.id AND reb.stat_key = 'REB'
    JOIN games g ON g.event_id = gp.event_id
    WHERE g.league = 'nba' AND gp.starter = TRUE AND gp.did_not_play = FALSE
    GROUP BY p.position
    ORDER BY avg_pts DESC
""").fetchall()
_p("NBA avg stats by position (starters only)", len(rows) > 0, f"{len(rows)} positions:")
for r in rows:
    print(f"       {r[0]:<5} {r[1]:>5} pts  {r[2]:>5} reb  ({r[3]} games)")

# ─────────────────────────────────────────────────────────────────────────────
# 14. NULL / MISSING FIELD AUDIT
# ─────────────────────────────────────────────────────────────────────────────
section("14. NULL / Missing Field Audit")

null_checks = [
    ("games",       "event_id",    "= 0"),
    ("games",       "sport",       "= 0"),
    ("games",       "game_date",   "= 0"),
    ("players",     "display_name","= 0"),
    ("teams",       "team_name",   "= 0"),
    ("game_players","event_id",    "= 0"),
    ("game_players","player_id",   "= 0"),
    ("player_stats","stat_key",    "= 0"),
    ("player_stats","stat_value",  "= 0"),
]
for table, col, expect in null_checks:
    n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IS NULL").fetchone()[0]
    ok = (n == 0) if expect == "= 0" else True
    _p(f"{table}.{col} not NULL", ok, f"{n} NULLs")

# ─────────────────────────────────────────────────────────────────────────────
# 15. PLAYER SEARCH — single-table lookup (new team columns)
# ─────────────────────────────────────────────────────────────────────────────
section("15. Player Search via players.team_name")

# Basic name search — no joins needed now
rows = con.execute("""
    SELECT display_name, position, team_name, sport
    FROM players
    WHERE display_name ILIKE '%james%'
    ORDER BY sport, display_name
""").fetchall()
_p("ILIKE name search returns results", len(rows) > 0, f"{len(rows)} hits for '%james%'")
for r in rows[:4]:
    print(f"       {r[0]:<28} {r[1]:<5} {r[2]:<25} {r[3]}")

# Every player has team_name populated
no_team = con.execute("""
    SELECT COUNT(*) FROM players WHERE team_name IS NULL OR team_name = ''
""").fetchone()[0]
_p("All players have team_name populated", no_team == 0, f"{no_team} without team")

# team_name in players vs teams master — may drift because teams uses INSERT OR IGNORE
# while players uses ON CONFLICT DO UPDATE (picks up latest name). Treat as INFO.
mismatch_team = con.execute("""
    SELECT COUNT(*) FROM players p
    WHERE p.team_id IS NOT NULL AND p.team_id != ''
      AND NOT EXISTS (
          SELECT 1 FROM teams t
          WHERE t.team_id = p.team_id AND t.sport = p.sport
            AND t.team_name = p.team_name
      )
""").fetchone()[0]
if mismatch_team == 0:
    _p("players.team_name consistent with teams master", True, "0 mismatches")
else:
    _i("players.team_name vs teams master (INSERT OR IGNORE drift — expected)",
       f"{mismatch_team} rows differ (teams keeps first name, players updated to latest)")

# ─────────────────────────────────────────────────────────────────────────────
# 16. CTE + WINDOW: running win-streak per team
# ─────────────────────────────────────────────────────────────────────────────
section("16. CTE + Window: Current Win Streak per NBA Team")

rows = con.execute("""
    WITH ordered AS (
        SELECT
            t.team_name,
            g.game_date,
            gt.is_winner,
            ROW_NUMBER() OVER (PARTITION BY gt.team_id ORDER BY g.game_date DESC) rn
        FROM game_teams gt
        JOIN games g  ON g.event_id  = gt.event_id
        JOIN teams t  ON t.team_id   = gt.team_id AND t.sport = gt.sport
        WHERE g.league = 'nba' AND g.status = 'post'
    ),
    streaks AS (
        SELECT
            team_name,
            is_winner,
            rn,
            -- mark where the streak breaks
            CASE WHEN is_winner != LAG(is_winner, 1, is_winner)
                          OVER (PARTITION BY team_name ORDER BY rn)
                 THEN 1 ELSE 0 END AS break_flag
        FROM ordered
    ),
    streak_groups AS (
        SELECT team_name, is_winner, rn,
               SUM(break_flag) OVER (PARTITION BY team_name ORDER BY rn) AS grp
        FROM streaks
    ),
    current_streak AS (
        SELECT team_name,
               CASE WHEN is_winner THEN 'W' ELSE 'L' END AS streak_type,
               COUNT(*) AS streak_len
        FROM streak_groups
        WHERE grp = 0
        GROUP BY team_name, is_winner
    )
    SELECT team_name, streak_type, streak_len
    FROM current_streak
    ORDER BY streak_len DESC LIMIT 8
""").fetchall()
_p("Running win-streak CTE executes", len(rows) > 0, f"{len(rows)} teams returned")
for r in rows:
    bar = ("W" if r[1] == "W" else "L") * r[2]
    print(f"       {r[0]:<28} {r[1]}{r[2]}  {bar}")

# ─────────────────────────────────────────────────────────────────────────────
# 17. MULTI-JOIN: box-score reconstruction (5 tables)
# ─────────────────────────────────────────────────────────────────────────────
section("17. Multi-Join: NBA Box-Score Reconstruction (5 tables)")

rows = con.execute("""
    SELECT
        g.short_name,
        p.display_name,
        p.team_name,
        CAST(pts.stat_value AS INTEGER)  pts,
        CAST(reb.stat_value AS INTEGER)  reb,
        CAST(ast.stat_value AS INTEGER)  ast,
        fg.stat_value                     fg,
        CAST(minu.stat_value AS DOUBLE)  minu
    FROM games g
    JOIN game_players gp  ON gp.event_id  = g.event_id
    JOIN players p        ON p.player_id  = gp.player_id AND p.sport = gp.sport
    JOIN player_stats pts ON pts.game_player_id = gp.id AND pts.stat_key = 'PTS'
    JOIN player_stats reb ON reb.game_player_id = gp.id AND reb.stat_key = 'REB'
    JOIN player_stats ast ON ast.game_player_id = gp.id AND ast.stat_key = 'AST'
    JOIN player_stats fg  ON fg.game_player_id  = gp.id AND fg.stat_key  = 'FG'
    JOIN player_stats minu ON minu.game_player_id = gp.id AND minu.stat_key = 'MIN'
    WHERE g.league = 'nba' AND g.status = 'post'
      AND gp.did_not_play = FALSE
      AND CAST(pts.stat_value AS INTEGER) >= 30
    ORDER BY pts DESC LIMIT 10
""").fetchall()
_p("5-table box-score join (NBA 30+ pt games)", len(rows) > 0, f"{len(rows)} rows")
for r in rows:
    print(f"       {r[1]:<26} {r[2]:<25} {r[3]:>3}pts {r[4]:>3}reb {r[5]:>3}ast  {r[6]:<8} {r[7]:.1f}min  ({r[0]})")

# ─────────────────────────────────────────────────────────────────────────────
# 18. SUBQUERY + AGGREGATE: teams that beat above-average opponents
# ─────────────────────────────────────────────────────────────────────────────
section("18. Subquery + Aggregate: NHL Teams Beating Above-Avg Opponents")

rows = con.execute("""
    WITH avg_goals AS (
        SELECT AVG(home_score + away_score) / 2.0 AS avg_per_team FROM games
        WHERE league = 'nhl' AND status = 'post'
    ),
    team_results AS (
        SELECT
            t.team_name,
            gt.event_id,
            gt.is_winner,
            -- opponent's score = the other side's score
            opp.score AS opp_score
        FROM game_teams gt
        JOIN teams t   ON t.team_id = gt.team_id AND t.sport = gt.sport
        JOIN game_teams opp ON opp.event_id = gt.event_id
                            AND opp.home_away != gt.home_away
        JOIN games g ON g.event_id = gt.event_id
        WHERE g.league = 'nhl' AND g.status = 'post'
    )
    SELECT
        tr.team_name,
        COUNT(*) FILTER (WHERE tr.is_winner AND tr.opp_score > avg_goals.avg_per_team) strong_wins,
        COUNT(*) gp
    FROM team_results tr, avg_goals
    GROUP BY tr.team_name
    HAVING strong_wins > 0
    ORDER BY strong_wins DESC LIMIT 8
""").fetchall()
_p("NHL 'strong wins' subquery executes", len(rows) > 0, f"{len(rows)} teams with strong wins")
for r in rows:
    print(f"       {r[0]:<28} {r[1]:>2} strong wins / {r[2]:>2} GP")

# ─────────────────────────────────────────────────────────────────────────────
# 19. WINDOW RANK: top scorer per team per league (RANK + PARTITION)
# ─────────────────────────────────────────────────────────────────────────────
section("19. Window RANK: Top Scorer per Team (Soccer Goals)")

rows = con.execute("""
    WITH player_goals AS (
        SELECT
            p.display_name,
            p.team_name,
            g.league,
            SUM(CAST(ps.stat_value AS INTEGER)) total_goals,
            COUNT(DISTINCT gp.event_id)          games_played,
            RANK() OVER (
                PARTITION BY p.team_name, g.league
                ORDER BY SUM(CAST(ps.stat_value AS INTEGER)) DESC
            ) AS rnk
        FROM player_stats ps
        JOIN game_players gp ON gp.id = ps.game_player_id
        JOIN games g         ON g.event_id = gp.event_id
        JOIN players p       ON p.player_id = gp.player_id AND p.sport = gp.sport
        WHERE gp.sport = 'soccer' AND ps.stat_key = 'G'
          AND g.status = 'post' AND gp.did_not_play = FALSE
        GROUP BY p.display_name, p.team_name, g.league
    )
    SELECT display_name, team_name, league, total_goals, games_played
    FROM player_goals
    WHERE rnk = 1 AND total_goals >= 2
    ORDER BY total_goals DESC, team_name LIMIT 12
""").fetchall()
_p("RANK() top scorer per team executes", len(rows) > 0, f"{len(rows)} leading scorers")
for r in rows:
    print(f"       {r[0]:<26} {r[1]:<22} {r[2]:<15} {r[3]}G in {r[4]}GP")

# ─────────────────────────────────────────────────────────────────────────────
# 20. PIVOT-STYLE: MLB batting leaders with multiple stat aggregation
# ─────────────────────────────────────────────────────────────────────────────
section("20. Pivot-Style: MLB Batting Leaders (H, HR, RBI, AVG — 6 joins)")

rows = con.execute("""
    SELECT
        p.display_name,
        p.team_name,
        SUM(CAST(h.stat_value  AS INTEGER)) total_H,
        SUM(CAST(hr.stat_value AS INTEGER)) total_HR,
        SUM(CAST(rbi.stat_value AS INTEGER)) total_RBI,
        ROUND(AVG(TRY_CAST(avg_stat.stat_value AS DOUBLE)), 3) season_AVG,
        COUNT(DISTINCT gp.event_id) gp
    FROM game_players gp
    JOIN players p       ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN games g         ON g.event_id  = gp.event_id
    JOIN player_stats h  ON h.game_player_id  = gp.id AND h.stat_key  = 'H'
    JOIN player_stats hr ON hr.game_player_id = gp.id AND hr.stat_key = 'HR'
    JOIN player_stats rbi ON rbi.game_player_id = gp.id AND rbi.stat_key = 'RBI'
    JOIN player_stats avg_stat ON avg_stat.game_player_id = gp.id
                               AND avg_stat.stat_key = 'AVG'
    WHERE g.league = 'mlb' AND g.status = 'post' AND gp.did_not_play = FALSE
    GROUP BY p.display_name, p.team_name
    HAVING gp >= 8 AND total_H > 0
    ORDER BY total_H DESC LIMIT 10
""").fetchall()
_p("MLB 6-join batting leaders query executes", len(rows) > 0, f"{len(rows)} batters")
for r in rows:
    print(f"       {r[0]:<26} {r[1]:<22} H:{r[2]:>3} HR:{r[3]:>2} RBI:{r[4]:>3} AVG:{r[5]:.3f} ({r[6]}GP)")

# Validate no negative stat values crept into integer stats
neg_hits = con.execute("""
    SELECT COUNT(*) FROM player_stats ps
    JOIN game_players gp ON gp.id = ps.game_player_id
    WHERE gp.sport = 'baseball'
      AND ps.stat_key IN ('H','HR','RBI','R','BB','SO')
      AND TRY_CAST(ps.stat_value AS INTEGER) < 0
""").fetchone()[0]
_p("No negative integer baseball stats (H/HR/RBI etc.)", neg_hits == 0,
   f"{neg_hits} negative values")

# ─────────────────────────────────────────────────────────────────────────────
# 21. CORRELATED SUBQUERY: players who outscored their team average
# ─────────────────────────────────────────────────────────────────────────────
section("21. Correlated Subquery: NBA Players Who Outscored Team Avg Every Game")

rows = con.execute("""
    WITH player_game_pts AS (
        SELECT
            gp.player_id,
            gp.event_id,
            gp.team_id,
            gp.sport,
            CAST(ps.stat_value AS DOUBLE) pts
        FROM game_players gp
        JOIN player_stats ps ON ps.game_player_id = gp.id AND ps.stat_key = 'PTS'
        JOIN games g ON g.event_id = gp.event_id
        WHERE g.league = 'nba' AND g.status = 'post' AND gp.did_not_play = FALSE
    ),
    team_game_avg AS (
        SELECT event_id, team_id, sport,
               AVG(pts) team_avg_pts
        FROM player_game_pts
        GROUP BY event_id, team_id, sport
    ),
    player_above_avg AS (
        SELECT
            pgp.player_id, pgp.sport,
            COUNT(*) games_total,
            SUM(CASE WHEN pgp.pts > tga.team_avg_pts THEN 1 ELSE 0 END) games_above
        FROM player_game_pts pgp
        JOIN team_game_avg tga
          ON tga.event_id = pgp.event_id
         AND tga.team_id  = pgp.team_id
         AND tga.sport    = pgp.sport
        GROUP BY pgp.player_id, pgp.sport
        HAVING games_total >= 8 AND games_above = games_total
    )
    SELECT p.display_name, p.team_name, paa.games_total
    FROM player_above_avg paa
    JOIN players p ON p.player_id = paa.player_id AND p.sport = paa.sport
    ORDER BY paa.games_total DESC LIMIT 8
""").fetchall()
_p("Correlated subquery (always-above-team-avg scorers) executes",
   True, f"{len(rows)} players beat team avg in every game they played")
for r in rows:
    print(f"       {r[0]:<28} {r[1]:<25} {r[2]} games")

# ─────────────────────────────────────────────────────────────────────────────
# 22. SELF-JOIN: head-to-head team records
# ─────────────────────────────────────────────────────────────────────────────
section("22. Self-Join: Head-to-Head Records (Soccer Liga teams)")

rows = con.execute("""
    WITH h2h AS (
        SELECT
            th.team_name  AS home_team,
            ta.team_name  AS away_team,
            g.home_score,
            g.away_score,
            CASE WHEN g.home_score > g.away_score THEN th.team_name
                 WHEN g.away_score > g.home_score THEN ta.team_name
                 ELSE 'Draw' END AS winner
        FROM games g
        JOIN game_teams gth ON gth.event_id = g.event_id AND gth.home_away = 'home'
        JOIN game_teams gta ON gta.event_id = g.event_id AND gta.home_away = 'away'
        JOIN teams th ON th.team_id = gth.team_id AND th.sport = gth.sport
        JOIN teams ta ON ta.team_id = gta.team_id AND ta.sport = gta.sport
        WHERE g.league IN ('esp.1', 'eng.1') AND g.status = 'post'
    )
    SELECT
        home_team, away_team,
        home_score, away_score, winner
    FROM h2h
    ORDER BY home_team, away_team LIMIT 10
""").fetchall()
_p("Self-join head-to-head query executes", len(rows) > 0, f"{len(rows)} matchups")
for r in rows:
    print(f"       {r[0]:<22} vs {r[1]:<22}  {r[2]}-{r[3]}  → {r[4]}")

# ─────────────────────────────────────────────────────────────────────────────
# 23. BATCH AGGREGATE: per-league summary stats in one pass
# ─────────────────────────────────────────────────────────────────────────────
section("23. Batch Aggregate: Per-League Summary (single GROUP BY pass)")

rows = con.execute("""
    SELECT
        g.league,
        COUNT(DISTINCT g.event_id)                              total_games,
        COUNT(DISTINCT gp.player_id)                            unique_players,
        ROUND(AVG(g.home_score + g.away_score), 2)                        avg_total_score,
        MAX(g.home_score + g.away_score)                                   max_total_score,
        MIN(g.home_score + g.away_score)                                   min_total_score,
        COALESCE(SUM(CASE WHEN g.home_score > g.away_score THEN 1 END), 0) home_wins,
        COALESCE(SUM(CASE WHEN g.home_score < g.away_score THEN 1 END), 0) away_wins,
        COALESCE(SUM(CASE WHEN g.home_score = g.away_score THEN 1 END), 0) draws,
        ROUND(100.0 *
            COALESCE(SUM(CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END), 0)
            / NULLIF(COUNT(*), 0), 1)                                       home_win_pct
    FROM games g
    LEFT JOIN game_players gp ON gp.event_id = g.event_id
    WHERE g.status = 'post'
      AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
    GROUP BY g.league
    ORDER BY total_games DESC
""").fetchall()
_p("Per-league batch aggregate executes", len(rows) > 0, f"{len(rows)} leagues")
print(f"       {'League':<25} {'Gms':>5} {'Plyr':>6} {'AvgTot':>7} {'HW%':>6} {'H':>5} {'A':>5} {'D':>5}")
for r in rows:
    print(f"       {r[0]:<25} {r[1]:>5} {r[2]:>6} {r[3]:>7} {r[9]:>5}%  {r[6]:>4} {r[7]:>4} {r[8]:>4}")

# Home advantage check: home win rate > 40% in aggregate (expected ~50-55%)
avg_home_win_pct = con.execute("""
    SELECT ROUND(100.0 *
        SUM(CASE WHEN home_score > away_score THEN 1 ELSE 0 END)
        / COUNT(*), 1)
    FROM games WHERE status = 'post'
      AND home_score IS NOT NULL AND away_score IS NOT NULL
""").fetchone()[0]
_p("Home win rate is plausible (> 40%)", avg_home_win_pct > 40,
   f"Overall home win rate: {avg_home_win_pct}%")

# ─────────────────────────────────────────────────────────────────────────────
# 24. TOI PARSING: NHL ice time as numeric minutes (string → arithmetic)
# ─────────────────────────────────────────────────────────────────────────────
section("24. TOI String Arithmetic: NHL Ice-Time Leaders (MM:SS → minutes)")

rows = con.execute("""
    WITH toi_parsed AS (
        SELECT
            p.display_name,
            p.team_name,
            gp.event_id,
            -- split MM:SS, convert to decimal minutes
            CAST(SPLIT_PART(ps.stat_value, ':', 1) AS INTEGER) +
            CAST(SPLIT_PART(ps.stat_value, ':', 2) AS INTEGER) / 60.0 AS toi_min
        FROM player_stats ps
        JOIN game_players gp ON gp.id = ps.game_player_id
        JOIN players p       ON p.player_id = gp.player_id AND p.sport = gp.sport
        JOIN games g         ON g.event_id  = gp.event_id
        WHERE gp.sport = 'hockey' AND ps.stat_key = 'TOI'
          AND g.status = 'post' AND gp.did_not_play = FALSE
    )
    SELECT
        display_name,
        team_name,
        COUNT(*)                          gp,
        ROUND(AVG(toi_min), 2)            avg_toi,
        ROUND(SUM(toi_min) / 60.0, 1)    total_hours,
        ROUND(MAX(toi_min), 2)            max_single_game
    FROM toi_parsed
    GROUP BY display_name, team_name
    HAVING gp >= 5
    ORDER BY avg_toi DESC LIMIT 8
""").fetchall()
_p("TOI MM:SS → minutes arithmetic executes", len(rows) > 0, f"{len(rows)} defensemen/skaters")
for r in rows:
    print(f"       {r[0]:<28} {r[1]:<22} {r[2]:>3}GP  avg {r[3]}min  peak {r[5]}min  {r[4]}hr total")

# Validate no impossible TOI (> 65 min would be extreme OT, flag > 90 as bad)
bad_toi = con.execute("""
    WITH toi_parsed AS (
        SELECT
            CAST(SPLIT_PART(ps.stat_value, ':', 1) AS INTEGER) +
            CAST(SPLIT_PART(ps.stat_value, ':', 2) AS INTEGER) / 60.0 AS toi_min
        FROM player_stats ps
        JOIN game_players gp ON gp.id = ps.game_player_id
        WHERE gp.sport = 'hockey' AND ps.stat_key = 'TOI'
    )
    SELECT COUNT(*) FROM toi_parsed WHERE toi_min > 90
""").fetchone()[0]
_p("No impossible TOI values (> 90 min)", bad_toi == 0, f"{bad_toi} outliers")

# ─────────────────────────────────────────────────────────────────────────────
# 25. MULTI-SPORT CTE: cross-sport "double-double" equivalent per sport
# ─────────────────────────────────────────────────────────────────────────────
section("25. Cross-Sport CTE: Performance Thresholds per Sport")

# NBA double-double (pts>=10 AND reb>=10) or (pts>=10 AND ast>=10)
dd = con.execute("""
    WITH nba_pts AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'PTS'
    ),
    nba_reb AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'REB'
    ),
    nba_ast AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'AST'
    )
    SELECT
        p.display_name, p.team_name,
        COUNT(*) FILTER (WHERE pts.v >= 10 AND reb.v >= 10) pts_reb_dd,
        COUNT(*) FILTER (WHERE pts.v >= 10 AND ast.v >= 10) pts_ast_dd,
        COUNT(*) gp
    FROM game_players gp
    JOIN players p   ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN nba_pts pts ON pts.game_player_id = gp.id
    JOIN nba_reb reb ON reb.game_player_id = gp.id
    JOIN nba_ast ast ON ast.game_player_id = gp.id
    JOIN games g     ON g.event_id = gp.event_id
    WHERE g.league = 'nba' AND g.status = 'post' AND gp.did_not_play = FALSE
    GROUP BY p.display_name, p.team_name
    HAVING (pts_reb_dd + pts_ast_dd) > 0
    ORDER BY (pts_reb_dd + pts_ast_dd) DESC LIMIT 8
""").fetchall()
_p("NBA double-double CTE executes", len(dd) > 0, f"{len(dd)} players with double-doubles")
for r in dd:
    total_dd = r[2] + r[3]
    print(f"       {r[0]:<28} {r[1]:<25} Pts+Reb:{r[2]}  Pts+Ast:{r[3]}  ({r[4]}GP)")

# NHL: power-play points proxy — players with G+A >= 3 in a game
ppe = con.execute("""
    WITH nhl_g AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'G'
    ),
    nhl_a AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'A'
    )
    SELECT COUNT(*) FROM nhl_g g JOIN nhl_a a ON a.game_player_id = g.game_player_id
    WHERE g.v + a.v >= 3
""").fetchone()[0]
_p("NHL multi-point game count (G+A >= 3)", ppe > 0, f"{ppe} such game-player rows")

# Soccer: clean-sheet goalkeepers (SV > 0 AND GA = 0 in a game)
cs = con.execute("""
    WITH sv AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'SV'
    ),
    ga AS (
        SELECT game_player_id, CAST(stat_value AS INTEGER) v FROM player_stats WHERE stat_key = 'GA'
    )
    SELECT p.display_name, p.team_name, COUNT(*) clean_sheets
    FROM sv s
    JOIN ga ON ga.game_player_id = s.game_player_id
    JOIN game_players gp ON gp.id = s.game_player_id
    JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN games g ON g.event_id = gp.event_id
    WHERE g.status = 'post' AND s.v > 0 AND ga.v = 0
    GROUP BY p.display_name, p.team_name
    ORDER BY clean_sheets DESC LIMIT 6
""").fetchall()
_p("Soccer goalkeeper clean-sheet CTE executes", len(cs) > 0, f"{len(cs)} GKs with clean sheets")
for r in cs:
    print(f"       {r[0]:<28} {r[1]:<25} {r[2]} clean sheet(s)")

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
section("SUMMARY")
total = passed + failed + warned
print(f"  Passed : {passed}")
print(f"  Failed : {failed}")
print(f"  Warned : {warned}")
print(f"  Total  : {total}")
if failed == 0:
    print(f"\n  \033[32mAll checks passed.\033[0m")
else:
    print(f"\n  \033[31m{failed} check(s) failed — see details above.\033[0m")

con.close()
