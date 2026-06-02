"""
enrich_players.py
=================
Loads each sport JSON in historical_data/ and fills in player box-score data
for every finished/live game that currently has an empty 'players' list.
Processes one sport file at a time so progress is saved after each one.

Usage:
    python enrich_players.py                          # all files in historical_data/
    python enrich_players.py --file historical_data/nba.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

# ---------------------------------------------------------------------------
# Shared ESPN http client + fetch_summary — imported from fetch_matches.py
# (avoids duplicating retry/backoff/client logic across scripts)
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_matches import ESPNRequester, fetch_summary  # noqa: E402

# ---------------------------------------------------------------------------
# Config — CANONICAL source used by daily_ingest.py and realtime_monitor.py
# ---------------------------------------------------------------------------

DATA_DIR = "historical_data"

# Maps league_key → data-file prefix in data/
SPORT_FILE_PREFIX: dict[str, str] = {
    "nba":        "nba",
    "wnba":       "nba",
    "ncaab":      "ncaab",
    "nhl":        "nhl",
    "nfl":        "nfl",
    "ncaaf":      "ncaaf",
    "mlb":        "mlb",
    "epl":        "soccer",
    "laliga":     "soccer",
    "bundesliga": "soccer",
    "ligue1":     "soccer",
    "ucl":        "soccer",
    "uel":        "soccer",
    "mls":        "soccer",
    # ---- Cricket (all leagues share one history file) ----------------------
    "ipl":            "cricket",
    "cricket_t20q":   "cricket",
    "cricket_sa":     "cricket",
    "cricket_shield": "cricket",
    "cricket_bbl":    "cricket",
    "cricket_tri":    "cricket",
    "cricket_bpl":    "cricket",
    "cricket_bcl":    "cricket",
    "cricket":        "cricket",  # legacy single-key fallback
}

# ---------------------------------------------------------------------------
# Sport file helpers — shared across all scripts
# ---------------------------------------------------------------------------

def find_sport_file(league_key: str, data_dir: str = DATA_DIR) -> str | None:
    """Return the data file for this league's prefix (e.g. historical_data/nba.json)."""
    prefix = SPORT_FILE_PREFIX.get(league_key, league_key)
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"{prefix}.json")
    return path if os.path.exists(path) else None


