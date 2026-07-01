"""
soccer_sofascore_props.py
=========================
Soccer market settlement using the SofaScore public API.

Uses curl_cffi for Cloudflare-bypass (same technique as sofascore_scraper/).

Data sources
------------
  GET /sport/football/scheduled-events/{YYYY-MM-DD}
      → locate event ID by fuzzy team-name matching

  GET /event/{id}
      → period scores (homeScore.period1 / period2, awayScore.period1 / period2)

  GET /event/{id}/statistics
      → corner counts per period (ALL / 1ST / 2ND)

  GET /event/{id}/incidents
      → goals (scorer names), cards (card-receiver names)

Public interface
----------------
  SOCCER_PROP_STAT_MAP   – dict mapping market name → (scope, market_type)
  prop_check(market, game_date, team, opponent, selection, pick, line)
      → dict  (same schema as soccer_period_props.prop_check)
"""

from __future__ import annotations

import difflib
import random
import re
import time
from typing import Any, Optional

import sofascore_db
import sofascore_live_lookup

# ---------------------------------------------------------------------------
# Market map  (scope: full | 1h | 2h,  market_type: internal key)
# ---------------------------------------------------------------------------

SOCCER_PROP_STAT_MAP: dict[str, tuple[str, str]] = {
    # Full-game markets
    "moneyline":                     ("full", "moneyline"),
    "spread":                        ("full", "spread"),
    "draw_bet":                      ("full", "draw_bet"),
    "both_teams_to_score":           ("full", "btts"),
    "total":                         ("full", "total_goals"),   # generic alias
    "total_goals":                   ("full", "total_goals"),
    "asian_total_goals":             ("full", "asian_total_goals"),
    "team_total":                    ("full", "team_total_goals"),
    "total_goals_odd_even":          ("full", "odd_even_goals"),
    "total_corners":                 ("full", "total_corners"),
    "team_total_corners":            ("full", "team_total_corners"),
    "total_corners_odd_even":        ("full", "odd_even_corners"),
    "goal_both_halves":              ("full", "goal_both_halves"),
    "anytime_goal_scorer":           ("full", "anytime_goal_scorer"),
    "first_goal_scorer":             ("full", "first_goal_scorer"),
    "last_goal_scorer":              ("full", "last_goal_scorer"),
    "anytime_card_receiver":         ("full", "anytime_card_receiver"),
    # 1st half
    "1st_half_both_teams_to_score":      ("1h", "btts"),
    "1st_half_draw_bet":                 ("1h", "draw_bet"),
    "1st_half_total_goals":              ("1h", "total_goals"),
    "1st_half_asian_total_goals":        ("1h", "asian_total_goals"),
    "1st_half_team_total":               ("1h", "team_total_goals"),
    "1st_half_total_goals_odd_even":     ("1h", "odd_even_goals"),
    "1st_half_total_corners":            ("1h", "total_corners"),
    "1st_half_team_total_corners":       ("1h", "team_total_corners"),
    # 2nd half
    "2nd_half_both_teams_to_score":      ("2h", "btts"),
    "2nd_half_draw_bet":                 ("2h", "draw_bet"),
    "2nd_half_total_goals":              ("2h", "total_goals"),
    "2nd_half_asian_total_goals":        ("2h", "asian_total_goals"),
    "2nd_half_team_total":               ("2h", "team_total_goals"),
    "2nd_half_total_corners":            ("2h", "total_corners"),
    "2nd_half_team_total_corners":       ("2h", "team_total_corners"),
}

# HTTP helpers handled by sofascore_client


# ---------------------------------------------------------------------------
# Name normalisation / fuzzy match
# ---------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    if not s:
        return ""
    out = s.lower()
    out = out.replace("&", " and ")
    # Strip common suffixes/prefixes that differ between data sources
    out = re.sub(
        r"\b(fc|cf|sc|afc|ac|club|atletico|athletic|deportivo|real|cd|ud|sd|rcd)\b",
        " ",
        out,
    )
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
    return difflib.SequenceMatcher(a=an, b=bn).ratio()


# ---------------------------------------------------------------------------
# Match lookup
# ---------------------------------------------------------------------------

