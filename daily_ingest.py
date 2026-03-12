"""
daily_ingest.py
===============
Fetches yesterday's (or a specified date's) completed games for all configured
leagues and APPENDS new games—with full player stats—to the sport-specific JSON
files in historical_data/.  Meant to be run once per day (e.g. via Windows Task Scheduler).

  - Deduplicates by event_id: will never double-insert a game.
  - Fetches player data automatically for every finished game.
  - Soccer games use roster/formation parsing; all other sports use boxscore.
  - Creates the historical_data/ file if it doesn't exist yet.

Usage:
    python daily_ingest.py                          # yesterday's games, all leagues
    python daily_ingest.py --date 2026-03-11        # specific date
    python daily_ingest.py --date 2026-03-11 --date 2026-03-10  # multiple dates
    python daily_ingest.py --days 3                 # last 3 days
    python daily_ingest.py --leagues nba nhl        # specific leagues only
    python daily_ingest.py --no-players             # skip player stats (faster)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

# --- Shared ESPN API layer ---------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_matches import (
    LEAGUES,
    ESPNRequester,
    fetch_scoreboard,
    fetch_odds,
    fetch_win_probabilities,
    _pick_provider,
    _parse_int_odds,
    _parse_float,
    _status_state,
    _estimate_team_totals,
)

# --- Enrichment layer (player stats + file helpers) -------------------------
from enrich_players import (
    DATA_DIR,
    SPORT_FILE_PREFIX,
    find_sport_file,
    ensure_sport_file,
    enrich_file,
)


# ---------------------------------------------------------------------------
# File management (ingest-specific helpers; file-finding imported from enrich_players)
# ---------------------------------------------------------------------------


def load_existing_ids(path: str) -> set[str]:
    """Load the set of event_ids already present in a data file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(g.get("event_id", "")) for g in data if g.get("event_id")}
    except (json.JSONDecodeError, FileNotFoundError):
        return set()


def append_games(path: str, new_games: list[dict]) -> None:
    """Load existing file, append new game dicts, save atomically."""
    try:
        with open(path, encoding="utf-8") as f:
            existing: list[dict] = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        existing = []
    existing.extend(new_games)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Game parsing — builds the same dict format as fetch_matches.py
# ---------------------------------------------------------------------------

