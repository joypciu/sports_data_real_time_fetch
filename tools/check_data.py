import json, os
from collections import Counter

data_dir = "historical_data"
files = sorted(f for f in os.listdir(data_dir) if f.endswith(".json"))

for fname in files:
    data = json.load(open(os.path.join(data_dir, fname), encoding="utf-8"))
    total = len(data)

    has_score = 0
    bad_score = 0
    has_players = 0
    statuses = Counter()
    sample_results = []
    sample_odds = []

    for g in data:
        status = g.get("status", "")
        statuses[status] += 1

        away = g.get("away") or {}
        home = g.get("home") or {}
        away_score = away.get("score")
        home_score = home.get("score")

        real_score = (
            away_score is not None
            and home_score is not None
            and not (away_score in ("0", "None", None) and home_score in ("0", "None", None) and status != "in")
        )
        if real_score:
            has_score += 1
            if status == "post" and len(sample_results) < 4:
                sample_results.append(
                    f"    {g.get('short_name','?'):25s}  {g['date'][:10]}  "
                    f"{away.get('team_abbr','?')} {away_score} - {home_score} {home.get('team_abbr','?')}"
                )
        else:
            bad_score += 1

        if g.get("players"):
            has_players += 1

        if g.get("home", {}) and g["home"].get("moneyline") and status == "post" and len(sample_odds) < 2:
            sample_odds.append(
                f"    {g.get('short_name','?'):25s}  ML away {g['away']['moneyline']:+d} / home {g['home']['moneyline']:+d}  "
                f"spread {g['away'].get('spread','?')}  total {g.get('game_total','?')}"
            )

    print(f"\n{'='*60}")
    print(f"  {fname}  ({total} games)")
    print(f"  Status: {dict(statuses)}")
    print(f"  Games with real scores : {has_score}")
    print(f"  Games without scores   : {bad_score}")
    print(f"  Games with player data : {has_players}")
    if sample_results:
        print("  Sample scores:")
        for s in sample_results:
            print(s)
    if sample_odds:
        print("  Sample odds (finished games):")
        for s in sample_odds:
            print(s)
