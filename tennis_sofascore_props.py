"""
tennis_sofascore_props.py
=========================
Tennis market settlement using the SofaScore public API.

Uses curl_cffi for Cloudflare-bypass (same technique as soccer_sofascore_props.py).

Data sources
------------
  GET /sport/tennis/scheduled-events/{YYYY-MM-DD}
      → locate event ID by fuzzy player-name matching

  GET /event/{id}
      → set-by-set game scores:
            homeScore.current   → sets won by home player
            homeScore.period1   → games won in set 1
            homeScore.period2   → games won in set 2
            ...  (up to period5 for Grand Slams / Davis Cup)

Public interface
----------------
  TENNIS_PROP_STAT_MAP   – dict mapping market name → (scope, market_type)
  prop_check(market, game_date, player, opponent, pick, line)
      → dict  (same schema as soccer_sofascore_props.prop_check)
"""

from __future__ import annotations

import difflib
import random
import re
import time
from typing import Any, Optional

import sofascore_client
import sofascore_db

# ---------------------------------------------------------------------------
# Market map  (scope: full | s1 | s2,  market_type: internal key)
# ---------------------------------------------------------------------------

TENNIS_PROP_STAT_MAP: dict[str, tuple[str, str]] = {
    # Full-match markets
    "moneyline": ("full", "moneyline"),
    # generic aliases — sportsbooks often use "spread" / "total" for tennis too
    "spread": ("full", "game_spread"),  # e.g. "Čilić -4.5 games"
    "total": ("full", "total_games"),  # e.g. "Over 22.5 games"
    "game_spread": ("full", "game_spread"),
    "total_games": ("full", "total_games"),
    "total_sets": ("full", "total_sets"),
    "set_handicap": ("full", "set_handicap"),
    "player_games_won": ("full", "player_games_won"),
    "player_sets_won": ("full", "player_sets_won"),
    # 1st set
    "1st_set_moneyline": ("s1", "moneyline"),
    "1st_set_game_spread": ("s1", "game_spread"),
    "1st_set_total_games": ("s1", "total_games"),
    "1st_set_will_there_be_a_tiebreak": ("s1", "tiebreak"),
    # 2nd set
    "2nd_set_moneyline": ("s2", "moneyline"),
    "2nd_set_total_games": ("s2", "total_games"),
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


# HTTP helpers handled by sofascore_client


# ---------------------------------------------------------------------------
# Name normalisation / fuzzy match
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    """Normalise a player name for fuzzy comparison."""
    if not s:
        return ""
    out = s.lower()
    # Collapse punctuation and abbreviation dots (e.g. "R. Federer" → "r federer")
    out = re.sub(r"[^a-z0-9]+", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _name_score(a: str | None, b: str | None) -> float:
    an, bn = _norm(a), _norm(b)
    if not an or not bn:
        return 0.0
    if an == bn:
        return 1.0
    if an in bn or bn in an:
        return 0.93
    # Last-name match (e.g. "Djokovic" vs "Novak Djokovic")
    a_parts = an.split()
    b_parts = bn.split()
    if a_parts and b_parts and a_parts[-1] == b_parts[-1]:
        return 0.90
    return difflib.SequenceMatcher(a=an, b=bn).ratio()


# ---------------------------------------------------------------------------
# Match lookup
# ---------------------------------------------------------------------------


def find_match(
    game_date: str,
    player: str | None,
    opponent: str | None,
) -> tuple[Optional[dict[str, Any]], str]:
    """
    Search SofaScore scheduled-events for a tennis match on *game_date*.

    Returns (dict, source).
    """
    db_event = sofascore_db.lookup_event("tennis", game_date, player, opponent)
    if db_event:
        best = db_event
        source = "sofascore_db"
    else:
        payload = sofascore_client.get(f"/sport/tennis/scheduled-events/{game_date}")
        events: list[dict] = payload.get("events") or []

        if not events:
            return None, "sofascore_tennis"

        best = None
        best_score = -1.0

        for ev in events:
            home_name = (ev.get("homeTeam") or {}).get("name")
            away_name = (ev.get("awayTeam") or {}).get("name")

            score = 0.0
            if player and opponent:
                score = max(
                    _name_score(player, home_name) + _name_score(opponent, away_name),
                    _name_score(player, away_name) + _name_score(opponent, home_name),
                )
            elif player:
                score = max(_name_score(player, home_name), _name_score(player, away_name))
            elif opponent:
                score = max(
                    _name_score(opponent, home_name), _name_score(opponent, away_name)
                )

            if score > best_score:
                best_score = score
                best = ev

        if not best or best_score < 0.55:
            return None, "sofascore_tennis"
        source = "sofascore_tennis"

    status_obj = best.get("status") or {}
    status_type = str(status_obj.get("type") or "").lower()
    status_desc = str(status_obj.get("description") or "").lower()
    finished = status_type == "finished" or status_desc in ("ended", "finished")

    return {
        "match_id": best.get("id"),
        "home_team": (best.get("homeTeam") or {}).get("name", ""),
        "away_team": (best.get("awayTeam") or {}).get("name", ""),
        "home_score_raw": best.get("homeScore") or {},
        "away_score_raw": best.get("awayScore") or {},
        "status_type": status_type,
        "finished": finished,
    }, source


# ---------------------------------------------------------------------------
# Score extraction
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _tennis_scores(
    home_score_raw: dict[str, Any],
    away_score_raw: dict[str, Any],
) -> dict[str, Any]:
    """
    Parse homeScore / awayScore from a Sofascore tennis event.

    Sofascore tennis score schema:
      homeScore.current   = sets won by home player (the headline score)
      homeScore.period1   = games won in set 1
      homeScore.period2   = games won in set 2
      ...  (period3/4/5 used for Grand Slams and Davis Cup)

    Returns
    -------
    {
        "sets":              (home_sets_won, away_sets_won),
        "period":            {1: (h_games, a_games), 2: ..., ...},
        "total_sets_played": int,
        "total_games":       int,
        "home_games_total":  int,
        "away_games_total":  int,
    }
    """
    h_sets = _to_int(home_score_raw.get("current") or home_score_raw.get("sets") or 0)
    a_sets = _to_int(away_score_raw.get("current") or away_score_raw.get("sets") or 0)

    periods: dict[int, tuple[int, int]] = {}
    home_games_total = 0
    away_games_total = 0

    for i in range(1, 6):
        h_raw = home_score_raw.get(f"period{i}")
        a_raw = away_score_raw.get(f"period{i}")
        if h_raw is None and a_raw is None:
            break
        h_g = _to_int(h_raw or 0)
        a_g = _to_int(a_raw or 0)
        periods[i] = (h_g, a_g)
        home_games_total += h_g
        away_games_total += a_g

    return {
        "sets": (h_sets, a_sets),
        "period": periods,
        "total_sets_played": h_sets + a_sets,
        "total_games": home_games_total + away_games_total,
        "home_games_total": home_games_total,
        "away_games_total": away_games_total,
    }


def _set_had_tiebreak(h_games: int, a_games: int) -> bool:
    """
    Under standard tennis scoring a tiebreak is played if and only if the
    game score reaches 6-6, producing a final set score of 7-6.
    (Super-tiebreak formats that end 10-x are not common in Sofascore data
    for the markets targeted here, but can be added if needed.)
    """
    return (h_games == 7 and a_games == 6) or (h_games == 6 and a_games == 7)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def prop_check(
    market: str,
    game_date: str,
    player: Optional[str] = None,
    opponent: Optional[str] = None,
    selection: Optional[str] = None,
    pick: Optional[str] = None,
    line: Optional[float] = None,
) -> dict[str, Any]:
    """
    Settle a tennis bet using SofaScore data.

    Parameters
    ----------
    market      : market key, e.g. "total_games", "1st_set_moneyline".
                  Spaces are converted to underscores automatically.
    game_date   : ISO date string, e.g. "2026-05-30"
    player      : one player's name (used for match lookup and player-
                  specific markets like player_games_won)
    opponent    : the opposing player's name (improves match-lookup accuracy)
    selection   : reserved for future player-specific sub-markets
    pick        : "over"/"under", "yes"/"no", or a player name for ML/spread
    line        : numeric line value (required for total/spread markets)

    Returns
    -------
    dict with keys:
      found, market, market_type, scope, stat_value,
      match_id, game_status, settled, finished,
      home_team, away_team, home_sets, away_sets,
      source ("sofascore_tennis")

    stat_value semantics
    --------------------
    moneyline     : 1.0 = home player won, 0.0 = away player won
    game_spread   : home_games_total − away_games_total  (positive = home ahead)
    set_handicap  : home_sets_won − away_sets_won
    total_games   : integer total games played in the market scope
    total_sets    : integer sets played in the match
    player_*      : integer count for the identified player
    tiebreak      : 1 = tiebreak occurred, 0 = no tiebreak
    """
    market_norm = market.strip().lower().replace(" ", "_")
    entry = TENNIS_PROP_STAT_MAP.get(market_norm)
    if not entry:
        return {
            "found": False,
            "note": f"Market '{market_norm}' not in TENNIS_PROP_STAT_MAP.",
            "source": "sofascore_tennis",
        }

    scope, market_type = entry

    # --- locate the match --------------------------------------------------
    match_info, source = find_match(game_date, player, opponent)
    if not match_info:
        return {
            "found": False,
            "note": (
                f"No SofaScore tennis match found for "
                f"date={game_date}, player={player!r}, opponent={opponent!r}."
            ),
            "source": source,
        }

    match_id = match_info["match_id"]
    home_name = match_info["home_team"]
    away_name = match_info["away_team"]
    finished = match_info["finished"]

    # --- parse scores ------------------------------------------------------
    scores = _tennis_scores(
        match_info["home_score_raw"],
        match_info["away_score_raw"],
    )

    h_sets, a_sets = scores["sets"]
    periods = scores["period"]  # {1: (hg, ag), 2: ...}
    total_sets_played = scores["total_sets_played"]
    total_games = scores["total_games"]
    h_games_total = scores["home_games_total"]
    a_games_total = scores["away_games_total"]

    # Per-set scores for scoped markets
    _scope_period = {"s1": 1, "s2": 2}
    period_num = _scope_period.get(scope)
    if period_num is not None:
        h_set_g, a_set_g = periods.get(period_num, (0, 0))
    else:
        h_set_g, a_set_g = 0, 0

    # --- compute stat_value -----------------------------------------------
    stat_value: float | int | None = None

    # ── Moneyline ──────────────────────────────────────────────────────────
    if market_type == "moneyline":
        if scope == "full":
            if h_sets > a_sets:
                stat_value = 1.0
            elif a_sets > h_sets:
                stat_value = 0.0
            else:
                stat_value = None  # match not yet decided
        else:
            # Set-level winner
            if period_num not in periods:
                stat_value = None  # set not yet played
            elif h_set_g > a_set_g:
                stat_value = 1.0
            elif a_set_g > h_set_g:
                stat_value = 0.0
            else:
                stat_value = None

    # ── Game Spread ────────────────────────────────────────────────────────
    elif market_type == "game_spread":
        if scope == "full":
            stat_value = h_games_total - a_games_total
        else:
            stat_value = h_set_g - a_set_g if period_num in periods else None

    # ── Total Games ────────────────────────────────────────────────────────
    elif market_type == "total_games":
        if scope == "full":
            stat_value = total_games
        else:
            stat_value = h_set_g + a_set_g if period_num in periods else None

    # ── Total Sets ─────────────────────────────────────────────────────────
    elif market_type == "total_sets":
        stat_value = total_sets_played

    # ── Set Handicap ───────────────────────────────────────────────────────
    elif market_type == "set_handicap":
        # Positive = home player ahead in sets
        stat_value = h_sets - a_sets

    # ── Player Games Won ───────────────────────────────────────────────────
    elif market_type == "player_games_won":
        target = player or selection or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "player is required for player_games_won.",
                "source": "sofascore_tennis",
            }
        is_home = _name_score(target, home_name) >= _name_score(target, away_name)
        stat_value = h_games_total if is_home else a_games_total

    # ── Player Sets Won ────────────────────────────────────────────────────
    elif market_type == "player_sets_won":
        target = player or selection or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "player is required for player_sets_won.",
                "source": "sofascore_tennis",
            }
        is_home = _name_score(target, home_name) >= _name_score(target, away_name)
        stat_value = h_sets if is_home else a_sets

    # ── Tiebreak ───────────────────────────────────────────────────────────
    elif market_type == "tiebreak":
        if period_num is None:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "tiebreak market requires a set scope (s1, s2).",
                "source": "sofascore_tennis",
            }
        if period_num not in periods:
            stat_value = None  # set not yet played
        else:
            h_g, a_g = periods[period_num]
            stat_value = 1 if _set_had_tiebreak(h_g, a_g) else 0

    else:
        return {
            "found": False,
            "match_id": match_id,
            "settled": finished,
            "note": f"Unhandled market_type '{market_type}'.",
            "source": source,
        }

    return {
        "found": True,
        "market": market_norm,
        "market_type": market_type,
        "scope": scope,
        "stat_value": stat_value,
        "match_id": match_id,
        "game_status": "Final" if finished else "InProgress",
        "settled": finished,
        "finished": finished,
        "home_team": home_name,
        "away_team": away_name,
        "home_sets": h_sets,
        "away_sets": a_sets,
        "source": source,
    }
