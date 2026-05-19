"""
mlb_period_props.py
===================
Extended MLB prop settlement for inning-by-inning and period-based markets.

Handles:
  - Inning totals (1st-9th inning total runs + odd/even)
  - Period totals (1st 3, 1st 7, 1st half)
  - Period moneylines and run lines
    - MLB batting props from Baseball Savant boxscore data
  - Team totals by period

Data sources:
  - find_game()  → statsapi.mlb.com/api/v1/schedule  (no auth, finds game_pk)
  - _get_gf()    → baseballsavant.mlb.com/gf?game_pk  (one call: linescore + boxscore)

MLB Stats API boxscore does NOT include linescore innings; Baseball Savant /gf
returns scoreboard.linescore.innings AND boxscore.teams in a single response.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Optional

import httpx

_log = logging.getLogger(__name__)

_SCHEDULE_BASE = "https://statsapi.mlb.com/api/v1"
_SAVANT_BASE = "https://baseballsavant.mlb.com"
_TIMEOUT = 2.5
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Market name → (inning_range, market_type)
# inning_range: (start_inning_0indexed, end_inning_0indexed) or "game"
# market_type: "total_runs" | "moneyline" | "run_line" | "team_total" | "odd_even"
PERIOD_PROP_STAT_MAP: dict[str, tuple] = {
    # Inning-by-inning totals (0-indexed, so inning 1 = index 0)
    "1st_inning_total_runs": ((0, 0), "total_runs"),
    "2nd_inning_total_runs": ((1, 1), "total_runs"),
    "3rd_inning_total_runs": ((2, 2), "total_runs"),
    "4th_inning_total_runs": ((3, 3), "total_runs"),
    "5th_inning_total_runs": ((4, 4), "total_runs"),
    "6th_inning_total_runs": ((5, 5), "total_runs"),
    "7th_inning_total_runs": ((6, 6), "total_runs"),
    "8th_inning_total_runs": ((7, 7), "total_runs"),
    "9th_inning_total_runs": ((8, 8), "total_runs"),
    "1st_inning_total_runs_odd_even": ((0, 0), "odd_even"),
    # Multi-inning periods
    "1st_3_innings_total_runs": ((0, 2), "total_runs"),
    "1st_7_innings_total_runs": ((0, 6), "total_runs"),
    "1st_half_total_runs": ((0, 4), "total_runs"),  # through 5 innings
    # Period moneylines (requires both team scores)
    "1st_inning_moneyline": ((0, 0), "moneyline"),
    "1st_3_innings_moneyline": ((0, 2), "moneyline"),
    "1st_7_innings_moneyline": ((0, 6), "moneyline"),
    "1st_half_moneyline": ((0, 4), "moneyline"),
    # Period run lines (requires both team scores)
    "1st_inning_run_line": ((0, 0), "run_line"),
    "1st_3_innings_run_line": ((0, 2), "run_line"),
    "1st_7_innings_run_line": ((0, 6), "run_line"),
    "1st_half_run_line": ((0, 4), "run_line"),
    # Period team totals
    "1st_half_team_total": ((0, 4), "team_total"),
    # Full-game markets
    "total_runs": ("game", "total_runs"),
    "total_runs_odd_even": ("game", "odd_even"),
    "moneyline": ("game", "moneyline"),
    "run_line": ("game", "run_line"),
    "team_total": ("game", "team_total"),
    # Player-level extensions
    "player_runs": (None, "player_runs"),
    "player_home_runs": (None, "player_home_runs"),
    "player_doubles": (None, "player_doubles"),
    "player_triples": (None, "player_triples"),
    "player_bases": (None, "player_bases"),  # total bases
    "player_singles": (None, "player_singles"),
    "player_hits_runs_rbis": (None, "player_hits_runs_rbis"),
}


def _get(url: str, params: dict | None = None) -> dict:
    """HTTP GET with timeout."""
    resp = httpx.get(
        url, params=params, timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS
    )
    resp.raise_for_status()
    return resp.json()


def find_game(
    game_date: str, team_name: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """
    Return {"game_pk": int, "status": "Final"|"Live"|"Preview"} for an MLB game
    on game_date (YYYY-MM-DD), optionally narrowed by team name substring.
    Uses statsapi.mlb.com schedule endpoint (reliable, no User-Agent required).
    """
    try:
        data = _get(
            f"{_SCHEDULE_BASE}/schedule",
            params={
                "sportId": 1,
                "date": game_date,
                "gameType": "R",
            },
        )
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
            "status": g.get("status", {}).get("abstractGameState", "unknown"),
        }

    if team_name:
        name_lower = team_name.lower()
        for g in games:
            home = (
                g.get("teams", {})
                .get("home", {})
                .get("team", {})
                .get("name", "")
                .lower()
            )
            away = (
                g.get("teams", {})
                .get("away", {})
                .get("team", {})
                .get("name", "")
                .lower()
            )
            if (
                name_lower in home
                or name_lower in away
                or home in name_lower
                or away in name_lower
            ):
                return _game_info(g)

    return _game_info(games[0])


def _get_gf(game_pk: int) -> dict:
    """
    Fetch Baseball Savant /gf?game_pk=X.

    Returns a dict with:
      gf["scoreboard"]["linescore"]["innings"] — per-inning run data
      gf["boxscore"]["teams"]                  — player stats (same structure as MLB Stats API boxscore)
      gf["game_status_code"]                   — "F" for Final, "L" for Live, etc.
    """
    return _get(f"{_SAVANT_BASE}/gf", params={"game_pk": game_pk})


def _extract_inning_runs(
    innings: list, inning_range: tuple[int, int]
) -> tuple[int, int]:
    """
    Extract total runs for both teams across inning range [start, end] (inclusive, 0-indexed).
    innings comes from gf["scoreboard"]["linescore"]["innings"].
    Returns (home_runs, away_runs).
    """
    home_runs = 0
    away_runs = 0
    start, end = inning_range
    for i in range(start, min(end + 1, len(innings))):
        inning = innings[i]
        home_runs += inning.get("home", {}).get("runs", 0) or 0
        away_runs += inning.get("away", {}).get("runs", 0) or 0
    return home_runs, away_runs


def _extract_game_runs(innings: list) -> tuple[int, int]:
    """Extract full-game runs by summing all available innings in the feed."""
    if not innings:
        return 0, 0
    return _extract_inning_runs(innings, (0, len(innings) - 1))


def _is_odd(n: int) -> bool:
    return n % 2 == 1


def _collect_hitters(boxscore: dict) -> dict[str, dict]:
    """
    Walk boxscore.teams hitters (from gf["boxscore"]).
    Returns {fullName: batting-stat dict}.
    Baseball Savant boxscore includes totalBases directly, so no calculation needed.
    """
    hitters: dict[str, dict] = {}
    for side in ("home", "away"):
        for _pid, pdata in (
            boxscore.get("teams", {}).get(side, {}).get("players", {}).items()
        ):
            full_name = pdata.get("person", {}).get("fullName", "")
            if not full_name:
                continue
            batting = pdata.get("stats", {}).get("batting", {})
            hits = batting.get("hits", 0) or 0
            doubles = batting.get("doubles", 0) or 0
            triples = batting.get("triples", 0) or 0
            home_runs = batting.get("homeRuns", 0) or 0
            total_bases = batting.get("totalBases", 0) or 0  # direct field in Savant
            singles = max(0, hits - doubles - triples - home_runs)
            hitters[full_name] = {
                "runs": batting.get("runs", 0) or 0,
                "hits": hits,
                "rbis": batting.get("rbi", 0) or 0,
                "total_bases": total_bases,
                "singles": singles,
                "home_runs": home_runs,
                "doubles": doubles,
                "triples": triples,
                "hits_runs_rbis": (hits + (batting.get("runs", 0) or 0) + (batting.get("rbi", 0) or 0)),
            }
    return hitters


def _match_player(player_name: str, candidates: dict[str, Any]) -> Optional[str]:
    """Fuzzy-match player_name against a dict of player names."""
    names = list(candidates.keys())

    # 1. difflib fuzzy
    matches = difflib.get_close_matches(player_name, names, n=1, cutoff=0.5)
    if matches:
        return matches[0]

    # 2. substring fallback
    pl = player_name.lower()
    for name in names:
        if pl in name.lower() or name.lower() in pl:
            return name

    return None


_PLAYER_PROP_FIELDS: dict[str, str] = {
    "player_runs": "runs",
    "player_home_runs": "home_runs",
    "player_doubles": "doubles",
    "player_triples": "triples",
    "player_bases": "total_bases",
    "player_singles": "singles",
    "player_hits_runs_rbis": "hits_runs_rbis",
}


def prop_check(
    player: str,
    market: str,
    game_date: str,
    team: Optional[str] = None,
    pick: Optional[str] = None,  # for moneyline/run_line
    line: Optional[float] = None,  # for run_line/total
) -> dict[str, Any]:
    """
    Settle a period-based MLB prop market.

    Returns a dict with keys:
      found        bool
      stat_value   float (if found)
      game_pk      int
      game_status  str   ("Final" | "Live" | "Preview" | "unknown")
      settled      bool
      source       str   always "mlb_period_props"
      note         str   (set on failure paths)
    """
    market_norm = market.strip().lower().replace(" ", "_")

    if market_norm not in PERIOD_PROP_STAT_MAP:
        return {
            "found": False,
            "note": f"Market '{market_norm}' is not in PERIOD_PROP_STAT_MAP.",
            "source": "mlb_period_props",
        }

    inning_range, market_type = PERIOD_PROP_STAT_MAP[market_norm]

    # ── Find game_pk ─────────────────────────────────────────────────────────
    game_info = find_game(game_date, team)
    if game_info is None:
        return {
            "found": False,
            "note": f"No MLB game found for date={game_date}, team={team!r}.",
            "source": "mlb_period_props",
        }

    game_pk = game_info["game_pk"]
    game_status = game_info["status"]
    settled = game_status == "Final"

    # ── Fetch Baseball Savant /gf (linescore + boxscore in one call) ─────────
    try:
        gf = _get_gf(game_pk)
    except Exception as exc:
        _log.warning("savant gf fetch failed game_pk=%s: %s", game_pk, exc)
        return {
            "found": False,
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": None,
            "away_score": None,
            "settled": settled,
            "note": f"Baseball Savant /gf fetch failed: {exc}",
            "source": "mlb_period_props",
        }

    innings = gf.get("scoreboard", {}).get("linescore", {}).get("innings", [])
    boxscore = gf.get("boxscore", {})

    # Extract final game score
    home_score, away_score = _extract_game_runs(innings)

    # Refine settled status from Savant (more reliable than schedule)
    if gf.get("game_status_code") == "F":
        settled = True
        game_status = "Final"

    # ── Handler by market type ────────────────────────────────────────────────

    if market_type == "total_runs":
        # Return total runs in the inning(s)
        if inning_range == "game":
            home_runs, away_runs = _extract_game_runs(innings)
        else:
            home_runs, away_runs = _extract_inning_runs(innings, inning_range)
        total = home_runs + away_runs

        return {
            "found": True,
            "stat_value": total,
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": home_score,
            "away_score": away_score,
            "settled": settled,
            "source": "mlb_period_props",
        }

    elif market_type == "odd_even":
        # Return 1 if odd, 0 if even
        if inning_range == "game":
            home_runs, away_runs = _extract_game_runs(innings)
        else:
            home_runs, away_runs = _extract_inning_runs(innings, inning_range)
        total = home_runs + away_runs
        result = 1 if _is_odd(total) else 0

        return {
            "found": True,
            "stat_value": result,  # 1 = odd, 0 = even
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": home_score,
            "away_score": away_score,
            "settled": settled,
            "source": "mlb_period_props",
        }

    elif market_type == "moneyline":
        # Return home win prob (> 0.5 if home ahead, < 0.5 if away ahead)
        if inning_range == "game":
            home_runs, away_runs = _extract_game_runs(innings)
        else:
            home_runs, away_runs = _extract_inning_runs(innings, inning_range)

        if home_runs > away_runs:
            result = 1.0  # home won the period
        elif away_runs > home_runs:
            result = 0.0  # away won the period
        else:
            result = 0.5  # tie

        return {
            "found": True,
            "stat_value": result,
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": home_score,
            "away_score": away_score,
            "settled": settled,
            "source": "mlb_period_props",
        }

    elif market_type == "run_line":
        # Return run differential (home - away)
        if inning_range == "game":
            home_runs, away_runs = _extract_game_runs(innings)
        else:
            home_runs, away_runs = _extract_inning_runs(innings, inning_range)
        diff = home_runs - away_runs

        return {
            "found": True,
            "stat_value": diff,
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": home_score,
            "away_score": away_score,
            "settled": settled,
            "source": "mlb_period_props",
        }

    elif market_type == "team_total":
        # Return runs for the specified team in the period
        if inning_range == "game":
            home_runs, away_runs = _extract_game_runs(innings)
        else:
            home_runs, away_runs = _extract_inning_runs(innings, inning_range)

        # Determine which team to return
        team_side = None
        if team:
            teams_data = boxscore.get("teams", {})
            home_team_name = (
                teams_data.get("home", {}).get("team", {}).get("name", "").lower()
            )
            away_team_name = (
                teams_data.get("away", {}).get("team", {}).get("name", "").lower()
            )

            team_lower = team.lower()
            if team_lower in home_team_name or home_team_name in team_lower:
                team_side = "home"
            elif team_lower in away_team_name or away_team_name in team_lower:
                team_side = "away"

        if team_side == "home":
            result = home_runs
        elif team_side == "away":
            result = away_runs
        else:
            return {
                "found": False,
                "game_pk": game_pk,
                "game_status": game_status,
                "home_score": home_score,
                "away_score": away_score,
                "settled": settled,
                "note": f"Could not determine team side for team={team!r}",
                "source": "mlb_period_props",
            }

        return {
            "found": True,
            "stat_value": result,
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": home_score,
            "away_score": away_score,
            "settled": settled,
            "source": "mlb_period_props",
        }

    elif market_type in _PLAYER_PROP_FIELDS:
        # Batter props for a player (runs, home runs, doubles, triples, bases, singles)
        hitters = _collect_hitters(boxscore)
        if not hitters:
            return {
                "found": False,
                "game_pk": game_pk,
                "game_status": game_status,
                "home_score": home_score,
                "away_score": away_score,
                "settled": settled,
                "note": "No hitter data in boxscore yet.",
                "source": "mlb_period_props",
            }

        matched = _match_player(player, hitters)
        if matched is None:
            return {
                "found": False,
                "game_pk": game_pk,
                "game_status": game_status,
                "home_score": home_score,
                "away_score": away_score,
                "settled": settled,
                "note": f"Player '{player}' not found in hitter stats.",
                "available_hitters": list(hitters.keys()),
                "source": "mlb_period_props",
            }

        stat_value = hitters[matched][_PLAYER_PROP_FIELDS[market_type]]
        return {
            "found": True,
            "player": matched,
            "stat_value": stat_value,
            "game_pk": game_pk,
            "game_status": game_status,
            "home_score": home_score,
            "away_score": away_score,
            "settled": settled,
            "source": "mlb_period_props",
        }

    else:
        return {
            "found": False,
            "note": f"Unknown market_type: {market_type}",
            "source": "mlb_period_props",
        }
