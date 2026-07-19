"""
hockey_sofascore_props.py
=========================
Ice hockey market settlement using the SofaScore public API.

Uses curl_cffi for Cloudflare-bypass (same technique as soccer_sofascore_props.py).

Data sources
------------
  GET /sport/ice-hockey/scheduled-events/{YYYY-MM-DD}
      → locate event ID by fuzzy team-name matching

  GET /event/{id}
      → period scores:
            homeScore.period1/period2/period3  → period goals
            homeScore.normaltime               → regulation total
            homeScore.overtime                 → OT goals (if any)
            homeScore.current                  → final total (incl. OT/SO)

  GET /event/{id}/incidents
      → goal scorers (incidentType="goal"), in chronological order

  GET /event/{id}/lineups
      → per-player stats: goals, assists, shots, saves
        home.players[].statistics  /  away.players[].statistics

Public interface
----------------
  HOCKEY_PROP_STAT_MAP   – dict mapping market name → (scope, market_type)
  prop_check(market, game_date, team, opponent, selection, pick, line)
      → dict  (same schema as soccer_sofascore_props.prop_check)

Scope keys
----------
  full  – entire game (includes OT/SO)
  reg   – regulation only (3 periods, no OT)
  p1    – 1st period
  p2    – 2nd period
  p3    – 3rd period
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
# Market map  (scope: full | reg | p1 | p2 | p3,  market_type: internal key)
# ---------------------------------------------------------------------------

HOCKEY_PROP_STAT_MAP: dict[str, tuple[str, str]] = {
    # ── Full-game markets ───────────────────────────────────────────────────
    "moneyline": ("full", "moneyline"),
    "puck_line": ("full", "puck_line"),
    "total_goals": ("full", "total_goals"),
    "total_points": ("full", "total_goals"),  # alias
    "total_goals_odd_even": ("full", "odd_even_goals"),
    "team_total": ("full", "team_total_goals"),
    "will_there_be_overtime": ("full", "overtime"),
    "both_teams_to_score": ("full", "btts"),
    "both_teams_to_score_reg_time": ("reg", "btts"),
    # Goal scorer markets
    "first_goal_scorer": ("full", "first_goal_scorer"),
    "last_goal_scorer": ("full", "last_goal_scorer"),
    "anytime_goal_scorer": ("full", "anytime_goal_scorer"),
    # Player prop markets (over/under against a line)
    "player_goals": ("full", "player_goals"),
    "player_assists": ("full", "player_assists"),
    "player_points": ("full", "player_points"),
    "player_saves": ("full", "player_saves"),
    "player_shots_on_goal": ("full", "player_shots_on_goal"),
    "player_power_play_points": ("full", "player_power_play_points"),
    # ── 1st Period ──────────────────────────────────────────────────────────
    "1st_period_moneyline": ("p1", "moneyline"),
    "1st_period_puck_line": ("p1", "puck_line"),
    "1st_period_total_goals": ("p1", "total_goals"),
    "1st_period_total_goals_odd_even": ("p1", "odd_even_goals"),
    "1st_period_team_total": ("p1", "team_total_goals"),
    "1st_period_both_teams_to_score": ("p1", "btts"),
    # ── 2nd Period ──────────────────────────────────────────────────────────
    "2nd_period_moneyline": ("p2", "moneyline"),
    "2nd_period_puck_line": ("p2", "puck_line"),
    "2nd_period_total_goals": ("p2", "total_goals"),
    "2nd_period_total_goals_odd_even": ("p2", "odd_even_goals"),
    "2nd_period_team_total": ("p2", "team_total_goals"),
    "2nd_period_both_teams_to_score": ("p2", "btts"),
    # ── 3rd Period ──────────────────────────────────────────────────────────
    "3rd_period_moneyline": ("p3", "moneyline"),
    "3rd_period_puck_line": ("p3", "puck_line"),
    "3rd_period_total_goals": ("p3", "total_goals"),
    "3rd_period_total_goals_odd_even": ("p3", "odd_even_goals"),
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


# HTTP helpers handled by sofascore_client


# ---------------------------------------------------------------------------
# Name normalisation / fuzzy match
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    if not s:
        return ""
    out = s.lower()
    # Strip common NHL-team suffixes that may differ across data sources
    out = re.sub(
        r"\b(hockey|club|hc|ice)\b",
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
    Search SofaScore scheduled-events for an ice-hockey match on *game_date*.

    Returns (dict, source).
    """
    best: dict[str, Any] | None = None
    source = "sofascore_hockey"

    if not skip_db:
        db_event = sofascore_db.lookup_event("ice-hockey", game_date, team, opponent)
        if db_event:
            best, source, _ = sofascore_live_lookup.refresh_db_event_if_stale(
                "ice-hockey", game_date, db_event, allow_live=allow_live
            )

    if best is None and allow_live:
        live_event = sofascore_live_lookup.find_live_event(
            "ice-hockey", game_date, team, opponent
        )
        if live_event is not None:
            best = live_event
            source = "sofascore_hockey"
            stored_date = sofascore_db.utc_game_date_from_event(live_event, game_date)
            sofascore_db.upsert_event("ice-hockey", stored_date, live_event)

    if best is None:
        return None, source

    finished = sofascore_live_lookup.event_is_finished(best)
    canceled = sofascore_live_lookup.event_is_canceled(best)

    return {
        "match_id": best.get("id"),
        "home_team": (best.get("homeTeam") or {}).get("name", ""),
        "away_team": (best.get("awayTeam") or {}).get("name", ""),
        "home_score_raw": best.get("homeScore") or {},
        "away_score_raw": best.get("awayScore") or {},
        "status_type": str((best.get("status") or {}).get("type") or "").lower(),
        "finished": finished,
        "canceled": canceled,
    }, source


