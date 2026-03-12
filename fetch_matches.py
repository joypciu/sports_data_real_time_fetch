"""
fetch_matches.py
================
Fetches 15 days of historical + real-time data for NBA, NHL, NCAAB, NCAAF, NFL,
MLB, MLS, EPL, La Liga, Bundesliga, Ligue 1, UCL, UEL, and ICC Cricket from
ESPN's public API.  Extracts scores, moneyline, spread, totals, team totals,
win probabilities, and per-player box scores.  All results are auto-saved to a
timestamped JSON file each run.

No Django stack required — pure httpx + standard library.

Usage:
    python fetch_matches.py                                # all leagues, 15 days
    python fetch_matches.py --leagues nba nhl --days 7
    python fetch_matches.py --output results.json
    python fetch_matches.py --no-players --summary-only
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
    # ---- Basketball -------------------------------------------------------
    "nba":         ("basketball", "nba"),
    "ncaab":       ("basketball", "mens-college-basketball"),
    # ---- Hockey ------------------------------------------------------------
    "nhl":         ("hockey",     "nhl"),
    # ---- American Football -------------------------------------------------
    "nfl":         ("football",   "nfl"),
    "ncaaf":       ("football",   "college-football"),
    # ---- Baseball ----------------------------------------------------------
    "mlb":         ("baseball",   "mlb"),
    # ---- Soccer ------------------------------------------------------------
    "epl":         ("soccer",     "eng.1"),          # English Premier League
    "laliga":      ("soccer",     "esp.1"),          # Spanish La Liga
    "bundesliga":  ("soccer",     "ger.1"),          # German Bundesliga
    "ligue1":      ("soccer",     "fra.1"),          # French Ligue 1
    "ucl":         ("soccer",     "uefa.champions"), # UEFA Champions League
    "uel":         ("soccer",     "uefa.europa"),    # UEFA Europa League
    "mls":         ("soccer",     "usa.1"),          # MLS
    # ---- Cricket -----------------------------------------------------------
    "ipl":            ("cricket", "8048"),  # Indian Premier League
    "cricket_t20q":   ("cricket", "8040"),  # ICC T20 World Cup Qualifier
    "cricket_sa":     ("cricket", "8041"),  # SuperSport Series (South Africa)
    "cricket_shield": ("cricket", "8043"),  # Sheffield Shield (Australia)
    "cricket_bbl":    ("cricket", "8044"),  # Big Bash League (Australia)
    "cricket_tri":    ("cricket", "8651"),  # Tri-Nation Tournament
    "cricket_bpl":    ("cricket", "8653"),  # Bangladesh Premier League
    "cricket_bcl":    ("cricket", "8701"),  # Bangladesh Cricket League
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

# Flashscore enrichment (via sportdb.dev proxy) — cricket live data
FLASHSCORE_API_BASE = "https://api.sportdb.dev"
# Key loaded from env var; falls back to the project key from list_of_api_to_use.txt
FLASHSCORE_API_KEY = __import__("os").environ.get(
    "SPORTDB_API_KEY",
    "REDACTED",
)


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
class PlayerStats:
    """Statistics for a single player in a game."""
    player_id: str
    display_name: str
    jersey: str
    position: str
    team_abbr: str
    home_away: str          # "home" | "away"
    starter: bool
    active: bool
    did_not_play: bool
    dnp_reason: str         # e.g. "DNP - Rest" or ""
    stats: dict[str, str]   # stat_name -> value, e.g. {"PTS": "15", "REB": "6"}


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

    # Draw odds (soccer 3-way market)
    draw_odds: int | None = None

    players: list[PlayerStats] = field(default_factory=list)
    formations: dict[str, str] = field(default_factory=dict)  # soccer: {"home": "4-3-3", "away": "4-2-3-1"}
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


def fetch_summary(
    http: ESPNRequester,
    sport: str,
    league: str,
    event_id: str,
) -> dict[str, Any]:
    """Return the full game summary (box score, play-by-play) for one event."""
    url = f"{SITE_API_BASE}/apis/site/v2/sports/{sport}/{league}/summary"
    return http.get(url, params={"event": event_id})


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


def parse_players(
    summary: dict[str, Any],
    home_id: str,
    away_id: str,
) -> list[PlayerStats]:
    """Extract per-player stats from a game summary's boxscore section.
    Works for NBA, NHL, MLB, NCAAB. NHL uses 'labels' instead of 'names'.
    """
    players: list[PlayerStats] = []
    for group in summary.get("boxscore", {}).get("players", []):
        team = group.get("team", {})
        team_id = str(team.get("id", ""))
        team_abbr = team.get("abbreviation", "")
        home_away = "home" if team_id == str(home_id) else "away"
        for stat_group in group.get("statistics", []):
            # NHL uses "labels"; NBA/MLB/NCAAB use "names" — fall back gracefully
            names: list[str] = stat_group.get("names") or stat_group.get("labels", [])
            for ae in stat_group.get("athletes", []):
                ath = ae.get("athlete", {})
                raw_stats: list[str] = ae.get("stats", [])
                players.append(PlayerStats(
                    player_id=str(ath.get("id", "")),
                    display_name=ath.get("displayName", ath.get("shortName", "")),
                    jersey=ath.get("jersey", ""),
                    position=ath.get("position", {}).get("abbreviation", ""),
                    team_abbr=team_abbr,
                    home_away=home_away,
                    starter=ae.get("starter", False),
                    active=ae.get("active", True),
                    did_not_play=ae.get("didNotPlay", False),
                    dnp_reason=ae.get("reason", ""),
                    stats=dict(zip(names, raw_stats)),
                ))
    return players


def parse_soccer_roster(
    summary: dict[str, Any],
) -> tuple[list[PlayerStats], dict[str, str]]:
    """Parse ESPN soccer summary rosters into players + formation map.
    Returns (players, formations) where formations = {"home": "4-3-3", "away": "4-2-3-1"}.
    """
    players: list[PlayerStats] = []
    formations: dict[str, str] = {}
    for entry in summary.get("rosters", []):
        home_away = entry.get("homeAway", "home")
        team = entry.get("team", {})
        team_abbr = team.get("abbreviation", "")
        formation = entry.get("formation", "")
        if formation:
            formations[home_away] = formation
        for ae in entry.get("roster", []):
            ath = ae.get("athlete", {})
            pos = ae.get("position", {})
            stats = {
                s["abbreviation"]: s.get("displayValue", str(s.get("value", "")))
                for s in ae.get("stats", [])
            }
            players.append(PlayerStats(
                player_id=str(ath.get("id", "")),
                display_name=ath.get("displayName", ath.get("shortName", "")),
                jersey=ae.get("jersey", ""),
                position=pos.get("abbreviation", "") if isinstance(pos, dict) else "",
                team_abbr=team_abbr,
                home_away=home_away,
                starter=ae.get("starter", False),
                active=ae.get("active", True),
                did_not_play=False,
                dnp_reason="",
                stats=stats,
            ))
    return players, formations


def parse_cricket_roster(
    summary: dict[str, Any],
) -> list[PlayerStats]:
    """Parse an ESPN cricket summary into PlayerStats.

    Player identity comes from ``rosters`` (team + position).
    Stats come from ``matchcards``:
      typeID 11 → batting  → keys: BAT_I{n}_RUNS / BALLS / 4S / 6S / SR / DISMISSAL
      typeID 12 → bowling  → keys: BWL_I{n}_OVERS / MAIDENS / RUNS / WICKETS / ECONOMY / NBW
    ``{n}`` is the innings number so Test-match dual stints don't collide.
    """
    player_map: dict[str, PlayerStats] = {}

    # 1. Build player shells from rosters (provides team, position, home/away)
    for entry in summary.get("rosters", []):
        home_away = entry.get("homeAway", "home")
        team = entry.get("team", {})
        team_abbr = team.get("abbreviation", "")
        for ae in entry.get("roster", []):
            ath = ae.get("athlete", {})
            player_id = str(ath.get("id", ""))
            if not player_id:
                continue
            pos = ae.get("position", {})
            player_map[player_id] = PlayerStats(
                player_id=player_id,
                display_name=ath.get("displayName", ath.get("shortName", "")),
                jersey="",
                position=pos.get("abbreviation", "") if isinstance(pos, dict) else "",
                team_abbr=team_abbr,
                home_away=home_away,
                starter=True,
                active=True,
                did_not_play=False,
                dnp_reason="",
                stats={},
            )

    # 2. Overlay stats from each matchcard entry
    for card in summary.get("matchcards", []):
        type_id = card.get("typeID")
        innings = card.get("inningsNumber", 1)
        inn_tag = f"I{innings}"

        if str(type_id) == "11":  # batting innings
            for row in card.get("playerDetails", []):
                pid = str(row.get("playerID", ""))
                if not pid:
                    continue
                if pid not in player_map:
                    # Substitute or unlisted player — create a minimal shell
                    player_map[pid] = PlayerStats(
                        player_id=pid,
                        display_name=str(row.get("playerName", "")),
                        jersey="", position="", team_abbr="", home_away="",
                        starter=False, active=True, did_not_play=False, dnp_reason="",
                        stats={},
                    )
                ps = player_map[pid]
                runs = row.get("runs")
                balls = row.get("ballsFaced")
                fours = row.get("fours")
                sixes = row.get("sixes")
                dismissal = row.get("dismissal", "")
                ps.stats[f"BAT_{inn_tag}_RUNS"] = str(runs) if runs is not None else ""
                ps.stats[f"BAT_{inn_tag}_BALLS"] = str(balls) if balls is not None else ""
                ps.stats[f"BAT_{inn_tag}_4S"] = str(fours) if fours is not None else ""
                ps.stats[f"BAT_{inn_tag}_6S"] = str(sixes) if sixes is not None else ""
                ps.stats[f"BAT_{inn_tag}_DISMISSAL"] = str(dismissal)
                try:
                    b = int(balls or 0)
                    if b > 0:
                        sr = round(int(runs or 0) / b * 100, 1)
                        ps.stats[f"BAT_{inn_tag}_SR"] = str(sr)
                except (ValueError, TypeError):
                    pass

        elif str(type_id) == "12":  # bowling innings
            for row in card.get("playerDetails", []):
                pid = str(row.get("playerID", ""))
                if not pid:
                    continue
                if pid not in player_map:
                    player_map[pid] = PlayerStats(
                        player_id=pid,
                        display_name=str(row.get("playerName", "")),
                        jersey="", position="", team_abbr="", home_away="",
                        starter=False, active=True, did_not_play=False, dnp_reason="",
                        stats={},
                    )
                ps = player_map[pid]
                overs = row.get("overs")
                maidens = row.get("maidens")
                conceded = row.get("conceded")
                wickets = row.get("wickets")
                economy = row.get("economyRate")
                nbw = row.get("nbw")
                ps.stats[f"BWL_{inn_tag}_OVERS"] = str(overs) if overs is not None else ""
                ps.stats[f"BWL_{inn_tag}_MAIDENS"] = str(maidens) if maidens is not None else ""
                ps.stats[f"BWL_{inn_tag}_RUNS"] = str(conceded) if conceded is not None else ""
                ps.stats[f"BWL_{inn_tag}_WICKETS"] = str(wickets) if wickets is not None else ""
                ps.stats[f"BWL_{inn_tag}_ECONOMY"] = str(economy) if economy is not None else ""
                ps.stats[f"BWL_{inn_tag}_NBW"] = str(nbw) if nbw is not None else ""

    return list(player_map.values())


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


# ---------------------------------------------------------------------------
# Flashscore enrichment helpers (cricket live data)
# ---------------------------------------------------------------------------

def _flashscore_get(path: str) -> dict[str, Any]:
    """One-shot Flashscore/sportdb.dev API call. Returns {} on any failure."""
    if not FLASHSCORE_API_KEY:
        return {}
    url = f"{FLASHSCORE_API_BASE}{path}"
    try:
        resp = httpx.get(
            url,
            headers={
                "x-api-key": FLASHSCORE_API_KEY,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (ESPN-Fetcher/1.0)",
            },
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            follow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def _enrich_flashscore_cricket(games: list["GameLines"]) -> None:
    """Try to enrich cricket GameLines with Flashscore live match metadata.

    Adds to game.formations:
      match_type  – cricket format string from Flashscore (e.g. "T20")
      result      – human-readable result sentence (e.g. "India won by 6 wkts")
      home_rr     – home team run rate (live)
      away_rr     – away team run rate (live)
    Matching is by fuzzy team-name substring (best-effort; skipped if ambiguous).
    """
    try:
        data = _flashscore_get("/api/flashscore/cricket/live")
        # Probe showed matches in a list; try common wrapper keys
        fs_matches: list[dict[str, Any]] = []
        if isinstance(data, list):
            fs_matches = data
        else:
            for key in ("data", "response", "events", "matches", "results"):
                if isinstance(data.get(key), list):
                    fs_matches = data[key]
                    break
        if not fs_matches:
            return
    except Exception as exc:
        print(f"  [warn] Flashscore fetch failed: {exc}", file=sys.stderr)
        return

    for game in games:
        if game.sport != "cricket":
            continue
        if not (game.home and game.away):
            continue
        g_home = game.home.team_name.lower()
        g_away = game.away.team_name.lower()
        for fs in fs_matches:
            fs_home = fs.get("homeName", "").lower()
            fs_away = fs.get("awayName", "").lower()
            # Accept if either team name is a substring in both directions
            home_match = fs_home and (fs_home in g_home or g_home in fs_home)
            away_match = fs_away and (fs_away in g_away or g_away in fs_away)
            if home_match or away_match:
                if fs.get("cricketType"):
                    game.formations["match_type"] = fs["cricketType"]
                if fs.get("cricketLiveSentence"):
                    game.formations["result"] = fs["cricketLiveSentence"]
                if fs.get("homeCricketRunRate") is not None:
                    game.formations["home_rr"] = str(fs["homeCricketRunRate"])
                if fs.get("awayCricketRunRate") is not None:
                    game.formations["away_rr"] = str(fs["awayCricketRunRate"])
                break


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
    fetch_players: bool = True,
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

    draw_odds_parsed: int | None = None

    if chosen:
        prov = chosen.get("provider", {})
        provider_name = prov.get("name")
        provider_id = str(prov.get("id", ""))

        game_total = _parse_float(chosen.get("overUnder"))
        over_odds = _parse_int_odds(chosen.get("overOdds"))
        under_odds = _parse_int_odds(chosen.get("underOdds"))

        # Soccer 3-way market: ESPN may expose draw odds at item level
        _draw_odds_raw = chosen.get("drawOdds") or chosen.get("draw", {}).get("moneyLine")
        draw_odds_parsed: int | None = _parse_int_odds(_draw_odds_raw)

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

    # ---- Player stats (box score / roster) ---------------------------------
    players: list[PlayerStats] = []
    formations: dict[str, str] = {}
    if fetch_players and state in ("in", "post"):
        try:
            summary = fetch_summary(http, sport, league, event_id)
            if sport == "soccer":
                players, formations = parse_soccer_roster(summary)
            elif sport == "cricket":
                players = parse_cricket_roster(summary)
                # Carry match_type from latest card headline into formations
                for card in summary.get("matchcards", []):
                    headline = card.get("headline", "")
                    if headline:
                        formations["headline"] = headline
                        break
            else:
                players = parse_players(summary, home_id, away_id)
        except Exception as exc:
            print(f"    [warn] player data for event {event_id}: {exc}", file=sys.stderr)

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
        draw_odds=draw_odds_parsed,
        players=players,
        formations=formations,
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
    fetch_players: bool = True,
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
                game = parse_game(http, sport, league, event, fetch_players=fetch_players)
                all_games.append(game)
            except Exception as exc:
                print(
                    f"    [warn] event {event_id} parse error: {exc}",
                    file=sys.stderr,
                )

    # For cricket, try to enrich with Flashscore live match metadata
    if sport == "cricket" and all_games:
        print(f"  Enriching {len(all_games)} cricket games from Flashscore ...", end="", flush=True)
        _enrich_flashscore_cricket(all_games)
        enriched = sum(1 for g in all_games if g.formations.get("match_type") or g.formations.get("result"))
        print(f" {enriched} matched.")

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


def _print_box_score(players: list[PlayerStats], away_abbr: str, home_abbr: str) -> None:
    """Print a condensed per-team box score to stdout."""
    if not players:
        return

    def _safe_float(val: str | None) -> float:
        try:
            return float(val or "0")
        except (ValueError, TypeError):
            return 0.0

    sample = next((p.stats for p in players if p.stats), {})
    is_basketball = "PTS" in sample
    is_hockey = "G" in sample and "A" in sample and "TOI" in sample
    is_cricket = any(
        k.startswith(("BAT_", "BWL_")) for p in players for k in p.stats
    )

    if is_cricket:
        # Collect all innings numbers present in this match
        innings_nums = sorted({
            int(k.split("_")[1][1:])
            for p in players for k in p.stats
            if k.startswith(("BAT_", "BWL_")) and "_" in k
        })
        for ha, abbr in [("away", away_abbr), ("home", home_abbr)]:
            team_players = [p for p in players if p.home_away == ha]
            if not team_players:
                continue
            print(f"\n  {abbr} Scorecard:")
            for inn in innings_nums:
                inn_tag = f"I{inn}"
                runs_key = f"BAT_{inn_tag}_RUNS"
                balls_key = f"BAT_{inn_tag}_BALLS"
                fours_key = f"BAT_{inn_tag}_4S"
                sixes_key = f"BAT_{inn_tag}_6S"
                sr_key = f"BAT_{inn_tag}_SR"
                dis_key = f"BAT_{inn_tag}_DISMISSAL"
                batters = [p for p in team_players if runs_key in p.stats]
                if batters:
                    print(f"    Innings {inn} — Batting")
                    print(f"    {'Player':<22} {'Pos':4} {'R':>4} {'B':>5} {'4S':>3} {'6S':>3} {'SR':>6}  Dismissal")
                    print(f"    {'-'*72}")
                    for p in sorted(batters, key=lambda x: _safe_float(x.stats.get(runs_key)), reverse=True):
                        s = p.stats
                        dismissal = s.get(dis_key, "")[:22]
                        print(
                            f"    {p.display_name[:21]:<22} {p.position:<4}"
                            f" {s.get(runs_key, ''):>4}"
                            f" {s.get(balls_key, ''):>5}"
                            f" {s.get(fours_key, ''):>3}"
                            f" {s.get(sixes_key, ''):>3}"
                            f" {s.get(sr_key, ''):>6}"
                            f"  {dismissal}"
                        )
                overs_key = f"BWL_{inn_tag}_OVERS"
                wkts_key = f"BWL_{inn_tag}_WICKETS"
                mdns_key = f"BWL_{inn_tag}_MAIDENS"
                bruns_key = f"BWL_{inn_tag}_RUNS"
                econ_key = f"BWL_{inn_tag}_ECONOMY"
                nbw_key = f"BWL_{inn_tag}_NBW"
                bowlers = [p for p in team_players if overs_key in p.stats]
                if bowlers:
                    print(f"    Innings {inn} — Bowling")
                    print(f"    {'Player':<22} {'Pos':4} {'O':>6} {'M':>3} {'R':>4} {'W':>3} {'ECON':>6}  NBW")
                    print(f"    {'-'*60}")
                    for p in sorted(bowlers, key=lambda x: _safe_float(x.stats.get(wkts_key)), reverse=True):
                        s = p.stats
                        print(
                            f"    {p.display_name[:21]:<22} {p.position:<4}"
                            f" {s.get(overs_key, ''):>6}"
                            f" {s.get(mdns_key, ''):>3}"
                            f" {s.get(bruns_key, ''):>4}"
                            f" {s.get(wkts_key, ''):>3}"
                            f" {s.get(econ_key, ''):>6}"
                            f"  {s.get(nbw_key, '')}"
                        )
        return

    for ha, abbr in [("away", away_abbr), ("home", home_abbr)]:
        active = [p for p in players if p.home_away == ha and not p.did_not_play]
        dnp    = [p for p in players if p.home_away == ha and p.did_not_play]
        if not active and not dnp:
            continue

        print(f"\n  {abbr} Box Score:")

        if is_basketball:
            print(f"  {'S':1} {'#':>2} {'Player':<22} {'Pos':3} {'MIN':>4} {'PTS':>4} {'REB':>4} {'AST':>4} {'STL':>3} {'BLK':>3} {'FG':>7} {'3P':>6} {'TO':>3}")
            print(f"  {'-'*78}")
            for p in sorted(active, key=lambda x: _safe_float(x.stats.get("PTS")), reverse=True):
                s = p.stats
                star = "*" if p.starter else " "
                print(
                    f"  {star} {p.jersey:>2} {p.display_name[:21]:<22} {p.position:<3} "
                    f"{s.get('MIN', '--'):>4} {s.get('PTS', ''):>4} {s.get('REB', ''):>4} "
                    f"{s.get('AST', ''):>4} {s.get('STL', ''):>3} {s.get('BLK', ''):>3} "
                    f"{s.get('FG', ''):>7} {s.get('3PT', ''):>6} {s.get('TO', ''):>3}"
                )
            if dnp:
                print("  DNP: " + ", ".join(
                    f"{p.display_name} ({p.dnp_reason})" if p.dnp_reason else p.display_name
                    for p in dnp
                ))

        elif is_hockey:
            goalies = [p for p in active if p.position == "G"]
            skaters = [p for p in active if p not in goalies]
            if skaters:
                print(f"  {'#':>2} {'Player':<22} {'Pos':3} {'G':>3} {'A':>3} {'+/-':>4} {'PIM':>4} {'SOG':>4} {'TOI':>6}")
                print(f"  {'-'*58}")
                for p in sorted(
                    skaters,
                    key=lambda x: _safe_float(x.stats.get("G", "0")) + _safe_float(x.stats.get("A", "0")),
                    reverse=True,
                ):
                    s = p.stats
                    print(
                        f"  {p.jersey:>2} {p.display_name[:21]:<22} {p.position:<3} "
                        f"{s.get('G', ''):>3} {s.get('A', ''):>3} {s.get('+/-', ''):>4} "
                        f"{s.get('PIM', ''):>4} {s.get('SOG', ''):>4} {s.get('TOI', ''):>6}"
                    )
            if goalies:
                print(f"\n  Goalies:")
                print(f"  {'#':>2} {'Player':<22} {'Pos':3} {'SA':>4} {'SV':>4} {'GA':>4} {'SV%':>6} {'TOI':>6}")
                print(f"  {'-'*58}")
                for p in goalies:
                    s = p.stats
                    print(
                        f"  {p.jersey:>2} {p.display_name[:21]:<22} {p.position:<3} "
                        f"{s.get('SA', ''):>4} {s.get('SV', ''):>4} {s.get('GA', ''):>4} "
                        f"{s.get('SV%', ''):>6} {s.get('TOI', ''):>6}"
                    )

        else:
            # Generic: show whatever stat columns arrived
            keys = list(sample.keys())[:10]
            print(f"  {'#':>2} {'Player':<22} " + " ".join(f"{k:>6}" for k in keys))
            print(f"  {'-'*50}")
            for p in active:
                row = (
                    f"  {p.jersey:>2} {p.display_name[:21]:<22} "
                    + " ".join(f"{p.stats.get(k, ''):>6}" for k in keys)
                )
                print(row)


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
    if g.draw_odds is not None:
        print(f"    Draw: {_fmt_ml(g.draw_odds)}")
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

    if g.players:
        _print_box_score(
            g.players,
            g.away.team_abbr if g.away else "",
            g.home.team_abbr if g.home else "",
        )


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
    p = argparse.ArgumentParser(
        description="Fetch historical + live data for NBA/NHL/NFL/NCAAB/NCAAF/MLB/Soccer/Cricket from ESPN"
    )
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
        default=15,
        help="Days of history to fetch (default: 15)",
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
    p.add_argument(
        "--no-players",
        action="store_true",
        help="Skip fetching player box-score data (faster; box scores are omitted)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    all_games: list[GameLines] = []

    with ESPNRequester() as http:
        for league_key in args.leagues:
            games = fetch_league(
                http, league_key,
                days_history=args.days,
                fetch_players=not args.no_players,
            )
            all_games.extend(games)

    if not args.summary_only:
        for game in all_games:
            print_game(game)

    print_summary(all_games)

    # Always save output — use explicit path or auto-generate a timestamped file
    out_path = args.output or f"matches_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    data = [asdict(g) for g in all_games]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\n  [saved] Results written to {out_path}")


if __name__ == "__main__":
    main()
