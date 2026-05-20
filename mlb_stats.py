"""
mlb_stats.py
============
Thin client for the official MLB Stats API (statsapi.mlb.com).
Settles MLB prop bets not available in ESPN data:
  - player_strikeouts  (pitching.strikeOuts)
  - player_earned_runs (pitching.earnedRuns)
    - player_outs        (pitching.outs)

No API key required.
Called by stats_api.py when market is in MLB_PROP_STAT_MAP.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Optional

import httpx

_log = logging.getLogger(__name__)

_BASE    = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = 2.5  # sports_bridge gives stats_api 3s total; MLB call must finish first

# market name → (boxscore stat category, stat key)
MLB_PROP_STAT_MAP: dict[str, tuple[str, str]] = {
    "player_strikeouts":  ("pitching", "strikeOuts"),
    "player_earned_runs": ("pitching", "earnedRuns"),
    "player_outs":        ("pitching", "outs"),
}


def _get(url: str, params: dict | None = None) -> dict:
    resp = httpx.get(url, params=params, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def find_game(game_date: str, team_name: Optional[str] = None) -> Optional[dict[str, Any]]:
    """
    Return {"game_pk": int, "status": "Final"|"Live"|"Preview"} for an MLB game
    on game_date (YYYY-MM-DD), optionally narrowed by team name substring.
    Returns None if no game found.
    """
    try:
        data = _get(f"{_BASE}/schedule", params={
            "sportId":  1,
            "date":     game_date,
            "gameType": "R",
        })
    except Exception as exc:
        _log.warning("mlb schedule fetch failed: %s", exc)
        return None

    games: list[dict] = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))

    if not games:
        return None

    def _game_info(g: dict) -> dict:
        return {
            "game_pk": g["gamePk"],
            "status":  g.get("status", {}).get("abstractGameState", "unknown"),
        }

    if team_name:
        name_lower = team_name.lower()
        for g in games:
            home = g.get("teams", {}).get("home", {}).get("team", {}).get("name", "").lower()
            away = g.get("teams", {}).get("away", {}).get("team", {}).get("name", "").lower()
            if (name_lower in home or name_lower in away
                    or home in name_lower or away in name_lower):
                return _game_info(g)

    # No team match or no team filter — return first game
    return _game_info(games[0])


def _collect_players(boxscore: dict, stat_category: str, stat_key: str) -> list[tuple[str, float]]:
    """Walk both sides of a boxscore and return [(fullName, stat_value)] for players
    who have a non-None value for stat_key in the given stat category."""
    results: list[tuple[str, float]] = []
    for side in ("home", "away"):
        for _pid, pdata in boxscore.get("teams", {}).get(side, {}).get("players", {}).items():
            full_name = pdata.get("person", {}).get("fullName", "")
            raw = pdata.get("stats", {}).get(stat_category, {}).get(stat_key)
            if raw is not None:
                try:
                    results.append((full_name, float(raw)))
                except (TypeError, ValueError):
                    pass
    return results


def _match_player(
    player_name: str,
    candidates: list[tuple[str, float]],
) -> Optional[tuple[str, float]]:
    """Fuzzy-match player_name against a list of (fullName, value) tuples."""
    names = [n for n, _ in candidates]

    # 1. difflib fuzzy
    matches = difflib.get_close_matches(player_name, names, n=1, cutoff=0.5)
    if matches:
        matched = matches[0]
        return next((n, v) for n, v in candidates if n == matched)

    # 2. substring fallback
    pl = player_name.lower()
    for name, val in candidates:
        if pl in name.lower() or name.lower() in pl:
            return name, val

    return None


def prop_check(
    player:    str,
    market:    str,
    game_date: str,
    team:      Optional[str] = None,
) -> dict[str, Any]:
    """
    Look up a player's MLB stat for the given market on game_date.

    Returns a dict with keys:
      found       bool
      player      str   (matched display name, if found)
      stat_value  float (if found)
      stat_key    str   (MLB API field name, if found)
      game_pk     int
      game_status str   ("Final" | "Live" | "Preview" | "unknown")
      settled     bool
      source      str   always "mlb_stats_api"
      note        str   (set on failure paths)
    """
    market_norm = market.strip().lower().replace(" ", "_")

    if market_norm not in MLB_PROP_STAT_MAP:
        return {
            "found":  False,
            "note":   f"Market '{market_norm}' is not in MLB_PROP_STAT_MAP.",
            "source": "mlb_stats_api",
        }

    stat_category, stat_key = MLB_PROP_STAT_MAP[market_norm]

    # ── 1. Find game ──────────────────────────────────────────────────────────
    game_info = find_game(game_date, team)
    if game_info is None:
        return {
            "found":  False,
            "note":   f"No MLB game found for date={game_date}, team={team!r}.",
            "source": "mlb_stats_api",
        }

    game_pk     = game_info["game_pk"]
    game_status = game_info["status"]
    settled     = (game_status == "Final")

    # ── 2. Fetch boxscore ─────────────────────────────────────────────────────
    try:
        boxscore = _get(f"{_BASE}/game/{game_pk}/boxscore")
    except Exception as exc:
        _log.warning("mlb boxscore fetch failed game_pk=%s: %s", game_pk, exc)
        return {
            "found":       False,
            "game_pk":     game_pk,
            "game_status": game_status,
            "settled":     settled,
            "note":        f"MLB boxscore fetch failed: {exc}",
            "source":      "mlb_stats_api",
        }

    candidates = _collect_players(boxscore, stat_category, stat_key)

    if not candidates:
        return {
            "found":       False,
            "game_pk":     game_pk,
            "game_status": game_status,
            "settled":     settled,
            "note":        (
                "No pitching stats in boxscore yet — "
                "game may not be final or stat not available."
            ),
            "source":      "mlb_stats_api",
        }

    # ── 3. Match player ───────────────────────────────────────────────────────
    match = _match_player(player, candidates)
    if match is None:
        available = [n for n, _ in candidates]
        return {
            "found":              False,
            "game_pk":            game_pk,
            "game_status":        game_status,
            "settled":            settled,
            "note":               f"Player '{player}' not found in MLB boxscore.",
            "available_pitchers": available,
            "source":             "mlb_stats_api",
        }

    matched_name, stat_value = match
    return {
        "found":       True,
        "player":      matched_name,
        "stat_value":  stat_value,
        "stat_key":    stat_key,
        "game_pk":     game_pk,
        "game_status": game_status,
        "settled":     settled,
        "source":      "mlb_stats_api",
    }
