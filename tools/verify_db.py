import duckdb
con = duckdb.connect("db/sports.db")

print("=== TABLE COUNTS ===")
for t in ["teams","players","games","game_teams","game_players","player_stats"]:
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t:<15} {n:>7,}")

print()
print("=== NBA top scorers last 7 days ===")
rows = con.execute("""
    SELECT p.display_name, t.team_name,
           ROUND(AVG(CAST(ps.stat_value AS DOUBLE)),1) avg_pts,
           COUNT(*) games
    FROM player_stats ps
    JOIN game_players gp ON gp.id = ps.game_player_id
    JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN teams t ON t.team_id = gp.team_id AND t.sport = gp.sport
    JOIN games g ON g.event_id = gp.event_id
    WHERE g.league = 'nba' AND ps.stat_key = 'PTS'
      AND g.game_date >= NOW() - INTERVAL 7 DAY
    GROUP BY p.display_name, t.team_name
    HAVING COUNT(*) >= 2
    ORDER BY avg_pts DESC LIMIT 10
""").fetchall()
for r in rows:
    print(f"  {r[0]:<28} {r[1]:<22} {r[2]:>5} pts  ({r[3]} games)")

print()
print("=== EPL results (last 5) ===")
rows = con.execute("""
    SELECT g.short_name, g.home_score, g.away_score, CAST(g.game_date AS DATE)
    FROM games g
    WHERE g.league = 'eng.1' AND g.status = 'post'
    ORDER BY g.game_date DESC LIMIT 5
""").fetchall()
for r in rows:
    print(f"  {r[3]}  {r[0]}  {r[1]}-{r[2]}")

print()
print("=== NHL players most TOI (single game) ===")
rows = con.execute("""
    SELECT p.display_name, t.team_name, ps.stat_value toi,
           CAST(g.game_date AS DATE) dt
    FROM player_stats ps
    JOIN game_players gp ON gp.id = ps.game_player_id
    JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN teams t ON t.team_id = gp.team_id AND t.sport = gp.sport
    JOIN games g ON g.event_id = gp.event_id
    WHERE gp.sport = 'hockey' AND ps.stat_key = 'TOI'
    ORDER BY toi DESC LIMIT 5
""").fetchall()
for r in rows:
    print(f"  {r[0]:<28} {r[1]:<22} {r[2]}  ({r[3]})")

print()
print("=== Soccer player stats (goals, top 5) ===")
rows = con.execute("""
    SELECT p.display_name, t.team_name,
           SUM(CAST(ps.stat_value AS INTEGER)) goals
    FROM player_stats ps
    JOIN game_players gp ON gp.id = ps.game_player_id
    JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
    JOIN teams t ON t.team_id = gp.team_id AND t.sport = gp.sport
    WHERE gp.sport = 'soccer' AND ps.stat_key = 'G'
    GROUP BY p.display_name, t.team_name
    ORDER BY goals DESC LIMIT 5
""").fetchall()
for r in rows:
    print(f"  {r[0]:<28} {r[1]:<22} {r[2]} goals")

print()
print("=== FK relationships verified ===")
orphan_gt = con.execute("SELECT COUNT(*) FROM game_teams gt WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.event_id = gt.event_id)").fetchone()[0]
orphan_gp = con.execute("SELECT COUNT(*) FROM game_players gp WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.event_id = gp.event_id)").fetchone()[0]
orphan_ps = con.execute("SELECT COUNT(*) FROM player_stats ps WHERE NOT EXISTS (SELECT 1 FROM game_players gp WHERE gp.id = ps.game_player_id)").fetchone()[0]
print(f"  orphan game_teams rows   : {orphan_gt}")
print(f"  orphan game_players rows : {orphan_gp}")
print(f"  orphan player_stats rows : {orphan_ps}")