def find_match(
    game_date: str,
    team: str | None,
    opponent: str | None,
    *,
    allow_live: bool = True,
    skip_db: bool = False,
) -> tuple[Optional[dict[str, Any]], str]:
    """
    Search SofaScore scheduled-events for a football match on *game_date*.

    Returns (dict, source).
    """
    best: dict[str, Any] | None = None
    source = "sofascore"

    if not skip_db:
        db_event = sofascore_db.lookup_event("football", game_date, team, opponent)
        if db_event:
            best, source, _ = sofascore_live_lookup.refresh_db_event_if_stale(
                "football", game_date, db_event, allow_live=allow_live
            )

    if best is None and allow_live:
        live_event = sofascore_live_lookup.find_live_event(
            "football", game_date, team, opponent
        )
        if live_event is not None:
            best = live_event
            source = "sofascore"
            stored_date = sofascore_db.utc_game_date_from_event(live_event, game_date)
            sofascore_db.upsert_event("football", stored_date, live_event)

    if best is None:
        return None, source

    finished = sofascore_live_lookup.event_is_finished(best)

    return {
        "match_id": best.get("id"),
        "home_team": (best.get("homeTeam") or {}).get("name", ""),
        "away_team": (best.get("awayTeam") or {}).get("name", ""),
        "home_score_raw": best.get("homeScore") or {},
        "away_score_raw": best.get("awayScore") or {},
        "status_type": str((best.get("status") or {}).get("type") or "").lower(),
        "finished": finished,
    }, source


# ---------------------------------------------------------------------------
# Statistics (corners by period)
# ---------------------------------------------------------------------------

def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _extract_corners_from_stats(
    statistics: list[dict[str, Any]],
) -> dict[str, tuple[int, int]]:
    """
    Parse the /statistics response into {period: (home_corners, away_corners)}.
    Periods: "full" (ALL), "1h" (1ST), "2h" (2ND).
    """
    period_map = {"ALL": "full", "1ST": "1h", "2ND": "2h"}
    out: dict[str, tuple[int, int]] = {"full": (0, 0), "1h": (0, 0), "2h": (0, 0)}

    for period_block in statistics:
        raw_period = str(period_block.get("period") or "").upper()
        scope = period_map.get(raw_period)
        if scope is None:
            continue

        for group in period_block.get("groups") or []:
            for item in group.get("statisticsItems") or []:
                if str(item.get("name") or "").lower() in ("corner kicks", "corners"):
                    home_val = _to_int(item.get("home", 0))
                    away_val = _to_int(item.get("away", 0))
                    out[scope] = (home_val, away_val)

    # Derive 2h from full - 1h when only full/1h are available
    if out["2h"] == (0, 0) and out["full"] != (0, 0) and out["1h"] != (0, 0):
        out["2h"] = (
            max(0, out["full"][0] - out["1h"][0]),
            max(0, out["full"][1] - out["1h"][1]),
        )

    return out


def _get_corners(
    match_id: int,
    *,
    allow_live: bool = True,
    force_live: bool = False,
) -> dict[str, tuple[int, int]]:
    payload = sofascore_live_lookup.fetch_event_detail(
        str(match_id), "statistics", allow_live=allow_live, force_live=force_live
    ) or {}
    stats: list[dict] = payload.get("statistics") or []
    return _extract_corners_from_stats(stats)


def _get_incidents(match_id: int, *, allow_live: bool = True) -> list[dict[str, Any]]:
    payload = sofascore_live_lookup.fetch_event_detail(
        str(match_id), "incidents", allow_live=allow_live
    ) or {}
    return payload.get("incidents") or []


# ---------------------------------------------------------------------------
# Period scores from event scorecard
# ---------------------------------------------------------------------------

def _period_scores(
    home_score_raw: dict, away_score_raw: dict
) -> tuple[
    tuple[int, int],  # full
    tuple[int, int],  # 1h
    tuple[int, int],  # 2h
]:
    """
    Extract (home, away) goal counts for full / 1st-half / 2nd-half
    directly from the event's homeScore / awayScore objects.

    SofaScore schema: {"current": N, "period1": N, "period2": N, ...}
    """
    h_full = _to_int(home_score_raw.get("current") or home_score_raw.get("normaltime", 0))
    a_full = _to_int(away_score_raw.get("current") or away_score_raw.get("normaltime", 0))
    h_1h = _to_int(home_score_raw.get("period1", 0))
    a_1h = _to_int(away_score_raw.get("period1", 0))
    # Derive 2nd half; prefer explicit field when available
    h_2h = _to_int(home_score_raw.get("period2", 0)) or max(0, h_full - h_1h)
    a_2h = _to_int(away_score_raw.get("period2", 0)) or max(0, a_full - a_1h)
    return (h_full, a_full), (h_1h, a_1h), (h_2h, a_2h)


# ---------------------------------------------------------------------------
# Incidents (goal scorers, card receivers)
# ---------------------------------------------------------------------------

