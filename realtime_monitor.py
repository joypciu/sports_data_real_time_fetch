"""
realtime_monitor.py
===================
Polls ESPN's scoreboard API every N seconds for all configured leagues and
tracks live match state in realtime.

Features:
  - Detects games that are currently live (status = "in")
  - Tracks score changes, period/clock, win probability updates
  - Emits a timestamped event log (JSON Lines) whenever anything changes
  - Writes a live_state.json snapshot (human-readable, always current)
  - Coloured terminal dashboard shows all live matches at a glance
  - Fetches player box score snapshots for live games periodically

Architecture:
  - One hot loop per iteration fetches today's scoreboard for every league
  - Previous snapshot is compared game-by-game; changes are recorded as events
  - No heavy dependencies — only httpx + standard library

Usage:
    python realtime_monitor.py                   # all leagues, poll every 30s
    python realtime_monitor.py --interval 60     # poll every 60s
    python realtime_monitor.py --leagues nba nhl --interval 20
    python realtime_monitor.py --no-players      # skip player box score pulls
    python realtime_monitor.py --output-dir live_data

Output files (all in --output-dir, default = 'live'):
    live_state.json         — current state split into pregame / live / finished sections
    events_YYYYMMDD.jsonl   — append-only event log for today
    (finished games are also auto-archived to data/ sport files)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

try:
    from live_sources import fetch_external_live as _fetch_external_live
    _EXTERNAL_SOURCES_AVAILABLE = True
except ImportError:
    _EXTERNAL_SOURCES_AVAILABLE = False

# How often to refresh external sources (365scores + 1xbet) in poll cycles
EXTERNAL_REFRESH_EVERY = 2  # every 2nd poll cycle (~60s at 30s interval)

# ---------------------------------------------------------------------------
# Leagues (same mapping as fetch_matches.py)
# ---------------------------------------------------------------------------

LEAGUES: dict[str, tuple[str, str]] = {
    "nba":        ("basketball",  "nba"),
    "wnba":       ("basketball",  "wnba"),
    "ncaab":      ("basketball",  "mens-college-basketball"),
    "nhl":        ("hockey",      "nhl"),
    "nfl":        ("football",    "nfl"),
    "ncaaf":      ("football",    "college-football"),
    "mlb":        ("baseball",    "mlb"),
    "epl":        ("soccer",      "eng.1"),
    "laliga":     ("soccer",      "esp.1"),
    "bundesliga": ("soccer",      "ger.1"),
    "ligue1":     ("soccer",      "fra.1"),
    "ucl":        ("soccer",      "uefa.champions"),
    "uel":        ("soccer",      "uefa.europa"),
    "mls":        ("soccer",      "usa.1"),
    "cricket":    ("cricket",     "icc-cricket"),
    "ufc":        ("mma",          "ufc"),
}

PREFERRED_PROVIDERS = [
    "espn bet", "draftkings", "fanduel", "caesars", "betmgm", "bet365",
]

SITE_API_BASE = "https://site.api.espn.com"
CORE_API_BASE = "https://sports.core.api.espn.com"

REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS  = 3
RETRY_BACKOFF   = 1.5

# How often (in poll cycles) to refresh player box scores for live games
PLAYER_REFRESH_EVERY = 3   # every 3rd poll cycle (~90s at 30s interval)

# How often to re-fetch odds/win-prob for PREGAME games (they change slowly)
PREGAME_ODDS_REFRESH_EVERY = 5  # every 5th poll cycle (~150s at 30s interval)

# How often to refresh injury reports (injuries change slowly)
INJURY_REFRESH_EVERY = 20  # every 20th poll cycle (~10 min at 30s interval)

# Sports/leagues to fetch injuries for (team-based sports only)
INJURY_LEAGUES: list[tuple[str, str, str]] = [
    ("basketball", "nba",             "nba"),
    ("basketball", "wnba",            "wnba"),
    ("hockey",     "nhl",             "nhl"),
    ("baseball",   "mlb",             "mlb"),
    ("soccer",     "eng.1",           "epl"),
    ("soccer",     "esp.1",           "laliga"),
    ("football",   "nfl",             "nfl"),
]

# Data directory for archiving finished games
DATA_DIR = "historical_data"

# SPORT_FILE_PREFIX imported from enrich_players — single source of truth
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from enrich_players import (
    SPORT_FILE_PREFIX,
    parse_players      as parse_players_boxscore,
    parse_soccer_roster as parse_players_soccer,
)

# Terminal colour codes
_C = {
    "red":    "\033[91m",
    "green":  "\033[92m",
    "yellow": "\033[93m",
    "blue":   "\033[94m",
    "cyan":   "\033[96m",
    "white":  "\033[97m",
    "grey":   "\033[90m",
    "bold":   "\033[1m",
    "reset":  "\033[0m",
}


def c(text: str, *codes: str) -> str:
    """Wrap text in ANSI colour codes; no-op on non-TTY."""
    if not sys.stdout.isatty():
        return text
    prefix = "".join(_C.get(k, "") for k in codes)
    return f"{prefix}{text}{_C['reset']}"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class ESPNRequester:
    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            headers={"User-Agent": "Mozilla/5.0 (ESPN-RT/1.0)", "Accept": "application/json"},
            follow_redirects=True,
        )

    def get(self, url: str, params: dict | None = None) -> dict:
        last_err: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code == 429:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                if resp.status_code in (400, 404):
                    return {}
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_err = exc
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
        # Silently return empty on network failure — don't crash the whole loop
        return {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# ESPN data fetchers
# ---------------------------------------------------------------------------

def fetch_scoreboard_today(http: ESPNRequester, sport: str, league: str) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    url = f"{SITE_API_BASE}/apis/site/v2/sports/{sport}/{league}/scoreboard"
    data = http.get(url, params={"dates": today, "limit": 200})
    return data.get("events", [])


def fetch_odds(http: ESPNRequester, sport: str, league: str, event_id: str) -> list[dict]:
    url = (
        f"{CORE_API_BASE}/v2/sports/{sport}/leagues/{league}"
        f"/events/{event_id}/competitions/{event_id}/odds"
    )
    return http.get(url).get("items", [])


def fetch_win_prob(http: ESPNRequester, sport: str, league: str, event_id: str) -> dict:
    url = (
        f"{CORE_API_BASE}/v2/sports/{sport}/leagues/{league}"
        f"/events/{event_id}/competitions/{event_id}/probabilities"
    )
    items = http.get(url).get("items", [])
    return items[-1] if items else {}


def fetch_summary(http: ESPNRequester, sport: str, league: str, event_id: str) -> dict:
    url = f"{SITE_API_BASE}/apis/site/v2/sports/{sport}/{league}/summary"
    return http.get(url, params={"event": event_id})


# ---------------------------------------------------------------------------
# Player parsing — handles both boxscore (non-soccer) and rosters (soccer)
# ---------------------------------------------------------------------------

# parse_players_boxscore and parse_players_soccer are imported from
# enrich_players at the top of the file — no local duplicates needed.


# ---------------------------------------------------------------------------
# Game state — the unit we track and diff
# ---------------------------------------------------------------------------

def _parse_int(v: Any) -> int | None:
    try:
        return int(float(str(v)))
    except (ValueError, TypeError):
        return None


def _parse_float(v: Any) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _pick_provider(items: list[dict]) -> dict | None:
    if not items:
        return None
    lmap = {item.get("provider", {}).get("name", "").lower(): item for item in items}
    for p in PREFERRED_PROVIDERS:
        if p in lmap:
            return lmap[p]
    return min(items, key=lambda x: x.get("provider", {}).get("priority", 9999))


def _parse_status(comp: dict) -> tuple[str, str, int, str]:
    """(state, detail, period, clock)"""
    st = comp.get("status", {})
    stt = st.get("type", {})
    return (
        stt.get("state", "pre"),
        stt.get("description", stt.get("name", "")),
        st.get("period", 0),
        st.get("displayClock", "0:00"),
    )


def build_game_state(
    http: ESPNRequester,
    sport: str,
    league: str,
    league_key: str,
    event: dict,
    fetch_players: bool = True,
    player_cycle: bool = False,
    refresh_extras: bool = True,
) -> dict:
    """Build a full game state dict from a raw ESPN event."""
    event_id = event.get("id", "")
    comps = event.get("competitions", [])
    if not comps:
        return {}

    comp = comps[0]
    state, detail, period, clock = _parse_status(comp)

    competitors = comp.get("competitors", [])
    home_raw = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away_raw = next((c for c in competitors if c.get("homeAway") == "away"), {})

    def team_info(raw: dict) -> dict:
        t = raw.get("team", {})
        return {
            "team_id":   t.get("id", ""),
            "team_name": t.get("displayName", t.get("name", "")),
            "team_abbr": t.get("abbreviation", ""),
            "score":     str(raw.get("score", "")) if raw.get("score") is not None else None,
            "is_winner": raw.get("winner"),
            "record":    raw.get("records", [{}])[0].get("summary", "") if raw.get("records") else "",
        }

    home = team_info(home_raw)
    away = team_info(away_raw)

    # Odds — skipped for pregame games on cached cycles (see refresh_extras)
    odds_items = fetch_odds(http, sport, league, event_id) if refresh_extras else []
    chosen = _pick_provider(odds_items)
    odds: dict = {}
    if chosen:
        h_odds = chosen.get("homeTeamOdds", {})
        a_odds = chosen.get("awayTeamOdds", {})
        raw_spread = _parse_float(chosen.get("spread"))
        home_fav = h_odds.get("favorite", False)
        if raw_spread is not None:
            home_spread = -abs(raw_spread) if home_fav else abs(raw_spread)
            away_spread = abs(raw_spread) if home_fav else -abs(raw_spread)
        else:
            home_spread = away_spread = None
        odds = {
            "provider":      chosen.get("provider", {}).get("name"),
            "home_ml":       _parse_int(h_odds.get("moneyLine")),
            "away_ml":       _parse_int(a_odds.get("moneyLine")),
            "home_spread":   home_spread,
            "away_spread":   away_spread,
            "home_spread_odds": _parse_int(h_odds.get("spreadOdds")),
            "away_spread_odds": _parse_int(a_odds.get("spreadOdds")),
            "game_total":    _parse_float(chosen.get("overUnder")),
            "over_odds":     _parse_int(chosen.get("overOdds")),
            "under_odds":    _parse_int(chosen.get("underOdds")),
            "draw_odds":     _parse_int(
                chosen.get("drawOdds") or (chosen.get("draw") or {}).get("moneyLine")
            ),
        }

    # If ESPN returned no odds for a live game, generate them from the score
    # so every live game always has moneyline / spread / total available.
    if not odds and state == "in" and _EXTERNAL_SOURCES_AVAILABLE:
        try:
            from live_sources import generate_odds as _gen_odds  # type: ignore
            hs = _parse_int(home.get("score") or 0) or 0
            as_ = _parse_int(away.get("score") or 0) or 0
            odds = _gen_odds(sport, hs, as_, detail, period)
        except Exception:
            pass

    # Win probability — also rate-limited for pregame
    prob = fetch_win_prob(http, sport, league, event_id) if refresh_extras else {}
    win_prob = {
        "home_pct": _parse_float(prob.get("homeWinPercentage")),
        "away_pct": _parse_float(prob.get("awayWinPercentage")),
        "source_time": prob.get("playId", ""),
    }

    # Players — only fetch during live/finished games and when cycle allows
    players: list[dict] = []
    formations: dict[str, str] = {}
    players_fetched_at: str | None = None

    if fetch_players and state in ("in", "post") and player_cycle:
        try:
            summary = fetch_summary(http, sport, league, event_id)
            if sport == "soccer":
                players, formations = parse_players_soccer(summary)
            else:
                players = parse_players_boxscore(summary, home["team_id"], away["team_id"])
            players_fetched_at = _now_iso()
        except Exception:
            pass

    # Live situation (period-specific stats from the competition)
    situation: dict = {}
    if state == "in":
        raw_sit = comp.get("situation", {})
        situation = {
            "down":           raw_sit.get("down"),               # NFL
            "distance":       raw_sit.get("distance"),           # NFL
            "yard_line":      raw_sit.get("yardLine"),           # NFL
            "possession":     raw_sit.get("possession", {}).get("id") if raw_sit.get("possession") else None,
            "last_play":      raw_sit.get("lastPlay", {}).get("text") if raw_sit.get("lastPlay") else None,
            "is_red_zone":    raw_sit.get("isRedZone"),          # NFL
        }
        # Scoring summary (goals/HRs/etc.) from linescores
        linescores = comp.get("linescores", [])
        situation["linescores"] = [
            {
                "period": ls.get("period", i + 1),
                "home":   ls.get("homeScore") if "homeScore" in ls else None,
                "away":   ls.get("awayScore") if "awayScore" in ls else None,
            }
            for i, ls in enumerate(linescores)
        ]
    else:
        # Still capture linescores for finished games
        linescores = comp.get("linescores", [])
        situation["linescores"] = [
            {
                "period": ls.get("period", i + 1),
                "home":   ls.get("homeScore") if "homeScore" in ls else None,
                "away":   ls.get("awayScore") if "awayScore" in ls else None,
            }
            for i, ls in enumerate(linescores)
        ]

    # Venue / broadcast info (static but useful)
    venue = comp.get("venue", {})
    broadcasts = [
        b.get("names", [""])[0]
        for b in comp.get("broadcasts", [])
        if b.get("names")
    ]

    return {
        # Identity
        "event_id":    event_id,
        "name":        event.get("name", ""),
        "short_name":  event.get("shortName", ""),
        "date":        event.get("date", ""),
        "sport":       sport,
        "league":      league,
        "league_key":  league_key,

        # Status
        "status":        state,
        "status_detail": detail,
        "period":        period,
        "clock":         clock,

        # Teams
        "home": home,
        "away": away,

        # Live situation
        "situation": situation,

        # Betting
        "odds": odds,

        # Win probability
        "win_prob": win_prob,

        # Players
        "players":              players,
        "formations":           formations,
        "players_fetched_at":   players_fetched_at,

        # Metadata
        "venue":       venue.get("fullName", ""),
        "city":        venue.get("address", {}).get("city", ""),
        "broadcasts":  broadcasts,

        # Internal tracking
        "_fetched_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# MMA bout state builder
# ---------------------------------------------------------------------------

def build_mma_bout_state(
    sport: str,
    league: str,
    league_key: str,
    event: dict,
    comp: dict,
) -> dict:
    """Build a game state dict for a single MMA bout from an ESPN competition."""
    event_id = event.get("id", "")
    comp_id  = comp.get("id", "")
    bout_id  = f"{event_id}_{comp_id}"

    state, detail, period, clock = _parse_status(comp)

    competitors = comp.get("competitors", [])
    # MMA has no homeAway; treat order=0 as "home" and order=1 as "away"
    f1_raw = next((c for c in competitors if c.get("order") == 0), competitors[0] if competitors else {})
    f2_raw = next((c for c in competitors if c.get("order") == 1), competitors[1] if len(competitors) > 1 else {})

    def fighter_info(raw: dict) -> dict:
        ath = raw.get("athlete", {})
        return {
            "team_id":   ath.get("id", ""),
            "team_name": ath.get("displayName", ath.get("fullName", "")),
            "team_abbr": ath.get("shortName", ath.get("displayName", "")[:10]),
            "score":     None,
            "is_winner": raw.get("winner"),
            "record":    ath.get("record", ""),
        }

    home = fighter_info(f1_raw)
    away = fighter_info(f2_raw)

    # Scheduled rounds from competition format
    comp_format = comp.get("format", {})
    reg = comp_format.get("regulation", {})
    scheduled_rounds = reg.get("periods") or reg.get("totalPeriods") or 3

    f1_name = home.get("team_name", "")
    f2_name = away.get("team_name", "")
    bout_name  = f"{f1_name} vs {f2_name}" if f1_name or f2_name else event.get("name", "")
    bout_short = bout_name[:40]

    venue_raw  = comp.get("venue", {})
    broadcasts = [
        b.get("names", [""])[0]
        for b in comp.get("broadcasts", [])
        if b.get("names")
    ]

    return {
        "event_id":          bout_id,
        "name":              bout_name,
        "short_name":        bout_short,
        "date":              event.get("date", ""),
        "sport":             sport,
        "league":            league,
        "league_key":        league_key,
        "status":            state,
        "status_detail":     detail,
        "period":            period,
        "clock":             clock,
        "scheduled_rounds":  scheduled_rounds,
        "home":              home,
        "away":              away,
        "situation":         {},
        "odds":              {},
        "win_prob":          {},
        "players":           [],
        "formations":        {},
        "players_fetched_at": None,
        "venue":             venue_raw.get("fullName", ""),
        "city":              venue_raw.get("address", {}).get("city", ""),
        "broadcasts":        broadcasts,
        "_fetched_at":       _now_iso(),
    }


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def _score_tuple(state: dict) -> tuple[str | None, str | None]:
    return (state.get("home", {}).get("score"), state.get("away", {}).get("score"))


def _status_tuple(state: dict) -> tuple[str, int, str]:
    return (state.get("status", ""), state.get("period", 0), state.get("clock", ""))


def detect_changes(old: dict, new: dict) -> list[dict]:
    """
    Compare two game state dicts and return a list of change events.
    Each event is a dict with 'type', 'game', 'timestamp', and relevant fields.
    """
    events: list[dict] = []
    eid = new.get("event_id", "?")
    short = new.get("short_name", eid)
    now = _now_iso()

    def ev(ev_type: str, **kwargs) -> dict:
        return {
            "type":       ev_type,
            "event_id":   eid,
            "game":       short,
            "sport":      new.get("sport"),
            "league":     new.get("league"),
            "timestamp":  now,
            **kwargs,
        }

    # Game went live
    if old.get("status") == "pre" and new.get("status") == "in":
        events.append(ev("GAME_STARTED",
            home=new["home"]["team_abbr"],
            away=new["away"]["team_abbr"],
        ))

    # Score changed
    old_score = _score_tuple(old)
    new_score = _score_tuple(new)
    if None not in new_score and old_score != new_score:
        old_h, old_a = old_score
        new_h, new_a = new_score
        events.append(ev("SCORE_UPDATE",
            home_score=new_h, away_score=new_a,
            prev_home=old_h, prev_away=old_a,
            period=new.get("period"),
            clock=new.get("clock"),
        ))

    # Period changed (and game still live)
    if new.get("status") == "in":
        old_period = old.get("period", 0)
        new_period = new.get("period", 0)
        if old_period and new_period and new_period != old_period:
            events.append(ev("PERIOD_CHANGE",
                new_period=new_period,
                old_period=old_period,
                home_score=new["home"].get("score"),
                away_score=new["away"].get("score"),
            ))

    # Game finished
    if old.get("status") == "in" and new.get("status") == "post":
        h = new["home"]
        a = new["away"]
        winner = h["team_abbr"] if h.get("is_winner") else (a["team_abbr"] if a.get("is_winner") else "DRAW")
        events.append(ev("GAME_FINISHED",
            home_score=h.get("score"),
            away_score=a.get("score"),
            home_abbr=h["team_abbr"],
            away_abbr=a["team_abbr"],
            winner=winner,
        ))

    # Win probability shift ≥ 5% — only meaningful for live (in-progress) games
    if new.get("status") == "in":
        old_hp = (old.get("win_prob") or {}).get("home_pct")
        new_hp = (new.get("win_prob") or {}).get("home_pct")
        if old_hp is not None and new_hp is not None:
            shift = abs(new_hp - old_hp)
            if shift >= 0.05:
                events.append(ev("WIN_PROB_SHIFT",
                    home_pct=round(new_hp, 3),
                    away_pct=round(1 - new_hp, 3),
                    prev_home_pct=round(old_hp, 3),
                    shift=round(shift, 3),
                ))

    # Odds/line move — ODDS_MOVE for live games, LINE_MOVE for pregame
    is_live_game = new.get("status") == "in"
    move_type = "ODDS_MOVE" if is_live_game else "LINE_MOVE"
    old_odds = old.get("odds") or {}
    new_odds = new.get("odds") or {}
    for side in ("home_ml", "away_ml"):
        o, n = old_odds.get(side), new_odds.get(side)
        if o is not None and n is not None and abs(n - o) >= 5:
            events.append(ev(move_type,
                field=side, old_value=o, new_value=n,
            ))
    # Total move ≥ 0.5
    o_tot, n_tot = old_odds.get("game_total"), new_odds.get("game_total")
    if o_tot is not None and n_tot is not None and abs(n_tot - o_tot) >= 0.5:
        events.append(ev("TOTAL_MOVE",
            old_total=o_tot, new_total=n_tot,
        ))

    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ml(ml: int | None) -> str:
    if ml is None:
        return "N/A"
    return f"+{ml}" if ml > 0 else str(ml)


def _fmt_score(score: str | None) -> str:
    return score if score is not None else "-"


# ---------------------------------------------------------------------------
# Terminal dashboard
# ---------------------------------------------------------------------------

STATUS_COLOUR = {
    "in":   "green",
    "pre":  "grey",
    "post": "white",
}

def _badge(status: str) -> str:
    colour = STATUS_COLOUR.get(status, "white")
    labels = {"in": " LIVE ", "pre": "  PRE ", "post": " FINAL"}
    label = labels.get(status, status.upper()[:6])
    return c(label, colour, "bold")


def print_dashboard(states: dict[str, dict], event_log: list[dict]) -> None:
    """Print a compact dashboard with pregame / live / finished sections."""
    os.system("cls" if os.name == "nt" else "clear")
    now = _now_str()
    total = len(states)
    live  = sum(1 for s in states.values() if s.get("status") == "in")
    pre   = sum(1 for s in states.values() if s.get("status") == "pre")
    post  = sum(1 for s in states.values() if s.get("status") == "post")

    print(c(f"  ESPN REALTIME MONITOR  [{now}]", "cyan", "bold"))
    print(c(f"  Tracking {total} games  —  {live} LIVE  {pre} pre  {post} final", "white"))

    # ── LIVE section ────────────────────────────────────────────────────────
    live_games = [gs for gs in states.values() if gs.get("status") == "in"]
    if live_games:
        print(f"\n  {c('LIVE NOW', 'green', 'bold')}  ({len(live_games)} games)")
        by_league: dict[str, list[dict]] = {}
        for gs in live_games:
            by_league.setdefault(gs.get("league_key", "?"), []).append(gs)
        for lk in sorted(by_league):
            print(f"    {c(lk.upper(), 'yellow')}")
            for g in by_league[lk]:
                _print_game_row(g, show_players=True)

    # ── PREGAME section ─────────────────────────────────────────────────────
    pre_games = [gs for gs in states.values() if gs.get("status") == "pre"]
    if pre_games:
        print(f"\n  {c('UPCOMING', 'grey', 'bold')}  ({len(pre_games)} games)")
        by_league2: dict[str, list[dict]] = {}
        for gs in pre_games:
            by_league2.setdefault(gs.get("league_key", "?"), []).append(gs)
        for lk in sorted(by_league2):
            print(f"    {c(lk.upper(), 'yellow')}")
            for g in sorted(by_league2[lk], key=lambda x: x.get("date", "")):
                _print_pregame_row(g)

    # ── FINISHED section (compact, last 8 per league) ────────────────────────
    finished = [gs for gs in states.values() if gs.get("status") == "post"]
    if finished:
        print(f"\n  {c('FINISHED TODAY', 'white', 'bold')}  ({len(finished)} games)")
        by_league3: dict[str, list[dict]] = {}
        for gs in finished:
            by_league3.setdefault(gs.get("league_key", "?"), []).append(gs)
        for lk in sorted(by_league3):
            games_f = sorted(by_league3[lk], key=lambda x: x.get("date", ""), reverse=True)[:6]
            print(f"    {c(lk.upper(), 'yellow')}")
            for g in games_f:
                _print_game_row(g, show_players=False)

    # ── Recent events ────────────────────────────────────────────────────────
    if event_log:
        recent = event_log[-10:]
        print(f"\n  {c('RECENT EVENTS', 'yellow', 'bold')}")
        for ev in reversed(recent):
            ev_type = ev.get("type", "")
            game    = ev.get("game", "")
            ts      = ev.get("timestamp", "")[-8:]
            col = {
                "SCORE_UPDATE":       "green",
                "GAME_STARTED":       "cyan",
                "GAME_FINISHED":      "yellow",
                "PERIOD_CHANGE":      "blue",
                "WIN_PROB_SHIFT":     "grey",
                "ODDS_MOVE":          "grey",
                "TOTAL_MOVE":         "grey",
                "LINE_MOVE":          "grey",
                "NEW_GAME_DISCOVERED": "cyan",
            }.get(ev_type, "white")
            detail = ""
            if ev_type == "SCORE_UPDATE":
                detail = f"{ev.get('away_score')} – {ev.get('home_score')}  (P{ev.get('period')} {ev.get('clock')})"
            elif ev_type == "GAME_STARTED":
                detail = f"{ev.get('away')} @ {ev.get('home')}"
            elif ev_type == "GAME_FINISHED":
                detail = f"{ev.get('away_abbr')} {ev.get('away_score')} – {ev.get('home_score')} {ev.get('home_abbr')}  Winner: {ev.get('winner')}"
            elif ev_type == "PERIOD_CHANGE":
                detail = f"P{ev.get('old_period')} → P{ev.get('new_period')}  {ev.get('away_score')}-{ev.get('home_score')}"
            elif ev_type == "WIN_PROB_SHIFT":
                detail = f"home {int(ev.get('prev_home_pct',0)*100)}% → {int(ev.get('home_pct',0)*100)}%"
            elif ev_type in ("ODDS_MOVE", "TOTAL_MOVE", "LINE_MOVE"):
                detail = f"{ev.get('field','total')} {ev.get('old_value','?')} → {ev.get('new_value') or ev.get('new_total','?')}"
            elif ev_type == "NEW_GAME_DISCOVERED":
                sched = ev.get('scheduled_at', '')[:16].replace('T', ' ')
                detail = f"{ev.get('status','?').upper()}  {sched} UTC"
            print(f"    {c(ts, 'grey')}  {c(ev_type, col):<22}  {c(game, 'white')}  {detail}")
    print()


def _print_game_row(g: dict, show_players: bool = False) -> None:
    """Print one game line for live or finished games."""
    h = g.get("home") or {}
    a = g.get("away") or {}
    st = g.get("status", "pre")
    badge = _badge(st)

    if st == "in":
        period = g.get("period", "")
        clock  = g.get("clock", "")
        period_str = f"P{period} {clock}" if period else ""
        score_line = (
            c(f"{a.get('team_abbr','?'):>4}", "white") + "  " +
            c(f"{_fmt_score(a.get('score')):>3}", "green", "bold") + "  –  " +
            c(f"{_fmt_score(h.get('score')):<3}", "green", "bold") + "  " +
            c(f"{h.get('team_abbr','?'):<4}", "white") +
            "   " + c(period_str, "cyan")
        )
    else:
        winner_h = h.get("is_winner")
        winner_a = a.get("is_winner")
        h_col = "green" if winner_h else "grey"
        a_col = "green" if winner_a else "grey"
        score_line = (
            c(f"{a.get('team_abbr','?'):>4}", a_col) + "  " +
            c(f"{_fmt_score(a.get('score')):>3}", a_col) + "  –  " +
            c(f"{_fmt_score(h.get('score')):<3}", h_col) + "  " +
            c(f"{h.get('team_abbr','?'):<4}", h_col)
        )

    odds = g.get("odds") or {}
    ml_h = _fmt_ml(odds.get("home_ml"))
    ml_a = _fmt_ml(odds.get("away_ml"))
    spr  = odds.get("home_spread")
    tot  = odds.get("game_total")
    odds_str = ""
    if ml_h != "N/A" or tot is not None:
        odds_str = c(
            f"  ML {ml_a}/{ml_h}  " +
            (f"Spr {spr:+.1f}  " if spr is not None else "") +
            (f"Tot {tot}" if tot is not None else ""),
            "grey"
        )

    wp = g.get("win_prob") or {}
    wp_str = ""
    if wp.get("home_pct") is not None and st == "in":
        hp = int(round(wp["home_pct"] * 100))
        ap = 100 - hp
        wp_str = c(f"  WP {ap}%/{hp}%", "blue")

    # Player count badge
    n_players = len(g.get("players") or [])
    pl_str = c(f"  [{n_players}pl]", "grey") if n_players else ""

    print(f"      {badge} {score_line}{odds_str}{wp_str}{pl_str}")

    # Show top 3 scorers inline for live games
    if show_players and st == "in" and n_players:
        players = g.get("players", [])
        sport = g.get("sport", "")
        _print_top_players_inline(players, sport)


def _print_top_players_inline(players: list[dict], sport: str) -> None:
    """Print top 3 performers per team inline for live games."""
    def _pts(p: dict) -> float:
        s = p.get("stats") or {}
        try:
            if sport == "basketball":
                return float(s.get("PTS", 0) or 0)
            if sport == "hockey":
                return float(s.get("G", 0) or 0) * 2 + float(s.get("A", 0) or 0)
            if sport == "baseball":
                h_ab = s.get("H-AB", "0-0")
                return float(str(h_ab).split("-")[0]) if "-" in str(h_ab) else 0
            if sport == "soccer":
                return float(s.get("G", 0) or 0) * 3 + float(s.get("A", 0) or 0)
        except (ValueError, TypeError):
            pass
        return 0

    for side in ("away", "home"):
        top = sorted(
            [p for p in players if p.get("home_away") == side and not p.get("did_not_play") and _pts(p) > 0],
            key=_pts, reverse=True
        )[:3]
        if not top:
            continue
        abbr = (top[0].get("team_abbr") or side)[:4]
        lines = []
        for p in top:
            s = p.get("stats") or {}
            name = (p.get("display_name") or "").split()[-1][:12]
            if sport == "basketball":
                lines.append(f"{name} {s.get('PTS','?')}pts/{s.get('REB','?')}reb/{s.get('AST','?')}ast")
            elif sport == "hockey":
                lines.append(f"{name} {s.get('G','?')}G/{s.get('A','?')}A")
            elif sport == "baseball":
                lines.append(f"{name} {s.get('H-AB','?')} {s.get('RBI','?')}RBI")
            elif sport == "soccer":
                lines.append(f"{name} {s.get('G','?')}G/{s.get('A','?')}A")
        print(c(f"        {abbr}: " + "  |  ".join(lines), "grey"))


def _print_pregame_row(g: dict) -> None:
    """Print one row for an upcoming game."""
    h = g.get("home") or {}
    a = g.get("away") or {}
    game_dt = g.get("date", "")[:16].replace("T", " ") + " UTC"
    odds = g.get("odds") or {}
    ml_h = _fmt_ml(odds.get("home_ml"))
    ml_a = _fmt_ml(odds.get("away_ml"))
    spr  = odds.get("home_spread")
    tot  = odds.get("game_total")

    # Show win records if available (e.g. "20-10" from ESPN)
    a_rec = a.get("record", "")
    h_rec = h.get("record", "")
    a_abbr = c(f"{a.get('team_abbr', '?'):>4}", "grey")
    h_abbr = c(f"{h.get('team_abbr', '?'):<4}", "grey")
    a_part = a_abbr + (c(f" ({a_rec})", "grey") if a_rec else "")
    h_part = h_abbr + (c(f" ({h_rec})", "grey") if h_rec else "")
    game_str = f"{a_part}  {c('vs', 'grey')}  {h_part}   {c(game_dt, 'grey')}"

    odds_parts = []
    if ml_h != "N/A":
        odds_parts.append(f"ML {ml_a}/{ml_h}")
    if spr is not None:
        odds_parts.append(f"Spr {spr:+.1f}")
    if tot is not None:
        odds_parts.append(f"Tot {tot}")
    odds_str = c("  " + "  ".join(odds_parts), "grey") if odds_parts else ""
    broadcasts = g.get("broadcasts", [])
    tv_str = c(f"  [{broadcasts[0]}]", "grey") if broadcasts else ""

    print(f"      {_badge('pre')} {game_str}{odds_str}{tv_str}")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_live_state(states: dict[str, dict], out_dir: str) -> None:
    """Write live_state.json split into pregame / live / finished sections.

    Also writes two separate dated files that are overwritten on every poll:
      live/live_YYYYMMDD.json    — only in-progress games
      live/pregame_YYYYMMDD.json — only upcoming games
    """
    os.makedirs(out_dir, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    now   = _now_iso()

    pregame  = [s for s in states.values() if s.get("status") == "pre"]
    live     = [s for s in states.values() if s.get("status") == "in"]
    finished = [s for s in states.values() if s.get("status") == "post"]

    # Sort pregame by scheduled time, live by league, finished by league
    pregame.sort(key=lambda g: (g.get("date", "")))
    live.sort(key=lambda g: (g.get("league_key", ""), g.get("date", "")))
    finished.sort(key=lambda g: (g.get("league_key", ""), g.get("date", "")))

    def _atomic_write(filepath: str, payload: dict) -> None:
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, filepath)

    # --- combined state (always written) ---
    _atomic_write(
        os.path.join(out_dir, "live_state.json"),
        {
            "updated_at":     now,
            "total_games":    len(states),
            "live_count":     len(live),
            "pregame_count":  len(pregame),
            "finished_count": len(finished),
            "live":           live,
            "pregame":        pregame,
            "finished":       finished,
        },
    )

    # --- live games: live/live_YYYYMMDD.json ---
    _atomic_write(
        os.path.join(out_dir, f"live_{today}.json"),
        {
            "updated_at": now,
            "count":      len(live),
            "games":      live,
        },
    )

    # --- pregame games: live/pregame_YYYYMMDD.json ---
    _atomic_write(
        os.path.join(out_dir, f"pregame_{today}.json"),
        {
            "updated_at": now,
            "count":      len(pregame),
            "games":      pregame,
        },
    )


def append_events(events: list[dict], out_dir: str) -> None:
    """Append change events to today's JSONL event log."""
    if not events:
        return
    os.makedirs(out_dir, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    path = os.path.join(out_dir, f"events_{today}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, default=str) + "\n")


def archive_finished_game(gs: dict, data_dir: str = DATA_DIR) -> None:
    """
    When a game finishes, append it to the appropriate historical_data/ sport file
    so it enters the permanent historical record.
    Skips if the event_id already exists in that file.
    """
    league_key = gs.get("league_key", "")
    event_id   = str(gs.get("event_id", ""))
    if not league_key or not event_id:
        return

    prefix = SPORT_FILE_PREFIX.get(league_key)
    if not prefix:
        return

    os.makedirs(data_dir, exist_ok=True)

    # Find existing file or create new one
    path = os.path.join(data_dir, f"{prefix}.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump([], f)

    # Load, check for duplicate, append
    try:
        with open(path, encoding="utf-8") as f:
            records: list[dict] = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        records = []

    existing_ids = {str(r.get("event_id", "")) for r in records}
    if event_id in existing_ids:
        return  # already archived

    # Build a clean record in the standard data/ format
    home = gs.get("home") or {}
    away = gs.get("away") or {}
    odds = gs.get("odds") or {}
    wp   = gs.get("win_prob") or {}
    record = {
        "event_id":      event_id,
        "name":          gs.get("name", ""),
        "short_name":    gs.get("short_name", ""),
        "date":          gs.get("date", ""),
        "status":        gs.get("status", ""),
        "status_detail": gs.get("status_detail", ""),
        "period":        gs.get("period", 0),
        "clock":         gs.get("clock", ""),
        "sport":         gs.get("sport", ""),
        "league":        gs.get("league", ""),
        "provider":      odds.get("provider"),
        "provider_id":   None,
        "home": {
            "team_id":               home.get("team_id", ""),
            "team_name":             home.get("team_name", ""),
            "team_abbr":             home.get("team_abbr", ""),
            "home_away":             "home",
            "score":                 home.get("score"),
            "is_winner":             home.get("is_winner"),
            "moneyline":             odds.get("home_ml"),
            "spread":                odds.get("home_spread"),
            "spread_odds":           odds.get("home_spread_odds"),
            "team_total":            None,
            "team_total_over_odds":  None,
            "team_total_under_odds": None,
        },
        "away": {
            "team_id":               away.get("team_id", ""),
            "team_name":             away.get("team_name", ""),
            "team_abbr":             away.get("team_abbr", ""),
            "home_away":             "away",
            "score":                 away.get("score"),
            "is_winner":             away.get("is_winner"),
            "moneyline":             odds.get("away_ml"),
            "spread":                odds.get("away_spread"),
            "spread_odds":           odds.get("away_spread_odds"),
            "team_total":            None,
            "team_total_over_odds":  None,
            "team_total_under_odds": None,
        },
        "game_total":   odds.get("game_total"),
        "over_odds":    odds.get("over_odds"),
        "under_odds":   odds.get("under_odds"),
        "open_spread":  None,
        "open_total":   None,
        "home_win_pct": wp.get("home_pct"),
        "away_win_pct": wp.get("away_pct"),
        "draw_odds":    odds.get("draw_odds"),
        "players":      gs.get("players", []),
        "formations":   gs.get("formations", {}),
        "raw_odds":     [],
    }

    records.append(record)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def poll_once(
    http: ESPNRequester,
    active_leagues: list[str],
    previous_states: dict[str, dict],
    fetch_players: bool,
    poll_count: int,
    data_dir: str = DATA_DIR,
) -> tuple[dict[str, dict], list[dict]]:
    """
    Fetch today's scoreboard for all active leagues.
    Returns (new_states, change_events).
    """
    new_states: dict[str, dict] = {}
    all_events: list[dict] = []

    for league_key in active_leagues:
        sport, league = LEAGUES[league_key]
        try:
            events = fetch_scoreboard_today(http, sport, league)
        except Exception:
            # Keep old state on network error
            for eid, old in previous_states.items():
                if old.get("league_key") == league_key:
                    new_states[eid] = old
            continue

        for event in events:
            eid = event.get("id", "")
            if not eid:
                continue

            # MMA: one ESPN event = entire card; flatten to individual bouts
            if sport == "mma":
                for comp in event.get("competitions", []):
                    comp_id = comp.get("id", "")
                    if not comp_id:
                        continue
                    bout_eid = f"{eid}_{comp_id}"
                    old_bout = previous_states.get(bout_eid, {})
                    new_comp_state = (
                        comp.get("status", {}).get("type", {}).get("state", "pre")
                    )
                    try:
                        gs = build_mma_bout_state(sport, league, league_key, event, comp)
                    except Exception:
                        gs = old_bout
                    if not gs:
                        continue
                    new_states[bout_eid] = gs
                    game_finishing = (
                        old_bout.get("status") == "in" and new_comp_state == "post"
                    )
                    if old_bout:
                        all_events.extend(detect_changes(old_bout, gs))
                        if game_finishing or (
                            old_bout.get("status") != "post" and gs.get("status") == "post"
                        ):
                            try:
                                archive_finished_game(gs, data_dir=data_dir)
                            except Exception as exc:
                                print(
                                    f"  [archive-warn] {gs.get('short_name')}: {exc}",
                                    file=sys.stderr,
                                )
                    else:
                        all_events.append({
                            "type":         "NEW_GAME_DISCOVERED",
                            "event_id":     gs["event_id"],
                            "game":         gs.get("short_name", gs.get("name", "")),
                            "sport":        gs.get("sport", ""),
                            "league":       gs.get("league", ""),
                            "league_key":   gs.get("league_key", ""),
                            "status":       gs.get("status", ""),
                            "scheduled_at": gs.get("date", ""),
                            "timestamp":    _now_iso(),
                        })
                continue  # skip standard build_game_state for MMA events

            old_state = previous_states.get(eid, {})

            # Determine whether to fetch player data this cycle.
            # - Always fetch on: first time seeing a live/finished game
            # - Every PLAYER_REFRESH_EVERY cycles for live games (box score updates)
            # - Once when a game transitions in → post (final box score)
            new_raw_state = (
                event.get("competitions", [{}])[0]
                .get("status", {}).get("type", {}).get("state", "pre")
            )
            game_becoming_live = old_state.get("status") != "in" and new_raw_state == "in"
            game_finishing = old_state.get("status") == "in" and new_raw_state == "post"
            is_live    = new_raw_state == "in"
            is_pregame = new_raw_state == "pre"

            player_cycle = fetch_players and (
                game_becoming_live
                or game_finishing
                or (is_live and poll_count % PLAYER_REFRESH_EVERY == 0)
                or (new_raw_state in ("in", "post") and not old_state.get("players"))
            )

            # Rate-limit odds/win-prob API calls for pregame games:
            # always fetch on first sight, when no odds exist yet, or every
            # PREGAME_ODDS_REFRESH_EVERY cycles; always refresh for live/post.
            refresh_extras = (
                not is_pregame                                    # live/post: always
                or not old_state                                  # first sighting
                or not old_state.get("odds")                     # no cached odds yet
                or poll_count % PREGAME_ODDS_REFRESH_EVERY == 0  # periodic refresh
            )

            try:
                gs = build_game_state(
                    http, sport, league, league_key, event,
                    fetch_players=fetch_players,
                    player_cycle=player_cycle,
                    refresh_extras=refresh_extras,
                )
            except Exception:
                gs = old_state  # keep old on error

            if not gs:
                continue

            # Carry cached odds/winprob forward when skipping the API call
            if not refresh_extras and old_state:
                gs["odds"]     = old_state.get("odds", {})
                gs["win_prob"] = old_state.get("win_prob", {})

            # Preserve previously fetched players if not refreshing this cycle
            if not player_cycle and old_state.get("players"):
                gs["players"]            = old_state["players"]
                gs["formations"]         = old_state.get("formations", {})
                gs["players_fetched_at"] = old_state.get("players_fetched_at")

            # Track the opening betting line: snapshot pregame odds when
            # the game first goes live so callers can compare opening vs live.
            if game_becoming_live and old_state.get("odds"):
                gs["opening_odds"] = old_state["odds"]
            elif old_state.get("opening_odds"):
                gs["opening_odds"] = old_state["opening_odds"]

            new_states[eid] = gs

            # Detect changes vs previous state
            if old_state:
                changes = detect_changes(old_state, gs)
                all_events.extend(changes)

                # Auto-archive: when a game finishes, write to data/ immediately
                if game_finishing or (old_state.get("status") != "post" and gs.get("status") == "post"):
                    try:
                        archive_finished_game(gs, data_dir=data_dir)
                    except Exception as exc:
                        print(f"  [archive-warn] {gs.get('short_name')}: {exc}", file=sys.stderr)
            else:
                # First time we've seen this game — emit a discovery event
                all_events.append({
                    "type":         "NEW_GAME_DISCOVERED",
                    "event_id":     gs["event_id"],
                    "game":         gs.get("short_name", gs.get("name", "")),
                    "sport":        gs.get("sport", ""),
                    "league":       gs.get("league", ""),
                    "league_key":   gs.get("league_key", ""),
                    "status":       gs.get("status", ""),
                    "scheduled_at": gs.get("date", ""),
                    "timestamp":    _now_iso(),
                })

    return new_states, all_events


# ---------------------------------------------------------------------------
# Injury feed
# ---------------------------------------------------------------------------

def fetch_and_save_injuries(http: "ESPNRequester", out_dir: str) -> None:
    """
    Fetch injury reports for all tracked team-based leagues and write
    live/injuries.json.

    Uses the ESPN roster endpoint (one call per team) which embeds injury
    status directly on each athlete object. Only athletes with a non-empty
    injuries array are included.  Errors per-team are silently skipped.
    """
    injuries: list[dict[str, Any]] = []
    fetched_at = _now_iso()

    for sport, league, league_key in INJURY_LEAGUES:
        # Get all teams in the league
        teams_url = f"{SITE_API_BASE}/apis/site/v2/sports/{sport}/{league}/teams"
        try:
            teams_data = http.get(teams_url, params={"limit": 100})
        except Exception:
            continue

        team_items = (
            teams_data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
            or teams_data.get("teams", [])
        )

        for t_entry in team_items:
            team      = t_entry.get("team", t_entry)
            team_id   = str(team.get("id", ""))
            team_name = team.get("displayName") or team.get("name", "")
            team_abbr = team.get("abbreviation", "")
            if not team_id:
                continue

            # Roster endpoint — one call gets all players + injury status
            roster_url = f"{SITE_API_BASE}/apis/site/v2/sports/{sport}/{league}/teams/{team_id}/roster"
            try:
                roster_data = http.get(roster_url)
            except Exception:
                continue

            for athlete in roster_data.get("athletes", []):
                inj_list = athlete.get("injuries", [])
                if not inj_list:
                    continue

                latest_inj = inj_list[0]
                inj_status = latest_inj.get("status", "")
                if not inj_status or inj_status.lower() == "active":
                    continue

                player_status = athlete.get("status", {})
                injuries.append({
                    "sport":       sport,
                    "league":      league,
                    "league_key":  league_key,
                    "team_id":     team_id,
                    "team_name":   team_name,
                    "team_abbr":   team_abbr,
                    "player_id":   str(athlete.get("id", "")),
                    "player_name": athlete.get("displayName") or athlete.get("fullName", ""),
                    "position":    (athlete.get("position") or {}).get("abbreviation", ""),
                    "jersey":      str(athlete.get("jersey", "")),
                    "status":      inj_status,
                    "player_availability": (player_status.get("abbreviation") or player_status.get("name", "")) if isinstance(player_status, dict) else str(player_status),
                    "report_date": latest_inj.get("date", ""),
                    "fetched_at":  fetched_at,
                })

    path = os.path.join(out_dir, "injuries.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": fetched_at, "count": len(injuries), "injuries": injuries}, fh)
    except OSError as exc:
        print(f"  [injuries] Failed to save: {exc}", file=sys.stderr)


def run(args: argparse.Namespace) -> None:
    active_leagues = args.leagues if args.leagues else list(LEAGUES.keys())
    interval = args.interval
    out_dir  = args.output_dir
    data_dir = args.data_dir
    fetch_players = not args.no_players

    print(c(f"\n  ESPN Realtime Monitor", "cyan", "bold"))
    print(f"  Leagues  : {', '.join(active_leagues)}")
    print(f"  Interval : {interval}s")
    print(f"  Players  : {'every '+str(PLAYER_REFRESH_EVERY)+' cycles' if fetch_players else 'disabled'}")
    print(f"  Output   : {os.path.abspath(out_dir)}/")
    print(f"  Archive  : {os.path.abspath(data_dir)}/  (finished games auto-archived to prefix.json)")
    print()

    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    states: dict[str, dict] = {}
    event_log: list[dict] = []
    poll_count = 0

    with ESPNRequester() as http:
        while True:
            poll_start = time.monotonic()
            print(c(f"  [{_now_str()}] Polling ... (cycle {poll_count+1})", "grey"))

            try:
                states, new_events = poll_once(
                    http, active_leagues, states,
                    fetch_players, poll_count, data_dir=data_dir,
                )
                event_log.extend(new_events)

                # Merge external sources (365scores + 1xbet) every N cycles.
                # External games get event_ids prefixed "365s_" or "1xb_" so
                # they never collide with ESPN event ids, and they are kept in
                # a separate key-space in the states dict.
                if _EXTERNAL_SOURCES_AVAILABLE and poll_count % EXTERNAL_REFRESH_EVERY == 0:
                    try:
                        ext = _fetch_external_live()

                        # Build ESPN token set to avoid adding duplicates
                        espn_tokens: set[str] = set()
                        for eid_espn, gs in states.items():
                            if eid_espn.startswith(("365s_", "1xb_")):
                                continue  # skip external entries
                            h = (gs.get("home") or {}).get("team_name", "").lower().split()
                            a = (gs.get("away") or {}).get("team_name", "").lower().split()
                            if h and a:
                                sp  = gs.get("sport", "")
                                tok = f"{sp}:{min(h[-1], a[-1])}:{max(h[-1], a[-1])}"
                                espn_tokens.add(tok)

                        # Remove stale external entries (game no longer in live feed)
                        current_ext_ids = set(ext.keys())
                        stale = [k for k in list(states.keys())
                                 if k.startswith(("365s_", "1xb_")) and k not in current_ext_ids]
                        for k in stale:
                            del states[k]

                        # Add/refresh external games not already covered by ESPN
                        added = updated = 0
                        for eid, eg in ext.items():
                            h = eg["home"]["team_name"].lower().split()
                            a = eg["away"]["team_name"].lower().split()
                            if not h or not a:
                                continue
                            sp  = eg.get("sport", "")
                            tok = f"{sp}:{min(h[-1], a[-1])}:{max(h[-1], a[-1])}"
                            if tok in espn_tokens:
                                continue  # already tracked by ESPN
                            if eid in states:
                                states[eid] = eg   # refresh score + odds
                                updated += 1
                            else:
                                states[eid] = eg
                                added += 1

                        if added or updated:
                            print(c(f"  [external] +{added} new  ~{updated} refreshed  -{len(stale)} removed  (365scores/1xbet)", "grey"))
                    except Exception as _ext_exc:
                        print(c(f"  [external] Error: {_ext_exc}", "red"), file=sys.stderr)

                # Persist
                save_live_state(states, out_dir)
                append_events(new_events, out_dir)

                # Injury feed — refresh every INJURY_REFRESH_EVERY cycles
                if poll_count % INJURY_REFRESH_EVERY == 0:
                    try:
                        fetch_and_save_injuries(http, out_dir)
                        print(c(f"  [injuries] Refreshed", "grey"), file=sys.stderr)
                    except Exception as exc:
                        print(c(f"  [injuries] Error: {exc}", "red"), file=sys.stderr)

                # Dashboard
                print_dashboard(states, event_log)

                # Print new events to stderr for easy piping/logging
                for ev in new_events:
                    print(
                        f"  EVENT: {ev['type']:<20} {ev['game']:<35} {ev.get('timestamp','')[-8:]}",
                        file=sys.stderr,
                    )

            except KeyboardInterrupt:
                print(c("\n  Stopped by user. Final state saved.", "yellow"))
                save_live_state(states, out_dir)
                break
            except Exception as exc:
                print(c(f"  [error] {exc}", "red"), file=sys.stderr)

            poll_count += 1

            # Sleep for the remainder of the interval
            elapsed = time.monotonic() - poll_start
            sleep_for = max(0, interval - elapsed)
            if sleep_for > 0:
                try:
                    time.sleep(sleep_for)
                except KeyboardInterrupt:
                    print(c("\n  Stopped by user. Final state saved.", "yellow"))
                    save_live_state(states, out_dir)
                    break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll ESPN for live match state and track real-time changes."
    )
    parser.add_argument(
        "--leagues", nargs="+", metavar="LEAGUE",
        choices=list(LEAGUES.keys()),
        help=f"Leagues to monitor (default: all). Choices: {', '.join(LEAGUES.keys())}",
    )
    parser.add_argument(
        "--interval", type=int, default=30, metavar="SECONDS",
        help="Poll interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--output-dir", default="live", metavar="DIR",
        help="Directory for live_state.json and event logs (default: live/)",
    )
    parser.add_argument(
        "--no-players", action="store_true",
        help="Skip fetching player box scores (faster polling)",
    )
    parser.add_argument(
        "--data-dir", default=DATA_DIR, metavar="DIR",
        help=f"Directory for archiving finished games (default: {DATA_DIR}/)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
