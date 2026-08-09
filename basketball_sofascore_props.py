"""
basketball_sofascore_props.py
=============================
NBA / WNBA market settlement using the SofaScore public API.

Data sources
------------
  GET /unique-tournament/{132|486}/scheduled-events/{YYYY-MM-DD}
      → locate event by fuzzy team-name matching (or sofascore_db cache)

  GET /event/{id}
      → quarter scores:
            homeScore.period1..period4  → Q1–Q4 points
            homeScore.normaltime        → regulation total
            homeScore.overtime          → OT points (when present)
            homeScore.current           → final total (incl. OT)

  GET /event/{id}/incidents
      → scoring plays (incidentType="goal") with incidentClass
            onePoint | twoPoints | threePoints
        Used for first-basket markets and period player points.

  GET /event/{id}/lineups
      → per-player box score stats (points, rebounds, assists, …)

Public interface
----------------
  BASKETBALL_PROP_STAT_MAP – market name → (scope, market_type)
  prop_check(...)          – same return shape as hockey_sofascore_props.prop_check

Scope keys
----------
  full  – entire game (includes OT)
  reg   – regulation only (Q1–Q4)
  q1..q4
  h1    – 1st half (Q1+Q2)
  h2    – 2nd half regulation (Q3+Q4)
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Optional

import sofascore_db
import sofascore_live_lookup

# ---------------------------------------------------------------------------
# Market map
# ---------------------------------------------------------------------------

BASKETBALL_PROP_STAT_MAP: dict[str, tuple[str, str]] = {
    # ── Full-game ───────────────────────────────────────────────────────────
    "moneyline": ("full", "moneyline"),
    "spread": ("full", "point_spread"),
    "point_spread": ("full", "point_spread"),
    "total": ("full", "total_points"),
    "total_points": ("full", "total_points"),
    "team_total": ("full", "team_total"),
    "total_points_odd_even": ("full", "odd_even"),
    "will_there_be_overtime": ("full", "overtime"),
    "team_first_basket": ("full", "team_first_basket"),
    "first_basket": ("full", "first_basket_fg"),
    "first_basket_including_ft": ("full", "first_basket_any"),
    # ── Quarters ────────────────────────────────────────────────────────────
    "1st_quarter_moneyline": ("q1", "moneyline"),
    "2nd_quarter_moneyline": ("q2", "moneyline"),
    "3rd_quarter_moneyline": ("q3", "moneyline"),
    "4th_quarter_moneyline": ("q4", "moneyline"),
    "1st_quarter_point_spread": ("q1", "point_spread"),
    "2nd_quarter_point_spread": ("q2", "point_spread"),
    "3rd_quarter_point_spread": ("q3", "point_spread"),
    "4th_quarter_point_spread": ("q4", "point_spread"),
    "1st_quarter_total_points": ("q1", "total_points"),
    "2nd_quarter_total_points": ("q2", "total_points"),
    "3rd_quarter_total_points": ("q3", "total_points"),
    "4th_quarter_total_points": ("q4", "total_points"),
    "1st_quarter_team_total": ("q1", "team_total"),
    "2nd_quarter_team_total": ("q2", "team_total"),
    "3rd_quarter_team_total": ("q3", "team_total"),
    "4th_quarter_team_total": ("q4", "team_total"),
    "1st_quarter_total_points_odd_even": ("q1", "odd_even"),
    "2nd_quarter_total_points_odd_even": ("q2", "odd_even"),
    "3rd_quarter_total_points_odd_even": ("q3", "odd_even"),
    "4th_quarter_total_points_odd_even": ("q4", "odd_even"),
    # ── Halves ──────────────────────────────────────────────────────────────
    "1st_half_moneyline": ("h1", "moneyline"),
    "2nd_half_moneyline": ("h2", "moneyline"),
    "1st_half_point_spread": ("h1", "point_spread"),
    "2nd_half_point_spread": ("h2", "point_spread"),
    "1st_half_total_points": ("h1", "total_points"),
    "2nd_half_total_points": ("h2", "total_points"),
    "1st_half_team_total": ("h1", "team_total"),
    "2nd_half_team_total": ("h2", "team_total"),
    "1st_half_total_points_odd_even": ("h1", "odd_even"),
    "2nd_half_total_points_odd_even": ("h2", "odd_even"),
    # ── Period player points (from incidents) ───────────────────────────────
    "1st_quarter_player_points": ("q1", "player_points"),
    "2nd_quarter_player_points": ("q2", "player_points"),
    "3rd_quarter_player_points": ("q3", "player_points"),
    "4th_quarter_player_points": ("q4", "player_points"),
    "1st_half_player_points": ("h1", "player_points"),
    "2nd_half_player_points": ("h2", "player_points"),
    # ── Full-game player props (from lineups) ───────────────────────────────
    "player_points": ("full", "player_box_points"),
    "player_rebounds": ("full", "player_rebounds"),
    "player_assists": ("full", "player_assists"),
    "player_threes": ("full", "player_threes"),
    "player_made_threes": ("full", "player_threes"),
    "player_3pm": ("full", "player_threes"),
    "player_steals": ("full", "player_steals"),
    "player_blocks": ("full", "player_blocks"),
    "player_turnovers": ("full", "player_turnovers"),
    "player_minutes": ("full", "player_minutes"),
    "player_fg_made": ("full", "player_fg_made"),
    "player_ft_made": ("full", "player_ft_made"),
    "player_points_rebounds_assists": ("full", "player_pra"),
    "player_points_rebounds": ("full", "player_pr"),
    "player_points_assists": ("full", "player_pa"),
    "player_rebounds_assists": ("full", "player_ra"),
    "player_double_double": ("full", "player_double_double"),
    "player_triple_double": ("full", "player_triple_double"),
}

# Cumulative game-clock minutes for quarter boundaries (SofaScore incident.time).
_SCOPE_TIME_RANGE: dict[str, tuple[float, float]] = {
    "q1": (0.0, 12.0),
    "q2": (12.0, 24.0),
    "q3": (24.0, 36.0),
    "q4": (36.0, 48.0),
    "h1": (0.0, 24.0),
    "h2": (24.0, 48.0),
}

_FG_CLASSES = frozenset({"twopoints", "threepoints"})
_POINT_VALUES = {
    "onepoint": 1,
    "twopoints": 2,
    "threepoints": 3,
}


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    if not s:
        return ""
    out = s.lower()
    out = re.sub(
        r"\b(basketball|club|fc|the)\b",
        " ",
        out,
    )
    out = re.sub(r"[^a-z0-9]+", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _name_score(a: str | None, b: str | None) -> float:
    an, bn = _norm(a), _norm(b)
    if not an or not bn:
        return 0.0
    if an == bn:
        return 1.0
    if an in bn or bn in an:
        return 0.93
    return difflib.SequenceMatcher(a=an, b=bn).ratio()


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    """Search SofaScore for an NBA/WNBA match on *game_date*."""
    best: dict[str, Any] | None = None
    source = "sofascore_basketball"

    if not skip_db:
        db_event = sofascore_db.lookup_event("basketball", game_date, team, opponent)
        if db_event:
            best, source, _ = sofascore_live_lookup.refresh_db_event_if_stale(
                "basketball", game_date, db_event, allow_live=allow_live
            )

    if best is None and allow_live:
        live_event = sofascore_live_lookup.find_live_event(
            "basketball", game_date, team, opponent
        )
        if live_event is not None:
            best = live_event
            source = "sofascore_basketball"
            stored_date = sofascore_db.utc_game_date_from_event(live_event, game_date)
            sofascore_db.upsert_event("basketball", stored_date, live_event)

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


def _basketball_scores(
    home_score_raw: dict[str, Any],
    away_score_raw: dict[str, Any],
) -> dict[str, Any]:
    """
    Parse SofaScore basketball homeScore / awayScore.

    Returns scope → (home, away) plus had_ot flag.
    """
    h_q1 = _to_int(home_score_raw.get("period1", 0))
    h_q2 = _to_int(home_score_raw.get("period2", 0))
    h_q3 = _to_int(home_score_raw.get("period3", 0))
    h_q4 = _to_int(home_score_raw.get("period4", 0))
    a_q1 = _to_int(away_score_raw.get("period1", 0))
    a_q2 = _to_int(away_score_raw.get("period2", 0))
    a_q3 = _to_int(away_score_raw.get("period3", 0))
    a_q4 = _to_int(away_score_raw.get("period4", 0))

    h_reg_raw = home_score_raw.get("normaltime")
    a_reg_raw = away_score_raw.get("normaltime")
    h_reg = (
        _to_int(h_reg_raw)
        if h_reg_raw is not None
        else (h_q1 + h_q2 + h_q3 + h_q4)
    )
    a_reg = (
        _to_int(a_reg_raw)
        if a_reg_raw is not None
        else (a_q1 + a_q2 + a_q3 + a_q4)
    )

    h_full = _to_int(
        home_score_raw.get("current") or home_score_raw.get("normaltime") or 0
    )
    a_full = _to_int(
        away_score_raw.get("current") or away_score_raw.get("normaltime") or 0
    )

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
        "q1": (h_q1, a_q1),
        "q2": (h_q2, a_q2),
        "q3": (h_q3, a_q3),
        "q4": (h_q4, a_q4),
        "h1": (h_q1 + h_q2, a_q1 + a_q2),
        "h2": (h_q3 + h_q4, a_q3 + a_q4),
        "had_ot": had_ot,
    }


# ---------------------------------------------------------------------------
# Incidents (scoring plays)
# ---------------------------------------------------------------------------


def _get_incidents(match_id: int, *, allow_live: bool = True) -> list[dict[str, Any]]:
    payload = sofascore_live_lookup.fetch_event_detail(
        str(match_id), "incidents", allow_live=allow_live
    ) or {}
    return payload.get("incidents") or []


def _incident_player_name(inc: dict) -> str:
    player = inc.get("player") or {}
    return (
        str(player.get("name") or player.get("shortName") or "")
        or str(inc.get("playerName") or "")
    ).strip()


def _incident_points(inc: dict) -> int:
    cls = str(inc.get("incidentClass") or "").lower().replace("_", "")
    return _POINT_VALUES.get(cls, 0)


def _scoring_plays(incidents: list[dict]) -> list[dict[str, Any]]:
    """
    Return scoring plays in chronological order.

    SofaScore returns incidents newest-first; reverse so earliest is first.
    Each play: {player, is_home, points, class, time, is_fg}
    """
    plays: list[dict[str, Any]] = []
    for inc in incidents:
        if str(inc.get("incidentType") or "").lower() != "goal":
            continue
        pts = _incident_points(inc)
        if pts <= 0:
            continue
        cls = str(inc.get("incidentClass") or "").lower().replace("_", "")
        time_v = _to_float(inc.get("time"))
        plays.append(
            {
                "player": _incident_player_name(inc),
                "is_home": bool(inc.get("isHome")),
                "points": pts,
                "class": cls,
                "time": time_v if time_v is not None else 0.0,
                "is_fg": cls in _FG_CLASSES,
            }
        )
    plays.reverse()
    return plays


def _plays_in_scope(plays: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    """Filter scoring plays to a quarter/half using cumulative clock minutes."""
    if scope == "full":
        return plays
    if scope == "reg":
        return [p for p in plays if p["time"] <= 48.0]
    bounds = _SCOPE_TIME_RANGE.get(scope)
    if not bounds:
        return plays
    lo, hi = bounds
    out: list[dict[str, Any]] = []
    for p in plays:
        t = p["time"]
        if lo == 0.0:
            if 0.0 <= t <= hi:
                out.append(p)
        elif lo < t <= hi:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Player stats (lineups)
# ---------------------------------------------------------------------------


def _get_player_stats(match_id: int, *, allow_live: bool = True) -> list[dict[str, Any]]:
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
    for k in keys:
        v = stats.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _double_triple_count(stats: dict[str, Any]) -> int:
    cats = [
        _player_stat_value(stats, "points") or 0,
        _player_stat_value(stats, "rebounds") or 0,
        _player_stat_value(stats, "assists") or 0,
        _player_stat_value(stats, "steals") or 0,
        _player_stat_value(stats, "blocks") or 0,
    ]
    return sum(1 for c in cats if c >= 10)


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
    Settle an NBA/WNBA bet using SofaScore data.

    stat_value semantics
    --------------------
    moneyline / point_spread : home − away point differential in scope
    total_points             : combined points in scope
    team_total               : identified team's points in scope
    odd_even                 : 1 = odd, 0 = even
    overtime                 : 1 = OT occurred, 0 = regulation only
    team_first_basket        : "home" | "away"
    first_basket_*           : 1 = named player scored first, else 0
    player_points (period)   : integer points for player in scope
    player_box_* / composites: integer (or 1/0 for double/triple-double)
    """
    market_norm = market.strip().lower().replace(" ", "_")
    entry = BASKETBALL_PROP_STAT_MAP.get(market_norm)
    if not entry:
        return {
            "found": False,
            "note": f"Market '{market_norm}' not in BASKETBALL_PROP_STAT_MAP.",
            "source": "sofascore_basketball",
        }

    scope, market_type = entry

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
                f"No SofaScore basketball match found for "
                f"date={game_date}, team={team!r}, opponent={opponent!r}."
            ),
            "source": source,
        }

    match_id = match_info["match_id"]
    home_name = match_info["home_team"]
    away_name = match_info["away_team"]
    finished = match_info["finished"]

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

    scores = _basketball_scores(
        match_info["home_score_raw"],
        match_info["away_score_raw"],
    )
    h_full, a_full = scores["full"]
    had_ot = scores["had_ot"]
    h_s, a_s = scores.get(scope, scores["full"])

    _incidents: list[dict] | None = None
    _players: list[dict] | None = None
    _plays: list[dict] | None = None

    def _incidents_cached() -> list[dict]:
        nonlocal _incidents
        if _incidents is None:
            _incidents = _get_incidents(match_id, allow_live=allow_live)
        return _incidents

    def _plays_cached() -> list[dict]:
        nonlocal _plays
        if _plays is None:
            _plays = _scoring_plays(_incidents_cached())
        return _plays

    def _players_cached() -> list[dict]:
        nonlocal _players
        if _players is None:
            _players = _get_player_stats(match_id, allow_live=allow_live)
        return _players

    stat_value: float | int | str | None = None

    # ── Score-based markets ─────────────────────────────────────────────────
    if market_type == "moneyline":
        stat_value = h_s - a_s

    elif market_type == "point_spread":
        stat_value = h_s - a_s

    elif market_type == "total_points":
        stat_value = h_s + a_s

    elif market_type == "team_total":
        if not team:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "team is required for team_total markets.",
                "source": "sofascore_basketball",
            }
        is_home = _name_score(team, home_name) >= _name_score(team, away_name)
        stat_value = h_s if is_home else a_s

    elif market_type == "odd_even":
        stat_value = 1 if (h_s + a_s) % 2 == 1 else 0

    elif market_type == "overtime":
        stat_value = 1 if had_ot else 0

    # ── First basket markets (incidents) ────────────────────────────────────
    elif market_type in ("team_first_basket", "first_basket_fg", "first_basket_any"):
        plays = _plays_cached()
        if market_type == "team_first_basket":
            first_fg = next((p for p in plays if p["is_fg"]), None)
            if first_fg is None:
                return {
                    "found": False,
                    "match_id": match_id,
                    "settled": finished,
                    "note": "No field-goal scoring plays found in SofaScore incidents.",
                    "source": "sofascore_basketball",
                }
            stat_value = "home" if first_fg["is_home"] else "away"
        else:
            target = selection or pick or ""
            if not target:
                return {
                    "found": False,
                    "match_id": match_id,
                    "settled": finished,
                    "note": "selection/pick (player name) is required for first-basket markets.",
                    "source": "sofascore_basketball",
                }
            if market_type == "first_basket_fg":
                first = next((p for p in plays if p["is_fg"]), None)
            else:
                first = plays[0] if plays else None
            if first is None:
                return {
                    "found": False,
                    "match_id": match_id,
                    "settled": finished,
                    "note": "No scoring plays found in SofaScore incidents.",
                    "source": "sofascore_basketball",
                }
            target_n = _norm(target)
            first_n = _norm(first["player"])
            stat_value = (
                1
                if first_n
                and (target_n == first_n or target_n in first_n or first_n in target_n)
                else 0
            )

    # ── Period player points (incidents) ────────────────────────────────────
    elif market_type == "player_points":
        target = selection or pick or team or ""
        if not target:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "selection/player is required for period player_points markets.",
                "source": "sofascore_basketball",
            }
        # If pick is over/under, the player name is in selection/team.
        if _norm(target) in {"over", "under", "yes", "no", "odd", "even"}:
            target = selection or team or ""
        if not target or _norm(target) in {"over", "under"}:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "player name is required for period player_points markets.",
                "source": "sofascore_basketball",
            }
        scoped = _plays_in_scope(_plays_cached(), scope)
        target_n = _norm(target)

        # Resolve to a single player name from scoring plays / lineups first.
        candidate_names = {p["player"] for p in scoped if p.get("player")}
        for pl in _players_cached():
            if pl.get("name"):
                candidate_names.add(pl["name"])

        best_name = ""
        best_score = 0.0
        for name in candidate_names:
            pn = _norm(name)
            if not pn:
                continue
            if target_n == pn or target_n in pn or pn in target_n:
                best_name = name
                best_score = 1.0
                break
            s = difflib.SequenceMatcher(a=target_n, b=pn).ratio()
            if s > best_score:
                best_score = s
                best_name = name

        if not best_name or best_score < 0.72:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": f"Player '{target}' not found in SofaScore data.",
                "source": "sofascore_basketball",
            }

        best_n = _norm(best_name)
        total = sum(
            p["points"]
            for p in scoped
            if _norm(p.get("player")) == best_n
        )
        stat_value = total

    # ── Full-game player box props (lineups) ────────────────────────────────
    elif market_type in (
        "player_box_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_steals",
        "player_blocks",
        "player_turnovers",
        "player_minutes",
        "player_fg_made",
        "player_ft_made",
        "player_pra",
        "player_pr",
        "player_pa",
        "player_ra",
        "player_double_double",
        "player_triple_double",
    ):
        target = selection or team or ""
        if not target or _norm(target) in {"over", "under"}:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": "selection or team (player name) is required for player prop markets.",
                "source": "sofascore_basketball",
            }
        pstats = _find_player_stats(_players_cached(), target)
        if pstats is None:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": f"Player '{target}' not found in SofaScore lineups.",
                "source": "sofascore_basketball",
            }

        if market_type == "player_box_points":
            stat_value = _player_stat_value(pstats, "points")
        elif market_type == "player_rebounds":
            stat_value = _player_stat_value(pstats, "rebounds")
        elif market_type == "player_assists":
            stat_value = _player_stat_value(pstats, "assists")
        elif market_type == "player_threes":
            stat_value = _player_stat_value(pstats, "threePointsMade")
        elif market_type == "player_steals":
            stat_value = _player_stat_value(pstats, "steals")
        elif market_type == "player_blocks":
            stat_value = _player_stat_value(pstats, "blocks")
        elif market_type == "player_turnovers":
            stat_value = _player_stat_value(pstats, "turnovers")
        elif market_type == "player_minutes":
            secs = _player_stat_value(pstats, "secondsPlayed")
            stat_value = int(secs // 60) if secs is not None else None
        elif market_type == "player_fg_made":
            stat_value = _player_stat_value(pstats, "fieldGoalsMade")
        elif market_type == "player_ft_made":
            stat_value = _player_stat_value(pstats, "freeThrowsMade")
        elif market_type == "player_pra":
            pts = _player_stat_value(pstats, "points") or 0
            reb = _player_stat_value(pstats, "rebounds") or 0
            ast = _player_stat_value(pstats, "assists") or 0
            stat_value = pts + reb + ast
        elif market_type == "player_pr":
            pts = _player_stat_value(pstats, "points") or 0
            reb = _player_stat_value(pstats, "rebounds") or 0
            stat_value = pts + reb
        elif market_type == "player_pa":
            pts = _player_stat_value(pstats, "points") or 0
            ast = _player_stat_value(pstats, "assists") or 0
            stat_value = pts + ast
        elif market_type == "player_ra":
            reb = _player_stat_value(pstats, "rebounds") or 0
            ast = _player_stat_value(pstats, "assists") or 0
            stat_value = reb + ast
        elif market_type == "player_double_double":
            stat_value = 1 if _double_triple_count(pstats) >= 2 else 0
        elif market_type == "player_triple_double":
            stat_value = 1 if _double_triple_count(pstats) >= 3 else 0

        if stat_value is None:
            return {
                "found": False,
                "match_id": match_id,
                "settled": finished,
                "note": (
                    f"Stat for '{market_type}' not available in SofaScore "
                    f"lineups for player '{target}'."
                ),
                "source": "sofascore_basketball",
            }

    else:
        return {
            "found": False,
            "match_id": match_id,
            "settled": finished,
            "note": f"Unhandled market_type '{market_type}'.",
            "source": "sofascore_basketball",
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