# ---------------------------------------------------------------------------
# Score extraction
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _hockey_scores(
    home_score_raw: dict[str, Any],
    away_score_raw: dict[str, Any],
) -> dict[str, Any]:
    """
    Parse homeScore / awayScore from a SofaScore ice-hockey event.

    SofaScore ice-hockey score schema:
      homeScore.current    = final score (includes OT/SO if applicable)
      homeScore.normaltime = regulation score (period1+period2+period3)
      homeScore.period1    = 1st period goals
      homeScore.period2    = 2nd period goals
      homeScore.period3    = 3rd period goals
      homeScore.overtime   = OT goals (present only when OT occurred)

    Returns
    -------
    {
        "full":       (h_full, a_full),        # current score incl OT
        "reg":        (h_reg, a_reg),          # regulation (normaltime)
        "p1":         (h_p1, a_p1),
        "p2":         (h_p2, a_p2),
        "p3":         (h_p3, a_p3),
        "had_ot":     bool,                    # True if OT/SO occurred
    }
    """
    h_full = _to_int(
        home_score_raw.get("current") or home_score_raw.get("normaltime") or 0
    )
    a_full = _to_int(
        away_score_raw.get("current") or away_score_raw.get("normaltime") or 0
    )

    h_reg_raw = home_score_raw.get("normaltime")
    a_reg_raw = away_score_raw.get("normaltime")

    h_p1 = _to_int(home_score_raw.get("period1", 0))
    h_p2 = _to_int(home_score_raw.get("period2", 0))
    h_p3 = _to_int(home_score_raw.get("period3", 0))
    a_p1 = _to_int(away_score_raw.get("period1", 0))
    a_p2 = _to_int(away_score_raw.get("period2", 0))
    a_p3 = _to_int(away_score_raw.get("period3", 0))

    # Regulation score: prefer explicit normaltime field, fall back to sum of 3 periods
    h_reg = _to_int(h_reg_raw) if h_reg_raw is not None else (h_p1 + h_p2 + h_p3)
    a_reg = _to_int(a_reg_raw) if a_reg_raw is not None else (a_p1 + a_p2 + a_p3)

    # Overtime occurred if OT key is present OR current > normaltime
    ot_h = home_score_raw.get("overtime")
    ot_a = away_score_raw.get("overtime")
    had_ot = (
        (ot_h is not None and _to_int(ot_h) > 0)
        or (ot_a is not None and _to_int(ot_a) > 0)
        or (h_full != h_reg or a_full != a_reg)
    )

    return {
        "full": (h_full, a_full),
        "reg": (h_reg, a_reg),
        "p1": (h_p1, a_p1),
        "p2": (h_p2, a_p2),
        "p3": (h_p3, a_p3),
        "had_ot": had_ot,
    }


