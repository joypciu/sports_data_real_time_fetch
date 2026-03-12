"""
audit_data.py — Comprehensive data quality audit for all sport JSON files.
"""
import json
import os
from collections import Counter

DATA_DIR = "historical_data"

def audit_file(path: str) -> None:
    fname = os.path.basename(path)
    fsize = os.path.getsize(path) / 1024
    with open(path, encoding="utf-8") as f:
        data: list[dict] = json.load(f)

    if not data:
        print(f"=== {fname} — EMPTY ===\n")
        return

    sport = data[0].get("sport", "?")
    leagues = Counter(g["league"] for g in data)
    statuses = Counter(g["status"] for g in data)

    # --- Score completeness ---
    finished = [g for g in data if g["status"] == "post"]
    def has_score(g):
        h = (g.get("home") or {}).get("score")
        a = (g.get("away") or {}).get("score")
        return h not in (None, "") and a not in (None, "")
    scored = [g for g in finished if has_score(g)]
    missing_score = [g for g in finished if not has_score(g)]

    # --- Winner flag ---
    no_winner = [
        g for g in finished
        if not (g.get("home") or {}).get("is_winner") and not (g.get("away") or {}).get("is_winner")
    ]

    # --- Odds ---
    has_ml     = sum(1 for g in data if (g.get("home") or {}).get("moneyline") not in (None, ""))
    has_spread = sum(1 for g in data if (g.get("home") or {}).get("spread") not in (None, ""))
    has_total  = sum(1 for g in data if g.get("game_total") not in (None, ""))
    has_wp     = sum(1 for g in data if g.get("home_win_pct") not in (None, ""))

    # --- Player coverage ---
    games_with_players = [g for g in data if g.get("players")]
    all_players = [p for g in data for p in g.get("players", [])]
    with_stats  = sum(1 for p in all_players if p.get("stats"))
    dnp_count   = sum(1 for p in all_players if p.get("did_not_play"))
    starters    = sum(1 for p in all_players if p.get("starter"))

    # Players per game (for enriched games)
    players_per_game = (
        [len(g["players"]) for g in games_with_players]
        if games_with_players else []
    )
    avg_ppg = sum(players_per_game) / len(players_per_game) if players_per_game else 0

    # --- Stats per player sample ---
    stat_key_counts: Counter = Counter()
    for p in all_players:
        for k in (p.get("stats") or {}):
            stat_key_counts[k] += 1
    top_stats = stat_key_counts.most_common(10)

    # --- Date range ---
    dates = sorted(g["date"][:10] for g in data)

    # --- Soccer formations ---
    form_games = sum(1 for g in data if g.get("formations"))

    # === PRINT REPORT ===
    bar = "=" * 62
    print(bar)
    print(f"  {fname}  ({fsize:.0f} KB)")
    print(bar)
    print(f"  Sport          : {sport}")
    print(f"  Leagues        : {dict(leagues)}")
    print(f"  Total games    : {len(data)}")
    print(f"  Status         : {dict(statuses)}")
    print(f"  Date range     : {dates[0]}  →  {dates[-1]}")
    print()
    print("  [SCORES]")
    print(f"    Finished games        : {len(finished)}")
    print(f"    With real scores      : {len(scored)}/{len(finished)}")
    if missing_score:
        print(f"    !! Missing scores     : {len(missing_score)}")
        for g in missing_score[:3]:
            h = (g.get("home") or {}).get("score")
            a = (g.get("away") or {}).get("score")
            print(f"       {g['short_name'][:40]}  home={h}  away={a}")
    else:
        print(f"    Missing scores        : 0  (OK)")
    if no_winner:
        print(f"    !! No winner flag     : {len(no_winner)} games")
    else:
        print(f"    Winner flag           : all set  (OK)")

    print()
    print("  [BETTING LINES]")
    pct = lambda n: f"{n}/{len(data)} ({100*n//len(data)}%)"
    print(f"    Moneyline coverage    : {pct(has_ml)}")
    print(f"    Spread coverage       : {pct(has_spread)}")
    print(f"    Game total coverage   : {pct(has_total)}")
    print(f"    Win probability       : {pct(has_wp)}")

    print()
    print("  [PLAYERS]")
    eligible = [g for g in data if g["status"] in ("post", "in")]
    print(f"    Eligible games        : {len(eligible)}")
    print(f"    Games w/ player data  : {len(games_with_players)}/{len(eligible)}")
    print(f"    Total players stored  : {len(all_players)}")
    if avg_ppg:
        print(f"    Avg players / game    : {avg_ppg:.1f}")
    if all_players:
        print(f"    Players w/ stats      : {with_stats}/{len(all_players)}")
        print(f"    Starters flagged      : {starters}")
        print(f"    DNP/inactive          : {dnp_count}")
    if top_stats:
        print(f"    Top stat keys         : {[k for k,_ in top_stats]}")

    if sport == "soccer":
        print()
        print("  [SOCCER EXTRA]")
        print(f"    Games w/ formations   : {form_games}/{len(data)}")
        subbed_in  = sum(1 for p in all_players if p.get("subbed_in"))
        subbed_out = sum(1 for p in all_players if p.get("subbed_out"))
        print(f"    Subbed-in players     : {subbed_in}")
        print(f"    Subbed-out players    : {subbed_out}")

    # --- Sample game ---
    sample = next(
        (g for g in data if g["status"] == "post" and has_score(g) and g.get("players")), None
    )
    if sample:
        h = sample.get("home") or {}
        a = sample.get("away") or {}
        print()
        print("  [SAMPLE FINISHED GAME]")
        print(f"    {sample['name']}")
        print(f"    Score   : {h.get('team_abbr')} {h.get('score')}  –  {a.get('score')} {a.get('team_abbr')}")
        print(f"    ML      : {h.get('moneyline')} / {a.get('moneyline')}")
        print(f"    Spread  : {h.get('spread')} ({h.get('spread_odds')}) / {a.get('spread')} ({a.get('spread_odds')})")
        print(f"    Total   : {sample.get('game_total')}  O{sample.get('over_odds')} / U{sample.get('under_odds')}")
        print(f"    Win Pct : home {sample.get('home_win_pct')}  away {sample.get('away_win_pct')}")
        strs = [p for p in sample["players"] if p.get("starter")]
        if strs:
            p0 = strs[0]
            print(f"    Sample starter: {p0['display_name']} ({p0.get('position')}, {p0.get('team_abbr')}) | stats: {p0.get('stats')}")
    print()


def main():
    files = sorted(
        os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR) if f.endswith(".json")
    )
    if not files:
        print(f"No JSON files found in {DATA_DIR}/")
        return
    for path in files:
        audit_file(path)


if __name__ == "__main__":
    main()
