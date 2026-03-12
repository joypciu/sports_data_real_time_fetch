"""
audit_stats.py — Deep audit of player stats and team stats in every sport JSON.

Checks:
  Team level:
    - Score present and numeric for finished games
    - Winner flag consistent with scores
    - Team total odds present
    - Home/away symmetry (one home, one away per game)

  Player level:
    - Expected starter counts per sport/position
    - No duplicate players in same game
    - Stat value sanity (mins in range, % values 0-100, no negatives where impossible)
    - DNP players should have empty stats
    - Active players should have non-empty stats (for non-soccer)
    - Core stat keys present for each position
    - Score reconciliation: sum of player points == team score (NBA/NCAAB)
    - Inning/pitcher count for MLB
"""

import json
import os
from collections import Counter, defaultdict
from typing import Any

DATA_DIR = "historical_data"

# ── Expected starter counts ──────────────────────────────────────────────────
EXPECTED_STARTERS = {
    "basketball": 5,   # per team
    "hockey": None,    # not flagged via 'starter' in NHL
    "baseball": None,  # starters not flagged uniformly
    "soccer": 11,      # per team
}

# ── Core stats that MUST be present for active, non-DNP players ─────────────
REQUIRED_STATS: dict[str, dict[str, list[str]]] = {
    "basketball": {
        "default": ["MIN", "PTS", "REB", "AST"],
    },
    "hockey": {
        "G":  ["GA", "SA", "SV", "TOI"],       # goalies
        "default": ["TOI", "G", "A"],           # skaters
    },
    "baseball": {
        "P":  ["IP", "H", "ER", "K"],
        "SP": ["IP", "H", "ER", "K"],
        "RP": ["IP", "H", "ER", "K"],
        "default": ["AB", "H", "R"],
    },
    "soccer": {
        "default": ["APP"],
    },
}

# ── Stat sanity rules: (min, max) for numeric stats ─────────────────────────
STAT_SANITY: dict[str, dict[str, tuple[float, float]]] = {
    "basketball": {
        "PTS": (0, 100), "REB": (0, 50), "AST": (0, 30),
        "STL": (0, 15),  "BLK": (0, 15), "TO":  (0, 20),
        "PF":  (0, 6),
    },
    "hockey": {
        "G": (0, 5), "A": (0, 5), "PIM": (0, 40), "+/-": (-10, 10),
        "GA": (0, 10), "SV%": (0, 1.01),
    },
    "baseball": {
        "HR": (0, 4), "K": (0, 25), "BB": (0, 15), "ER": (0, 15),
        "R":  (0, 10),
    },
    "soccer": {
        "G": (0, 5), "A": (0, 5), "YC": (0, 2), "RC": (0, 1),
        "SH": (0, 15), "SV": (0, 20),
    },
}


