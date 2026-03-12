"""
enrich_flashscore.py
====================
Enriches all sport JSON files in historical_data/ with secondary data from:

  Flashscore (via sportdb.dev proxy) — ALL sports:
    • period_scores : quarter/period/half breakdowns per team
      e.g. {"home": ["38","24","34","32"], "away": ["35","27","27","33"]}
    • home_logo / away_logo : team logo image URLs
    • fs_id          : Flashscore match identifier (for follow-up calls)
    • fs_tournament  : Flashscore tournament name  (e.g. "USA: NBA")
    • match_stats    : team-level stats per period
      e.g. {"Match": {"Ball possession": {"home":"54%","away":"46%"}, ...}}
    • fs_odds        : multi-bookmaker closing + opening odds
      e.g. [{"bookmaker":"bwin","home":"2.1","draw":"3.4","away":"3.2",...}]

  Transfermarkt (soccer only) — per unique player:
    • tm_id            : Transfermarkt numeric player id
    • tm_position      : position label from TM (e.g. "Right Winger")
    • market_value_eur : market value in euros as integer
    • nationality      : primary nationality string
    • tm_age           : age at time of enrichment

These fields let clients write queries such as:
  "Teams with highest Q1 points in NBA"
  "EPL games where the team with <50 % possession won"
  "Most valuable starting XI of the week"
  "NHL games with save % > 95 %"

Usage:
    python enrich_flashscore.py                       # all files in historical_data/
    python enrich_flashscore.py --file nba.json       # one file by name
    python enrich_flashscore.py --no-match-stats      # skip Flashscore stats/odds
    python enrich_flashscore.py --no-players          # skip Transfermarkt players
    python enrich_flashscore.py --no-players --days 1 # only enrich today's games
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enrich_players import DATA_DIR, SPORT_FILE_PREFIX  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SPORTDB_KEY = os.environ.get("SPORTDB_API_KEY", "REDACTED")
SPORTDB_BASE = "https://api.sportdb.dev"

# Player enrichment cache (keyed by display_name.lower())
PLAYER_CACHE_FILE = os.path.join(DATA_DIR, "_player_cache.json")

# Rate limit between consecutive API calls (seconds)
RATE_SLEEP = 0.20

# Max days old a game can be and still be in the live feed
LIVE_FEED_WINDOW_DAYS = 3

# Max tolerance for date matching in historical league results (±days)
HIST_DATE_TOLERANCE_DAYS = 2

# Max results pages to fetch per league (page ~50-111 games each)
MAX_RESULT_PAGES = 5

# Flashscore sport slug for each ESPN sport value
ESPN_TO_FS_SPORT: dict[str, str] = {
    "basketball": "basketball",
    "hockey":     "hockey",
    "baseball":   "baseball",
    "football":   "american-football",   # ESPN "football" = American football
    "soccer":     "football",            # ESPN "soccer" = Flashscore "football"
    "cricket":    "cricket",
}

# ESPN league slug → base Flashscore results URL (page appended as ?page=N)
# Current season as of 2025-2026 sports calendar.
# fra.1 (Ligue 1) omitted — not available via this API endpoint.
LEAGUE_RESULTS_URLS: dict[str, str] = {
    # Basketball
    "nba":                     "/api/flashscore/basketball/usa:200/nba:IBmris38/2025-2026/results",
    "mens-college-basketball":  "/api/flashscore/basketball/usa:200/ncaa:jPojkLXK/2025-2026/results",
    # Ice Hockey
    "nhl":                     "/api/flashscore/hockey/usa:200/nhl:G2Op923t/2025-2026/results",
    # Baseball (MLB spring training / regular season 2026)
    "mlb":                     "/api/flashscore/baseball/usa:200/mlb:zcDLaZ3b/2026/results",
    # Soccer
    "eng.1":                   "/api/flashscore/football/england:198/premier-league:dYlOSQOD/2025-2026/results",
    "esp.1":                   "/api/flashscore/football/spain:176/laliga:QVmLl54o/2025-2026/results",
    "ger.1":                   "/api/flashscore/football/germany:81/bundesliga:W6BOzpK2/2025-2026/results",
    "usa.1":                   "/api/flashscore/football/usa:200/mls:CQv5qrFt/2026/results",
    "uefa.champions":          "/api/flashscore/football/europe:6/champions-league:xGrwqq16/2025-2026/results",
    "uefa.europa":             "/api/flashscore/football/europe:6/europa-league:ClDjv3V5/2025-2026/results",
}

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class _Client:
    """Rate-limited httpx wrapper for sportdb.dev."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            base_url=SPORTDB_BASE,
            headers={
                "x-api-key": SPORTDB_KEY,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Enricher/1.0)",
            },
            timeout=httpx.Timeout(20),
            follow_redirects=True,
        )
        self._last: float = 0.0

    def get(self, path: str, params: dict | None = None) -> Any:
        """Rate-limited GET. Returns parsed JSON or {} / [] on error."""
        gap = time.time() - self._last
        if gap < RATE_SLEEP:
            time.sleep(RATE_SLEEP - gap)
        self._last = time.time()
        for attempt in range(3):
            try:
                r = self._http.get(path, params=params)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    wait = 10 * (2 ** attempt)
                    print(f"  [rate-limit 429] {path}  → sleeping {wait}s", file=sys.stderr, flush=True)
                    time.sleep(wait)
                    continue
                # Log unexpected non-200 once (not on retry)
                if attempt == 0:
                    print(f"  [warn] HTTP {r.status_code} {path}", file=sys.stderr, flush=True)
                break
            except Exception as exc:
                print(f"  [warn] GET {path}: {exc}", file=sys.stderr, flush=True)
                break
        return {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Fuzzy name matching
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Lowercase, strip punctuation/abbreviation noise, collapse spaces."""
    s = name.lower()
    # Common short-form expansions that differ between ESPN and Flashscore
    s = re.sub(r"\bfc\b|\bac\b|\bsc\b|\bfk\b|\bsk\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _match(a: str, b: str) -> bool:
    """True if normalised names share a significant substring."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    # Direct substring check
    if na in nb or nb in na:
        return True
    # Any individual word of length ≥ 4 present in both
    wa = {w for w in na.split() if len(w) >= 4}
    wb = {w for w in nb.split() if len(w) >= 4}
    return bool(wa & wb)


# ---------------------------------------------------------------------------
# Period score extraction from Flashscore match dict
# ---------------------------------------------------------------------------

def _period_scores(fs: dict) -> dict:
    """Extract per-period/quarter scores. Returns {} if none present."""
    home_ps, away_ps = [], []
    for i in range(1, 12):
        hv = fs.get(f"homeResultPeriod{i}")
        av = fs.get(f"awayResultPeriod{i}")
        if hv is None and av is None:
            break
        home_ps.append("" if hv is None else str(hv))
        away_ps.append("" if av is None else str(av))
    if home_ps or away_ps:
        return {"home": home_ps, "away": away_ps}
    return {}


# ---------------------------------------------------------------------------
# Flashscore live-feed index
# ---------------------------------------------------------------------------

def _build_live_index(client: _Client, fs_sport: str) -> list[dict]:
    """Fetch the live/recent feed for one sport. Returns list of match dicts."""
    data = client.get(f"/api/flashscore/{fs_sport}/live")
    if isinstance(data, list):
        return data
    return []


def _fetch_league_results(client: _Client, base_url: str, max_pages: int = MAX_RESULT_PAGES) -> list[dict]:
    """
    Paginate through a Flashscore league results endpoint.
    Returns a flat list of all match dicts collected.
    Stops early if a page returns fewer than 10 items (likely the last page).
    """
    all_matches: list[dict] = []
    for page in range(1, max_pages + 1):
        data = client.get(base_url, params={"page": page})
        if isinstance(data, list) and data:
            all_matches.extend(data)
            if len(data) < 10:
                break  # last partial page
        else:
            break
    return all_matches


def _build_history_index(client: _Client, games: list[dict]) -> list[dict]:
    """
    For all distinct ESPN leagues present in `games`, fetch Flashscore paginated
    results and return the combined list of Flashscore match dicts.
    """
    present_leagues = sorted({g.get("league", "") for g in games if g.get("league")})
    all_fs: list[dict] = []
    for league in present_leagues:
        base_url = LEAGUE_RESULTS_URLS.get(league)
        if not base_url:
            print(f"    [skip] no results URL for league='{league}'", flush=True)
            continue
        print(f"    Fetching historical results for '{league}' ...", end="", flush=True)
        matches = _fetch_league_results(client, base_url)
        print(f" {len(matches)} matches", flush=True)
        all_fs.extend(matches)
    return all_fs


def _find_in_index(
    feed: list[dict],
    home_name: str,
    away_name: str,
    game_date: str,  # YYYY-MM-DD or ISO-8601
    date_tolerance: int = LIVE_FEED_WINDOW_DAYS,
    exclude_ids: set[str] | None = None,
) -> dict | None:
    """Fuzzy-match a game to a Flashscore match. Returns first match or None.
    exclude_ids: set of eventIds already claimed by other games — skip them.
    """
    gdate = game_date[:10]
    for fs in feed:
        if not isinstance(fs, dict):
            continue
        event_id = fs.get("eventId", "")
        if exclude_ids and event_id in exclude_ids:
            continue
        fh = fs.get("homeName") or fs.get("homeFirstName", "")
        fa = fs.get("awayName") or fs.get("awayFirstName", "")
        if not (_match(home_name, fh) and _match(away_name, fa)):
            continue
        # Date sanity
        ts = fs.get("startUtime") or fs.get("startTime", "")
        if ts:
            try:
                fs_date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                gap = abs(
                    (datetime.strptime(gdate, "%Y-%m-%d") -
                     datetime.strptime(fs_date, "%Y-%m-%d")).days
                )
                if gap > date_tolerance:
                    continue
            except Exception:
                pass
        return fs
    return None


# ---------------------------------------------------------------------------
# Match-level Flashscore enrichment
# ---------------------------------------------------------------------------

def _fetch_match_stats(client: _Client, fs_id: str) -> dict:
    """Fetch and flatten per-period stats into {period: {statName: {home, away}}}."""
    data = client.get(f"/api/flashscore/match/{fs_id}/stats")
    if not isinstance(data, list):
        return {}
    result: dict[str, dict] = {}
    for block in data:
        if not isinstance(block, dict):
            continue
        period = str(block.get("period", "Match"))
        stat_dict: dict[str, dict] = {}
        for stat in block.get("stats", []):
            name = stat.get("statName", "")
            if not name:
                continue
            stat_dict[name] = {
                "home": stat.get("homeValue", ""),
                "away": stat.get("awayValue", ""),
            }
        if stat_dict:
            result[period] = stat_dict
    return result


def _fetch_match_odds(client: _Client, fs_id: str) -> list[dict]:
    """Fetch multi-bookmaker odds. Returns list of bookmaker records."""
    data = client.get(f"/api/flashscore/match/{fs_id}/odds")
    if not isinstance(data, list):
        return []
    seen: set[str] = set()
    records: list[dict] = []
    for item in data:
        if str(item.get("bettingScope", "")).upper() != "MATCH":
            continue
        bname = item.get("bookmakerName", "")
        if bname in seen:
            continue
        seen.add(bname)
        odds_list: list[dict] = item.get("odds", [])
        # For HOME_DRAW_AWAY: item[0]=home or away, item[1]=other, item[2]=draw
        # The 'position' key or eventParticipantId tells us which is which.
        # Store raw for flexibility; add opening vs closing.
        rec: dict[str, Any] = {
            "bookmaker": bname,
            "betting_type": item.get("bettingType", ""),
            "odds": [
                {
                    "value":   o.get("value"),
                    "opening": o.get("opening"),
                    "participant_id": o.get("eventParticipantId"),
                }
                for o in odds_list
            ],
        }
        records.append(rec)
    return records


def apply_fs_match(client: _Client, game: dict, fs: dict) -> None:
    """Write Flashscore match data into a game dict in-place."""
    fs_id = fs.get("eventId", "")
    game["fs_id"]         = fs_id
    game["home_logo"]     = fs.get("homeLogo", "")
    game["away_logo"]     = fs.get("awayLogo", "")
    game["fs_tournament"] = fs.get("tournamentName", "")

    ps = _period_scores(fs)
    if ps:
        game["period_scores"] = ps

    if fs_id:
        stats = _fetch_match_stats(client, fs_id)
        if stats:
            game["match_stats"] = stats

        odds = _fetch_match_odds(client, fs_id)
        if odds:
            game["fs_odds"] = odds


# ---------------------------------------------------------------------------
# Transfermarkt player cache helpers
# ---------------------------------------------------------------------------

def _load_cache(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(path: str, cache: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, default=str)
    os.replace(tmp, path)


def _tm_search(client: _Client, name: str) -> dict:
    """
    Search Transfermarkt for a player; return the best matching result dict,
    or {} if nothing found.  The result already contains: id, name, position,
    marketValue, age, nationalities, club.
    """
    safe = re.sub(r"[^a-zA-Z0-9 ]", " ", name).strip()
    if not safe:
        return {}
    data = client.get(f"/api/transfermarkt/players/search/{safe}")
    results: list[dict] = data.get("results", []) if isinstance(data, dict) else []
    for r in results[:5]:
        if _match(name, r.get("name", "")):
            return r
    return {}


def _tm_to_player_fields(result: dict) -> dict:
    """
    Convert a Transfermarkt search result into the fields we store on players.
    Intentionally lightweight — uses only the search result (no extra profile call).
    """
    mv = result.get("marketValue")
    if isinstance(mv, str):
        # Sometimes returned as "€200m" or integer string; strip to int
        mv_clean = re.sub(r"[^0-9]", "", str(mv))
        mv = int(mv_clean) if mv_clean else None

    nats = result.get("nationalities", [])
    nationality = nats[0] if isinstance(nats, list) and nats else ""

    pos = result.get("position", "")
    if isinstance(pos, dict):
        pos = pos.get("main", "")

    return {
        "tm_id":            str(result.get("id", "")),
        "tm_position":      str(pos),
        "market_value_eur": mv,
        "nationality":      nationality,
        "tm_age":           result.get("age"),
    }


# ---------------------------------------------------------------------------
# Player enrichment pass
# ---------------------------------------------------------------------------

def enrich_players_tm(
    client: _Client,
    games: list[dict],
    cache: dict,
    verbose: bool,
) -> int:
    """
    For every soccer player record in `games`, attempt Transfermarkt enrichment.
    Results are written into the player dicts and cached by display_name.lower().
    Returns number of player records enriched.
    """
    enriched = 0
    new_lookups = 0
    CHECKPOINT = 40  # save cache every N new API lookups

    for game in games:
        if game.get("sport") != "soccer":
            continue
        for player in game.get("players", []):
            if player.get("tm_id"):
                # Already has TM data — refresh from cache if cache has it
                enriched += 1
                continue

            name = player.get("display_name", "")
            if not name:
                continue

            key = name.lower()

            if key not in cache:
                result = _tm_search(client, name)
                cache[key] = _tm_to_player_fields(result) if result else {"_not_found": True}
                new_lookups += 1
                if new_lookups % CHECKPOINT == 0:
                    _save_cache(PLAYER_CACHE_FILE, cache)
                    if verbose:
                        print(f"    [checkpoint] {new_lookups} TM lookups, cache saved", flush=True)

            profile = cache[key]
            if profile and not profile.get("_not_found"):
                player.update(profile)
                enriched += 1

    return enriched


# ---------------------------------------------------------------------------
# Per-file enrichment orchestrator
# ---------------------------------------------------------------------------

def enrich_file(
    client: _Client,
    path: str,
    do_match_stats: bool,
    do_players: bool,
    cache: dict,
    verbose: bool,
) -> None:
    # File-level header always prints (not gated by verbose)
    print(f"\n{'='*62}")
    print(f"  {os.path.basename(path)}", flush=True)

    with open(path, encoding="utf-8") as f:
        games: list[dict] = json.load(f)

    if not games:
        print("  Empty — skipping.")
        return

    sample_sport = games[0].get("sport", "")
    fs_sport = ESPN_TO_FS_SPORT.get(sample_sport)

    # ── Flashscore match enrichment ──────────────────────────────────────────
    if do_match_stats and fs_sport:
        need_fs = [
            g for g in games
            if not g.get("fs_id") and g.get("status") in ("post", "in")
        ]
        print(f"  Games needing Flashscore:  {len(need_fs)} / {len(games)}", flush=True)

        if need_fs:
            # Step 1: live feed (fast, covers games within ~3 days)
            print(f"  [1/2] Fetching '{fs_sport}' live feed ...", end="", flush=True)
            live_feed = _build_live_index(client, fs_sport)
            print(f" {len(live_feed)} matches.", flush=True)

            # Step 2: league-specific historical results (covers older games)
            print(f"  [2/2] Fetching league historical results ...", flush=True)
            hist_feed = _build_history_index(client, need_fs)
            print(f"        Combined historical: {len(hist_feed)} matches", flush=True)

            # Merge: live first (higher priority for in-progress games)
            matched = 0
            used_fs_ids: set[str] = set()
            for i, game in enumerate(need_fs, 1):
                home = (game.get("home") or {}).get("team_name", "")
                away = (game.get("away") or {}).get("team_name", "")
                gdate = game.get("date", "")
                # Try live index first (strict 3-day window)
                fs = _find_in_index(live_feed, home, away, gdate, LIVE_FEED_WINDOW_DAYS, used_fs_ids)
                # Fall back to historical index (2-day tolerance)
                if fs is None:
                    fs = _find_in_index(hist_feed, home, away, gdate, HIST_DATE_TOLERANCE_DAYS, used_fs_ids)
                if fs:
                    event_id = fs.get("eventId", "")
                    used_fs_ids.add(event_id)
                    apply_fs_match(client, game, fs)
                    matched += 1
                    if verbose:
                        print(
                            f"  ✓ {game.get('short_name','')[:35]:<36}"
                            f"  fs={game['fs_id']}"
                            f"  period_scores={bool(game.get('period_scores'))}"
                            f"  stats={len(game.get('match_stats', {}))} periods"
                        )
                # Progress dot every 25 games even in quiet mode
                if not verbose and i % 25 == 0:
                    print(f"  ... {i}/{len(need_fs)} processed ({matched} matched so far)", flush=True)

            still_needed = len(need_fs) - matched
            print(
                f"  Flashscore: matched {matched}/{len(need_fs)}"
                + (f"  ({still_needed} unmatched)" if still_needed else " — all matched!"),
                flush=True,
            )

    # ── Transfermarkt player enrichment (soccer only) ────────────────────────
    if do_players and sample_sport == "soccer":
        need_tm = sum(
            1 for g in games
            for p in g.get("players", [])
            if not p.get("tm_id") and p.get("display_name")
        )
        print(f"  Soccer players needing TM:  {need_tm}", flush=True)
        if need_tm > 0:
            n = enrich_players_tm(client, games, cache, verbose)
            print(f"  Transfermarkt: enriched {n} player records", flush=True)

    # ── Save ─────────────────────────────────────────────────────────────────
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2, default=str)
    os.replace(tmp, path)
    print(f"  Saved → {path}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Enrich historical_data/ JSON files with Flashscore and Transfermarkt data."
    )
    p.add_argument(
        "--file",
        default=None,
        help="Single JSON file to enrich (name only, e.g. nba.json). Default: all files.",
    )
    p.add_argument(
        "--no-match-stats",
        action="store_true",
        help="Skip Flashscore period scores / match stats / odds enrichment.",
    )
    p.add_argument(
        "--no-players",
        action="store_true",
        help="Skip Transfermarkt soccer player enrichment.",
    )
    p.add_argument(
        "--data-dir",
        default=DATA_DIR,
        help=f"Directory containing sport JSON files (default: {DATA_DIR})",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-match output.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    verbose = not args.quiet
    do_match = not args.no_match_stats
    do_players = not args.no_players

    data_dir = args.data_dir
    if args.file:
        files = [
            args.file if os.path.isabs(args.file)
            else os.path.join(data_dir, args.file)
        ]
    else:
        files = sorted(
            os.path.join(data_dir, fn)
            for fn in os.listdir(data_dir)
            if fn.endswith(".json") and not fn.startswith("_")
        )

    print(f"Files   : {[os.path.basename(f) for f in files]}")
    print(f"Mode    : match_stats={do_match}  player_tm={do_players}")

    cache = _load_cache(PLAYER_CACHE_FILE)
    print(f"TM cache: {len(cache)} entries loaded from {PLAYER_CACHE_FILE}")

    with _Client() as client:
        for path in files:
            try:
                enrich_file(client, path, do_match, do_players, cache, verbose)
            except Exception as exc:
                print(f"  [error] {path}: {exc}", file=sys.stderr)

    if do_players:
        _save_cache(PLAYER_CACHE_FILE, cache)
        print(f"\nTM cache saved: {len(cache)} entries → {PLAYER_CACHE_FILE}")

    print("\nEnrichment complete.")


if __name__ == "__main__":
    main()