def parse_game_to_dict(
    http: ESPNRequester,
    sport: str,
    league: str,
    league_key: str,
    event: dict,
) -> dict | None:
    """Parse a single ESPN event into the standard game record dict (without players).
    Player data is filled in separately by enrich_players.enrich_file()."""
    event_id = event.get("id", "")
    name = event.get("name", "")
    short_name = event.get("shortName", name)
    event_date = event.get("date", "")

    comps = event.get("competitions", [])
    if not comps:
        return None

    comp = comps[0]
    competition_id = comp.get("id", event_id)
    state, detail, period, clock = _status_state(comp)

    # Only ingest finished games
    if state != "post":
        return None

    # Teams
    competitors = comp.get("competitors", [])
    home_raw = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away_raw = next((c for c in competitors if c.get("homeAway") == "away"), None)

    def _team(raw: dict | None) -> tuple[str, str, str, str | None, bool | None]:
        if not raw:
            return "", "", "", None, None
        t = raw.get("team", {})
        return (
            t.get("id", ""),
            t.get("displayName", t.get("name", "")),
            t.get("abbreviation", ""),
            str(raw.get("score", "")) if raw.get("score") is not None else None,
            raw.get("winner"),
        )

    home_id, home_name, home_abbr, home_score, home_winner = _team(home_raw)
    away_id, away_name, away_abbr, away_score, away_winner = _team(away_raw)

    # Odds
    odds_items = fetch_odds(http, sport, league, event_id, competition_id)
    chosen = _pick_provider(odds_items)

    provider_name = provider_id = None
    game_total = over_odds = under_odds = None
    open_spread = open_total = None
    home_ml = away_ml = home_spread = away_spread = None
    home_spread_odds = away_spread_odds = None
    home_tt = away_tt = None
    home_tt_over = home_tt_under = away_tt_over = away_tt_under = None
    draw_odds_val = None

    if chosen:
        prov = chosen.get("provider", {})
        provider_name = prov.get("name")
        provider_id = str(prov.get("id", ""))
        game_total = _parse_float(chosen.get("overUnder"))
        over_odds = _parse_int_odds(chosen.get("overOdds"))
        under_odds = _parse_int_odds(chosen.get("underOdds"))
        draw_odds_val = _parse_int_odds(
            chosen.get("drawOdds") or (chosen.get("draw") or {}).get("moneyLine")
        )
        raw_spread = _parse_float(chosen.get("spread"))
        h_o = chosen.get("homeTeamOdds", {})
        a_o = chosen.get("awayTeamOdds", {})
        home_ml = _parse_int_odds(h_o.get("moneyLine"))
        away_ml = _parse_int_odds(a_o.get("moneyLine"))
        home_spread_odds = _parse_int_odds(h_o.get("spreadOdds"))
        away_spread_odds = _parse_int_odds(a_o.get("spreadOdds"))
        if raw_spread is not None:
            if h_o.get("favorite", False):
                home_spread, away_spread = -abs(raw_spread), abs(raw_spread)
            else:
                home_spread, away_spread = abs(raw_spread), -abs(raw_spread)
        # Opening lines
        open_d = chosen.get("open", {})
        open_spread = _parse_float(
            open_d.get("spread", {}).get("value") if isinstance(open_d.get("spread"), dict)
            else open_d.get("spread")
        )
        open_total = _parse_float(
            open_d.get("over", {}).get("value") if isinstance(open_d.get("over"), dict)
            else open_d.get("overUnder")
        )
        # Team totals
        h_tt = h_o.get("teamTotal", {})
        a_tt = a_o.get("teamTotal", {})
        if isinstance(h_tt, dict):
            home_tt = _parse_float(h_tt.get("overUnder") or h_tt.get("total"))
            home_tt_over = _parse_int_odds(h_tt.get("overOdds"))
            home_tt_under = _parse_int_odds(h_tt.get("underOdds"))
        if isinstance(a_tt, dict):
            away_tt = _parse_float(a_tt.get("overUnder") or a_tt.get("total"))
            away_tt_over = _parse_int_odds(a_tt.get("overOdds"))
            away_tt_under = _parse_int_odds(a_tt.get("underOdds"))
        if game_total is not None and home_tt is None and away_tt is None:
            home_tt, away_tt = _estimate_team_totals(game_total, home_ml, away_ml)

    # Win probability
    prob = fetch_win_probabilities(http, sport, league, event_id, competition_id)
    home_win_pct = _parse_float(prob.get("homeWinPercentage"))
    away_win_pct = _parse_float(prob.get("awayWinPercentage"))

    # Players
    # Build the standard dict record (players filled in by enrich_file below)
    return {
        "event_id":       event_id,
        "name":           name,
        "short_name":     short_name,
        "date":           event_date,
        "status":         state,
        "status_detail":  detail,
        "period":         period,
        "clock":          clock,
        "sport":          sport,
        "league":         league,
        "provider":       provider_name,
        "provider_id":    provider_id,
        "home": {
            "team_id":              home_id,
            "team_name":            home_name,
            "team_abbr":            home_abbr,
            "home_away":            "home",
            "score":                home_score,
            "is_winner":            home_winner,
            "moneyline":            home_ml,
            "spread":               home_spread,
            "spread_odds":          home_spread_odds,
            "team_total":           home_tt,
            "team_total_over_odds": home_tt_over,
            "team_total_under_odds": home_tt_under,
        },
        "away": {
            "team_id":              away_id,
            "team_name":            away_name,
            "team_abbr":            away_abbr,
            "home_away":            "away",
            "score":                away_score,
            "is_winner":            away_winner,
            "moneyline":            away_ml,
            "spread":               away_spread,
            "spread_odds":          away_spread_odds,
            "team_total":           away_tt,
            "team_total_over_odds": away_tt_over,
            "team_total_under_odds": away_tt_under,
        },
        "game_total":     game_total,
        "over_odds":      over_odds,
        "under_odds":     under_odds,
        "open_spread":    open_spread,
        "open_total":     open_total,
        "home_win_pct":   home_win_pct,
        "away_win_pct":   away_win_pct,
        "draw_odds":      draw_odds_val,
        "players":        [],
        "formations":     {},
        "raw_odds":       odds_items,
    }


# ---------------------------------------------------------------------------
# Core ingest logic
# ---------------------------------------------------------------------------

