"""
live_sources.py
===============
Fetches live game data from 365scores and 1xbet, generates synthetic market
odds from live scores, and merges external games into the live_state.json
produced by realtime_monitor.py.

Called from realtime_monitor.py at the end of each poll cycle.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS_365 = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.365scores.com/",
    "Accept": "application/json",
}

_HEADERS_1XBET = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",  # no br — httpx auto-decompresses gzip
    "Referer": "https://1xlite-08668.world/en/live",
    "Origin": "https://1xlite-08668.world",
}

# 365scores sportId → our sport label
_365_SPORT_MAP: dict[int, str] = {
    1: "soccer",
    2: "basketball",
    4: "hockey",
    13: "baseball",
}

# 365scores: well-known league name fragments → ESPN-style league key
_365_LEAGUE_MAP: list[tuple[str, str]] = [
    ("Premier League",   "eng.1"),
    ("La Liga",          "esp.1"),
    ("MLS",              "usa.1"),
    ("Champions League", "uefa.champions"),
    ("Bundesliga",       "ger.1"),
    ("Serie A",          "ita.1"),
    ("Ligue 1",          "fra.1"),
    ("NBA",              "nba"),
    ("WNBA",             "wnba"),
    ("NHL",              "nhl"),
    ("MLB",              "mlb"),
]

# 1xbet sport name → our sport label
_1XBET_SPORT_MAP: dict[str, str] = {
    "Football":    "soccer",
    "Basketball":  "basketball",
    "Ice Hockey":  "hockey",
    "Baseball":    "baseball",
    "Cricket":     "cricket",
    "Tennis":      "tennis",
    "Table Tennis": "table_tennis",
    "Volleyball":  "volleyball",
}

# Patterns that identify virtual/cyber/simulated matches in 1xbet data
_VIRTUAL_TEAM_RE = re.compile(
    r"\(cyber\)|\bvirtual\b|[\+＋]\s*$|^\s*[\+＋]|\(esport\)|\(sim\)",
    re.IGNORECASE,
)
_VIRTUAL_LEAGUE_RE = re.compile(
    r"\bshort\s+football\b|\blfl\b|\bcyber\b|\besport\b|"
    r"\bvirtual\b|\b2x2\b|\b3x3\b|\b4x4\b|\b5x5\b|\b6x6\b|"
    r"\bsimulated\b|\bsim\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Odds generation from live scores
# ---------------------------------------------------------------------------

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _prob_to_american(p: float) -> int:
    """Convert win probability (0–1) to American moneyline odds."""
    p = max(0.01, min(0.99, p))
    if p >= 0.5:
        return int(round(-100 * p / (1.0 - p) / 5) * 5)
    else:
        return int(round(100 * (1.0 - p) / p / 5) * 5)


def _parse_soccer_minute(status_text: str) -> int:
    """Extract elapsed minute from soccer status like '45+2', 'HT', '67'."""
    if not status_text:
        return 45
    st = status_text.strip().upper()
    if "HT" in st or "HALF" in st:
        return 45
    if "FT" in st or "FINAL" in st or "FULL" in st:
        return 90
    m = re.search(r"(\d+)", st)
    if m:
        return min(int(m.group(1)), 90)
    return 45


def _parse_basketball_seconds_remaining(status_text: str, period: int = 0) -> float:
    """Estimate seconds remaining in an NBA game from status text."""
    st = (status_text or "").strip().upper()
    # Try to extract clock like "4:32" or "12:00"
    m = re.search(r"(\d+):(\d+)", st)
    mins_left_in_period = 12.0  # default
    if m:
        mins_left_in_period = int(m.group(1)) + int(m.group(2)) / 60.0
    # periods remaining (4 total, each 12 min)
    if "HALF" in st:
        return 24 * 60  # halftime
    # Try "Q1", "Q2" etc.
    q_match = re.search(r"Q(\d)", st)
    current_q = int(q_match.group(1)) if q_match else max(1, period)
    periods_after = max(0, 4 - current_q)
    return mins_left_in_period * 60 + periods_after * 720


def _parse_hockey_seconds_remaining(status_text: str, period: int = 0) -> float:
    """Estimate seconds remaining in an NHL game."""
    st = (status_text or "").strip().upper()
    m = re.search(r"(\d+):(\d+)", st)
    mins_left = 20.0
    if m:
        mins_left = int(m.group(1)) + int(m.group(2)) / 60.0
    p_match = re.search(r"(\d)(ST|ND|RD|TH)?\s*P", st)
    current_period = int(p_match.group(1)) if p_match else max(1, period)
    periods_after = max(0, 3 - current_period)
    return mins_left * 60 + periods_after * 1200


def _parse_period(sport: str, status_text: str) -> int:
    """
    Parse current period/quarter/half/inning from a status string.
    Returns 0 when the period cannot be determined.
    """
    if not status_text:
        return 0
    st = status_text.strip().upper()

    if sport == "soccer":
        if "2ND HALF" in st or "2ND" in st:
            return 2
        if "1ST HALF" in st or "HT" in st or "1ST" in st:
            return 1
        # Minute-based: >45 → 2nd half
        m = re.search(r"(\d+)", st)
        if m:
            return 2 if int(m.group(1)) > 45 else 1
        return 0

    if sport == "basketball":
        m = re.search(r"Q(\d)", st)
        if m:
            return int(m.group(1))
        if "OT" in st or "OVERTIME" in st:
            return 5
        if "2ND HALF" in st:
            return 3
        if "1ST HALF" in st or "HALFTIME" in st or "HALF" in st:
            return 2
        # Ordinal numbers: "3RD QUARTER" etc.
        ord_m = re.search(r"(\d)(ST|ND|RD|TH)\s*(QUARTER|QTR)", st)
        if ord_m:
            return int(ord_m.group(1))
        return 0

    if sport == "hockey":
        ord_m = re.search(r"(\d)(ST|ND|RD|TH)?\s*P(ERIOD)?", st)
        if ord_m:
            return int(ord_m.group(1))
        if "OT" in st or "OVERTIME" in st:
            return 4
        if "1ST" in st:
            return 1
        if "2ND" in st:
            return 2
        if "3RD" in st:
            return 3
        return 0

    if sport == "baseball":
        ord_m = re.search(r"(\d+)(ST|ND|RD|TH)?\s*INN(ING)?", st)
        if ord_m:
            return int(ord_m.group(1))
        return 0

    if sport == "cricket":
        # Innings number from text like "1st Innings", "2nd Innings"
        ord_m = re.search(r"(\d+)(ST|ND|RD|TH)?\s*INN(ING)?", st)
        if ord_m:
            return int(ord_m.group(1))
        return 0

    if sport in ("tennis", "table_tennis", "volleyball"):
        # Set number from text like "3rd set", "2nd set"
        ord_m = re.search(r"(\d+)(ST|ND|RD|TH)?\s*SET", st)
        if ord_m:
            return int(ord_m.group(1))
        # Bare number like "6 Set"
        num_m = re.search(r"^(\d+)\s*SET", st)
        if num_m:
            return int(num_m.group(1))
        return 0

    return 0


def _parse_baseball_outs_remaining(status_text: str) -> float:
    """Estimate outs remaining in an MLB game from status text."""
    st = (status_text or "").strip().upper()
    # Try inning number
    inn_m = re.search(r"(\d+)(ST|ND|RD|TH)?\s*INN", st)
    if inn_m:
        inning = int(inn_m.group(1))
        # Each half-inning = 3 outs, 9 innings = 54 outs
        outs_gone = (inning - 1) * 6  # rough
        return max(0, 54 - outs_gone)
    return 27  # middle of game estimate


def generate_odds(
    sport: str,
    home_score: int,
    away_score: int,
    status_text: str,
    period: int = 0,
) -> dict[str, Any]:
    """
    Generate synthetic market odds from a live score.
    Returns odds dict compatible with live_state.json format.
    Uses simple win-probability models; marked provider='generated'.
    """
    diff = home_score - away_score

    if sport == "soccer":
        minute = _parse_soccer_minute(status_text)
        time_frac = min(1.0, minute / 90.0)
        # Strength of lead increases as game progresses
        lead_strength = diff * 1.2 * (0.3 + 0.7 * time_frac)
        home_raw = _sigmoid(lead_strength)
        draw_raw = max(0.05, 0.28 * (1 - time_frac) * math.exp(-0.6 * abs(diff)))
        # Re-normalise
        total = home_raw + (1 - home_raw) + draw_raw
        home_prob = home_raw / (home_raw + (1 - home_raw) * (1 - draw_raw / total) + draw_raw)
        away_prob = (1 - home_raw) / (home_raw + (1 - home_raw) * (1 - draw_raw / total) + draw_raw)
        draw_prob = max(0.05, 1 - home_prob - away_prob)
        # Normalise
        s = home_prob + away_prob + draw_prob
        home_prob /= s; away_prob /= s; draw_prob /= s

        home_ml = _prob_to_american(home_prob)
        away_ml = _prob_to_american(away_prob)
        draw_ml = _prob_to_american(draw_prob)

        # Total: projected final goals based on current pace
        projected_total = (home_score + away_score) / max(time_frac, 0.15) if time_frac > 0.05 else 2.5
        game_total = round(projected_total * 2) / 2  # round to nearest 0.5
        game_total = max(1.0, game_total)
        current_total = home_score + away_score
        over_prob = 0.7 if current_total > game_total else (0.3 if current_total < game_total - 1 else 0.5)
        # Spread
        home_spread = round(-diff * (0.5 + 0.5 * time_frac) * 2) / 2

        return {
            "provider": "generated",
            "home_ml": home_ml,
            "away_ml": away_ml,
            "draw_odds": draw_ml,
            "home_spread": home_spread,
            "away_spread": -home_spread,
            "home_spread_odds": -110,
            "away_spread_odds": -110,
            "game_total": game_total,
            "over_odds": _prob_to_american(over_prob),
            "under_odds": _prob_to_american(1 - over_prob),
        }

    elif sport == "basketball":
        secs_left = _parse_basketball_seconds_remaining(status_text, period)
        # NBA: ~3.25 pts/min scoring rate; lead value diminishes as time runs out
        mins_left = secs_left / 60.0
        if mins_left < 0.1:
            lead_strength = diff * 100  # game essentially over
        else:
            lead_strength = diff / math.sqrt(max(1.0, mins_left) * 3.0)
        home_prob = _sigmoid(lead_strength)
        away_prob = 1 - home_prob

        home_ml = _prob_to_american(home_prob)
        away_ml = _prob_to_american(away_prob)

        # Total: project final score from current pace
        total_mins = 48.0
        elapsed_mins = total_mins - mins_left
        if elapsed_mins > 1:
            pace = (home_score + away_score) / elapsed_mins
            projected = pace * total_mins
        else:
            projected = 220.0  # typical NBA total
        game_total = round(projected * 2) / 2
        game_total = max(150.0, game_total)
        current_total = home_score + away_score
        over_prob = 0.65 if current_total > game_total * (elapsed_mins / total_mins) * 1.05 else 0.45
        home_spread = round(-diff * (1 - mins_left / total_mins) * 2) / 2

        return {
            "provider": "generated",
            "home_ml": home_ml,
            "away_ml": away_ml,
            "draw_odds": None,
            "home_spread": home_spread,
            "away_spread": -home_spread,
            "home_spread_odds": -110,
            "away_spread_odds": -110,
            "game_total": game_total,
            "over_odds": _prob_to_american(over_prob),
            "under_odds": _prob_to_american(1 - over_prob),
        }

    elif sport == "hockey":
        secs_left = _parse_hockey_seconds_remaining(status_text, period)
        mins_left = secs_left / 60.0
        total_mins = 60.0
        elapsed_mins = total_mins - mins_left
        if mins_left < 0.1:
            lead_strength = diff * 100
        else:
            lead_strength = diff / math.sqrt(max(0.5, mins_left) * 0.5)
        home_prob = _sigmoid(lead_strength)
        away_prob = 1 - home_prob

        if elapsed_mins > 1:
            pace = (home_score + away_score) / elapsed_mins
            projected = pace * total_mins
        else:
            projected = 5.5
        game_total = round(projected * 2) / 2
        game_total = max(3.0, game_total)
        current_total = home_score + away_score
        over_prob = 0.6 if current_total > game_total * (elapsed_mins / total_mins) else 0.4
        home_spread = round(-diff * (1 - mins_left / total_mins) * 2) / 2

        return {
            "provider": "generated",
            "home_ml": _prob_to_american(home_prob),
            "away_ml": _prob_to_american(away_prob),
            "draw_odds": None,
            "home_spread": home_spread,
            "away_spread": -home_spread,
            "home_spread_odds": -110,
            "away_spread_odds": -110,
            "game_total": game_total,
            "over_odds": _prob_to_american(over_prob),
            "under_odds": _prob_to_american(1 - over_prob),
        }

    elif sport == "baseball":
        outs_left = _parse_baseball_outs_remaining(status_text)
        total_outs = 54.0
        fraction_done = 1 - outs_left / total_outs
        if outs_left < 1:
            lead_strength = diff * 100
        else:
            lead_strength = diff * 0.8 * (0.2 + 0.8 * fraction_done)
        home_prob = _sigmoid(lead_strength)
        away_prob = 1 - home_prob

        elapsed_outs = total_outs - outs_left
        if elapsed_outs > 3:
            pace = (home_score + away_score) / (elapsed_outs / 6)  # per inning
            projected = pace * 9
        else:
            projected = 8.5
        game_total = round(projected * 2) / 2
        game_total = max(4.0, game_total)
        current_total = home_score + away_score
        over_prob = 0.55 if current_total > game_total * fraction_done else 0.45
        home_spread = round(-diff * (0.5 + 0.5 * fraction_done) * 2) / 2

        return {
            "provider": "generated",
            "home_ml": _prob_to_american(home_prob),
            "away_ml": _prob_to_american(away_prob),
            "draw_odds": None,
            "home_spread": home_spread,
            "away_spread": -home_spread,
            "home_spread_odds": -110,
            "away_spread_odds": -110,
            "game_total": game_total,
            "over_odds": _prob_to_american(over_prob),
            "under_odds": _prob_to_american(1 - over_prob),
        }

    elif sport == "cricket":
        # Score = runs; treat like baseball but innings-based
        # S1/S2 are total runs; a lead matters more in later innings
        total_runs = home_score + away_score
        if total_runs == 0:
            home_prob = 0.5
        else:
            # Simple: team with more runs is favoured, scaled by lead magnitude
            lead_frac = diff / max(1, total_runs)
            home_prob = _sigmoid(lead_frac * 3.0)
        away_prob = 1 - home_prob
        return {
            "provider":          "generated",
            "home_ml":           _prob_to_american(home_prob),
            "away_ml":           _prob_to_american(away_prob),
            "draw_odds":         None,
            "home_spread":       None,
            "away_spread":       None,
            "home_spread_odds":  None,
            "away_spread_odds":  None,
            "game_total":        None,
            "over_odds":         None,
            "under_odds":        None,
        }

    elif sport in ("tennis", "table_tennis"):
        # S1/S2 are sets won; current set score not in FS
        if home_score == away_score:
            home_prob = 0.5
        else:
            # Each set won counts; more sets ahead → bigger favourite
            home_prob = _sigmoid((home_score - away_score) * 1.5)
        away_prob = 1 - home_prob
        return {
            "provider":          "generated",
            "home_ml":           _prob_to_american(home_prob),
            "away_ml":           _prob_to_american(away_prob),
            "draw_odds":         None,
            "home_spread":       None,
            "away_spread":       None,
            "home_spread_odds":  None,
            "away_spread_odds":  None,
            "game_total":        None,
            "over_odds":         None,
            "under_odds":        None,
        }

    elif sport == "volleyball":
        # S1/S2 are sets won; same model as tennis
        if home_score == away_score:
            home_prob = 0.5
        else:
            home_prob = _sigmoid((home_score - away_score) * 1.5)
        away_prob = 1 - home_prob
        return {
            "provider":          "generated",
            "home_ml":           _prob_to_american(home_prob),
            "away_ml":           _prob_to_american(away_prob),
            "draw_odds":         None,
            "home_spread":       None,
            "away_spread":       None,
            "home_spread_odds":  None,
            "away_spread_odds":  None,
            "game_total":        None,
            "over_odds":         None,
            "under_odds":        None,
        }

    # Fallback: no odds generated
    return {}


# ---------------------------------------------------------------------------
# Virtual game filter
# ---------------------------------------------------------------------------

def _is_virtual(home: str, away: str, league_raw: str = "") -> bool:
    for name in (home, away):
        if _VIRTUAL_TEAM_RE.search(name):
            return True
    if league_raw and _VIRTUAL_LEAGUE_RE.search(league_raw):
        return True
    return False


# ---------------------------------------------------------------------------
# 365scores fetcher
# ---------------------------------------------------------------------------

def _365_league_key(comp_name: str) -> str:
    lower = comp_name.lower()
    for fragment, key in _365_LEAGUE_MAP:
        if fragment.lower() in lower:
            return key
    return comp_name[:30].lower().replace(" ", "_")


def _fetch_365scores_live() -> list[dict]:
    """Fetch live games from 365scores public REST API."""
    url = "https://webws.365scores.com/web/games/allscores/"
    params = {"langId": "1", "sports": "1,2,4,13", "statusGroup": "1"}
    now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        r = httpx.get(url, params=params, headers=_HEADERS_365, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    result: list[dict] = []
    for g in data.get("games", []):
        if g.get("statusGroup") != 1:
            continue
        sport = _365_SPORT_MAP.get(g.get("sportId", 0))
        if not sport:
            continue

        comp     = g.get("competitionDisplayName", "")
        home_c   = g.get("homeCompetitor", {})
        away_c   = g.get("awayCompetitor", {})
        home_name = home_c.get("name", "Unknown")
        away_name = away_c.get("name", "Unknown")

        if _is_virtual(home_name, away_name, comp):
            continue

        try:
            home_score = int(home_c.get("score", 0) or 0)
            away_score = int(away_c.get("score", 0) or 0)
        except (ValueError, TypeError):
            home_score = away_score = 0

        status_text = g.get("statusText", "Live")
        gt          = g.get("gameTimeDisplay", "")
        full_status = f"{status_text} {gt}".strip()
        league_key  = _365_league_key(comp)
        period      = _parse_period(sport, full_status)

        odds = generate_odds(sport, home_score, away_score, full_status, period)
        event_id = f"365s_{g.get('id', '')}"

        result.append({
            "event_id":      event_id,
            "name":          f"{away_name} at {home_name}",
            "short_name":    f"{away_name} @ {home_name}",
            "date":          today,
            "sport":         sport,
            "league":        league_key,
            "league_key":    league_key,
            "status":        "in",
            "status_detail": full_status,
            "period":        period,
            "clock":         gt,
            "home": {
                "team_id":   str(home_c.get("id", "")),
                "team_name": home_name,
                "team_abbr": home_name[:4].upper(),
                "score":     str(home_score),
                "is_winner": None,
                "record":    "",
            },
            "away": {
                "team_id":   str(away_c.get("id", "")),
                "team_name": away_name,
                "team_abbr": away_name[:4].upper(),
                "score":     str(away_score),
                "is_winner": None,
                "record":    "",
            },
            "odds":     odds,
            "win_prob": {},
            "players":  [],
            "situation": {},
            "broadcasts": [],
            "venue": "",
            "city": "",
            "_source": "365scores",
            "_fetched_at": now_str,
        })

    return result


# ---------------------------------------------------------------------------
# 1xbet fetcher
# ---------------------------------------------------------------------------

def _fetch_1xbet_live() -> list[dict]:
    """Fetch live games from 1xlite-08668.world in a single API call."""
    url = "https://1xlite-08668.world/service-api/LiveFeed/Get1x2_VZip"
    params = {
        "count":              "200",
        "lng":                "en",
        "gr":                 "1258",
        "mode":               "4",
        "country":            "19",
        "top":                "true",
        "virtualSports":      "true",
        "noFilterBlockEvent": "true",
    }
    now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        r = httpx.get(
            url, params=params, headers=_HEADERS_1XBET,
            timeout=15, follow_redirects=True,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    if not data.get("Success") or not data.get("Value"):
        return []

    result: list[dict] = []
    for m in data["Value"]:
        home_name = m.get("O1E") or m.get("O1", "")
        away_name = m.get("O2E") or m.get("O2", "")
        if not home_name or not away_name:
            continue

        sport_name = m.get("SN", "")
        sport = _1XBET_SPORT_MAP.get(sport_name)
        if not sport:
            continue

        raw_league = m.get("L", "") or ""
        if _is_virtual(home_name, away_name, raw_league):
            continue

        sc  = m.get("SC", {}) or {}
        fs  = sc.get("FS", {}) or {}
        sls = sc.get("SLS", "In Progress") or "In Progress"
        # CP = current period (int); CPS = human-readable label e.g. "2nd quarter"
        cp  = sc.get("CP", 0) or 0
        cps = sc.get("CPS", "") or ""

        try:
            home_score = int(fs.get("S1", 0) or 0)
            away_score = int(fs.get("S2", 0) or 0)
        except (ValueError, TypeError):
            home_score = away_score = 0

        # Cricket-specific: use SC.P for innings, SC.S for rich score display
        if sport == "cricket":
            innings = sc.get("P") or 0
            try:
                period = int(innings)
            except (TypeError, ValueError):
                period = 0

            # Parse rich score from SC.S (e.g. "131/5" includes wickets)
            s_list = sc.get("S") or []
            t1_score = t2_score = ""
            for entry in s_list:
                k = entry.get("Key", "")
                v = entry.get("Value", "")
                if k == "Team1Scores":
                    t1_score = str(v)
                elif k == "Team2Scores":
                    t2_score = str(v)
            # InnsStats: "innings;wickets;overs" e.g. "2;5;24.4"
            inns_stats = next((e.get("Value","") for e in s_list if e.get("Key")=="InnsStats"), "")
            clock_label = f"Innings {period}" if period else "In Progress"
            if inns_stats:
                parts = str(inns_stats).split(";")
                if len(parts) >= 3:
                    clock_label = f"Inn {parts[0]}, {parts[2]} overs"

            # Use run totals from FS for odds; display score includes wickets
            home_display = t1_score or str(home_score)
            away_display = t2_score or str(away_score)
            odds = generate_odds(sport, home_score, away_score, clock_label, period)

            league_key = raw_league[:40].lower().replace(" ", "_") or sport
            event_id   = f"1xb_{m.get('I', '')}"
            result.append({
                "event_id":      event_id,
                "name":          f"{away_name} at {home_name}",
                "short_name":    f"{away_name} @ {home_name}",
                "date":          today,
                "sport":         sport,
                "league":        league_key,
                "league_key":    league_key,
                "status":        "in",
                "status_detail": str(sls),
                "period":        period,
                "clock":         clock_label,
                "home": {
                    "team_id":   "",
                    "team_name": home_name,
                    "team_abbr": home_name[:4].upper(),
                    "score":     home_display,
                    "is_winner": None,
                    "record":    "",
                },
                "away": {
                    "team_id":   "",
                    "team_name": away_name,
                    "team_abbr": away_name[:4].upper(),
                    "score":     away_display,
                    "is_winner": None,
                    "record":    "",
                },
                "odds":      odds,
                "win_prob":  {},
                "players":   [],
                "situation": {},
                "broadcasts": [],
                "venue": "",
                "city":  "",
                "_source":     "1xbet",
                "_headline":   raw_league,
                "_fetched_at": now_str,
            })
            continue  # skip the generic append below

        league_key  = raw_league[:40].lower().replace(" ", "_") or sport
        sls_str     = str(sls)
        # Use CP directly; fall back to text parsing only if CP is missing
        period      = int(cp) if cp else _parse_period(sport, cps or sls_str)
        clock_label = cps if cps else sls_str
        odds        = generate_odds(sport, home_score, away_score, clock_label, period)
        event_id    = f"1xb_{m.get('I', '')}"

        result.append({
            "event_id":      event_id,
            "name":          f"{away_name} at {home_name}",
            "short_name":    f"{away_name} @ {home_name}",
            "date":          today,
            "sport":         sport,
            "league":        league_key,
            "league_key":    league_key,
            "status":        "in",
            "status_detail": sls_str,
            "period":        period,
            "clock":         clock_label,
            "home": {
                "team_id":   "",
                "team_name": home_name,
                "team_abbr": home_name[:4].upper(),
                "score":     str(home_score),
                "is_winner": None,
                "record":    "",
            },
            "away": {
                "team_id":   "",
                "team_name": away_name,
                "team_abbr": away_name[:4].upper(),
                "score":     str(away_score),
                "is_winner": None,
                "record":    "",
            },
            "odds":     odds,
            "win_prob": {},
            "players":  [],
            "situation": {},
            "broadcasts": [],
            "venue": "",
            "city": "",
            "_source": "1xbet",
            "_headline": raw_league,
            "_fetched_at": now_str,
        })

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_external_live(sport_filter: str | None = None) -> dict[str, dict]:
    """
    Fetch live games from all external sources (365scores + 1xbet).
    Returns a dict keyed by event_id, compatible with realtime_monitor's
    states dict so it can be merged directly into live_state.json.

    sport_filter: if set, only return games for this sport (e.g. "soccer").
    """
    games: list[dict] = []

    try:
        games.extend(_fetch_365scores_live())
    except Exception as exc:
        pass  # network issues shouldn't crash the monitor

    try:
        games.extend(_fetch_1xbet_live())
    except Exception as exc:
        pass

    result: dict[str, dict] = {}
    seen_tokens: set[str] = set()

    for g in games:
        if sport_filter and g["sport"] != sport_filter:
            continue

        # Deduplicate across sources: last-word-of-team + sport token
        home_key  = g["home"]["team_name"].lower().split()[-1]
        away_key  = g["away"]["team_name"].lower().split()[-1]
        dedup_tok = f"{g['sport']}:{min(home_key, away_key)}:{max(home_key, away_key)}"

        if dedup_tok in seen_tokens:
            continue
        seen_tokens.add(dedup_tok)

        result[g["event_id"]] = g

    return result
