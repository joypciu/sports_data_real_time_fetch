"""
baseball_sofascore_props.py
===========================
MLB market settlement using the SofaScore public API.

Data sources
------------
  GET /unique-tournament/11205/scheduled-events/{YYYY-MM-DD}
      → locate MLB event by fuzzy team-name matching (or sofascore_db cache)

  GET /event/{id}
      → inning scores in homeScore/awayScore (period1..9, innings.inningN.run)

  GET /event/{id}/lineups
      → per-player batting and pitching statistics

Public interface
----------------
  BASEBALL_PROP_STAT_MAP  – market name → (inning_range, market_type)
  normalize_market()      – alias resolution (1st_5_innings_*, spread, total)
  prop_check(...)         – same return shape as mlb_period_props.prop_check
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Optional

import sofascore_db
import sofascore_live_lookup
import sofascore_live_lookup

MLB_TOURNAMENT_ID = 11205

MARKET_ALIASES: dict[str, str] = {
    "1st_5_innings_moneyline": "1st_half_moneyline",
    "1st_5_innings_total": "1st_half_total_runs",
    "1st_5_innings_team_total": "1st_half_team_total",
    "1st_5_innings_run_line": "1st_half_run_line",
    "spread": "run_line",
    "total": "total_runs",
}

BASEBALL_PROP_STAT_MAP: dict[str, tuple[Any, str]] = {
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
    "1st_3_innings_total_runs": ((0, 2), "total_runs"),
    "1st_7_innings_total_runs": ((0, 6), "total_runs"),
    "1st_half_total_runs": ((0, 4), "total_runs"),
    "1st_inning_moneyline": ((0, 0), "moneyline"),
    "1st_3_innings_moneyline": ((0, 2), "moneyline"),
    "1st_7_innings_moneyline": ((0, 6), "moneyline"),
    "1st_half_moneyline": ((0, 4), "moneyline"),
    "1st_inning_run_line": ((0, 0), "run_line"),
    "1st_3_innings_run_line": ((0, 2), "run_line"),
    "1st_7_innings_run_line": ((0, 6), "run_line"),
    "1st_half_run_line": ((0, 4), "run_line"),
    "1st_half_team_total": ((0, 4), "team_total"),
    "total_runs": ("game", "total_runs"),
    "total_runs_odd_even": ("game", "odd_even"),
    "moneyline": ("game", "moneyline"),
    "run_line": ("game", "run_line"),
    "team_total": ("game", "team_total"),
    "player_runs": (None, "player_runs"),
    "player_hits": (None, "player_hits"),
    "player_rbis": (None, "player_rbis"),
    "player_home_runs": (None, "player_home_runs"),
    "player_doubles": (None, "player_doubles"),
    "player_triples": (None, "player_triples"),
    "player_bases": (None, "player_bases"),
    "player_singles": (None, "player_singles"),
    "player_hits_runs_rbis": (None, "player_hits_runs_rbis"),
    "player_strikeouts": (None, "player_strikeouts"),
    "player_earned_runs": (None, "player_earned_runs"),
    "player_outs": (None, "player_outs"),
}

_PLAYER_BATTING_FIELDS: dict[str, str] = {
    "player_runs": "battingRuns",
    "player_hits": "battingHits",
    "player_rbis": "battingRbi",
    "player_home_runs": "battingHomeRuns",
    "player_doubles": "battingDoubles",
    "player_triples": "battingTriples",
    "player_bases": "battingTotalBases",
    "player_singles": "battingSingles",
}

_PLAYER_PITCHING_FIELDS: dict[str, str] = {
    "player_strikeouts": "pitchingStrikeOuts",
    "player_earned_runs": "pitchingEarnedRuns",
    "player_outs": "pitchingOuts",
}


def normalize_market(market: str) -> str:
    market_norm = market.strip().lower().replace(" ", "_")
    return MARKET_ALIASES.get(market_norm, market_norm)


def _norm(s: str | None) -> str:
    if not s:
        return ""
    out = s.lower()
    out = re.sub(r"\b(baseball|club|mlb)\b", " ", out)
    out = re.sub(r"[^a-z0-9]+", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _is_odd(n: int) -> bool:
    return n % 2 == 1


def _inning_runs(home_score: dict, away_score: dict, inning_index: int) -> tuple[int, int]:
    key = f"inning{inning_index + 1}"
    innings_h = home_score.get("innings") or {}
    innings_a = away_score.get("innings") or {}
    if key in innings_h or key in innings_a:
        return (
            _to_int((innings_h.get(key) or {}).get("run")),
            _to_int((innings_a.get(key) or {}).get("run")),
        )
    period_key = f"period{inning_index + 1}"
    return _to_int(home_score.get(period_key)), _to_int(away_score.get(period_key))


def _extract_inning_runs(
    home_score: dict,
    away_score: dict,
    inning_range: tuple[int, int],
) -> tuple[int, int]:
    start, end = inning_range
    home_total = 0
    away_total = 0
    for idx in range(start, end + 1):
        h, a = _inning_runs(home_score, away_score, idx)
        home_total += h
        away_total += a
    return home_total, away_total


def _extract_game_runs(home_score: dict, away_score: dict) -> tuple[int, int]:
    home = home_score.get("current")
    away = away_score.get("current")
    if home is not None and away is not None:
        return _to_int(home), _to_int(away)
    home_total = 0
    away_total = 0
    for idx in range(9):
        h, a = _inning_runs(home_score, away_score, idx)
        home_total += h
        away_total += a
    return home_total, away_total


def _game_status_label(status_type: str, description: str) -> str:
    desc = (description or "").lower()
    if status_type == "finished" or desc in {"ended", "finished", "aet"}:
        return "Final"
    if status_type in {"inprogress", "live"}:
        return "Live"
    if status_type in {"notstarted", "postponed", "canceled", "cancelled"}:
        return "Preview"
    return status_type or "unknown"


def find_match(
    game_date: str,
    team: str | None,
    opponent: str | None,
    *,
    allow_live: bool = True,
    skip_db: bool = False,
) -> tuple[Optional[dict[str, Any]], str]:
    best: dict[str, Any] | None = None
    source = "sofascore_baseball"

    if not skip_db:
        db_event = sofascore_db.lookup_event("baseball", game_date, team, opponent)
        if db_event:
            best, source, _ = sofascore_live_lookup.refresh_db_event_if_stale(
                "baseball", game_date, db_event, allow_live=allow_live
            )

    if best is None and allow_live:
        live_event = sofascore_live_lookup.find_live_event(
            "baseball", game_date, team, opponent
        )
        if live_event is not None:
            best = live_event
            source = "sofascore_baseball"
            stored_date = sofascore_db.utc_game_date_from_event(live_event, game_date)
            sofascore_db.upsert_event("baseball", stored_date, live_event)

    if best is None:
        return None, "sofascore_baseball"

    status_type = str((best.get("status") or {}).get("type") or "").lower()
    description = str((best.get("status") or {}).get("description") or "")
    finished = sofascore_live_lookup.event_is_finished(best)

    return {
        "match_id": best.get("id"),
        "home_team": (best.get("homeTeam") or {}).get("name", ""),
        "away_team": (best.get("awayTeam") or {}).get("name", ""),
        "home_score_raw": best.get("homeScore") or {},
        "away_score_raw": best.get("awayScore") or {},
        "status_type": status_type,
        "game_status": _game_status_label(status_type, description),
        "finished": finished,
    }, source


def _load_lineups(match_id: str, *, allow_live: bool = True) -> dict[str, Any] | None:
    return sofascore_live_lookup.fetch_event_detail(
        match_id, "lineups", allow_live=allow_live
    )


def _iter_players(lineups: dict[str, Any]) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    for side in ("home", "away"):
        for entry in (lineups.get(side) or {}).get("players") or []:
            player = entry.get("player") or {}
            name = player.get("name") or player.get("shortName") or ""
            if name:
                players.append(
                    {
                        "name": name,
                        "statistics": entry.get("statistics") or {},
                    }
                )
    return players


def _match_player_name(player_name: str, candidates: list[str]) -> Optional[str]:
    if not player_name or not candidates:
        return None
    matches = difflib.get_close_matches(player_name, candidates, n=1, cutoff=0.5)
    if matches:
        return matches[0]
    target = player_name.lower()
    for name in candidates:
        if target in name.lower() or name.lower() in target:
            return name
    return None


def _resolve_team_side(
    team: str | None,
    home_team: str,
    away_team: str,
) -> Optional[str]:
    if not team:
        return None
    team_n = _norm(team)
    home_n = _norm(home_team)
    away_n = _norm(away_team)
    if team_n and (team_n in home_n or home_n in team_n):
        return "home"
    if team_n and (team_n in away_n or away_n in team_n):
        return "away"
    return None


def prop_check(
    player: Optional[str],
    market: str,
    game_date: str,
    team: Optional[str] = None,
    opponent: Optional[str] = None,
    pick: Optional[str] = None,
    line: Optional[float] = None,
    *,
    allow_live: bool = True,
    skip_db: bool = False,
) -> dict[str, Any]:
    market_norm = normalize_market(market)
    entry = BASEBALL_PROP_STAT_MAP.get(market_norm)
    if not entry:
        return {
            "found": False,
            "note": f"Market '{market_norm}' is not in BASEBALL_PROP_STAT_MAP.",
            "source": "sofascore_baseball",
        }

    inning_range, market_type = entry

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
            "note": f"No MLB SofaScore match for date={game_date}, team={team!r}.",
            "source": "sofascore_baseball",
        }

    match_id = str(match_info["match_id"])
    home_score = match_info["home_score_raw"]
    away_score = match_info["away_score_raw"]
    game_status = match_info["game_status"]
    settled = match_info["finished"]
    home_total, away_total = _extract_game_runs(home_score, away_score)

    base = {
        "match_id": match_id,
        "game_status": game_status,
        "home_score": home_total,
        "away_score": away_total,
        "settled": settled,
        "source": source,
    }

    if market_type == "total_runs":
        if inning_range == "game":
            h, a = home_total, away_total
        else:
            h, a = _extract_inning_runs(home_score, away_score, inning_range)
        return {"found": True, "stat_value": h + a, **base}

    if market_type == "odd_even":
        if inning_range == "game":
            h, a = home_total, away_total
        else:
            h, a = _extract_inning_runs(home_score, away_score, inning_range)
        return {"found": True, "stat_value": 1 if _is_odd(h + a) else 0, **base}

    if market_type == "moneyline":
        if inning_range == "game":
            h, a = home_total, away_total
        else:
            h, a = _extract_inning_runs(home_score, away_score, inning_range)
        if h > a:
            stat_value = 1.0
        elif a > h:
            stat_value = 0.0
        else:
            stat_value = 0.5
        return {"found": True, "stat_value": stat_value, **base}

    if market_type == "run_line":
        if inning_range == "game":
            h, a = home_total, away_total
        else:
            h, a = _extract_inning_runs(home_score, away_score, inning_range)
        return {"found": True, "stat_value": float(h - a), **base}

    if market_type == "team_total":
        if inning_range == "game":
            h, a = home_total, away_total
        else:
            h, a = _extract_inning_runs(home_score, away_score, inning_range)
        side = _resolve_team_side(team, match_info["home_team"], match_info["away_team"])
        if side == "home":
            return {"found": True, "stat_value": float(h), **base}
        if side == "away":
            return {"found": True, "stat_value": float(a), **base}
        return {
            "found": False,
            "note": f"Could not determine team side for team={team!r}",
            **base,
        }

    if market_type in _PLAYER_BATTING_FIELDS or market_type == "player_hits_runs_rbis":
        if not player:
            return {
                "found": False,
                "note": "player name is required for player prop markets.",
                **base,
            }
        lineups = _load_lineups(match_id, allow_live=allow_live)
        if not lineups:
            return {
                "found": False,
                "note": "Lineups not available from SofaScore yet.",
                **base,
            }
        players = _iter_players(lineups)
        names = [p["name"] for p in players]
        matched = _match_player_name(player, names)
        if not matched:
            return {
                "found": False,
                "note": f"Player '{player}' not found in SofaScore lineups.",
                "available_players": names[:30],
                **base,
            }
        stats = next(p["statistics"] for p in players if p["name"] == matched)
        if market_type == "player_hits_runs_rbis":
            stat_value = (
                _to_int(stats.get("battingHits"))
                + _to_int(stats.get("battingRuns"))
                + _to_int(stats.get("battingRbi"))
            )
        else:
            stat_value = _to_int(stats.get(_PLAYER_BATTING_FIELDS[market_type]))
        return {
            "found": True,
            "player": matched,
            "stat_value": float(stat_value),
            **base,
        }

    if market_type in _PLAYER_PITCHING_FIELDS:
        if not player:
            return {
                "found": False,
                "note": "player name is required for pitcher prop markets.",
                **base,
            }
        lineups = _load_lineups(match_id, allow_live=allow_live)
        if not lineups:
            return {
                "found": False,
                "note": "Lineups not available from SofaScore yet.",
                **base,
            }
        players = _iter_players(lineups)
        pitching_candidates = [
            p for p in players if _to_int(p["statistics"].get("pitchingOuts")) > 0
            or _to_int(p["statistics"].get("pitchingInningsPitched")) > 0
            or _to_int(p["statistics"].get(_PLAYER_PITCHING_FIELDS[market_type])) > 0
        ]
        names = [p["name"] for p in pitching_candidates] or [p["name"] for p in players]
        matched = _match_player_name(player, names)
        if not matched:
            return {
                "found": False,
                "note": f"Pitcher '{player}' not found in SofaScore lineups.",
                "available_pitchers": names[:30],
                **base,
            }
        stats = next(p["statistics"] for p in players if p["name"] == matched)
        stat_value = _to_int(stats.get(_PLAYER_PITCHING_FIELDS[market_type]))
        return {
            "found": True,
            "player": matched,
            "stat_value": float(stat_value),
            "stat_key": _PLAYER_PITCHING_FIELDS[market_type],
            **base,
        }

    return {
        "found": False,
        "note": f"Unknown market_type: {market_type}",
        **base,
    }
