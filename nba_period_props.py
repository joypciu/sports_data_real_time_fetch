"""
nba_period_props.py
===================
NBA period/specialty market settlement using DataBallr endpoints.

Handles:
  - Quarter totals, moneylines, point spreads, team totals
  - Half totals, moneylines, point spreads, team totals
  - Odd/even totals (quarter/half/game)
  - First basket / first basket including FT / team first basket
  - Overtime yes/no

Data sources:
  - /api/live/box-score/{game_id}?date=YYYY-MM-DD
  - /api/bdl/plays/{game_id}
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

_log = logging.getLogger(__name__)

_BASE = "https://api.databallr.com/api"
_TIMEOUT = 2.5


# market name -> (scope, market_type)
NBA_PROP_STAT_MAP: dict[str, tuple[str, str]] = {
    # Quarter totals
    "1st_quarter_total_points": ("q1", "total_points"),
    "2nd_quarter_total_points": ("q2", "total_points"),
    "3rd_quarter_total_points": ("q3", "total_points"),
    "4th_quarter_total_points": ("q4", "total_points"),
    "1st_quarter_total_points_odd_even": ("q1", "odd_even"),
    "2nd_quarter_total_points_odd_even": ("q2", "odd_even"),
    "3rd_quarter_total_points_odd_even": ("q3", "odd_even"),

    # Quarter moneyline / spread / team total
    "1st_quarter_moneyline": ("q1", "moneyline"),
    "2nd_quarter_moneyline": ("q2", "moneyline"),
    "3rd_quarter_moneyline": ("q3", "moneyline"),
    "4th_quarter_moneyline": ("q4", "moneyline"),
    "1st_quarter_point_spread": ("q1", "point_spread"),
    "2nd_quarter_point_spread": ("q2", "point_spread"),
    "3rd_quarter_point_spread": ("q3", "point_spread"),
    "4th_quarter_point_spread": ("q4", "point_spread"),
    "1st_quarter_team_total": ("q1", "team_total"),
    "2nd_quarter_team_total": ("q2", "team_total"),
    "3rd_quarter_team_total": ("q3", "team_total"),
    "4th_quarter_team_total": ("q4", "team_total"),

    # Half totals
    "1st_half_total_points": ("h1", "total_points"),
    "1st_half_total_points_odd_even": ("h1", "odd_even"),
    "2nd_half_total_points_odd_even": ("h2", "odd_even"),

    # Half moneyline / spread / team total
    "1st_half_moneyline": ("h1", "moneyline"),
    "1st_half_point_spread": ("h1", "point_spread"),
    "1st_half_team_total": ("h1", "team_total"),

    # Player period points (from play-by-play scoring events)
    "1st_quarter_player_points": ("q1", "player_points"),
    "2nd_quarter_player_points": ("q2", "player_points"),
    "3rd_quarter_player_points": ("q3", "player_points"),
    "4th_quarter_player_points": ("q4", "player_points"),
    "1st_half_player_points": ("h1", "player_points"),
    "2nd_half_player_points": ("h2", "player_points"),

    # Full-game odd/even + overtime
    "total_points_odd_even": ("game", "odd_even"),
    "will_there_be_overtime": ("game", "overtime_yes_no"),

    # Sequence markets from plays
    "team_first_basket": ("game", "team_first_basket"),
    "first_basket": ("game", "first_basket_fg"),
    "first_basket_including_ft": ("game", "first_basket_any"),
}

# Backward-compatible alias used by existing market-check routing code.
NBA_PERIOD_PROP_STAT_MAP = NBA_PROP_STAT_MAP


def _get(url: str, params: dict | None = None) -> dict:
    resp = httpx.get(url, params=params, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _fetch_box_score(game_id: int, game_date: str) -> dict:
    return _get(f"{_BASE}/live/box-score/{game_id}", params={"date": game_date})


def _fetch_plays(game_id: int) -> dict:
    return _get(f"{_BASE}/bdl/plays/{game_id}")


def _fetch_games_by_date(game_date: str) -> dict:
    return _get(f"{_BASE}/live/games/{game_date}")


def _status_to_settled(status: str | None) -> tuple[str, bool]:
    s = (status or "").strip().lower()
    if s == "final":
        return "Final", True
    if s in {"in progress", "live"}:
        return "Live", False
    if s in {"scheduled", "pre", "pregame"}:
        return "Preview", False
    return "unknown", False


def _period_scores(box_data: dict, scope: str) -> tuple[int | None, int | None]:
    hq1 = box_data.get("home_q1")
    hq2 = box_data.get("home_q2")
    hq3 = box_data.get("home_q3")
    hq4 = box_data.get("home_q4")
    aq1 = box_data.get("visitor_q1")
    aq2 = box_data.get("visitor_q2")
    aq3 = box_data.get("visitor_q3")
    aq4 = box_data.get("visitor_q4")

    if scope == "q1":
        return hq1, aq1
    if scope == "q2":
        return hq2, aq2
    if scope == "q3":
        return hq3, aq3
    if scope == "q4":
        return hq4, aq4
    if scope == "h1":
        if None in (hq1, hq2, aq1, aq2):
            return None, None
        return (hq1 + hq2), (aq1 + aq2)
    if scope == "h2":
        if None in (hq3, hq4, aq3, aq4):
            return None, None
        return (hq3 + hq4), (aq3 + aq4)
    if scope == "game":
        return box_data.get("home_team_score"), box_data.get("visitor_team_score")
    return None, None


def _extract_first_scoring_play(plays: list[dict], include_ft: bool) -> dict | None:
    for p in sorted(plays, key=lambda x: x.get("order", 10**9)):
        if not p.get("scoring_play"):
            continue
        text = (p.get("text") or "").lower()
        if not include_ft and "free throw" in text:
            continue
        return p
    return None


def _extract_player_from_play_text(text: str) -> str | None:
    m = re.match(r"^([^()]+?)\s+(makes|misses)\b", (text or "").strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def _normalize_name(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (v or "").lower()).strip()


def _name_matches(query: str, candidate: str) -> bool:
    q = _normalize_name(query)
    c = _normalize_name(candidate)
    if not q or not c:
        return False
    return q == c or q in c or c in q


def _period_in_scope(period: Any, scope: str) -> bool:
    p = int(period or 0)
    if scope == "q1":
        return p == 1
    if scope == "q2":
        return p == 2
    if scope == "q3":
        return p == 3
    if scope == "q4":
        return p == 4
    if scope == "h1":
        return p in {1, 2}
    if scope == "h2":
        return p in {3, 4}
    if scope == "game":
        return p >= 1
    return False


def _boxscore_player_name(box_data: dict, query: str) -> str | None:
    for side in ("home_team", "visitor_team"):
        team = box_data.get(side) or {}
        for pl in team.get("players") or []:
            p = pl.get("player") or {}
            full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if _name_matches(query, full):
                return full
    return None


def _resolve_game_id(game_date: str, team: Optional[str]) -> int | None:
    """Resolve a game id from date + optional team filter using live games feed."""
    date_candidates = [game_date]
    try:
        base = datetime.strptime(game_date, "%Y-%m-%d").date()
        date_candidates.extend(
            [
                (base - timedelta(days=1)).isoformat(),
                (base + timedelta(days=1)).isoformat(),
            ]
        )
    except ValueError:
        pass

    seen_dates: set[str] = set()
    date_candidates = [d for d in date_candidates if not (d in seen_dates or seen_dates.add(d))]

    for d in date_candidates:
        try:
            data = _fetch_games_by_date(d)
        except Exception as exc:
            _log.warning("nba live games fetch failed date=%s: %s", d, exc)
            continue

        games = (data or {}).get("data") or []
        if not games:
            continue

        if not team:
            gid = games[0].get("id")
            return int(gid) if gid is not None else None

        t = team.strip().lower()
        for g in games:
            home = g.get("home_team") or {}
            away = g.get("visitor_team") or {}
            home_name = (home.get("full_name") or "").lower()
            away_name = (away.get("full_name") or "").lower()
            home_abbr = (home.get("abbreviation") or "").lower()
            away_abbr = (away.get("abbreviation") or "").lower()
            if (
                t in home_name
                or t in away_name
                or t == home_abbr
                or t == away_abbr
                or home_name in t
                or away_name in t
            ):
                gid = g.get("id")
                return int(gid) if gid is not None else None
    return None


def _result_base(
    *,
    found: bool,
    player: str,
    stat_value: Any,
    stat_key: str,
    game_status: str,
    settled: bool,
    note: str = "",
) -> dict[str, Any]:
    return {
        "found": found,
        "player": player,
        "stat_value": stat_value,
        "stat_key": stat_key,
        "game_status": game_status,
        "settled": settled,
        "source": "nba_period_props",
        "note": note,
    }


def prop_check(
    player: str,
    market: str,
    game_date: str,
    team: Optional[str] = None,
    game_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Guide-compliant market check contract for NBA period/specialty settlement.

    Required keys returned:
      found, player, stat_value, stat_key, game_status, settled, source, note
    """
    market_norm = market.strip().lower().replace(" ", "_")

    if market_norm not in NBA_PROP_STAT_MAP:
        return _result_base(
            found=False,
            player=player,
            stat_value=None,
            stat_key=market_norm,
            game_status="unknown",
            settled=False,
            note=f"Market '{market_norm}' is not in NBA_PROP_STAT_MAP.",
        )

    scope, market_type = NBA_PROP_STAT_MAP[market_norm]

    resolved_game_id = game_id
    if resolved_game_id is None:
        resolved_game_id = _resolve_game_id(game_date, team)
    if resolved_game_id is None:
        return _result_base(
            found=False,
            player=player,
            stat_value=None,
            stat_key=market_type,
            game_status="unknown",
            settled=False,
            note=f"No NBA game found for date={game_date}, team={team!r}.",
        )

    try:
        box_resp = _fetch_box_score(resolved_game_id, game_date)
    except Exception as exc:
        _log.warning("nba box-score fetch failed game_id=%s: %s", resolved_game_id, exc)
        out = _result_base(
            found=False,
            player=player,
            stat_value=None,
            stat_key=market_type,
            game_status="unknown",
            settled=False,
            note=f"Live box-score fetch failed: {exc}",
        )
        out["game_id"] = resolved_game_id
        return out

    box_data = (box_resp or {}).get("data") or {}
    game_status, settled = _status_to_settled(box_data.get("status"))
    home_score = box_data.get("home_team_score")
    away_score = box_data.get("visitor_team_score")

    if market_type in {"total_points", "odd_even", "moneyline", "point_spread", "team_total", "overtime_yes_no"}:
        home_pts, away_pts = _period_scores(box_data, scope)
        if home_pts is None or away_pts is None:
            out = _result_base(
                found=False,
                player=player,
                stat_value=None,
                stat_key=market_type,
                game_status=game_status,
                settled=settled,
                note=f"Missing period score data for scope '{scope}'.",
            )
            out["game_id"] = resolved_game_id
            out["home_score"] = home_score
            out["away_score"] = away_score
            return out

        if market_type == "total_points":
            value: Any = int(home_pts + away_pts)
        elif market_type == "odd_even":
            total = int(home_pts + away_pts)
            value = 1 if (total % 2 == 1) else 0
        elif market_type == "moneyline":
            if home_pts > away_pts:
                value = 1.0
            elif away_pts > home_pts:
                value = 0.0
            else:
                value = 0.5
        elif market_type == "point_spread":
            value = float(home_pts - away_pts)
        elif market_type == "team_total":
            home_name = ((box_data.get("home_team") or {}).get("full_name") or "").lower()
            away_name = ((box_data.get("visitor_team") or {}).get("full_name") or "").lower()
            home_abbr = ((box_data.get("home_team") or {}).get("abbreviation") or "").lower()
            away_abbr = ((box_data.get("visitor_team") or {}).get("abbreviation") or "").lower()
            team_norm = (team or "").strip().lower()

            if team_norm and (team_norm in home_name or team_norm == home_abbr or home_name in team_norm):
                value = float(home_pts)
            elif team_norm and (team_norm in away_name or team_norm == away_abbr or away_name in team_norm):
                value = float(away_pts)
            else:
                out = _result_base(
                    found=False,
                    player=player,
                    stat_value=None,
                    stat_key=market_type,
                    game_status=game_status,
                    settled=settled,
                    note=f"Could not determine team side for team={team!r}.",
                )
                out["game_id"] = resolved_game_id
                out["home_score"] = home_score
                out["away_score"] = away_score
                return out
        else:
            value = (box_data.get("period") or 0) > 4

        out = _result_base(
            found=True,
            player=player,
            stat_value=value,
            stat_key=market_type,
            game_status=game_status,
            settled=settled,
        )
        out["game_id"] = resolved_game_id
        out["home_score"] = home_score
        out["away_score"] = away_score
        return out

    if market_type == "player_points":
        if not player or not player.strip():
            out = _result_base(
                found=False,
                player=player,
                stat_value=None,
                stat_key=market_type,
                game_status=game_status,
                settled=settled,
                note="player is required for player_points markets.",
            )
            out["game_id"] = resolved_game_id
            out["home_score"] = home_score
            out["away_score"] = away_score
            return out

        try:
            plays_resp = _fetch_plays(resolved_game_id)
        except Exception as exc:
            _log.warning("nba plays fetch failed game_id=%s: %s", resolved_game_id, exc)
            out = _result_base(
                found=False,
                player=player,
                stat_value=None,
                stat_key=market_type,
                game_status=game_status,
                settled=settled,
                note=f"Plays fetch failed: {exc}",
            )
            out["game_id"] = resolved_game_id
            out["home_score"] = home_score
            out["away_score"] = away_score
            return out

        plays = (plays_resp or {}).get("data") or []
        box_name = _boxscore_player_name(box_data, player)
        resolved_player = box_name or player

        total_points = 0
        for p in plays:
            if not p.get("scoring_play"):
                continue
            if not _period_in_scope(p.get("period"), scope):
                continue
            scorer = _extract_player_from_play_text(str(p.get("text") or ""))
            if not scorer or not _name_matches(resolved_player, scorer):
                continue
            sv = p.get("score_value")
            try:
                pts = int(sv)
            except (TypeError, ValueError):
                continue
            if pts > 0:
                total_points += pts

        if box_name is None:
            out = _result_base(
                found=False,
                player=player,
                stat_value=None,
                stat_key=market_type,
                game_status=game_status,
                settled=settled,
                note=f"Player '{player}' not found in game box score.",
            )
            out["game_id"] = resolved_game_id
            out["home_score"] = home_score
            out["away_score"] = away_score
            return out

        out = _result_base(
            found=True,
            player=resolved_player,
            stat_value=float(total_points),
            stat_key=market_type,
            game_status=game_status,
            settled=settled,
        )
        out["game_id"] = resolved_game_id
        out["home_score"] = home_score
        out["away_score"] = away_score
        return out

    try:
        plays_resp = _fetch_plays(resolved_game_id)
    except Exception as exc:
        _log.warning("nba plays fetch failed game_id=%s: %s", resolved_game_id, exc)
        out = _result_base(
            found=False,
            player=player,
            stat_value=None,
            stat_key=market_type,
            game_status=game_status,
            settled=settled,
            note=f"Plays fetch failed: {exc}",
        )
        out["game_id"] = resolved_game_id
        out["home_score"] = home_score
        out["away_score"] = away_score
        return out

    plays = (plays_resp or {}).get("data") or []
    if not plays:
        out = _result_base(
            found=False,
            player=player,
            stat_value=None,
            stat_key=market_type,
            game_status=game_status,
            settled=settled,
            note="No plays found in play-by-play feed.",
        )
        out["game_id"] = resolved_game_id
        out["home_score"] = home_score
        out["away_score"] = away_score
        return out

    include_ft = market_type == "first_basket_any"
    first = _extract_first_scoring_play(plays, include_ft=include_ft)
    if not first:
        out = _result_base(
            found=False,
            player=player,
            stat_value=None,
            stat_key=market_type,
            game_status=game_status,
            settled=settled,
            note="No qualifying scoring play found.",
        )
        out["game_id"] = resolved_game_id
        out["home_score"] = home_score
        out["away_score"] = away_score
        return out

    first_team = ((first.get("team") or {}).get("abbreviation") or "").upper()
    first_text = first.get("text") or ""
    first_player = _extract_player_from_play_text(first_text)

    if market_type == "team_first_basket":
        stat_value = first_team
        resolved_player = player
    else:
        stat_value = first_player
        resolved_player = first_player or player

    out = _result_base(
        found=True,
        player=resolved_player,
        stat_value=stat_value,
        stat_key=market_type,
        game_status=game_status,
        settled=settled,
    )
    out["game_id"] = resolved_game_id
    out["home_score"] = home_score
    out["away_score"] = away_score
    out["first_team"] = first_team
    out["first_play_text"] = first_text
    return out


def market_check(
    game_id: int,
    market: str,
    game_date: str,
    team: Optional[str] = None,
) -> dict[str, Any]:
    return prop_check(
        player="",
        market=market,
        game_date=game_date,
        team=team,
        game_id=game_id,
    )