def ensure_sport_file(league_key: str, data_dir: str = DATA_DIR) -> str:
    """Return existing data file path, or create a fresh empty one."""
    existing = find_sport_file(league_key, data_dir)
    if existing:
        return existing
    prefix = SPORT_FILE_PREFIX.get(league_key, league_key)
    path = os.path.join(data_dir, f"{prefix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([], f)
    return path


def parse_players(summary: dict, home_id: str, away_id: str) -> list[dict]:
    players = []
    for group in summary.get("boxscore", {}).get("players", []):
        team = group.get("team", {})
        team_id = str(team.get("id", ""))
        team_abbr = team.get("abbreviation", "")
        home_away = "home" if team_id == str(home_id) else "away"
        for stat_group in group.get("statistics", []):
            # NHL uses "labels"; NBA/MLB/NCAAB use "names"; fall back gracefully
            names: list[str] = stat_group.get("names") or stat_group.get("labels", [])
            for ae in stat_group.get("athletes", []):
                ath = ae.get("athlete", {})
                raw_stats: list[str] = ae.get("stats", [])
                players.append({
                    "player_id":    str(ath.get("id", "")),
                    "display_name": ath.get("displayName", ath.get("shortName", "")),
                    "jersey":       ath.get("jersey", ""),
                    "position":     ath.get("position", {}).get("abbreviation", ""),
                    "team_abbr":    team_abbr,
                    "home_away":    home_away,
                    "starter":      ae.get("starter", False),
                    "active":       ae.get("active", True),
                    "did_not_play": ae.get("didNotPlay", False),
                    "dnp_reason":   ae.get("reason", ""),
                    "stats":        dict(zip(names, raw_stats)),
                })
    return players


def parse_soccer_roster(summary: dict) -> tuple[list[dict], dict[str, str]]:
    """
    Parse ESPN soccer summary rosters into a flat player list + formation map.
    Returns (players, formations) where formations = {"home": "4-3-3", "away": "4-2-3-1"}.
    """
    players: list[dict] = []
    formations: dict[str, str] = {}

    for roster_entry in summary.get("rosters", []):
        home_away  = roster_entry.get("homeAway", "home")
        team       = roster_entry.get("team", {})
        team_abbr  = team.get("abbreviation", "")
        formation  = roster_entry.get("formation", "")
        if formation:
            formations[home_away] = formation

        for ae in roster_entry.get("roster", []):
            ath = ae.get("athlete", {})
            pos = ae.get("position", {})
            # Build stats dict from the list of stat objects
            stats = {
                s["abbreviation"]: s.get("displayValue", str(s.get("value", "")))
                for s in ae.get("stats", [])
            }
            players.append({
                "player_id":       str(ath.get("id", "")),
                "display_name":    ath.get("displayName", ath.get("shortName", "")),
                "jersey":          ae.get("jersey", ""),
                "position":        pos.get("abbreviation", "") if isinstance(pos, dict) else "",
                "position_name":   pos.get("name", "") if isinstance(pos, dict) else "",
                "team_abbr":       team_abbr,
                "home_away":       home_away,
                "starter":         ae.get("starter", False),
                "active":          ae.get("active", True),
                "subbed_in":       ae.get("subbedIn", False),
                "subbed_out":      ae.get("subbedOut", False),
                "formation_place": ae.get("formationPlace", ""),
                "did_not_play":    False,
                "dnp_reason":      "",
                "stats":           stats,
            })

    return players, formations


def parse_cricket_roster(summary: dict) -> list[dict]:
    """Parse ESPN cricket summary into a flat player list with innings-based stats.

    Batting keys: BAT_I{n}_RUNS / BALLS / 4S / 6S / SR / DISMISSAL
    Bowling keys: BWL_I{n}_OVERS / MAIDENS / RUNS / WICKETS / ECONOMY / NBW
    """
    player_map: dict[str, dict] = {}

    for entry in summary.get("rosters", []):
        home_away = entry.get("homeAway", "home")
        team = entry.get("team", {})
        team_abbr = team.get("abbreviation", "")
        for ae in entry.get("roster", []):
            ath = ae.get("athlete", {})
            player_id = str(ath.get("id", ""))
            if not player_id:
                continue
            pos = ae.get("position", {})
            player_map[player_id] = {
                "player_id":    player_id,
                "display_name": ath.get("displayName", ath.get("shortName", "")),
                "jersey":       "",
                "position":     pos.get("abbreviation", "") if isinstance(pos, dict) else "",
                "team_abbr":    team_abbr,
                "home_away":    home_away,
                "starter":      True,
                "active":       True,
                "did_not_play": False,
                "dnp_reason":   "",
                "stats":        {},
            }

    for card in summary.get("matchcards", []):
        type_id = card.get("typeID")
        innings = card.get("inningsNumber", 1)
        inn_tag = f"I{innings}"

        if str(type_id) == "11":  # batting
            for row in card.get("playerDetails", []):
                pid = str(row.get("playerID", ""))
                if not pid:
                    continue
                if pid not in player_map:
                    player_map[pid] = {
                        "player_id": pid,
                        "display_name": str(row.get("playerName", "")),
                        "jersey": "", "position": "", "team_abbr": "", "home_away": "",
                        "starter": False, "active": True, "did_not_play": False,
                        "dnp_reason": "", "stats": {},
                    }
                s = player_map[pid]["stats"]
                runs  = row.get("runs")
                balls = row.get("ballsFaced")
                fours = row.get("fours")
                sixes = row.get("sixes")
                s[f"BAT_{inn_tag}_RUNS"]     = str(runs)  if runs  is not None else ""
                s[f"BAT_{inn_tag}_BALLS"]    = str(balls) if balls is not None else ""
                s[f"BAT_{inn_tag}_4S"]       = str(fours) if fours is not None else ""
                s[f"BAT_{inn_tag}_6S"]       = str(sixes) if sixes is not None else ""
                s[f"BAT_{inn_tag}_DISMISSAL"] = str(row.get("dismissal", ""))
                try:
                    b = int(balls or 0)
                    if b > 0:
                        s[f"BAT_{inn_tag}_SR"] = str(round(int(runs or 0) / b * 100, 1))
                except (ValueError, TypeError):
                    pass

        elif str(type_id) == "12":  # bowling
            for row in card.get("playerDetails", []):
                pid = str(row.get("playerID", ""))
                if not pid:
                    continue
                if pid not in player_map:
                    player_map[pid] = {
                        "player_id": pid,
                        "display_name": str(row.get("playerName", "")),
                        "jersey": "", "position": "", "team_abbr": "", "home_away": "",
                        "starter": False, "active": True, "did_not_play": False,
                        "dnp_reason": "", "stats": {},
                    }
                s = player_map[pid]["stats"]
                overs   = row.get("overs")
                maidens = row.get("maidens")
                conceded = row.get("conceded")
                wickets = row.get("wickets")
                economy = row.get("economyRate")
                nbw     = row.get("nbw")
                s[f"BWL_{inn_tag}_OVERS"]   = str(overs)    if overs    is not None else ""
                s[f"BWL_{inn_tag}_MAIDENS"] = str(maidens)  if maidens  is not None else ""
                s[f"BWL_{inn_tag}_RUNS"]    = str(conceded) if conceded is not None else ""
                s[f"BWL_{inn_tag}_WICKETS"] = str(wickets)  if wickets  is not None else ""
                s[f"BWL_{inn_tag}_ECONOMY"] = str(economy)  if economy  is not None else ""
                s[f"BWL_{inn_tag}_NBW"]     = str(nbw)      if nbw      is not None else ""

    return list(player_map.values())


# ---------------------------------------------------------------------------
# Per-game enrichment — usable by daily_ingest, realtime_monitor, etc.
# ---------------------------------------------------------------------------

def enrich_game(http: ESPNRequester, game: dict) -> bool:
    """
    Fetch and fill in player stats for a single game dict in-place.
    Returns True if enrichment succeeded, False otherwise.
    Requires 'event_id', 'sport', 'league', and home/away 'team_id' fields.
    """
    event_id = game.get("event_id", "")
    sport    = game.get("sport", "")
    league   = game.get("league", "")
    home_id  = (game.get("home") or {}).get("team_id", "")
    away_id  = (game.get("away") or {}).get("team_id", "")
    if not (event_id and sport and league):
        return False
    try:
        summary = fetch_summary(http, sport, league, event_id)
        if sport == "soccer":
            players, formations = parse_soccer_roster(summary)
            game["players"]    = players
            game["formations"] = formations
        elif sport == "cricket":
            game["players"] = parse_cricket_roster(summary)
        else:
            game["players"] = parse_players(summary, home_id, away_id)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Enrich one file
# ---------------------------------------------------------------------------

def enrich_file(http: ESPNRequester, path: str, verbose: bool = True) -> None:
    if verbose:
        print(f"\n{'='*60}")
        print(f"  Loading {path} ...")
    with open(path, encoding="utf-8") as f:
        games: list[dict] = json.load(f)

    to_enrich = [
        g for g in games
        if g.get("status") in ("post", "in") and not g.get("players")
    ]
    already_done = sum(1 for g in games if g.get("players"))

    if verbose:
        print(f"  Total games        : {len(games)}")
        print(f"  Already have data  : {already_done}")
        print(f"  Need enrichment    : {len(to_enrich)}")

    if not to_enrich:
        if verbose:
            print("  Nothing to do — skipping.")
        return

    enriched = 0
    failed = 0

    for i, g in enumerate(to_enrich, 1):
        name = g.get("short_name", g.get("event_id", ""))
        if verbose:
            print(f"  [{i}/{len(to_enrich)}] {name[:35]:<35} ... ", end="", flush=True)
        ok = enrich_game(http, g)
        n_players = len(g.get("players") or [])
        if ok:
            enriched += 1
            if verbose:
                print(f"{n_players} players")
        else:
            failed += 1
            if verbose:
                print("FAILED")

        # Save progress every 25 games so nothing is lost on interrupt
        if i % 25 == 0:
            _save(path, games)
            if verbose:
                print(f"    [checkpoint] saved after {i} games")

    _save(path, games)
    if verbose:
        print(f"\n  Done — enriched {enriched}, failed {failed}. Saved to {path}")


def _save(path: str, games: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Enrich sport JSON files with player box-score data")
    p.add_argument("--file", type=str, default=None, help="Single file to enrich (default: all files in data/)")
    args = p.parse_args()

    if args.file:
        files = [args.file]
    else:
        data_dir = DATA_DIR
        files = sorted(
            os.path.join(data_dir, f)
            for f in os.listdir(data_dir)
            if f.endswith(".json")
        )

    print(f"Files to process: {[os.path.basename(f) for f in files]}")

    with ESPNRequester() as http:
        for path in files:
            enrich_file(http, path)

    print("\n\nAll files enriched.")


if __name__ == "__main__":
    main()