def _incident_player_name(inc: dict) -> str:
    player = inc.get("player") or inc.get("playerIn") or {}
    return (
        str(player.get("name") or player.get("shortName") or "")
        or str(inc.get("playerName") or "")
    ).strip()


def _goal_scorer_names(incidents: list[dict]) -> list[str]:
    """Return scorer names in chronological order (own goals excluded by convention)."""
    scorers: list[str] = []
    for inc in incidents:
        if str(inc.get("incidentType") or "").lower() == "goal":
            if inc.get("incidentClass", "").lower() in ("owngoal", "own_goal"):
                continue
            name = _incident_player_name(inc)
            if name:
                scorers.append(name)
    return scorers


def _card_receiver_names(incidents: list[dict]) -> list[str]:
    names: list[str] = []
    for inc in incidents:
        if str(inc.get("incidentType") or "").lower() == "card":
            name = _incident_player_name(inc)
            if name:
                names.append(name)
    return names


# ---------------------------------------------------------------------------
# Asian handicap evaluator
# ---------------------------------------------------------------------------

def _evaluate_asian_total(
    stat_value: int | float,
    line: float,
    pick_norm: str,
) -> str:
    """
    Evaluate an Asian total bet returning 'win', 'loss', or 'push'.

    Quarter-handicap lines (x.25 / x.75) are split bets:
      - x.75 = 50% on (x+0.5) and 50% on (x+1.0)
      - x.25 = 50% on  x       and 50% on (x+0.5)

    When one half of the split results in a push (exact line hit),
    the combined outcome is mapped to 'push' (half-win or half-loss).
    """
    frac = round(line % 1, 2)

    if frac not in (0.25, 0.75):
        # Standard half/whole line
        if stat_value > line:
            return "win" if pick_norm == "over" else "loss"
        if stat_value < line:
            return "win" if pick_norm == "under" else "loss"
        return "push"

    # Quarter-line: split components
    low = round(line - 0.25, 2)   # e.g. 1.75 → 1.50
    high = round(line + 0.25, 2)  # e.g. 1.75 → 2.00

    if pick_norm == "over":
        if stat_value > high:
            return "win"
        if stat_value == high:
            return "push"   # win on low half, push on high half
        if stat_value == low:
            return "push"   # push on low half, lose on high half
        return "loss"
    else:  # under
        if stat_value < low:
            return "win"
        if stat_value == low:
            return "push"   # push on low half, win on high half
        if stat_value == high:
            return "push"   # lose on low half, push on high half
        return "loss"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def prop_check(
    market: str,
    game_date: str,
    team: Optional[str] = None,
    opponent: Optional[str] = None,
    selection: Optional[str] = None,
    pick: Optional[str] = None,
    line: Optional[float] = None,
    *,
    allow_live: bool = True,
    skip_db: bool = False,
) -> dict[str, Any]:
    """
    Settle a soccer bet using SofaScore data.

    Parameters
    ----------
    market      : market key (e.g. "total_corners", "1st_half_team_total")
    game_date   : ISO date string, e.g. "2026-05-24"
    team        : one team name (used for team-total markets + match lookup)
    opponent    : the opposing team (improves match-lookup accuracy)
    selection   : free-text selection (used for goal/card scorer markets)
    pick        : "over"/"under", "yes"/"no", "home"/"away"/"draw", or team name
    line        : numeric line (required for total/spread markets)

    Returns
    -------
    dict with keys:
      found, market, market_type, scope, stat_value,
      match_id, game_status, settled, finished,
      home_team, away_team, home_score, away_score,
      source ("sofascore")
    """
    market_norm = market.strip().lower().replace(" ", "_")
    entry = SOCCER_PROP_STAT_MAP.get(market_norm)
    if not entry:
        return {
            "found": False,
            "note": f"Market '{market_norm}' not in SOCCER_PROP_STAT_MAP.",
            "source": "sofascore",
        }

    scope, market_type = entry

    # --- locate the match --------------------------------------------------
    match_info, source = find_match(
        game_date,
        team,
        opponent,
        allow_live=allow_live,
        skip_db=skip_db,
    )
    if not match_info:
        return {
            "found": False,
            "note": (
                f"No SofaScore match found for date={game_date}, "
                f"team={team!r}, opponent={opponent!r}."
            ),
            "source": source,
        }

    match_id = match_info["match_id"]
    home_name = match_info["home_team"]
    away_name = match_info["away_team"]
    finished = match_info["finished"]
    refresh_details = source == "sofascore"

    # --- period goal scores ------------------------------------------------
    (h_full, a_full), (h_1h, a_1h), (h_2h, a_2h) = _period_scores(
        match_info["home_score_raw"],
        match_info["away_score_raw"],
    )

    # --- corner counts (fetched only when needed) --------------------------
    corners: dict[str, tuple[int, int]] | None = None

    def _get_corners_cached() -> dict[str, tuple[int, int]]:
        nonlocal corners
        if corners is None:
            corners = _get_corners(
                match_id,
                allow_live=allow_live,
                force_live=refresh_details and market_type in {
                    "total_corners",
                    "team_total_corners",
                    "odd_even_corners",
                },
            )
        return corners

    # --- select goals by scope --------------------------------------------
    by_scope_goals = {
        "full": (h_full, a_full),
        "1h":   (h_1h,  a_1h),
        "2h":   (h_2h,  a_2h),
    }
    home_s, away_s = by_scope_goals[scope]

    # --- compute stat_value -----------------------------------------------
    stat_value: float | int | None = None

    if market_type in ("moneyline", "draw_bet"):
        if home_s > away_s:
            stat_value = 1.0   # home win
        elif away_s > home_s:
            stat_value = 0.0   # away win
        else:
            stat_value = 0.5   # draw

    elif market_type == "spread":
        # Return raw scores; spread evaluation (pick side + line) done in stats_api
        stat_value = home_s - away_s   # goal differential (positive = home winning)

    elif market_type == "btts":
        stat_value = 1 if (home_s > 0 and away_s > 0) else 0

    elif market_type in ("total_goals", "asian_total_goals"):
        stat_value = home_s + away_s

    elif market_type == "team_total_goals":
        if not team:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "team is required for team_total markets.",
                "source": "sofascore",
            }
        is_home = _name_score(team, home_name) >= _name_score(team, away_name)
        stat_value = home_s if is_home else away_s

    elif market_type == "odd_even_goals":
        stat_value = 1 if (home_s + away_s) % 2 == 1 else 0

    elif market_type == "total_corners":
        c = _get_corners_cached()
        hc, ac = c.get(scope, (0, 0))
        stat_value = hc + ac

    elif market_type == "team_total_corners":
        if not team:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "team is required for team_total_corners markets.",
                "source": "sofascore",
            }
        c = _get_corners_cached()
        hc, ac = c.get(scope, (0, 0))
        is_home = _name_score(team, home_name) >= _name_score(team, away_name)
        stat_value = hc if is_home else ac

    elif market_type == "odd_even_corners":
        c = _get_corners_cached()
        hc, ac = c.get(scope, (0, 0))
        stat_value = 1 if (hc + ac) % 2 == 1 else 0

    elif market_type == "goal_both_halves":
        stat_value = 1 if ((h_1h + a_1h) > 0 and (h_2h + a_2h) > 0) else 0

    elif market_type in ("anytime_goal_scorer", "first_goal_scorer", "last_goal_scorer"):
        target = selection or pick or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "selection/pick is required for goal-scorer markets.",
                "source": "sofascore",
            }
        incidents = _get_incidents(match_id, allow_live=allow_live)
        scorers = _goal_scorer_names(incidents)
        target_n = _norm(target)
        scorers_n = [_norm(p) for p in scorers]

        if market_type == "anytime_goal_scorer":
            stat_value = 1 if any(
                target_n == p or target_n in p or p in target_n for p in scorers_n
            ) else 0
        elif market_type == "first_goal_scorer":
            first = scorers_n[0] if scorers_n else ""
            stat_value = 1 if first and (
                target_n == first or target_n in first or first in target_n
            ) else 0
        else:  # last
            last = scorers_n[-1] if scorers_n else ""
            stat_value = 1 if last and (
                target_n == last or target_n in last or last in target_n
            ) else 0

    elif market_type == "anytime_card_receiver":
        target = selection or pick or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "selection/pick is required for anytime_card_receiver.",
                "source": "sofascore",
            }
        incidents = _get_incidents(match_id, allow_live=allow_live)
        receivers = _card_receiver_names(incidents)
        target_n = _norm(target)
        stat_value = 1 if any(
            target_n == _norm(p) or target_n in _norm(p) or _norm(p) in target_n
            for p in receivers
        ) else 0

    else:
        return {
            "found": False,
            "match_id": match_id,
            "settled": finished,
            "note": f"Unhandled market_type '{market_type}'.",
            "source": "sofascore",
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
        "home_score": h_full,
        "away_score": a_full,
        "source": source,
    }