# ---------------------------------------------------------------------------
# Incidents (goal scorers)
# ---------------------------------------------------------------------------


def _get_incidents(match_id: int, *, allow_live: bool = True) -> list[dict[str, Any]]:
    payload = sofascore_live_lookup.fetch_event_detail(
        str(match_id), "incidents", allow_live=allow_live
    ) or {}
    return payload.get("incidents") or []


def _incident_player_name(inc: dict) -> str:
    player = inc.get("player") or inc.get("playerIn") or {}
    return (
        str(player.get("name") or player.get("shortName") or "")
        or str(inc.get("playerName") or "")
    ).strip()


def _goal_scorer_names(incidents: list[dict]) -> list[str]:
    """Return goal scorer names in chronological order."""
    scorers: list[str] = []
    for inc in incidents:
        inc_type = str(inc.get("incidentType") or "").lower()
        if inc_type == "goal":
            # Exclude own goals and penalty shots where scorer is irrelevant
            inc_class = str(inc.get("incidentClass") or "").lower()
            if inc_class in ("owngoal", "own_goal"):
                continue
            name = _incident_player_name(inc)
            if name:
                scorers.append(name)
    return scorers


# ---------------------------------------------------------------------------
# Player stats (from lineups endpoint)
# ---------------------------------------------------------------------------


def _get_player_stats(match_id: int, *, allow_live: bool = True) -> list[dict[str, Any]]:
    """
    Fetch both teams' player stats from /event/{id}/lineups.

    Returns a flat list of dicts:
      {"name": str, "statistics": {goals, assists, shotsOnGoal, saves, ...}}
    """
    payload = sofascore_live_lookup.fetch_event_detail(
        str(match_id), "lineups", allow_live=allow_live
    ) or {}
    players: list[dict[str, Any]] = []

    for side in ("home", "away"):
        team_data = payload.get(side) or {}
        for player_entry in team_data.get("players") or []:
            player = player_entry.get("player") or {}
            stats = player_entry.get("statistics") or {}
            name = (player.get("name") or player.get("shortName") or "").strip()
            if name:
                players.append({"name": name, "statistics": stats})

    return players


def _find_player_stats(
    players: list[dict[str, Any]],
    target: str,
) -> Optional[dict[str, Any]]:
    """Fuzzy-match target name against the player list; return statistics dict or None."""
    target_n = _norm(target)
    best: dict[str, Any] | None = None
    best_score = 0.0

    for p in players:
        pn = _norm(p["name"])
        if not pn:
            continue
        if target_n == pn or target_n in pn or pn in target_n:
            return p["statistics"]
        s = difflib.SequenceMatcher(a=target_n, b=pn).ratio()
        if s > best_score:
            best_score = s
            best = p

    if best and best_score >= 0.72:
        return best["statistics"]
    return None


