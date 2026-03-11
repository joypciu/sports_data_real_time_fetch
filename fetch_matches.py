"""
fetch_matches.py
================
Fetches real-time results + 2 weeks of historical data for NHL and NCAAB
from ESPN's public API, then calculates/extracts moneyline, spread, and
total (game total + per-team total) for every match found.

No Django stack required — pure httpx + standard library.

Usage:
    python fetch_matches.py
    python fetch_matches.py --leagues nhl ncaab --output results.json
    python fetch_matches.py --leagues nhl --days 7
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITE_API_BASE = "https://site.api.espn.com"
CORE_API_BASE = "https://sports.core.api.espn.com"

LEAGUES: dict[str, tuple[str, str]] = {
    "nhl":   ("hockey",     "nhl"),
    "ncaab": ("basketball", "mens-college-basketball"),
}

# Preferred betting providers (in priority order — first match used)
PREFERRED_PROVIDERS = [
    "espn bet",
    "draftkings",
    "fanduel",
    "caesars",
    "betmgm",
    "bet365",
]

REQUEST_TIMEOUT = 30.0
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1.5  # seconds


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TeamLine:
    """Betting lines for one side of a game."""
    team_id: str
    team_name: str
    team_abbr: str
    home_away: str          # "home" | "away"
    score: str | None       # final/current score, None if not started
    is_winner: bool | None

    # Moneyline
    moneyline: int | None         # e.g. -165 or +145

    # Spread
    spread: float | None          # e.g. -3.5 (negative = favourite)
    spread_odds: int | None       # juice, e.g. -110

    # Team total (over/under for this team's score specifically)
    team_total: float | None
    team_total_over_odds: int | None
    team_total_under_odds: int | None


@dataclass
class GameLines:
    """All betting lines for a single game."""
    event_id: str
    name: str
    short_name: str
    date: str                      # ISO-8601
    status: str                    # "pre" | "in" | "post"
    status_detail: str
    period: int
    clock: str
    sport: str
    league: str
    provider: str | None           # Which book the lines came from
    provider_id: str | None

    home: TeamLine | None
    away: TeamLine | None

    # Game total (over/under)
    game_total: float | None
    over_odds: int | None
    under_odds: int | None

    # Opening lines (for comparison)
    open_spread: float | None
    open_total: float | None

    # Win probability (ESPN model)
    home_win_pct: float | None
    away_win_pct: float | None

    raw_odds: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class ESPNRequester:
    """Thin httpx wrapper with retry/backoff."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            headers={
                "User-Agent": "Mozilla/5.0 (ESPN-Fetcher/1.0)",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with retry and exponential backoff. Returns parsed JSON."""
        last_err: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    print(f"  [rate-limit] sleeping {wait:.1f}s ...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                # 404 = resource not found, 400 = bad request (e.g. historical
                # endpoints that only exist for live games). Treat both as empty.
                if resp.status_code in (400, 404):
                    return {}
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_err = exc
                if attempt < RETRY_ATTEMPTS - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    print(
                        f"  [retry {attempt+1}/{RETRY_ATTEMPTS}] sleeping {wait:.1f}s",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
        raise RuntimeError(f"ESPN request failed after {RETRY_ATTEMPTS} attempts: {last_err}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ESPNRequester":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# ESPN API calls
# ---------------------------------------------------------------------------

def fetch_scoreboard(
    http: ESPNRequester,
    sport: str,
    league: str,
    date_str: str,          # YYYYMMDD
) -> list[dict[str, Any]]:
    """Return raw event dicts from the scoreboard endpoint for one date."""
    url = f"{SITE_API_BASE}/apis/site/v2/sports/{sport}/{league}/scoreboard"
    data = http.get(url, params={"dates": date_str, "limit": 200})
    return data.get("events", [])


def fetch_odds(
    http: ESPNRequester,
    sport: str,
    league: str,
    event_id: str,
    competition_id: str,
) -> list[dict[str, Any]]:
    """Return raw odds items from the core API for one competition."""
    url = (
        f"{CORE_API_BASE}/v2/sports/{sport}/leagues/{league}"
        f"/events/{event_id}/competitions/{competition_id}/odds"
    )
    data = http.get(url)
    return data.get("items", [])


def fetch_win_probabilities(
    http: ESPNRequester,
    sport: str,
    league: str,
    event_id: str,
    competition_id: str,
) -> dict[str, Any]:
    """Return the latest win-probability item, or {} if unavailable."""
    url = (
        f"{CORE_API_BASE}/v2/sports/{sport}/leagues/{league}"
        f"/events/{event_id}/competitions/{competition_id}/probabilities"
    )
    data = http.get(url)
    items = data.get("items", [])
    return items[-1] if items else {}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _pick_provider(
    odds_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the best available provider from a list of odds items."""
    if not odds_items:
        return None

    lower_map: dict[str, dict[str, Any]] = {
        item.get("provider", {}).get("name", "").lower(): item
        for item in odds_items
    }

    for preferred in PREFERRED_PROVIDERS:
        if preferred in lower_map:
            return lower_map[preferred]

    # Fall back to highest-priority (lowest priority number) provider
    return min(
        odds_items,
        key=lambda x: x.get("provider", {}).get("priority", 9999),
    )


