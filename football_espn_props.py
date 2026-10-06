"""
NFL / American-football specialty markets via ESPN summary scoring plays.

Only used when stats_api sport is football/nfl (not soccer).
"""
from __future__ import annotations

import re
from datetime import date as dt_date, timedelta
from typing import Any, Optional

import httpx

FOOTBALL_SPORTS = frozenset(
    {
        "football",
        "nfl",
        "american football",
        "american-football",
        "ncaaf",
        "college football",
        "college-football",
    }
)

FOOTBALL_PROP_STAT_MAP: dict[str, str] = {
    "total_touchdowns": "total_touchdowns",
    "total_field_goals": "total_field_goals",
    "team_total_touchdowns": "team_total_touchdowns",
    "1st_half_total_touchdowns": "1st_half_total_touchdowns",
    "first_team_to_score": "first_team_to_score",
}

_LEAGUE_PATHS = ("nfl", "college-football")
_TIMEOUT = 8.0


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _name_hit(a: str, b: str) -> bool:
    an, bn = _norm(a), _norm(b)
    if not an or not bn:
        return False
    return an == bn or an in bn or bn in an


def _play_is_td(play: dict[str, Any]) -> bool:
    scoring = str((play.get("scoringType") or {}).get("abbreviation") or "").upper()
    typ = str((play.get("type") or {}).get("abbreviation") or "").upper()
    text = str(play.get("text") or play.get("shortText") or "").lower()
    if scoring in {"TD"} or typ in {"TD"}:
        return True
    return "touchdown" in text and "field goal" not in text


def _play_is_fg(play: dict[str, Any]) -> bool:
    scoring = str((play.get("scoringType") or {}).get("abbreviation") or "").upper()
    typ = str((play.get("type") or {}).get("abbreviation") or "").upper()
    text = str(play.get("text") or play.get("shortText") or "").lower()
    if scoring in {"FG"} or typ in {"FG"}:
        return True
    return "field goal" in text


def _play_team_name(play: dict[str, Any]) -> str:
    team = play.get("team") or {}
    return str(team.get("displayName") or team.get("name") or "")


def _period_number(play: dict[str, Any]) -> int:
    period = play.get("period") or {}
    try:
        return int(period.get("number") or play.get("period") or 0)
    except (TypeError, ValueError):
        return 0


def _fetch_scoreboard(league: str, yyyymmdd: str) -> dict[str, Any] | None:
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
        f"?dates={yyyymmdd}"
    )
    try:
        r = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _fetch_summary(league: str, event_id: str) -> dict[str, Any] | None:
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/football/{league}/summary"
        f"?event={event_id}"
    )
    try:
        r = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _match_event(
    scoreboard: dict[str, Any],
    team: str | None,
    opponent: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    hints = [n for n in (_norm(team), _norm(opponent)) if n]
    if not hints:
        return None, None
    for event in scoreboard.get("events") or []:
        for comp in event.get("competitions") or []:
            names = []
            for c in comp.get("competitors") or []:
                t = c.get("team") or {}
                names.append(
                    str(t.get("displayName") or t.get("name") or t.get("shortDisplayName") or "")
                )
            hits = sum(any(_name_hit(h, n) for n in names) for h in hints)
            if hits >= min(2, len(hints)):
                return str(event.get("id") or ""), comp
    return None, None


def _home_away(comp: dict[str, Any]) -> tuple[str, str]:
    home = away = ""
    for c in comp.get("competitors") or []:
        t = c.get("team") or {}
        name = str(t.get("displayName") or t.get("name") or "")
        if str(c.get("homeAway") or "") == "home":
            home = name
        else:
            away = name
    return home, away


def prop_check(
    market: str,
    game_date: str,
    team: Optional[str] = None,
    opponent: Optional[str] = None,
    pick: Optional[str] = None,
) -> dict[str, Any]:
    market_norm = market.strip().lower().replace(" ", "_")
    market_type = FOOTBALL_PROP_STAT_MAP.get(market_norm)
    if not market_type:
        return {
            "found": False,
            "note": f"Market '{market_norm}' is not an ESPN football specialty market.",
            "source": "espn_football",
        }

    dates = [game_date.replace("-", "")]
    try:
        base = dt_date.fromisoformat(game_date)
        dates += [
            (base - timedelta(days=1)).strftime("%Y%m%d"),
            (base + timedelta(days=1)).strftime("%Y%m%d"),
        ]
    except ValueError:
        pass

    event_id = None
    comp = None
    league_used = "nfl"
    for league in _LEAGUE_PATHS:
        for d in dates:
            board = _fetch_scoreboard(league, d)
            if not board:
                continue
            event_id, comp = _match_event(board, team, opponent)
            if event_id and comp:
                league_used = league
                break
        if event_id and comp:
            break

    if not event_id or not comp:
        return {
            "found": False,
            "note": f"No ESPN football game for date={game_date} team={team!r}.",
            "source": "espn_football",
        }

    home_name, away_name = _home_away(comp)
    status = str(((comp.get("status") or {}).get("type") or {}).get("name") or "")
    finished = status.lower() in {"status_final", "final"} or str(
        ((comp.get("status") or {}).get("type") or {}).get("completed")
    ).lower() in {"true", "1"}
    status_state = str(((comp.get("status") or {}).get("type") or {}).get("state") or "").lower()
    finished = finished or status_state == "post"

    summary = _fetch_summary(league_used, event_id) or {}
    scoring = summary.get("scoringPlays") or []
    if not scoring:
        drives = ((summary.get("drives") or {}).get("previous") or [])
        for drive in drives:
            for play in drive.get("plays") or []:
                if play.get("scoringPlay"):
                    scoring.append(play)

    tds = 0
    fgs = 0
    tds_h1 = 0
    team_tds = 0
    first_team = ""
    target = team or pick or ""

    for play in scoring:
        is_td = _play_is_td(play)
        is_fg = _play_is_fg(play)
        if not is_td and not is_fg:
            continue
        pname = _play_team_name(play)
        period = _period_number(play)
        if not first_team and (is_td or is_fg):
            first_team = pname
        if is_td:
            tds += 1
            if period in (1, 2):
                tds_h1 += 1
            if target and _name_hit(target, pname):
                team_tds += 1
        if is_fg:
            fgs += 1

    stat_value: float | int | None
    if market_type == "total_touchdowns":
        stat_value = tds
    elif market_type == "total_field_goals":
        stat_value = fgs
    elif market_type == "team_total_touchdowns":
        if not target:
            return {
                "found": False,
                "note": "team is required for team_total_touchdowns.",
                "source": "espn_football",
            }
        stat_value = team_tds
    elif market_type == "1st_half_total_touchdowns":
        stat_value = tds_h1
    else:
        if not first_team:
            if finished:
                return {
                    "found": True,
                    "stat_value": None,
                    "void": True,
                    "settled": True,
                    "finished": True,
                    "home_team": home_name,
                    "away_team": away_name,
                    "match_id": event_id,
                    "game_status": "Final",
                    "source": "espn_football",
                    "note": "No scoring plays — bet voided.",
                }
            stat_value = None
        elif _name_hit(first_team, home_name):
            stat_value = 1.0
        elif _name_hit(first_team, away_name):
            stat_value = 0.0
        else:
            stat_value = None

    return {
        "found": True,
        "market": market_norm,
        "market_type": market_type,
        "stat_value": stat_value,
        "match_id": event_id,
        "game_status": "Final" if finished else "InProgress",
        "settled": finished,
        "finished": finished,
        "home_team": home_name,
        "away_team": away_name,
        "source": "espn_football",
    }