def _player_stat_value(stats: dict[str, Any], *keys: str) -> int | None:
    """Try multiple stat-key candidates in order; return first non-None int found."""
    for k in keys:
        v = stats.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


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
    Settle an ice-hockey bet using SofaScore data.

    Parameters
    ----------
    market      : market key, e.g. "total_goals", "1st_period_moneyline".
                  Spaces are converted to underscores automatically.
    game_date   : ISO date string, e.g. "2026-05-24"
    team        : one team name (used for team-total markets + match lookup)
    opponent    : the opposing team (improves match-lookup accuracy)
    selection   : free-text selection (used for goal-scorer markets)
    pick        : "over"/"under", "yes"/"no", "home"/"away", or a team name
    line        : numeric line (required for total/spread markets)

    Returns
    -------
    dict with keys:
      found, market, market_type, scope, stat_value,
      match_id, game_status, settled, finished,
      home_team, away_team, home_score, away_score,
      source ("sofascore_hockey")

    stat_value semantics
    --------------------
    moneyline / puck_line : home_score – away_score (goal differential)
    total_goals           : integer total goals in scope
    team_total_goals      : integer goals for the identified team in scope
    btts                  : 1 = both scored, 0 = did not
    odd_even_goals        : 1 = odd total, 0 = even total
    overtime              : 1 = OT occurred, 0 = regulation only
    anytime_goal_scorer   : 1 = player scored, 0 = did not
    first_goal_scorer     : 1 = player scored first, 0 = did not
    last_goal_scorer      : 1 = player scored last, 0 = did not
    player_goals/assists/ : integer count for identified player
    player_points/saves/  :
    player_shots_on_goal  :
    player_power_play_points :
    """
    market_norm = market.strip().lower().replace(" ", "_")
    entry = HOCKEY_PROP_STAT_MAP.get(market_norm)
    if not entry:
        return {
            "found": False,
            "note": f"Market '{market_norm}' not in HOCKEY_PROP_STAT_MAP.",
            "source": "sofascore_hockey",
        }

    scope, market_type = entry

    # ── locate the match ────────────────────────────────────────────────────
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
                f"No SofaScore ice-hockey match found for "
                f"date={game_date}, team={team!r}, opponent={opponent!r}."
            ),
            "source": source,
        }

    match_id = match_info["match_id"]
    home_name = match_info["home_team"]
    away_name = match_info["away_team"]
    finished = match_info["finished"]

    # Canceled before play: no contest. Settlement treats this as void.
    if match_info.get("canceled"):
        return {
            "found": True,
            "market": market_norm,
            "market_type": market_type,
            "scope": scope,
            "stat_value": None,
            "match_id": match_id,
            "game_status": "Canceled",
            "settled": True,
            "finished": False,
            "void": True,
            "home_team": home_name,
            "away_team": away_name,
            "home_score": None,
            "away_score": None,
            "source": source,
            "note": "Match canceled — bet voided.",
        }

    # ── parse scores ────────────────────────────────────────────────────────
    scores = _hockey_scores(
        match_info["home_score_raw"],
        match_info["away_score_raw"],
    )

    h_full, a_full = scores["full"]
    had_ot = scores["had_ot"]

    # Select (h, a) for the requested scope
    scope_scores: dict[str, tuple[int, int]] = {
        "full": scores["full"],
        "reg": scores["reg"],
        "p1": scores["p1"],
        "p2": scores["p2"],
        "p3": scores["p3"],
    }
    h_s, a_s = scope_scores.get(scope, scores["full"])

    # ── lazy-loaded resources ────────────────────────────────────────────────
    _incidents: list[dict] | None = None
    _players: list[dict] | None = None

    def _get_incidents_cached() -> list[dict]:
        nonlocal _incidents
        if _incidents is None:
            _incidents = _get_incidents(match_id, allow_live=allow_live)
        return _incidents

    def _get_players_cached() -> list[dict]:
        nonlocal _players
        if _players is None:
            _players = _get_player_stats(match_id, allow_live=allow_live)
        return _players

    # ── compute stat_value ───────────────────────────────────────────────────
    stat_value: float | int | None = None

    # ── Moneyline ───────────────────────────────────────────────────────────
    if market_type == "moneyline":
        # Return goal differential; caller resolves the pick side
        stat_value = h_s - a_s

    # ── Puck Line (spread) ──────────────────────────────────────────────────
    elif market_type == "puck_line":
        stat_value = h_s - a_s  # positive = home winning

    # ── Total Goals ─────────────────────────────────────────────────────────
    elif market_type == "total_goals":
        stat_value = h_s + a_s

    # ── Team Total ──────────────────────────────────────────────────────────
    elif market_type == "team_total_goals":
        if not team:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "team is required for team_total markets.",
                "source": "sofascore_hockey",
            }
        is_home = _name_score(team, home_name) >= _name_score(team, away_name)
        stat_value = h_s if is_home else a_s

    # ── Odd/Even ────────────────────────────────────────────────────────────
    elif market_type == "odd_even_goals":
        stat_value = 1 if (h_s + a_s) % 2 == 1 else 0

    # ── Both Teams To Score ─────────────────────────────────────────────────
    elif market_type == "btts":
        stat_value = 1 if (h_s > 0 and a_s > 0) else 0

    # ── Will There Be Overtime ──────────────────────────────────────────────
    elif market_type == "overtime":
        stat_value = 1 if had_ot else 0

    # ── Goal Scorer Markets ─────────────────────────────────────────────────
    elif market_type in (
        "anytime_goal_scorer",
        "first_goal_scorer",
        "last_goal_scorer",
    ):
        target = selection or pick or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "selection/pick is required for goal-scorer markets.",
                "source": "sofascore_hockey",
            }
        incidents = _get_incidents_cached()
        scorers = _goal_scorer_names(incidents)
        target_n = _norm(target)
        scorers_n = [_norm(p) for p in scorers]

        if market_type == "anytime_goal_scorer":
            stat_value = (
                1
                if any(
                    target_n == p or target_n in p or p in target_n for p in scorers_n
                )
                else 0
            )
        elif market_type == "first_goal_scorer":
            first = scorers_n[0] if scorers_n else ""
            stat_value = (
                1
                if first
                and (target_n == first or target_n in first or first in target_n)
                else 0
            )
        else:  # last_goal_scorer
            last = scorers_n[-1] if scorers_n else ""
            stat_value = (
                1
                if last and (target_n == last or target_n in last or last in target_n)
                else 0
            )

    # ── Player Stat Markets ─────────────────────────────────────────────────
    elif market_type in (
        "player_goals",
        "player_assists",
        "player_points",
        "player_saves",
        "player_shots_on_goal",
        "player_power_play_points",
    ):
        target = selection or team or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "selection or team is required for player stat markets.",
                "source": "sofascore_hockey",
            }
        players = _get_players_cached()
        pstats = _find_player_stats(players, target)
        if pstats is None:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": f"Player '{target}' not found in SofaScore lineups.",
                "source": "sofascore_hockey",
            }

        if market_type == "player_goals":
            stat_value = _player_stat_value(pstats, "goals", "goal", "G")
        elif market_type == "player_assists":
            stat_value = _player_stat_value(pstats, "assists", "assist", "A")
        elif market_type == "player_points":
            # Points = goals + assists
            g = _player_stat_value(pstats, "goals", "goal", "G") or 0
            a = _player_stat_value(pstats, "assists", "assist", "A") or 0
            stat_value = g + a
        elif market_type == "player_saves":
            stat_value = _player_stat_value(
                pstats, "saves", "savesOfShots", "savesMade", "SV"
            )
        elif market_type == "player_shots_on_goal":
            stat_value = _player_stat_value(
                pstats, "shotsOnGoal", "shotsOnTarget", "shots", "SOG", "S"
            )
        elif market_type == "player_power_play_points":
            stat_value = _player_stat_value(pstats, "powerPlayPoints")

        if stat_value is None:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": (
                    f"Stat for '{market_type}' not available in SofaScore "
                    f"lineups for player '{target}'."
                ),
                "source": "sofascore_hockey",
            }

    else:
        return {
            "found": False,
            "match_id": match_id,
            "settled": finished,
            "note": f"Unhandled market_type '{market_type}'.",
            "source": "sofascore_hockey",
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
        "had_ot": had_ot,
        "source": source,
    }