def _parse_int_odds(value: Any) -> int | None:
    """Parse an odds value to integer (e.g. '-110' → -110)."""
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return None


def _parse_float(value: Any) -> float | None:
    """Safe float parse."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _status_state(competition: dict[str, Any]) -> tuple[str, str, int, str]:
    """
    Extract (state, detail, period, clock) from a competition dict.
    state: "pre" | "in" | "post"
    """
    status = competition.get("status", {})
    status_type = status.get("type", {})
    state = status_type.get("state", "pre")
    detail = status_type.get("description", status_type.get("name", ""))
    period = status.get("period", 0)
    clock = status.get("displayClock", "0:00")
    return state, detail, period, clock


def parse_game(
    http: ESPNRequester,
    sport: str,
    league: str,
    event: dict[str, Any],
) -> GameLines:
    """Parse a single ESPN event dict into a GameLines object."""
    event_id = event.get("id", "")
    name = event.get("name", "")
    short_name = event.get("shortName", name)
    event_date = event.get("date", "")

    competitions = event.get("competitions", [])
    if not competitions:
        # Return a shell with no lines
        return GameLines(
            event_id=event_id, name=name, short_name=short_name,
            date=event_date, status="pre", status_detail="",
            period=0, clock="", sport=sport, league=league,
            provider=None, provider_id=None,
            home=None, away=None,
            game_total=None, over_odds=None, under_odds=None,
            open_spread=None, open_total=None,
            home_win_pct=None, away_win_pct=None,
        )

    comp = competitions[0]
    competition_id = comp.get("id", event_id)
    state, detail, period, clock = _status_state(comp)

    # ---- Team data ---------------------------------------------------------
    competitors = comp.get("competitors", [])
    home_raw = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away_raw = next((c for c in competitors if c.get("homeAway") == "away"), None)

    def _team_stub(raw: dict[str, Any] | None) -> tuple[str, str, str]:
        """Return (id, display_name, abbreviation) from a competitor dict."""
        if raw is None:
            return "", "", ""
        t = raw.get("team", {})
        return (
            t.get("id", ""),
            t.get("displayName", t.get("name", "")),
            t.get("abbreviation", ""),
        )

    home_id, home_name, home_abbr = _team_stub(home_raw)
    away_id, away_name, away_abbr = _team_stub(away_raw)

    home_score = home_raw.get("score") if home_raw else None
    away_score = away_raw.get("score") if away_raw else None
    home_winner = home_raw.get("winner") if home_raw else None
    away_winner = away_raw.get("winner") if away_raw else None

    # ---- Odds --------------------------------------------------------------
    odds_items = fetch_odds(http, sport, league, event_id, competition_id)
    chosen = _pick_provider(odds_items)

    provider_name: str | None = None
    provider_id: str | None = None
    game_total: float | None = None
    over_odds: int | None = None
    under_odds: int | None = None
    open_spread: float | None = None
    open_total: float | None = None

    home_ml: int | None = None
    away_ml: int | None = None
    home_spread: float | None = None
    away_spread: float | None = None
    home_spread_odds: int | None = None
    away_spread_odds: int | None = None

    # Team totals are rarely exposed by ESPN — we derive them if not present
    home_team_total: float | None = None
    away_team_total: float | None = None
    home_tt_over: int | None = None
    home_tt_under: int | None = None
    away_tt_over: int | None = None
    away_tt_under: int | None = None

    if chosen:
        prov = chosen.get("provider", {})
        provider_name = prov.get("name")
        provider_id = str(prov.get("id", ""))

        game_total = _parse_float(chosen.get("overUnder"))
        over_odds = _parse_int_odds(chosen.get("overOdds"))
        under_odds = _parse_int_odds(chosen.get("underOdds"))

        raw_spread = _parse_float(chosen.get("spread"))

        home_odds_data = chosen.get("homeTeamOdds", {})
        away_odds_data = chosen.get("awayTeamOdds", {})

        home_ml = _parse_int_odds(home_odds_data.get("moneyLine"))
        away_ml = _parse_int_odds(away_odds_data.get("moneyLine"))

        # ESPN's `spread` is from the perspective of the FAVOURITE.
        # homeTeamOdds.spreadOdds gives the juice for the home spread.
        home_spread_odds = _parse_int_odds(home_odds_data.get("spreadOdds"))
        away_spread_odds = _parse_int_odds(away_odds_data.get("spreadOdds"))

        # Derive home/away spread from the raw spread + favourite flag
        if raw_spread is not None:
            home_fav = home_odds_data.get("favorite", False)
            if home_fav:
                home_spread = -abs(raw_spread)
                away_spread = abs(raw_spread)
            else:
                home_spread = abs(raw_spread)
                away_spread = -abs(raw_spread)

        # Opening lines
        open_data = chosen.get("open", {})
        open_spread = _parse_float(open_data.get("spread", {}).get("value") if isinstance(open_data.get("spread"), dict) else open_data.get("spread"))
        open_total = _parse_float(open_data.get("over", {}).get("value") if isinstance(open_data.get("over"), dict) else open_data.get("overUnder"))

        # Team totals — ESPN sometimes exposes these under homeTeamOdds/awayTeamOdds
        home_tt = home_odds_data.get("teamTotal", {})
        away_tt = away_odds_data.get("teamTotal", {})

        if isinstance(home_tt, dict):
            home_team_total = _parse_float(home_tt.get("overUnder") or home_tt.get("total"))
            home_tt_over = _parse_int_odds(home_tt.get("overOdds"))
            home_tt_under = _parse_int_odds(home_tt.get("underOdds"))

        if isinstance(away_tt, dict):
            away_team_total = _parse_float(away_tt.get("overUnder") or away_tt.get("total"))
            away_tt_over = _parse_int_odds(away_tt.get("overOdds"))
            away_tt_under = _parse_int_odds(away_tt.get("underOdds"))

        # If ESPN didn't supply team totals but we have a game total, we can
        # derive a rough estimate: roughly split proportionally by moneyline.
        # This is an approximation often used in sports betting.
        if game_total is not None and home_team_total is None and away_team_total is None:
            home_team_total, away_team_total = _estimate_team_totals(
                game_total, home_ml, away_ml
            )

    # ---- Win probability ---------------------------------------------------
    prob = fetch_win_probabilities(http, sport, league, event_id, competition_id)
    home_win_pct = _parse_float(prob.get("homeWinPercentage"))
    away_win_pct = _parse_float(prob.get("awayWinPercentage"))

    # ---- Assemble ----------------------------------------------------------
    home_line = TeamLine(
        team_id=home_id, team_name=home_name, team_abbr=home_abbr,
        home_away="home", score=str(home_score) if home_score is not None else None,
        is_winner=home_winner,
        moneyline=home_ml, spread=home_spread, spread_odds=home_spread_odds,
        team_total=home_team_total, team_total_over_odds=home_tt_over,
        team_total_under_odds=home_tt_under,
    )
    away_line = TeamLine(
        team_id=away_id, team_name=away_name, team_abbr=away_abbr,
        home_away="away", score=str(away_score) if away_score is not None else None,
        is_winner=away_winner,
        moneyline=away_ml, spread=away_spread, spread_odds=away_spread_odds,
        team_total=away_team_total, team_total_over_odds=away_tt_over,
        team_total_under_odds=away_tt_under,
    )

    return GameLines(
        event_id=event_id,
        name=name,
        short_name=short_name,
        date=event_date,
        status=state,
        status_detail=detail,
        period=period,
        clock=clock,
        sport=sport,
        league=league,
        provider=provider_name,
        provider_id=provider_id,
        home=home_line,
        away=away_line,
        game_total=game_total,
        over_odds=over_odds,
        under_odds=under_odds,
        open_spread=open_spread,
        open_total=open_total,
        home_win_pct=home_win_pct,
        away_win_pct=away_win_pct,
        raw_odds=odds_items,
    )


# ---------------------------------------------------------------------------
# Team total estimation (when ESPN doesn't supply one)
# ---------------------------------------------------------------------------

def _moneyline_to_implied_prob(ml: int | None) -> float | None:
    """Convert American moneyline to implied probability (0-1)."""
    if ml is None:
        return None
    if ml < 0:
        return abs(ml) / (abs(ml) + 100)
    else:
        return 100 / (ml + 100)


def _estimate_team_totals(
    game_total: float,
    home_ml: int | None,
    away_ml: int | None,
) -> tuple[float, float]:
    """
    Estimate each team's expected total (offensive output) from the game total
    and moneylines.

    Method:
      1. Convert each moneyline to an implied win probability.
      2. Normalise so they sum to 1.0.
      3. The stronger team is expected to score slightly more.
         We use a simple linear model:
           team_total = game_total × (0.5 + 0.5 × (team_prob - 0.5))
         This distributes the game total weighted toward the favourite.

    When moneylines are unavailable, we split the total evenly.
    """
    home_prob = _moneyline_to_implied_prob(home_ml)
    away_prob = _moneyline_to_implied_prob(away_ml)

    if home_prob is None or away_prob is None:
        half = round(game_total / 2, 1)
        return half, half

    total_prob = home_prob + away_prob
    home_norm = home_prob / total_prob
    away_norm = away_prob / total_prob

    # Weight from 0.5 toward the favourite's share
    home_share = 0.5 + 0.5 * (home_norm - 0.5)
    away_share = 1.0 - home_share

    home_tt = round(game_total * home_share, 1)
    away_tt = round(game_total * away_share, 1)
    return home_tt, away_tt


# ---------------------------------------------------------------------------
# Main fetch loop
# ---------------------------------------------------------------------------

def date_range(start: date, end: date) -> list[date]:
    """Inclusive date range from start to end."""
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur)
        cur += timedelta(days=1)
    return dates


def fetch_league(
    http: ESPNRequester,
    league_key: str,
    days_history: int = 14,
) -> list[GameLines]:
    """
    Fetch real-time (today) + historical (last `days_history` days) games
    for the given league key.
    """
    sport, league = LEAGUES[league_key]
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_history)

    all_games: list[GameLines] = []
    dates = date_range(start, today)

    print(f"\n[{league_key.upper()}] Fetching {len(dates)} days ({start} to {today}) ...")

    seen_event_ids: set[str] = set()

    for d in dates:
        date_str = d.strftime("%Y%m%d")
        print(f"  {date_str} ... ", end="", flush=True)

        try:
            events = fetch_scoreboard(http, sport, league, date_str)
        except Exception as exc:
            print(f"ERROR fetching scoreboard: {exc}", file=sys.stderr)
            continue

        new_events = [e for e in events if e.get("id") not in seen_event_ids]
        print(f"{len(new_events)} games", flush=True)

        for event in new_events:
            event_id = event.get("id", "")
            seen_event_ids.add(event_id)
            try:
                game = parse_game(http, sport, league, event)
                all_games.append(game)
            except Exception as exc:
                print(
                    f"    [warn] event {event_id} parse error: {exc}",
                    file=sys.stderr,
                )

    return all_games


# ---------------------------------------------------------------------------
# Display / output helpers
# ---------------------------------------------------------------------------

def _fmt_ml(ml: int | None) -> str:
    if ml is None:
        return "N/A"
    return f"+{ml}" if ml > 0 else str(ml)


def _fmt_spread(spread: float | None, odds: int | None) -> str:
    if spread is None:
        return "N/A"
    s = f"{spread:+.1f}"
    if odds is not None:
        s += f" ({_fmt_ml(odds)})"
    return s


def _fmt_total(total: float | None, over: int | None, under: int | None) -> str:
    if total is None:
        return "N/A"
    s = f"O/U {total}"
    if over is not None:
        s += f"  Over {_fmt_ml(over)} / Under {_fmt_ml(under)}"
    return s


def print_game(g: GameLines) -> None:
    """Pretty-print a GameLines to stdout."""
    status_str = g.status_detail or g.status
    if g.status == "in":
        status_str = f"LIVE P{g.period} {g.clock}"
    elif g.status == "post":
        status_str = "FINAL"

    print(f"\n{'-'*70}")
    print(f"  {g.short_name}  [{g.league.upper()}]  {g.date[:10]}  {status_str}")
    if g.provider:
        print(f"  Lines source: {g.provider}")

    a = g.away
    h = g.home

    away_score = f"  {a.score}" if a and a.score else ""
    home_score = f"  {h.score}" if h and h.score else ""
    print(f"  {'AWAY':6} {a.team_abbr if a else '?':5}{away_score}")
    print(f"  {'HOME':6} {h.team_abbr if h else '?':5}{home_score}")

    print(f"\n  {'Moneyline':}")
    print(f"    Away: {_fmt_ml(a.moneyline if a else None)}")
    print(f"    Home: {_fmt_ml(h.moneyline if h else None)}")

    print(f"\n  Spread")
    print(f"    Away: {_fmt_spread(a.spread if a else None, a.spread_odds if a else None)}")
    print(f"    Home: {_fmt_spread(h.spread if h else None, h.spread_odds if h else None)}")

    print(f"\n  Game Total: {_fmt_total(g.game_total, g.over_odds, g.under_odds)}")

    print(f"\n  Team Totals")
    print(f"    Away ({a.team_abbr if a else '?'}): {_fmt_total(a.team_total if a else None, a.team_total_over_odds if a else None, a.team_total_under_odds if a else None)}")
    print(f"    Home ({h.team_abbr if h else '?'}): {_fmt_total(h.team_total if h else None, h.team_total_over_odds if h else None, h.team_total_under_odds if h else None)}")

    if g.home_win_pct is not None:
        print(f"\n  Win Prob  Away {g.away_win_pct:.1%}  /  Home {g.home_win_pct:.1%}")

    if g.open_spread is not None or g.open_total is not None:
        print(f"\n  Opening   Spread {g.open_spread:+.1f}" if g.open_spread else "", end="")
        print(f"   Total {g.open_total}" if g.open_total else "")


def print_summary(games: list[GameLines]) -> None:
    """Print a compact table summary."""
    live = [g for g in games if g.status == "in"]
    final = [g for g in games if g.status == "post"]
    upcoming = [g for g in games if g.status == "pre"]

    print(f"\n{'='*70}")
    print(f"  SUMMARY   Total: {len(games)}  |  Live: {len(live)}  Final: {len(final)}  Upcoming: {len(upcoming)}")
    print(f"{'='*70}")
    print(f"  {'Game':<30} {'Status':<10} {'ML Away':>8} {'ML Home':>8} {'Spread':>8} {'Total':>7}")
    print(f"  {'-'*67}")

    for g in sorted(games, key=lambda x: x.date, reverse=True):
        status_str = "LIVE" if g.status == "in" else ("FINAL" if g.status == "post" else "pre")
        ml_away = _fmt_ml(g.away.moneyline if g.away else None)
        ml_home = _fmt_ml(g.home.moneyline if g.home else None)

        away_spr = f"{g.away.spread:+.1f}" if g.away and g.away.spread is not None else "N/A"
        total = str(g.game_total) if g.game_total else "N/A"

        name = g.short_name[:29]
        print(f"  {name:<30} {status_str:<10} {ml_away:>8} {ml_home:>8} {away_spr:>8} {total:>7}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch NHL/NCAAB results + betting lines from ESPN")
    p.add_argument(
        "--leagues",
        nargs="+",
        choices=list(LEAGUES.keys()),
        default=list(LEAGUES.keys()),
        help="Which leagues to fetch (default: all)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=14,
        help="Days of history to fetch (default: 14)",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional JSON output file path",
    )
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only the summary table, not per-game detail",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    all_games: list[GameLines] = []

    with ESPNRequester() as http:
        for league_key in args.leagues:
            games = fetch_league(http, league_key, days_history=args.days)
            all_games.extend(games)

    if not args.summary_only:
        for game in all_games:
            print_game(game)

    print_summary(all_games)

    if args.output:
        out_path = args.output
        # Serialize dataclasses to plain dicts
        data = [asdict(g) for g in all_games]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  [done] Results written to {out_path}")


if __name__ == "__main__":
    main()