def ingest_date(
    http: ESPNRequester,
    target_date: date,
    league_keys: list[str],
    fetch_players: bool,
    data_dir: str,
) -> None:
    """Fetch all completed games for target_date and append new ones to data files."""
    date_str = target_date.strftime("%Y%m%d")
    date_label = target_date.strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  Ingesting  {date_label}")
    print(f"{'='*60}")

    # Group league_keys by their data file prefix so we open each file once
    prefix_to_keys: dict[str, list[str]] = {}
    for lk in league_keys:
        prefix = SPORT_FILE_PREFIX.get(lk, lk)
        prefix_to_keys.setdefault(prefix, []).append(lk)

    for prefix, keys in prefix_to_keys.items():
        sport_file = ensure_sport_file(keys[0], data_dir)
        existing_ids = load_existing_ids(sport_file)
        new_games: list[dict] = []

        for league_key in keys:
            sport, league = LEAGUES[league_key]
            print(f"\n  [{league_key.upper():<12}] Fetching {date_label} ... ", end="", flush=True)
            try:
                events = fetch_scoreboard(http, sport, league, date_str)
            except Exception as exc:
                print(f"ERROR: {exc}")
                continue

            finished = [e for e in events if
                        e.get("competitions", [{}])[0].get("status", {})
                        .get("type", {}).get("state") == "post"
                        and e.get("id") not in existing_ids]
            print(f"{len(finished)} new finished games (of {len(events)} total)")

            for i, event in enumerate(finished, 1):
                eid = event.get("id", "")
                short = event.get("shortName", eid)
                print(f"    [{i}/{len(finished)}] {short[:40]:<40} ... ", end="", flush=True)
                try:
                    rec = parse_game_to_dict(http, sport, league, league_key, event)
                    if rec:
                        new_games.append(rec)
                        existing_ids.add(eid)
                        print("OK")
                    else:
                        print("skipped (not finished)")
                except Exception as exc:
                    print(f"ERROR: {exc}")

        if new_games:
            append_games(sport_file, new_games)
            print(f"\n  Appended {len(new_games)} games → {sport_file}")
            if fetch_players:
                print(f"  Enriching player stats via enrich_players …")
                enrich_file(http, sport_file, verbose=True)
        else:
            print(f"\n  Nothing new to append for prefix '{prefix}' on {date_label}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daily incremental ingest: append new finished games to historical_data/ files.",
    )
    p.add_argument(
        "--date",
        action="append",
        dest="dates",
        metavar="YYYY-MM-DD",
        help="Specific date(s) to ingest (can be repeated). Default: yesterday.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=0,
        metavar="N",
        help="Ingest the last N days (alternative to --date). Default: 1 (yesterday).",
    )
    p.add_argument(
        "--leagues",
        nargs="+",
        choices=list(LEAGUES.keys()),
        default=list(LEAGUES.keys()),
        metavar="LEAGUE",
        help=f"Leagues to ingest (default: all). Choices: {', '.join(LEAGUES.keys())}",
    )
    p.add_argument(
        "--no-players",
        action="store_true",
        help="Skip player stat fetching (faster, for quick data checks).",
    )
    p.add_argument(
        "--data-dir",
        default=DATA_DIR,
        metavar="DIR",
        help=f"Directory containing sport JSON files (default: {DATA_DIR})",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    today = date.today()

    # Build list of target dates
    target_dates: list[date] = []
    if args.dates:
        for ds in args.dates:
            try:
                target_dates.append(date.fromisoformat(ds))
            except ValueError:
                print(f"[error] invalid date '{ds}', expected YYYY-MM-DD", file=sys.stderr)
                sys.exit(1)
    elif args.days > 0:
        for offset in range(args.days, 0, -1):
            target_dates.append(today - timedelta(days=offset))
    else:
        # Default: yesterday
        target_dates = [today - timedelta(days=1)]

    print(f"\n  Daily Ingest — {len(target_dates)} date(s) — {len(args.leagues)} league(s)")
    print(f"  Dates   : {[str(d) for d in target_dates]}")
    print(f"  Leagues : {', '.join(args.leagues)}")
    print(f"  Players : {'yes' if not args.no_players else 'no'}")
    print(f"  Data dir: {os.path.abspath(args.data_dir)}")

    with ESPNRequester() as http:
        for d in target_dates:
            ingest_date(http, d, args.leagues, not args.no_players, args.data_dir)


if __name__ == "__main__":
    main()