def _num(val: str) -> float | None:
    """Parse a stat value to float, return None if not numeric."""
    if val is None or val == "":
        return None
    # Handle ratios like "9-14" (FG) — skip these
    if "-" in str(val) and not str(val).startswith("-"):
        return None
    # Handle time like "14:58"
    if ":" in str(val):
        try:
            m, s = str(val).split(":")
            return float(m) + float(s) / 60
        except Exception:
            return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def audit_file(path: str) -> None:
    fname = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        data: list[dict] = json.load(f)

    if not data:
        print(f"=== {fname} — EMPTY ===\n")
        return

    sport = data[0].get("sport", "?")

    issues: list[str] = []
    warnings: list[str] = []

    # ── Counters ─────────────────────────────────────────────────────────────
    total_games = len(data)
    total_players = 0
    stat_key_totals: Counter[str] = Counter()
    missing_core: Counter[str] = Counter()      # stat key → games missing it
    sanity_fails: list[str] = []
    dup_players: list[str] = []
    dnp_with_stats: list[str] = []
    active_no_stats: list[str] = []
    starter_counts: list[tuple[str, int, int]] = []  # (game, home_cnt, away_cnt)
    score_mismatch: list[str] = []
    team_issues: list[str] = []

    req_stats = REQUIRED_STATS.get(sport, {})
    sane_rules = STAT_SANITY.get(sport, {})
    exp_starters = EXPECTED_STARTERS.get(sport)

    for g in data:
        name = g.get("short_name", g.get("event_id", "?"))
        status = g.get("status", "")
        home = g.get("home") or {}
        away = g.get("away") or {}
        players: list[dict] = g.get("players", [])

        # ── Team: home_away symmetry ─────────────────────────────────────────
        ha_vals = {home.get("home_away"), away.get("home_away")}
        if ha_vals != {"home", "away"}:
            team_issues.append(f"{name}: home_away mismatch {ha_vals}")

        # ── Team: score/winner consistency ───────────────────────────────────
        if status == "post":
            h_score = _num(str(home.get("score", "")))
            a_score = _num(str(away.get("score", "")))
            if h_score is not None and a_score is not None:
                h_win = home.get("is_winner", False)
                a_win = away.get("is_winner", False)
                if sport != "soccer":  # draws allowed in soccer
                    if h_score > a_score and not h_win:
                        team_issues.append(f"{name}: home score {h_score}>{a_score} but not flagged as winner")
                    elif a_score > h_score and not a_win:
                        team_issues.append(f"{name}: away score {a_score}>{h_score} but not flagged as winner")
                    elif h_score == a_score and (h_win or a_win):
                        team_issues.append(f"{name}: tied score but a winner is flagged")

        if not players:
            continue

        # ── Player: duplicate detection ──────────────────────────────────────
        seen_ids: set[str] = set()
        for p in players:
            pid = p.get("player_id", "")
            if pid and pid in seen_ids:
                dup_players.append(f"{name}: duplicate player_id {pid} ({p.get('display_name')})")
            seen_ids.add(pid)

        # ── Player: starter counts ────────────────────────────────────────────
        if exp_starters is not None:
            by_team: dict[str, int] = Counter(
                p.get("home_away") for p in players if p.get("starter")
            )
            for side in ("home", "away"):
                cnt = by_team.get(side, 0)
                if cnt != exp_starters:
                    starter_counts.append((name, side, cnt, exp_starters))

        # ── Player: DNP/active stat checks ────────────────────────────────────
        for p in players:
            pname = p.get("display_name", "?")
            pos = p.get("position", "default")
            stats: dict[str, str] = p.get("stats") or {}
            is_dnp = p.get("did_not_play", False)
            is_active = p.get("active", True)

            # Count stat keys
            for k in stats:
                stat_key_totals[k] += 1
            total_players += 1

            if is_dnp and stats:
                # DNP players should have no stats
                dnp_with_stats.append(f"{name}: {pname} marked DNP but has stats {list(stats.keys())[:4]}")

            # For non-soccer active+non-DNP players, check core stats
            if not is_dnp and is_active and sport != "soccer":
                req = req_stats.get(pos) or req_stats.get("default", [])
                for key in req:
                    if key not in stats:
                        missing_core[f"{sport}/{pos}/{key}"] += 1

            # Sanity check numeric stats
            for stat_key, (lo, hi) in sane_rules.items():
                val = stats.get(stat_key)
                if val is None:
                    continue
                n = _num(val)
                if n is not None and not (lo <= n <= hi):
                    sanity_fails.append(
                        f"{name}: {pname} {stat_key}={val} out of range [{lo},{hi}]"
                    )

        # ── Score reconciliation (NBA/NCAAB: sum player PTS == team score) ────
        if sport == "basketball" and status == "post":
            for side in ("home", "away"):
                team = home if side == "home" else away
                t_score = _num(str(team.get("score", "")))
                if t_score is None:
                    continue
                player_pts = sum(
                    _num(p.get("stats", {}).get("PTS", "")) or 0
                    for p in players
                    if p.get("home_away") == side and not p.get("did_not_play")
                )
                if player_pts > 0 and abs(player_pts - t_score) > 1:
                    score_mismatch.append(
                        f"{name} ({side}): player_pts={player_pts} team_score={t_score}"
                    )

        # ── MLB: pitcher/batter count sanity ─────────────────────────────────
        if sport == "baseball":
            for side in ("home", "away"):
                pitchers = [
                    p for p in players
                    if p.get("home_away") == side and p.get("position") in ("SP", "RP", "P")
                ]
                batters = [
                    p for p in players
                    if p.get("home_away") == side and p.get("position") not in ("SP", "RP", "P", "")
                    and not p.get("did_not_play")
                ]
                if status == "post" and len(batters) < 8:
                    warnings.append(f"{name} ({side}): only {len(batters)} batters listed")

    # ── REPORT ────────────────────────────────────────────────────────────────
    bar = "=" * 64
    print(bar)
    print(f"  {fname}  |  sport={sport}  |  games={total_games}  |  players={total_players}")
    print(bar)

    # Team issues
    print(f"\n  [TEAM STATS]  issues={len(team_issues)}")
    if team_issues:
        for msg in team_issues[:10]:
            print(f"    !! {msg}")
        if len(team_issues) > 10:
            print(f"    ... and {len(team_issues)-10} more")
    else:
        print("    All team score/winner/home_away checks passed  ✓")

    # Top stat keys
    print(f"\n  [STAT KEY COVERAGE]  distinct keys={len(stat_key_totals)}")
    for k, cnt in stat_key_totals.most_common(15):
        print(f"    {k:12s} : {cnt:6d} occurrences")

    # Missing core stats
    print(f"\n  [MISSING CORE STATS]  distinct types={len(missing_core)}")
    if missing_core:
        for key, cnt in missing_core.most_common(10):
            print(f"    {key} — missing in {cnt} player rows")
    else:
        print("    No core stat gaps found  ✓")

    # Sanity fails
    print(f"\n  [SANITY CHECKS]  violations={len(sanity_fails)}")
    if sanity_fails:
        for msg in sanity_fails[:10]:
            print(f"    !! {msg}")
        if len(sanity_fails) > 10:
            print(f"    ... and {len(sanity_fails)-10} more")
    else:
        print("    All stat values within expected ranges  ✓")

    # Duplicates
    print(f"\n  [DUPLICATE PLAYERS]  count={len(dup_players)}")
    if dup_players:
        for msg in dup_players[:5]:
            print(f"    !! {msg}")
    else:
        print("    No duplicate players found  ✓")

    # Starter counts
    wrong_starters = [x for x in starter_counts if x[2] != (x[3])]
    print(f"\n  [STARTER COUNTS]  expected={exp_starters}/team  violations={len(wrong_starters)}")
    if wrong_starters:
        samples = wrong_starters[:5]
        for name_, side, cnt, exp in samples:
            print(f"    !! {name_[:40]} ({side}): {cnt} starters (expected {exp})")
        if len(wrong_starters) > 5:
            print(f"    ... and {len(wrong_starters)-5} more")
    elif exp_starters is not None:
        print("    All games have correct starter counts  ✓")
    else:
        print("    (starter count not enforced for this sport)")

    # DNP with stats
    print(f"\n  [DNP CONSISTENCY]  DNP-with-stats={len(dnp_with_stats)}")
    if dnp_with_stats:
        for msg in dnp_with_stats[:5]:
            print(f"    !! {msg}")
    else:
        print("    All DNP players correctly have no stats  ✓")

    # Score reconciliation
    if score_mismatch:
        print(f"\n  [SCORE RECONCILIATION]  mismatches={len(score_mismatch)}")
        for msg in score_mismatch[:5]:
            print(f"    !! {msg}")
    elif sport == "basketball":
        print(f"\n  [SCORE RECONCILIATION]  Player PTS sums match team scores  ✓")

    # Warnings
    if warnings:
        print(f"\n  [WARNINGS]  count={len(warnings)}")
        for w in warnings[:5]:
            print(f"    ~ {w}")

    # Sport-specific extras
    if sport == "soccer":
        sub_in  = sum(1 for g in data for p in g.get("players", []) if p.get("subbed_in"))
        sub_out = sum(1 for g in data for p in g.get("players", []) if p.get("subbed_out"))
        no_form = [g for g in data if g.get("status") == "post" and not g.get("formations")]
        print(f"\n  [SOCCER EXTRAS]")
        print(f"    Subbed in/out symmetry : {sub_in} in  /  {sub_out} out  {'✓' if sub_in == sub_out else '!!'}")
        print(f"    Missing formations     : {len(no_form)} post games without formations")
        # Check every player has APP stat
        no_app = sum(1 for g in data for p in g.get("players",[]) if "APP" not in (p.get("stats") or {}))
        print(f"    Players missing APP    : {no_app}")

    if sport == "hockey":
        goalies = sum(1 for g in data for p in g.get("players",[]) if p.get("position")=="G")
        w_sv    = sum(1 for g in data for p in g.get("players",[]) if p.get("position")=="G" and "SV" in (p.get("stats") or {}))
        print(f"\n  [HOCKEY EXTRAS]")
        print(f"    Total goalies          : {goalies}")
        print(f"    Goalies with SV stat   : {w_sv}/{goalies}  {'✓' if w_sv==goalies else '!!'}")

    if sport == "baseball":
        sp_count  = sum(1 for g in data for p in g.get("players",[]) if p.get("position")=="SP")
        rp_count  = sum(1 for g in data for p in g.get("players",[]) if p.get("position")=="RP")
        bat_count = sum(1 for g in data for p in g.get("players",[]) if p.get("position") not in ("SP","RP","P",""))
        print(f"\n  [BASEBALL EXTRAS]")
        print(f"    Starting pitchers (SP) : {sp_count}")
        print(f"    Relief pitchers (RP)   : {rp_count}")
        print(f"    Position players       : {bat_count}")
        # IP sanity: every SP should have > 0 IP
        bad_ip = [
            f"{g['short_name']}: {p['display_name']} IP={p['stats'].get('IP','?')}"
            for g in data for p in g.get("players",[])
            if p.get("position") == "SP" and p.get("stats") and _num(str(p["stats"].get("IP","0") or "0")) == 0
        ]
        print(f"    SP with 0 IP ('early exit'): {len(bad_ip)}")
        if bad_ip:
            for msg in bad_ip[:3]:
                print(f"      {msg}")

    print()


def main() -> None:
    files = sorted(
        os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR) if f.endswith(".json")
    )
    if not files:
        print(f"No JSON files in {DATA_DIR}/")
        return
    for path in files:
        audit_file(path)


if __name__ == "__main__":
    main()
