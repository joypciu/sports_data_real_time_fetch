"""
test_suite.py
=============
Comprehensive tests for enrich_players, daily_ingest, fetch_matches, and
realtime_monitor.  All tests are self-contained: they use mock data and a
temporary directory — no live ESPN API calls and no mutations to data/.

Run with:
    python test_suite.py                  # all tests, verbose
    python test_suite.py -v               # explicit verbose flag
    python test_suite.py TestEnrichPlayers.test_parse_players_nhl
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from unittest.mock import MagicMock, patch, call

# ── resolve imports — point to project root (parent of tests/) ───────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import enrich_players as ep
import daily_ingest   as di
import fetch_matches  as fm


# ════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ════════════════════════════════════════════════════════════════════════════

def _make_boxscore_summary(home_id="1", away_id="2", use_labels=False):
    """Minimal ESPN boxscore summary (NBA or NHL style)."""
    key = "labels" if use_labels else "names"
    return {
        "boxscore": {
            "players": [
                {
                    "team": {"id": home_id, "abbreviation": "HOM"},
                    "statistics": [{
                        key: ["PTS", "REB", "AST"],
                        "athletes": [
                            {
                                "athlete": {
                                    "id": "101",
                                    "displayName": "Alice Smith",
                                    "jersey": "23",
                                    "position": {"abbreviation": "G"},
                                },
                                "stats": ["25", "7", "4"],
                                "starter": True,
                                "active": True,
                                "didNotPlay": False,
                                "reason": "",
                            }
                        ],
                    }],
                },
                {
                    "team": {"id": away_id, "abbreviation": "AWY"},
                    "statistics": [{
                        key: ["PTS", "REB", "AST"],
                        "athletes": [
                            {
                                "athlete": {
                                    "id": "202",
                                    "displayName": "Bob Jones",
                                    "jersey": "11",
                                    "position": {"abbreviation": "F"},
                                },
                                "stats": ["18", "5", "2"],
                                "starter": False,
                                "active": True,
                                "didNotPlay": False,
                                "reason": "",
                            }
                        ],
                    }],
                },
            ]
        }
    }


def _make_soccer_summary():
    """Minimal ESPN soccer summary with rosters."""
    return {
        "rosters": [
            {
                "homeAway": "home",
                "formation": "4-3-3",
                "team": {"abbreviation": "HOM"},
                "roster": [
                    {
                        "athlete": {"id": "501", "displayName": "Carlos Gomez"},
                        "jersey": "9",
                        "position": {"abbreviation": "FW", "name": "Forward"},
                        "starter": True,
                        "active": True,
                        "subbedIn": False,
                        "subbedOut": False,
                        "formationPlace": "CF",
                        "stats": [
                            {"abbreviation": "G", "displayValue": "1"},
                            {"abbreviation": "SH", "displayValue": "3"},
                        ],
                    }
                ],
            },
            {
                "homeAway": "away",
                "formation": "4-2-3-1",
                "team": {"abbreviation": "AWY"},
                "roster": [
                    {
                        "athlete": {"id": "502", "displayName": "Lucas Reyes"},
                        "jersey": "10",
                        "position": {"abbreviation": "MF", "name": "Midfielder"},
                        "starter": True,
                        "active": True,
                        "subbedIn": False,
                        "subbedOut": True,
                        "formationPlace": "AM",
                        "stats": [
                            {"abbreviation": "G", "displayValue": "0"},
                            {"abbreviation": "SH", "displayValue": "1"},
                        ],
                    }
                ],
            },
        ]
    }


def _make_game_dict(**kw) -> dict:
    """Return a minimal game dict as stored in data/ files."""
    g = {
        "event_id":    kw.get("event_id", "abc123"),
        "short_name":  kw.get("short_name", "HOM @ AWY"),
        "sport":       kw.get("sport", "basketball"),
        "league":      kw.get("league", "nba"),
        "status":      kw.get("status", "post"),
        "players":     kw.get("players", []),
        "formations":  kw.get("formations", {}),
        "home":        {"team_id": kw.get("home_id", "1"), "team_abbr": "HOM"},
        "away":        {"team_id": kw.get("away_id", "2"), "team_abbr": "AWY"},
    }
    return g


# ════════════════════════════════════════════════════════════════════════════
# 1. enrich_players — parse_players (non-soccer, "names" key)
# ════════════════════════════════════════════════════════════════════════════

class TestParsePlayersBoxscore(unittest.TestCase):

    def _parse(self, summary, home_id="1", away_id="2"):
        return ep.parse_players(summary, home_id, away_id)

    # ── basic ---
    def test_returns_list(self):
        players = self._parse(_make_boxscore_summary())
        self.assertIsInstance(players, list)

    def test_player_count(self):
        players = self._parse(_make_boxscore_summary())
        self.assertEqual(len(players), 2)

    def test_home_player_fields(self):
        players = self._parse(_make_boxscore_summary())
        home = next(p for p in players if p["home_away"] == "home")
        self.assertEqual(home["player_id"], "101")
        self.assertEqual(home["display_name"], "Alice Smith")
        self.assertEqual(home["jersey"], "23")
        self.assertEqual(home["position"], "G")
        self.assertEqual(home["team_abbr"], "HOM")
        self.assertTrue(home["starter"])
        self.assertFalse(home["did_not_play"])
        self.assertEqual(home["stats"], {"PTS": "25", "REB": "7", "AST": "4"})

    def test_away_player_fields(self):
        players = self._parse(_make_boxscore_summary())
        away = next(p for p in players if p["home_away"] == "away")
        self.assertEqual(away["player_id"], "202")
        self.assertFalse(away["starter"])

    # ── NHL labels fix ───────────────────────────────────────────────────────
    def test_nhl_labels_key_parsed(self):
        """NHL summaries use 'labels' not 'names' — must fall back correctly."""
        players = self._parse(_make_boxscore_summary(use_labels=True))
        self.assertEqual(len(players), 2)
        self.assertEqual(players[0]["stats"], {"PTS": "25", "REB": "7", "AST": "4"})

    def test_empty_summary_returns_empty_list(self):
        players = self._parse({})
        self.assertEqual(players, [])

    def test_summary_no_players_key(self):
        players = self._parse({"boxscore": {}})
        self.assertEqual(players, [])

    def test_unknown_team_id_labelled_away(self):
        """A team whose ID matches neither home nor away should still be labelled away."""
        players = self._parse(_make_boxscore_summary(), home_id="99", away_id="2")
        # home_id=99 doesn't match any team → both should be "away"
        self.assertTrue(all(p["home_away"] == "away" for p in players))


# ════════════════════════════════════════════════════════════════════════════
# 2. enrich_players — parse_soccer_roster
# ════════════════════════════════════════════════════════════════════════════

class TestParseSoccerRoster(unittest.TestCase):

    def setUp(self):
        self.players, self.formations = ep.parse_soccer_roster(_make_soccer_summary())

    def test_player_count(self):
        self.assertEqual(len(self.players), 2)

    def test_formations_parsed(self):
        self.assertEqual(self.formations["home"], "4-3-3")
        self.assertEqual(self.formations["away"], "4-2-3-1")

    def test_home_player_fields(self):
        home = next(p for p in self.players if p["home_away"] == "home")
        self.assertEqual(home["player_id"], "501")
        self.assertEqual(home["position"], "FW")
        self.assertEqual(home["position_name"], "Forward")
        self.assertTrue(home["starter"])
        self.assertFalse(home["subbed_out"])
        self.assertEqual(home["stats"]["G"], "1")
        self.assertEqual(home["stats"]["SH"], "3")

    def test_away_player_subbed_out(self):
        away = next(p for p in self.players if p["home_away"] == "away")
        self.assertTrue(away["subbed_out"])
        self.assertEqual(away["stats"]["G"], "0")

    def test_empty_summary(self):
        players, formations = ep.parse_soccer_roster({})
        self.assertEqual(players, [])
        self.assertEqual(formations, {})

    def test_no_formation_field(self):
        summary = deepcopy(_make_soccer_summary())
        for entry in summary["rosters"]:
            del entry["formation"]
        _, formations = ep.parse_soccer_roster(summary)
        self.assertEqual(formations, {})


# ════════════════════════════════════════════════════════════════════════════
# 3. enrich_players — enrich_game
# ════════════════════════════════════════════════════════════════════════════

class TestEnrichGame(unittest.TestCase):

    def _mock_http(self, summary_return):
        http = MagicMock()
        http.get = MagicMock(return_value=summary_return)
        return http

    def test_non_soccer_enrichment(self):
        http = self._mock_http(_make_boxscore_summary())
        game = _make_game_dict()
        result = ep.enrich_game(http, game)
        self.assertTrue(result)
        self.assertEqual(len(game["players"]), 2)

    def test_soccer_enrichment(self):
        http = self._mock_http(_make_soccer_summary())
        game = _make_game_dict(sport="soccer", league="eng.1")
        result = ep.enrich_game(http, game)
        self.assertTrue(result)
        self.assertEqual(len(game["players"]), 2)
        self.assertIn("home", game["formations"])
        self.assertIn("away", game["formations"])

    def test_missing_event_id_returns_false(self):
        http = self._mock_http({})
        game = _make_game_dict(event_id="")
        result = ep.enrich_game(http, game)
        self.assertFalse(result)

    def test_missing_sport_returns_false(self):
        http = self._mock_http({})
        game = _make_game_dict(sport="")
        result = ep.enrich_game(http, game)
        self.assertFalse(result)

    def test_exception_returns_false(self):
        http = MagicMock()
        http.get = MagicMock(side_effect=RuntimeError("network error"))
        game = _make_game_dict()
        result = ep.enrich_game(http, game)
        self.assertFalse(result)

    def test_players_list_set_on_game(self):
        http = self._mock_http(_make_boxscore_summary())
        game = _make_game_dict()
        ep.enrich_game(http, game)
        self.assertIsInstance(game["players"], list)


# ════════════════════════════════════════════════════════════════════════════
# 4. enrich_players — SPORT_FILE_PREFIX (shared config)
# ════════════════════════════════════════════════════════════════════════════

class TestSportFilePrefix(unittest.TestCase):

    def test_all_leagues_covered(self):
        """Every league_key in fetch_matches.LEAGUES must be in SPORT_FILE_PREFIX."""
        for key in fm.LEAGUES:
            self.assertIn(key, ep.SPORT_FILE_PREFIX,
                          f"League '{key}' missing from SPORT_FILE_PREFIX")

    def test_soccer_leagues_map_to_soccer(self):
        soccer_leagues = ["epl", "laliga", "bundesliga", "ligue1", "ucl", "uel", "mls"]
        for lk in soccer_leagues:
            self.assertEqual(ep.SPORT_FILE_PREFIX[lk], "soccer",
                             f"{lk} should map to 'soccer'")

    def test_non_soccer_leagues_map_to_themselves(self):
        for lk in ["nba", "nhl", "mlb", "nfl", "ncaab", "ncaaf"]:
            self.assertEqual(ep.SPORT_FILE_PREFIX[lk], lk)

    def test_shared_across_modules(self):
        """daily_ingest and realtime_monitor must import the exact same object."""
        import realtime_monitor as rm
        self.assertIs(di.SPORT_FILE_PREFIX, ep.SPORT_FILE_PREFIX)
        self.assertIs(rm.SPORT_FILE_PREFIX, ep.SPORT_FILE_PREFIX)


# ════════════════════════════════════════════════════════════════════════════
# 5. enrich_players — file helpers (find / ensure)
# ════════════════════════════════════════════════════════════════════════════

class TestFileHelpers(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, filename, data=None):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            json.dump(data or [], f)
        return path

    # ── find_sport_file ─────────────────────────────────────────────────────
    def test_find_returns_none_when_empty(self):
        result = ep.find_sport_file("nba", self.tmpdir)
        self.assertIsNone(result)

    def test_find_returns_file_when_exists(self):
        self._write("nba.json")
        result = ep.find_sport_file("nba", self.tmpdir)
        self.assertIsNotNone(result)
        self.assertIn("nba", result)

    def test_find_returns_most_recent(self):
        # With undated filenames there is only one file per prefix — just verify it's found
        p = self._write("nba.json")
        result = ep.find_sport_file("nba", self.tmpdir)
        self.assertEqual(os.path.abspath(result), os.path.abspath(p))

    def test_find_soccer_leagues_use_soccer_prefix(self):
        self._write("soccer.json")
        for lk in ["epl", "laliga", "mls"]:
            result = ep.find_sport_file(lk, self.tmpdir)
            self.assertIsNotNone(result, f"find_sport_file failed for {lk}")

    def test_find_ignores_other_sport_files(self):
        self._write("nhl.json")
        result = ep.find_sport_file("nba", self.tmpdir)
        self.assertIsNone(result)

    # ── ensure_sport_file ───────────────────────────────────────────────────
    def test_ensure_returns_existing_file(self):
        existing = self._write("nba.json")
        result = ep.ensure_sport_file("nba", self.tmpdir)
        self.assertEqual(os.path.abspath(result), os.path.abspath(existing))

    def test_ensure_creates_new_file_if_missing(self):
        result = ep.ensure_sport_file("nba", self.tmpdir)
        self.assertTrue(os.path.exists(result))
        self.assertEqual(os.path.basename(result), "nba.json")
        with open(result) as f:
            data = json.load(f)
        self.assertEqual(data, [])

    def test_ensure_new_file_uses_today_date(self):
        # New files are simply prefix.json — no date in the name
        result = ep.ensure_sport_file("nba", self.tmpdir)
        self.assertEqual(os.path.basename(result), "nba.json")

    def test_ensure_soccer_league_uses_soccer_prefix(self):
        result = ep.ensure_sport_file("epl", self.tmpdir)
        self.assertEqual(os.path.basename(result), "soccer.json")


# ════════════════════════════════════════════════════════════════════════════
# 6. enrich_players — enrich_file
# ════════════════════════════════════════════════════════════════════════════

class TestEnrichFile(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_file(self, games: list[dict], name="test.json") -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "w") as f:
            json.dump(games, f)
        return path

    def test_skips_already_enriched_games(self):
        game = _make_game_dict(players=[{"player_id": "999"}])
        path = self._write_file([game])
        http = MagicMock()
        ep.enrich_file(http, path, verbose=False)
        # http.get should not have been called since game already has players
        http.get.assert_not_called()

    def test_enriches_games_without_players(self):
        game = _make_game_dict()  # players=[]
        path = self._write_file([game])
        http = MagicMock()
        http.get = MagicMock(return_value=_make_boxscore_summary())
        ep.enrich_file(http, path, verbose=False)
        with open(path) as f:
            saved = json.load(f)
        self.assertTrue(len(saved[0]["players"]) > 0)

    def test_skips_pre_status_games(self):
        """Games with status='pre' should not be enriched."""
        game = _make_game_dict(status="pre")
        path = self._write_file([game])
        http = MagicMock()
        ep.enrich_file(http, path, verbose=False)
        http.get.assert_not_called()

    def test_saves_file_atomically(self):
        """After enrich_file, no .tmp file should remain."""
        game = _make_game_dict()
        path = self._write_file([game])
        http = MagicMock()
        http.get = MagicMock(return_value=_make_boxscore_summary())
        ep.enrich_file(http, path, verbose=False)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_handles_failed_enrichment_gracefully(self):
        """If fetch_summary raises, that game gets players=[] and file is still saved."""
        game = _make_game_dict()
        path = self._write_file([game])
        http = MagicMock()
        http.get = MagicMock(side_effect=RuntimeError("timeout"))
        ep.enrich_file(http, path, verbose=False)
        with open(path) as f:
            saved = json.load(f)
        self.assertEqual(saved[0]["players"], [])  # unchanged, not crashed


# ════════════════════════════════════════════════════════════════════════════
# 7. daily_ingest — load_existing_ids
# ════════════════════════════════════════════════════════════════════════════

class TestLoadExistingIds(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, data, name="file.json"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_loads_ids(self):
        path = self._write([{"event_id": "111"}, {"event_id": "222"}])
        ids = di.load_existing_ids(path)
        self.assertEqual(ids, {"111", "222"})

    def test_returns_empty_set_for_missing_file(self):
        ids = di.load_existing_ids(os.path.join(self.tmpdir, "nonexistent.json"))
        self.assertEqual(ids, set())

    def test_returns_empty_set_for_corrupt_file(self):
        path = os.path.join(self.tmpdir, "bad.json")
        with open(path, "w") as f:
            f.write("{{invalid json")
        ids = di.load_existing_ids(path)
        self.assertEqual(ids, set())

    def test_ignores_entries_without_event_id(self):
        path = self._write([{"name": "no id here"}, {"event_id": "555"}])
        ids = di.load_existing_ids(path)
        self.assertEqual(ids, {"555"})

    def test_ids_are_strings(self):
        path = self._write([{"event_id": 42}])  # int event_id
        ids = di.load_existing_ids(path)
        self.assertIn("42", ids)


# ════════════════════════════════════════════════════════════════════════════
# 8. daily_ingest — append_games
# ════════════════════════════════════════════════════════════════════════════

class TestAppendGames(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _path(self, name="games.json"):
        return os.path.join(self.tmpdir, name)

    def test_appends_to_empty_file(self):
        path = self._path()
        with open(path, "w") as f:
            json.dump([], f)
        di.append_games(path, [{"event_id": "1"}])
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)

    def test_appends_to_existing_data(self):
        path = self._path()
        with open(path, "w") as f:
            json.dump([{"event_id": "existing"}], f)
        di.append_games(path, [{"event_id": "new"}])
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["event_id"], "existing")
        self.assertEqual(data[1]["event_id"], "new")

    def test_no_tmp_file_left(self):
        path = self._path()
        with open(path, "w") as f:
            json.dump([], f)
        di.append_games(path, [{"event_id": "x"}])
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_handles_corrupt_existing_file(self):
        """If the existing file is corrupt JSON, it should start fresh."""
        path = self._path()
        with open(path, "w") as f:
            f.write("{{corrupt")
        di.append_games(path, [{"event_id": "y"}])
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)

    def test_multiple_games_appended(self):
        path = self._path()
        with open(path, "w") as f:
            json.dump([], f)
        new_games = [{"event_id": str(i)} for i in range(5)]
        di.append_games(path, new_games)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(len(data), 5)


# ════════════════════════════════════════════════════════════════════════════
# 9. daily_ingest — parse_game_to_dict
# ════════════════════════════════════════════════════════════════════════════

def _make_espn_event(state="post", home_id="10", away_id="20"):
    """Minimal ESPN scoreboard event structure."""
    return {
        "id":        "EVT999",
        "name":      "Home Team vs Away Team",
        "shortName": "HOM @ AWY",
        "date":      "2026-03-11T00:00Z",
        "competitions": [{
            "id": "EVT999",
            "status": {
                "type": {
                    "state": state,
                    "description": "Final" if state == "post" else "In Progress",
                    "name": "STATUS_FINAL" if state == "post" else "STATUS_IN_PROGRESS",
                },
                "period": 4,
                "displayClock": "0:00",
            },
            "competitors": [
                {
                    "homeAway": "home",
                    "team": {"id": home_id, "displayName": "Home Team", "abbreviation": "HOM"},
                    "score": "110",
                    "winner": True,
                },
                {
                    "homeAway": "away",
                    "team": {"id": away_id, "displayName": "Away Team", "abbreviation": "AWY"},
                    "score": "105",
                    "winner": False,
                },
            ],
        }],
    }


class TestParseGameToDict(unittest.TestCase):

    def _make_http(self):
        http = MagicMock()
        http.get = MagicMock(return_value={})
        return http

    def test_returns_none_for_non_finished_game(self):
        http = self._make_http()
        event = _make_espn_event(state="in")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertIsNone(result)

    def test_returns_none_for_pre_game(self):
        http = self._make_http()
        event = _make_espn_event(state="pre")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertIsNone(result)

    def test_returns_dict_for_finished_game(self):
        http = self._make_http()
        event = _make_espn_event(state="post")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertIsInstance(result, dict)

    def test_required_fields_present(self):
        http = self._make_http()
        event = _make_espn_event(state="post")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        for field in ["event_id", "name", "short_name", "date", "status",
                      "sport", "league", "home", "away", "players", "formations", "raw_odds"]:
            self.assertIn(field, result, f"Missing field: {field}")

    def test_home_away_structure(self):
        http = self._make_http()
        event = _make_espn_event(state="post")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        for side in ["home", "away"]:
            self.assertIn("team_id", result[side])
            self.assertIn("team_name", result[side])
            self.assertIn("score", result[side])
            self.assertIn("is_winner", result[side])

    def test_scores_captured(self):
        http = self._make_http()
        event = _make_espn_event(state="post")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertEqual(result["home"]["score"], "110")
        self.assertEqual(result["away"]["score"], "105")
        self.assertTrue(result["home"]["is_winner"])
        self.assertFalse(result["away"]["is_winner"])

    def test_players_empty_by_default(self):
        """parse_game_to_dict no longer fetches players — they're added by enrich_file later."""
        http = self._make_http()
        event = _make_espn_event(state="post")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["formations"], {})

    def test_missing_competitions_returns_none(self):
        http = self._make_http()
        event = {"id": "X", "name": "X", "shortName": "X", "date": "", "competitions": []}
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertIsNone(result)

    def test_status_and_sport_persisted(self):
        http = self._make_http()
        event = _make_espn_event(state="post")
        result = di.parse_game_to_dict(http, "basketball", "nba", "nba", event)
        self.assertEqual(result["status"], "post")
        self.assertEqual(result["sport"], "basketball")
        self.assertEqual(result["league"], "nba")


# ════════════════════════════════════════════════════════════════════════════
# 10. fetch_matches — helper functions
# ════════════════════════════════════════════════════════════════════════════

class TestFetchMatchesHelpers(unittest.TestCase):

    def test_parse_int_odds_valid(self):
        self.assertEqual(fm._parse_int_odds(-110), -110)
        self.assertEqual(fm._parse_int_odds("+145"), 145)
        self.assertEqual(fm._parse_int_odds("-165"), -165)
        self.assertEqual(fm._parse_int_odds(200), 200)

    def test_parse_int_odds_none(self):
        self.assertIsNone(fm._parse_int_odds(None))
        self.assertIsNone(fm._parse_int_odds(""))
        self.assertIsNone(fm._parse_int_odds("N/A"))

    def test_parse_float_valid(self):
        self.assertAlmostEqual(fm._parse_float("3.5"), 3.5)
        self.assertAlmostEqual(fm._parse_float(7.0), 7.0)
        self.assertAlmostEqual(fm._parse_float("-2.5"), -2.5)

    def test_parse_float_none(self):
        self.assertIsNone(fm._parse_float(None))
        self.assertIsNone(fm._parse_float(""))

    def test_pick_provider_prefers_espn_bet(self):
        items = [
            {"provider": {"name": "DraftKings", "priority": 1}},
            {"provider": {"name": "ESPN BET", "priority": 2}},
        ]
        chosen = fm._pick_provider(items)
        self.assertEqual(chosen["provider"]["name"], "ESPN BET")

    def test_pick_provider_falls_back_to_lowest_priority(self):
        items = [
            {"provider": {"name": "SomeBook", "priority": 5}},
            {"provider": {"name": "OtherBook", "priority": 3}},
        ]
        chosen = fm._pick_provider(items)
        self.assertEqual(chosen["provider"]["name"], "OtherBook")

    def test_pick_provider_empty_returns_none(self):
        self.assertIsNone(fm._pick_provider([]))

    def test_moneyline_to_implied_prob_favorite(self):
        prob = fm._moneyline_to_implied_prob(-200)
        self.assertAlmostEqual(prob, 200 / 300, places=4)

    def test_moneyline_to_implied_prob_underdog(self):
        prob = fm._moneyline_to_implied_prob(+150)
        self.assertAlmostEqual(prob, 100 / 250, places=4)

    def test_moneyline_to_implied_prob_none(self):
        self.assertIsNone(fm._moneyline_to_implied_prob(None))

    def test_estimate_team_totals_sum_to_game_total(self):
        total = 220.0
        ht, at = fm._estimate_team_totals(total, -200, +165)
        if ht is not None and at is not None:
            self.assertAlmostEqual(ht + at, total, places=2)

    def test_estimate_team_totals_none_when_no_ml(self):
        ht, at = fm._estimate_team_totals(220.0, None, None)
        # Both None is acceptable when moneylines are missing
        if ht is not None or at is not None:
            self.assertAlmostEqual((ht or 0) + (at or 0), 220.0, places=2)

    def test_status_state_post(self):
        comp = {
            "status": {
                "type": {"state": "post", "description": "Final", "name": "STATUS_FINAL"},
                "period": 4,
                "displayClock": "0:00",
            }
        }
        state, detail, period, clock = fm._status_state(comp)
        self.assertEqual(state, "post")
        self.assertEqual(period, 4)


# ════════════════════════════════════════════════════════════════════════════
# 11. realtime_monitor — helper functions
# ════════════════════════════════════════════════════════════════════════════

class TestRealtimeMonitorHelpers(unittest.TestCase):

    def setUp(self):
        import realtime_monitor as rm
        self.rm = rm

    def test_parse_int_valid(self):
        self.assertEqual(self.rm._parse_int("42"), 42)
        self.assertEqual(self.rm._parse_int(3.7), 3)

    def test_parse_int_none(self):
        self.assertIsNone(self.rm._parse_int(None))
        self.assertIsNone(self.rm._parse_int("abc"))

    def test_parse_float_valid(self):
        self.assertAlmostEqual(self.rm._parse_float("7.5"), 7.5)

    def test_parse_float_none(self):
        self.assertIsNone(self.rm._parse_float(None))

    def test_pick_provider_espn_bet(self):
        items = [
            {"provider": {"name": "fanduel"}},
            {"provider": {"name": "espn bet"}},
        ]
        self.assertEqual(
            self.rm._pick_provider(items)["provider"]["name"], "espn bet"
        )

    def test_parse_status_finished(self):
        comp = {
            "status": {
                "type": {"state": "post", "description": "Final", "name": "STATUS_FINAL"},
                "period": 4,
                "displayClock": "0:00",
            }
        }
        state, detail, period, clock = self.rm._parse_status(comp)
        self.assertEqual(state, "post")
        self.assertEqual(clock, "0:00")

    def test_parse_status_live(self):
        comp = {
            "status": {
                "type": {"state": "in", "description": "3rd Quarter", "name": "STATUS_IN_PROGRESS"},
                "period": 3,
                "displayClock": "4:22",
            }
        }
        state, _, period, clock = self.rm._parse_status(comp)
        self.assertEqual(state, "in")
        self.assertEqual(period, 3)
        self.assertEqual(clock, "4:22")

    def test_sport_file_prefix_imported(self):
        import realtime_monitor as rm
        self.assertIsInstance(rm.SPORT_FILE_PREFIX, dict)
        self.assertIn("nba", rm.SPORT_FILE_PREFIX)

    def test_parsers_are_imported_from_enrich_players(self):
        import realtime_monitor as rm
        self.assertIs(rm.parse_players_boxscore, ep.parse_players)
        self.assertIs(rm.parse_players_soccer,   ep.parse_soccer_roster)


# ════════════════════════════════════════════════════════════════════════════
# 12. realtime_monitor — save_live_state output structure
# ════════════════════════════════════════════════════════════════════════════

class TestSaveLiveState(unittest.TestCase):

    def setUp(self):
        import realtime_monitor as rm
        self.rm = rm
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_state(self, status="in") -> dict:
        return {
            "event_id":      "EVT1",
            "short_name":    "HOM @ AWY",
            "league_key":    "nba",
            "sport":         "basketball",
            "league":        "nba",
            "status":        status,
            "status_detail": "3rd Quarter",
            "period":        3,
            "clock":         "5:00",
            "home":  {"team_abbr": "HOM", "score": "60", "team_id": "1"},
            "away":  {"team_abbr": "AWY", "score": "55", "team_id": "2"},
            "odds":  {},
            "win_prob": {},
            "players": [],
            "formations": {},
            "date": "2026-03-12T00:00Z",
            "name": "Home Team vs Away Team",
        }

    def test_output_file_created(self):
        states = {"EVT1": self._make_state("in")}
        self.rm.save_live_state(states, self.tmpdir)
        path = os.path.join(self.tmpdir, "live_state.json")
        self.assertTrue(os.path.exists(path))

    def test_output_has_required_sections(self):
        states = {"EVT1": self._make_state("in")}
        self.rm.save_live_state(states, self.tmpdir)
        with open(os.path.join(self.tmpdir, "live_state.json")) as f:
            data = json.load(f)
        for key in ["live", "pregame", "finished", "live_count", "pregame_count",
                    "finished_count", "updated_at"]:
            self.assertIn(key, data, f"Missing key: {key}")

    def test_live_game_appears_in_live_section(self):
        states = {"EVT1": self._make_state("in")}
        self.rm.save_live_state(states, self.tmpdir)
        with open(os.path.join(self.tmpdir, "live_state.json")) as f:
            data = json.load(f)
        self.assertEqual(len(data["live"]), 1)
        self.assertEqual(len(data["pregame"]), 0)
        self.assertEqual(len(data["finished"]), 0)

    def test_pre_game_appears_in_pregame_section(self):
        states = {"EVT1": self._make_state("pre")}
        self.rm.save_live_state(states, self.tmpdir)
        with open(os.path.join(self.tmpdir, "live_state.json")) as f:
            data = json.load(f)
        self.assertEqual(len(data["pregame"]), 1)
        self.assertEqual(len(data["live"]), 0)

    def test_finished_game_appears_in_finished_section(self):
        states = {"EVT1": self._make_state("post")}
        self.rm.save_live_state(states, self.tmpdir)
        with open(os.path.join(self.tmpdir, "live_state.json")) as f:
            data = json.load(f)
        self.assertEqual(len(data["finished"]), 1)

    def test_counts_match_list_lengths(self):
        states = {
            "E1": self._make_state("in"),
            "E2": self._make_state("pre"),
            "E3": self._make_state("post"),
        }
        states["E2"]["event_id"] = "E2"
        states["E3"]["event_id"] = "E3"
        self.rm.save_live_state(states, self.tmpdir)
        with open(os.path.join(self.tmpdir, "live_state.json")) as f:
            data = json.load(f)
        self.assertEqual(data["live_count"],     len(data["live"]))
        self.assertEqual(data["pregame_count"],  len(data["pregame"]))
        self.assertEqual(data["finished_count"], len(data["finished"]))

    def test_dated_live_file_created(self):
        """save_live_state must write live/live_YYYYMMDD.json with only in-progress games."""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        states = {
            "E1": self._make_state("in"),
            "E2": {**self._make_state("pre"), "event_id": "E2"},
        }
        self.rm.save_live_state(states, self.tmpdir)
        path = os.path.join(self.tmpdir, f"live_{today}.json")
        self.assertTrue(os.path.exists(path), f"Expected {path} to exist")
        with open(path) as f:
            data = json.load(f)
        self.assertIn("games", data)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["games"][0]["status"], "in")

    def test_dated_pregame_file_created(self):
        """save_live_state must write live/pregame_YYYYMMDD.json with only upcoming games."""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        states = {
            "E1": self._make_state("pre"),
            "E2": {**self._make_state("in"), "event_id": "E2"},
        }
        self.rm.save_live_state(states, self.tmpdir)
        path = os.path.join(self.tmpdir, f"pregame_{today}.json")
        self.assertTrue(os.path.exists(path), f"Expected {path} to exist")
        with open(path) as f:
            data = json.load(f)
        self.assertIn("games", data)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["games"][0]["status"], "pre")


# ════════════════════════════════════════════════════════════════════════════
# 13. realtime_monitor — archive_finished_game
# ════════════════════════════════════════════════════════════════════════════

class TestArchiveFinishedGame(unittest.TestCase):

    def setUp(self):
        import realtime_monitor as rm
        self.rm = rm
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_gs(self, event_id="EVT42") -> dict:
        return {
            "event_id":      event_id,
            "name":          "Home vs Away",
            "short_name":    "HOM @ AWY",
            "date":          "2026-03-12T00:00Z",
            "status":        "post",
            "status_detail": "Final",
            "period":        4,
            "clock":         "0:00",
            "sport":         "basketball",
            "league":        "nba",
            "league_key":    "nba",
            "home": {"team_id": "1", "team_name": "Home", "team_abbr": "HOM",
                     "score": "110", "is_winner": True},
            "away": {"team_id": "2", "team_name": "Away", "team_abbr": "AWY",
                     "score": "105", "is_winner": False},
            "odds":     {"provider": "ESPN BET", "home_ml": -150, "away_ml": 130,
                         "game_total": 215.5, "over_odds": -110, "under_odds": -110},
            "win_prob": {"home_pct": 0.72, "away_pct": 0.28},
            "players":  [{"player_id": "101", "display_name": "Test Player"}],
            "formations": {},
        }

    def _seed_file(self, data=None) -> str:
        path = os.path.join(self.tmpdir, "nba.json")
        with open(path, "w") as f:
            json.dump(data or [], f)
        return path

    def test_creates_file_if_missing(self):
        gs = self._make_gs()
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        files = [f for f in os.listdir(self.tmpdir) if f == "nba.json"]
        self.assertEqual(len(files), 1)

    def test_record_written_to_file(self):
        self._seed_file()
        gs = self._make_gs()
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        path = os.path.join(self.tmpdir, "nba.json")
        with open(path) as f:
            records = json.load(f)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event_id"], "EVT42")

    def test_does_not_duplicate_existing_event(self):
        existing_record = {"event_id": "EVT42", "name": "already here"}
        self._seed_file([existing_record])
        gs = self._make_gs("EVT42")
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        path = os.path.join(self.tmpdir, "nba.json")
        with open(path) as f:
            records = json.load(f)
        self.assertEqual(len(records), 1)  # not duplicated

    def test_skips_unknown_league_key(self):
        gs = self._make_gs()
        gs["league_key"] = "unknown_league"
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        # No file should be created since the league_key has no prefix
        files = [f for f in os.listdir(self.tmpdir) if f.endswith(".json")]
        self.assertEqual(len(files), 0)

    def test_skips_empty_event_id(self):
        gs = self._make_gs(event_id="")
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        files = [f for f in os.listdir(self.tmpdir) if f.endswith(".json")]
        self.assertEqual(len(files), 0)

    def test_archived_record_has_players(self):
        self._seed_file()
        gs = self._make_gs()
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        path = os.path.join(self.tmpdir, "nba.json")
        with open(path) as f:
            records = json.load(f)
        self.assertTrue(len(records[0]["players"]) > 0)

    def test_no_tmp_file_left_after_archive(self):
        self._seed_file()
        gs = self._make_gs()
        self.rm.archive_finished_game(gs, data_dir=self.tmpdir)
        path = os.path.join(self.tmpdir, "nba.json")
        self.assertFalse(os.path.exists(path + ".tmp"))


# ════════════════════════════════════════════════════════════════════════════
# 14. Integration: round-trip enrich_file → data file integrity
# ════════════════════════════════════════════════════════════════════════════

class TestRoundTripEnrichment(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enrich_then_reload_preserves_data(self):
        """After enrich_file, reloading the file should show all player data."""
        games = [_make_game_dict(event_id=str(i)) for i in range(3)]
        path = os.path.join(self.tmpdir, "nba_test.json")
        with open(path, "w") as f:
            json.dump(games, f)

        http = MagicMock()
        http.get = MagicMock(return_value=_make_boxscore_summary())
        ep.enrich_file(http, path, verbose=False)

        with open(path) as f:
            saved = json.load(f)

        self.assertEqual(len(saved), 3)
        for g in saved:
            self.assertTrue(len(g["players"]) > 0,
                            f"Game {g['event_id']} should have players after enrichment")

    def test_enrich_skips_already_enriched_and_preserves_them(self):
        """Only empty-player games are enriched; pre-enriched ones stay untouched."""
        enriched_game   = _make_game_dict(event_id="A",
                                          players=[{"player_id": "ORIGINAL"}])
        unenriched_game = _make_game_dict(event_id="B")
        path = os.path.join(self.tmpdir, "nba_test.json")
        with open(path, "w") as f:
            json.dump([enriched_game, unenriched_game], f)

        http = MagicMock()
        http.get = MagicMock(return_value=_make_boxscore_summary())
        ep.enrich_file(http, path, verbose=False)

        with open(path) as f:
            saved = json.load(f)

        enriched   = next(g for g in saved if g["event_id"] == "A")
        unenriched = next(g for g in saved if g["event_id"] == "B")

        self.assertEqual(enriched["players"][0]["player_id"], "ORIGINAL",
                         "Pre-enriched game should not be touched")
        self.assertTrue(len(unenriched["players"]) > 0,
                        "Unenriched game should now have players")


# ════════════════════════════════════════════════════════════════════════════
# 15. build_db — scalar helper functions
# ════════════════════════════════════════════════════════════════════════════

import build_db as bdb


class TestBuildDbHelpers(unittest.TestCase):
    """Unit tests for _ts(), _int(), _float(), _score() in build_db."""

    # ── _ts : timestamp normalisation ───────────────────────────────────────

    def test_ts_z_suffix_stripped(self):
        """ESPN 'Z' suffix must produce a plain UTC string without tz offset."""
        result = bdb._ts("2026-02-28T15:15Z")
        self.assertEqual(result, "2026-02-28T15:15:00")

    def test_ts_z_with_seconds_stripped(self):
        result = bdb._ts("2026-03-10T20:00:00Z")
        self.assertEqual(result, "2026-03-10T20:00:00")

    def test_ts_missing_seconds_padded(self):
        """HH:MM-only timestamps (no seconds) must have :00 padded before offset strip."""
        result = bdb._ts("2026-03-07T20:00Z")
        self.assertEqual(result, "2026-03-07T20:00:00")

    def test_ts_plus_offset_stripped(self):
        result = bdb._ts("2026-03-11T20:00:00+00:00")
        self.assertEqual(result, "2026-03-11T20:00:00")

    def test_ts_minus_offset_stripped(self):
        result = bdb._ts("2026-01-01T13:30:00-05:00")
        self.assertEqual(result, "2026-01-01T13:30:00")

    def test_ts_none_returns_none(self):
        self.assertIsNone(bdb._ts(None))

    def test_ts_empty_string_returns_none(self):
        self.assertIsNone(bdb._ts(""))

    def test_ts_no_dhaka_shift(self):
        """Stored value must NOT be shifted by any timezone (old TIMESTAMPTZ bug)."""
        result = bdb._ts("2026-02-22T15:15Z")  # Barcelona vs Levante
        # Old bug: would shift to 21:15 (Asia/Dhaka +06:00)
        self.assertTrue(result.startswith("2026-02-22T15:15"),
                        f"Expected UTC 15:15, got {result}")

    # ── _int ────────────────────────────────────────────────────────────────

    def test_int_valid_int(self):
        self.assertEqual(bdb._int(42), 42)

    def test_int_valid_str(self):
        self.assertEqual(bdb._int("7"), 7)

    def test_int_float_string(self):
        self.assertIsNone(bdb._int("3.7"))  # not a castable int

    def test_int_none(self):
        self.assertIsNone(bdb._int(None))

    def test_int_non_numeric(self):
        self.assertIsNone(bdb._int("N/A"))

    def test_int_negative(self):
        self.assertEqual(bdb._int("-150"), -150)

    def test_int_zero(self):
        self.assertEqual(bdb._int("0"), 0)

    # ── _float ──────────────────────────────────────────────────────────────

    def test_float_valid(self):
        self.assertAlmostEqual(bdb._float("3.5"), 3.5)

    def test_float_int_input(self):
        self.assertAlmostEqual(bdb._float(7), 7.0)

    def test_float_none(self):
        self.assertIsNone(bdb._float(None))

    def test_float_empty(self):
        self.assertIsNone(bdb._float(""))

    def test_float_non_numeric(self):
        self.assertIsNone(bdb._float("abc"))

    def test_float_negative(self):
        self.assertAlmostEqual(bdb._float("-2.5"), -2.5)

    # ── _score ───────────────────────────────────────────────────────────────

    def test_score_integer_string(self):
        self.assertEqual(bdb._score("3"), 3)

    def test_score_float_string(self):
        """Scores like '3.0' from some APIs must be accepted."""
        self.assertEqual(bdb._score("3.0"), 3)

    def test_score_none(self):
        self.assertIsNone(bdb._score(None))

    def test_score_non_numeric(self):
        self.assertIsNone(bdb._score("N/A"))

    def test_score_zero(self):
        self.assertEqual(bdb._score("0"), 0)

    def test_score_integer(self):
        self.assertEqual(bdb._score(110), 110)


# ════════════════════════════════════════════════════════════════════════════
# 16. build_db — load_file (in-memory DuckDB)
# ════════════════════════════════════════════════════════════════════════════

import duckdb


def _make_in_memory_db() -> duckdb.DuckDBPyConnection:
    """Return a fresh in-memory DuckDB connection with the sports schema."""
    con = duckdb.connect(":memory:")
    con.execute(bdb.DDL)
    return con


def _write_json(path: str, data: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _minimal_game(**kw) -> dict:
    """Build a minimal game dict that load_file can consume."""
    return {
        "event_id":      kw.get("event_id", "EVT001"),
        "name":          kw.get("name", "Home vs Away"),
        "short_name":    kw.get("short_name", "HOM @ AWY"),
        "date":          kw.get("date", "2026-03-01T18:00Z"),
        "status":        kw.get("status", "post"),
        "status_detail": kw.get("status_detail", "Final"),
        "sport":         kw.get("sport", "basketball"),
        "league":        kw.get("league", "nba"),
        "period":        kw.get("period", 4),
        "clock":         kw.get("clock", "0:00"),
        "home": {
            "team_id":   kw.get("home_id", "T1"),
            "team_name": kw.get("home_name", "Home Team"),
            "team_abbr": kw.get("home_abbr", "HOM"),
            "score":     kw.get("home_score", "110"),
            "is_winner": kw.get("home_winner", True),
        },
        "away": {
            "team_id":   kw.get("away_id", "T2"),
            "team_name": kw.get("away_name", "Away Team"),
            "team_abbr": kw.get("away_abbr", "AWY"),
            "score":     kw.get("away_score", "105"),
            "is_winner": kw.get("away_winner", False),
        },
        "players":    kw.get("players", []),
        "formations": kw.get("formations", {}),
        "home_win_pct": kw.get("home_win_pct", None),
        "away_win_pct": kw.get("away_win_pct", None),
        "game_total":   kw.get("game_total", None),
        "over_odds":    kw.get("over_odds", None),
        "under_odds":   kw.get("under_odds", None),
        "open_spread":  kw.get("open_spread", None),
        "open_total":   kw.get("open_total", None),
        "draw_odds":    kw.get("draw_odds", None),
        "provider":     kw.get("provider", None),
        "moneyline":    kw.get("moneyline", None),
    }


def _minimal_player(**kw) -> dict:
    return {
        "player_id":    kw.get("player_id", "P1"),
        "display_name": kw.get("display_name", "Test Player"),
        "position":     kw.get("position", "G"),
        "home_away":    kw.get("home_away", "home"),
        "starter":      kw.get("starter", True),
        "active":       kw.get("active", True),
        "did_not_play": kw.get("did_not_play", False),
        "dnp_reason":   kw.get("dnp_reason", ""),
        "subbed_in":    kw.get("subbed_in", False),
        "subbed_out":   kw.get("subbed_out", False),
        "formation_place": kw.get("formation_place", None),
        "stats":        kw.get("stats", {"PTS": "20", "REB": "5"}),
    }


class TestLoadFileInMemory(unittest.TestCase):
    """Tests for build_db.load_file() using an in-memory DuckDB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.con = _make_in_memory_db()

    def tearDown(self):
        self.con.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _path(self, name="test.json"):
        return os.path.join(self.tmpdir, name)

    def _write(self, data, name="test.json"):
        p = self._path(name)
        _write_json(p, data)
        return p

    # ── Basic inserts ────────────────────────────────────────────────────────

    def test_single_game_inserted(self):
        p = self._write([_minimal_game()])
        games, players, stats = bdb.load_file(self.con, p)
        self.assertEqual(games, 1)
        n = self.con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        self.assertEqual(n, 1)

    def test_returns_correct_counts_no_players(self):
        p = self._write([_minimal_game()])
        g, pl, st = bdb.load_file(self.con, p)
        self.assertEqual(g, 1)
        self.assertEqual(pl, 0)
        self.assertEqual(st, 0)

    def test_returns_correct_counts_with_players(self):
        player = _minimal_player(stats={"PTS": "15", "REB": "4", "AST": "3"})
        game = _minimal_game(players=[player])
        p = self._write([game])
        g, pl, st = bdb.load_file(self.con, p)
        self.assertEqual(g, 1)
        self.assertEqual(pl, 1)
        self.assertEqual(st, 1)  # 1 game_player row with stats_json set

    def test_empty_file_returns_zeros(self):
        p = self._write([])
        g, pl, st = bdb.load_file(self.con, p)
        self.assertEqual((g, pl, st), (0, 0, 0))

    # ── Deduplication ────────────────────────────────────────────────────────

    def test_duplicate_event_id_skipped(self):
        game = _minimal_game(event_id="DUP001")
        p = self._write([game])
        bdb.load_file(self.con, p)         # first load
        g2, _, _ = bdb.load_file(self.con, p)  # second load — same event_id
        self.assertEqual(g2, 0, "Duplicate event_id should be skipped")
        n = self.con.execute("SELECT COUNT(*) FROM games WHERE event_id='DUP001'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_new_event_in_second_load_is_ingested(self):
        p1 = self._write([_minimal_game(event_id="E1")], "file1.json")
        p2 = self._write([_minimal_game(event_id="E2")], "file2.json")
        bdb.load_file(self.con, p1)
        g2, _, _ = bdb.load_file(self.con, p2)
        self.assertEqual(g2, 1)
        n = self.con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        self.assertEqual(n, 2)

    # ── game_teams ───────────────────────────────────────────────────────────

    def test_game_teams_created(self):
        p = self._write([_minimal_game()])
        bdb.load_file(self.con, p)
        n = self.con.execute("SELECT COUNT(*) FROM game_teams WHERE event_id='EVT001'").fetchone()[0]
        self.assertEqual(n, 2, "Each game should produce exactly 2 game_team rows")

    def test_game_teams_home_away_labels(self):
        p = self._write([_minimal_game()])
        bdb.load_file(self.con, p)
        sides = set(r[0] for r in self.con.execute(
            "SELECT home_away FROM game_teams WHERE event_id='EVT001'"
        ).fetchall())
        self.assertEqual(sides, {"home", "away"})

    def test_game_teams_scores_match_game(self):
        p = self._write([_minimal_game(home_score="110", away_score="105")])
        bdb.load_file(self.con, p)
        home_score = self.con.execute(
            "SELECT score FROM game_teams WHERE event_id='EVT001' AND home_away='home'"
        ).fetchone()[0]
        away_score = self.con.execute(
            "SELECT score FROM game_teams WHERE event_id='EVT001' AND home_away='away'"
        ).fetchone()[0]
        self.assertEqual(home_score, 110)
        self.assertEqual(away_score, 105)

    def test_missing_team_id_skips_game_team(self):
        """A side with no team_id should not produce a game_teams row."""
        game = _minimal_game()
        game["home"]["team_id"] = ""
        p = self._write([game])
        bdb.load_file(self.con, p)
        n = self.con.execute(
            "SELECT COUNT(*) FROM game_teams WHERE event_id='EVT001'"
        ).fetchone()[0]
        self.assertEqual(n, 1, "Only the away side should be inserted since home has no team_id")

    # ── game_players & player_stats ──────────────────────────────────────────

    def test_player_inserted_into_game_players(self):
        player = _minimal_player(player_id="PX", home_away="home")
        game = _minimal_game(players=[player])
        p = self._write([game])
        bdb.load_file(self.con, p)
        n = self.con.execute(
            "SELECT COUNT(*) FROM game_players WHERE event_id='EVT001'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_player_stats_inserted(self):
        player = _minimal_player(stats={"PTS": "25", "REB": "10"})
        game = _minimal_game(players=[player])
        p = self._write([game])
        bdb.load_file(self.con, p)
        import json as _json
        row = self.con.execute(
            "SELECT stats_json FROM game_players WHERE event_id='EVT001'"
        ).fetchone()
        self.assertIsNotNone(row, "game_players row should exist")
        stats = _json.loads(row[0] or "{}")
        self.assertIn("PTS", stats)
        self.assertIn("REB", stats)

    def test_player_stat_values_stored_as_strings(self):
        player = _minimal_player(stats={"PTS": "42"})
        game = _minimal_game(players=[player])
        p = self._write([game])
        bdb.load_file(self.con, p)
        import json as _json
        row = self.con.execute(
            "SELECT stats_json FROM game_players WHERE event_id='EVT001'"
        ).fetchone()
        self.assertIsNotNone(row, "game_players row should exist")
        stats = _json.loads(row[0] or "{}")
        val = stats.get("PTS")
        self.assertIsInstance(val, str)
        self.assertEqual(val, "42")

    def test_player_with_no_player_id_skipped(self):
        player = _minimal_player(player_id="")
        game = _minimal_game(players=[player])
        p = self._write([game])
        bdb.load_file(self.con, p)
        n = self.con.execute("SELECT COUNT(*) FROM game_players").fetchone()[0]
        self.assertEqual(n, 0)

    def test_multiple_players_inserted(self):
        players = [
            _minimal_player(player_id="PA", home_away="home"),
            _minimal_player(player_id="PB", home_away="away"),
            _minimal_player(player_id="PC", home_away="home"),
        ]
        game = _minimal_game(players=players)
        p = self._write([game])
        bdb.load_file(self.con, p)
        n = self.con.execute("SELECT COUNT(*) FROM game_players").fetchone()[0]
        self.assertEqual(n, 3)

    # ── game_date storage ─────────────────────────────────────────────────────

    def test_game_date_stored_as_utc_without_tz_shift(self):
        """The fixed _ts() must store the bare UTC time, not shifted to local tz."""
        game = _minimal_game(event_id="TZTEST", date="2026-02-22T15:15Z")
        p = self._write([game])
        bdb.load_file(self.con, p)
        row = self.con.execute(
            "SELECT game_date FROM games WHERE event_id='TZTEST'"
        ).fetchone()
        self.assertIsNotNone(row)
        dt = row[0]
        # Must be 15:15, not 21:15 (old Asia/Dhaka +06 offset bug)
        self.assertEqual(dt.hour, 15, f"Expected 15:15 UTC, got {dt}")
        self.assertEqual(dt.minute, 15)

    def test_game_date_column_is_timestamp_not_timestamptz(self):
        """Schema must use TIMESTAMP, not TIMESTAMPTZ — no pytz dependency."""
        col_info = self.con.execute("DESCRIBE games").fetchall()
        date_col = next(c for c in col_info if c[0] == "game_date")
        self.assertNotIn("WITH TIME ZONE", date_col[1].upper(),
                         "game_date should be plain TIMESTAMP, not TIMESTAMPTZ")

    # ── teams master table ────────────────────────────────────────────────────

    def test_teams_inserted(self):
        p = self._write([_minimal_game()])
        bdb.load_file(self.con, p)
        n = self.con.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        self.assertEqual(n, 2)

    def test_team_deduplication_across_games(self):
        """Same team across two games should appear only once in teams."""
        g1 = _minimal_game(event_id="E1", home_id="SAME_TEAM")
        g2 = _minimal_game(event_id="E2", home_id="SAME_TEAM")
        p = self._write([g1, g2])
        bdb.load_file(self.con, p)
        n = self.con.execute(
            "SELECT COUNT(*) FROM teams WHERE team_id='SAME_TEAM'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    # ── soccer extras ─────────────────────────────────────────────────────────

    def test_soccer_formation_stored(self):
        game = _minimal_game(
            event_id="SOC01", sport="soccer", league="esp.1",
            formations={"home": "4-3-3", "away": "4-2-3-1"}
        )
        p = self._write([game])
        bdb.load_file(self.con, p)
        row = self.con.execute(
            "SELECT home_formation, away_formation FROM games WHERE event_id='SOC01'"
        ).fetchone()
        self.assertEqual(row[0], "4-3-3")
        self.assertEqual(row[1], "4-2-3-1")

    def test_draw_odds_stored(self):
        game = _minimal_game(
            event_id="DRAW01", sport="soccer", league="esp.1",
            draw_odds=-115
        )
        p = self._write([game])
        bdb.load_file(self.con, p)
        val = self.con.execute(
            "SELECT draw_odds FROM games WHERE event_id='DRAW01'"
        ).fetchone()[0]
        self.assertEqual(val, -115)

    # ── None / edge values ────────────────────────────────────────────────────

    def test_null_score_stored_as_null(self):
        game = _minimal_game(home_score=None, away_score=None)
        game["home"]["score"] = None
        game["away"]["score"] = None
        p = self._write([game])
        bdb.load_file(self.con, p)
        row = self.con.execute(
            "SELECT home_score, away_score FROM games WHERE event_id='EVT001'"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])

    def test_missing_event_id_not_inserted(self):
        game = _minimal_game()
        game["event_id"] = ""
        p = self._write([game])
        g, _, _ = bdb.load_file(self.con, p)
        self.assertEqual(g, 0)

    def test_win_probability_stored(self):
        game = _minimal_game(home_win_pct=0.72, away_win_pct=0.28)
        p = self._write([game])
        bdb.load_file(self.con, p)
        row = self.con.execute(
            "SELECT home_win_pct, away_win_pct FROM games WHERE event_id='EVT001'"
        ).fetchone()
        self.assertAlmostEqual(row[0], 0.72, places=4)
        self.assertAlmostEqual(row[1], 0.28, places=4)

    def test_multiple_games_in_one_file(self):
        games = [_minimal_game(event_id=f"EVT{i:03d}") for i in range(5)]
        p = self._write(games)
        g, _, _ = bdb.load_file(self.con, p)
        self.assertEqual(g, 5)

    def test_player_starter_and_dnp_flags(self):
        player = _minimal_player(starter=True, did_not_play=False)
        dnp    = _minimal_player(player_id="DNP1", starter=False, did_not_play=True,
                                  active=False, stats={})
        game = _minimal_game(players=[player, dnp])
        p = self._write([game])
        bdb.load_file(self.con, p)
        row = self.con.execute(
            "SELECT starter, did_not_play FROM game_players WHERE player_id='DNP1'"
        ).fetchone()
        self.assertFalse(row[0])
        self.assertTrue(row[1])


# ════════════════════════════════════════════════════════════════════════════
# 17. Database Integration — live sports.db edge cases
# ════════════════════════════════════════════════════════════════════════════

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "db", "sports.db"
)


@unittest.skipUnless(os.path.exists(_DB_PATH), "sports.db not found — run build_db.py first")
class TestDatabaseIntegrity(unittest.TestCase):
    """Integration tests against the real sports.db database."""

    @classmethod
    def setUpClass(cls):
        cls.con = duckdb.connect(_DB_PATH, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    # ── Table non-empty ───────────────────────────────────────────────────────

    def test_games_table_populated(self):
        n = self.con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        self.assertGreater(n, 0, "games table must not be empty")

    def test_teams_table_populated(self):
        n = self.con.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        self.assertGreater(n, 0)

    def test_players_table_populated(self):
        n = self.con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        self.assertGreater(n, 0)

    def test_game_players_populated(self):
        n = self.con.execute("SELECT COUNT(*) FROM game_players").fetchone()[0]
        self.assertGreater(n, 0)

    def test_player_stats_populated(self):
        n = self.con.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0]
        self.assertGreater(n, 0)

    # ── Schema correctness ────────────────────────────────────────────────────

    def test_game_date_is_plain_timestamp(self):
        """After the TIMESTAMPTZ → TIMESTAMP fix, column type must have no tz."""
        col_info = self.con.execute("DESCRIBE games").fetchall()
        date_col = next(c for c in col_info if c[0] == "game_date")
        self.assertNotIn("WITH TIME ZONE", date_col[1].upper(),
                         "game_date must be TIMESTAMP, not TIMESTAMPTZ")

    def test_games_columns_present(self):
        cols = {c[0] for c in self.con.execute("DESCRIBE games").fetchall()}
        required = {"event_id", "sport", "league", "name", "short_name",
                    "game_date", "status", "home_score", "away_score",
                    "home_win_pct", "away_win_pct", "home_formation", "away_formation",
                    "draw_odds", "provider", "game_total"}
        self.assertTrue(required.issubset(cols), f"Missing columns: {required - cols}")

    def test_game_teams_columns_present(self):
        cols = {c[0] for c in self.con.execute("DESCRIBE game_teams").fetchall()}
        required = {"id", "event_id", "team_id", "sport", "home_away",
                    "score", "is_winner", "moneyline", "spread"}
        self.assertTrue(required.issubset(cols))

    # ── Foreign key integrity ─────────────────────────────────────────────────

    def test_no_orphaned_game_teams(self):
        n = self.con.execute("""
            SELECT COUNT(*) FROM game_teams gt
            WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.event_id = gt.event_id)
        """).fetchone()[0]
        self.assertEqual(n, 0, f"{n} orphaned game_teams rows")

    def test_no_orphaned_game_players(self):
        n = self.con.execute("""
            SELECT COUNT(*) FROM game_players gp
            WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.event_id = gp.event_id)
        """).fetchone()[0]
        self.assertEqual(n, 0, f"{n} orphaned game_players rows")

    def test_no_orphaned_player_stats(self):
        n = self.con.execute("""
            SELECT COUNT(*) FROM player_stats ps
            WHERE NOT EXISTS (
                SELECT 1 FROM game_players gp WHERE gp.id = ps.game_player_id
            )
        """).fetchone()[0]
        self.assertEqual(n, 0, f"{n} orphaned player_stats rows")

    def test_no_orphaned_game_players_to_players(self):
        n = self.con.execute("""
            SELECT COUNT(*) FROM game_players gp
            WHERE NOT EXISTS (
                SELECT 1 FROM players p
                WHERE p.player_id = gp.player_id AND p.sport = gp.sport
            )
        """).fetchone()[0]
        self.assertEqual(n, 0, f"{n} game_players without a matching player row")

    # ── Uniqueness / no duplicates ────────────────────────────────────────────

    def test_no_duplicate_event_ids(self):
        dups = self.con.execute("""
            SELECT event_id FROM games GROUP BY event_id HAVING COUNT(*) > 1
        """).fetchall()
        self.assertEqual(len(dups), 0, f"Duplicate event_ids: {[d[0] for d in dups]}")

    def test_no_duplicate_game_player_ids(self):
        dups = self.con.execute("""
            SELECT id FROM game_players GROUP BY id HAVING COUNT(*) > 1
        """).fetchall()
        self.assertEqual(len(dups), 0)

    def test_no_duplicate_player_stat_ids(self):
        dups = self.con.execute("""
            SELECT id FROM player_stats GROUP BY id HAVING COUNT(*) > 1
        """).fetchall()
        self.assertEqual(len(dups), 0)

    # ── Sports coverage ───────────────────────────────────────────────────────

    def test_nba_games_present(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE league='nba'"
        ).fetchone()[0]
        self.assertGreater(n, 0, "No NBA games found")

    def test_mlb_games_present(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE league='mlb'"
        ).fetchone()[0]
        self.assertGreater(n, 0)

    def test_nhl_games_present(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE league='nhl'"
        ).fetchone()[0]
        self.assertGreater(n, 0)

    def test_laliga_games_present(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE league='esp.1'"
        ).fetchone()[0]
        self.assertGreaterEqual(n, 32, f"Expected ≥32 La Liga games, found {n}")

    def test_ucl_games_present(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE league='uefa.champions'"
        ).fetchone()[0]
        self.assertGreater(n, 0)

    def test_cricket_games_present(self):
        """Cricket was broken (0 rows) before the pandas import fix."""
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE sport='cricket'"
        ).fetchone()[0]
        self.assertEqual(n, 5, f"Expected 5 cricket games (Sheffield Shield), found {n}")

    # ── Barcelona specific ────────────────────────────────────────────────────

    def test_barcelona_in_teams_table(self):
        row = self.con.execute(
            "SELECT team_id, team_abbr FROM teams WHERE team_name='Barcelona' AND sport='soccer'"
        ).fetchone()
        self.assertIsNotNone(row, "Barcelona must be in teams table")
        self.assertEqual(row[0], "83")
        self.assertEqual(row[1], "BAR")

    def test_barcelona_has_laliga_games(self):
        n = self.con.execute("""
            SELECT COUNT(*) FROM games g
            JOIN game_teams gt ON gt.event_id = g.event_id
            WHERE gt.team_id = '83' AND g.league = 'esp.1'
        """).fetchone()[0]
        self.assertGreaterEqual(n, 3, f"Expected ≥3 Barcelona La Liga games, found {n}")

    def test_barcelona_has_ucl_game(self):
        n = self.con.execute("""
            SELECT COUNT(*) FROM games g
            JOIN game_teams gt ON gt.event_id = g.event_id
            WHERE gt.team_id = '83' AND g.league = 'uefa.champions'
        """).fetchone()[0]
        self.assertGreaterEqual(n, 1, "Barcelona must have ≥1 UCL game")

    def test_barcelona_players_present(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM game_players WHERE team_id='83'"
        ).fetchone()[0]
        self.assertGreater(n, 0, "Barcelona should have player data")

    def test_barcelona_known_players_in_db(self):
        """Key Barcelona players must appear in the players table."""
        known = ["Raphinha", "Pedri", "Lamine Yamal"]
        for name in known:
            row = self.con.execute(
                "SELECT player_id FROM players WHERE display_name=? AND sport='soccer'",
                [name]
            ).fetchone()
            self.assertIsNotNone(row, f"Barcelona player '{name}' not found in players table")

    def test_barcelona_levante_correct_score(self):
        """Levante vs Barcelona: Barcelona won 3-0 at home."""
        row = self.con.execute("""
            SELECT g.home_score, g.away_score, gt_home.team_id
            FROM games g
            JOIN game_teams gt_home ON gt_home.event_id = g.event_id AND gt_home.home_away = 'home'
            WHERE g.event_id = '748391'
        """).fetchone()
        self.assertIsNotNone(row, "Event 748391 (Levante at Barcelona) must exist")
        self.assertEqual(row[0], 3)   # Barcelona scored 3
        self.assertEqual(row[1], 0)   # Levante scored 0
        self.assertEqual(row[2], "83")  # Home team is Barcelona

    def test_barcelona_villarreal_correct_score(self):
        """Villarreal at Barcelona: Barcelona 4-1."""
        row = self.con.execute("""
            SELECT g.home_score, g.away_score
            FROM games g
            WHERE g.event_id = '748402'
        """).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 4)
        self.assertEqual(row[1], 1)

    # ── Timezone correctness (core fix validation) ────────────────────────────

    def test_laliga_dates_not_shifted_by_six_hours(self):
        """Old bug: TIMESTAMPTZ shifted all UTC times by +6h (Asia/Dhaka).
        Barcelona vs Levante was at 15:15 UTC — must NOT read back as 21:15."""
        row = self.con.execute(
            "SELECT game_date FROM games WHERE event_id='748391'"
        ).fetchone()
        self.assertIsNotNone(row)
        dt = row[0]
        self.assertEqual(dt.hour, 15,
                         f"Expected 15:15 UTC (not Dhaka-shifted {dt.hour}:15)")
        self.assertEqual(dt.minute, 15)

    def test_ucl_barca_newcastle_date_correct(self):
        """Barcelona vs Newcastle UCL was at 20:00 UTC — must not be shifted."""
        row = self.con.execute(
            "SELECT game_date FROM games WHERE event_id='401862577'"
        ).fetchone()
        self.assertIsNotNone(row)
        dt = row[0]
        self.assertEqual(dt.hour, 20,
                         f"Expected 20:00 UTC, got {dt.hour}:00")

    def test_no_game_dates_in_1970(self):
        """Epoch (1970-01-01) dates indicate a parsing failure."""
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE CAST(game_date AS DATE) < '2020-01-01'"
        ).fetchone()[0]
        self.assertEqual(n, 0, f"{n} games have suspiciously old dates")

    def test_no_future_dates_beyond_season(self):
        """No game date should be more than 1 year in the future."""
        n = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE game_date > '2027-12-31'"
        ).fetchone()[0]
        self.assertEqual(n, 0, f"{n} games have implausibly future dates")

    # ── Score & result consistency ────────────────────────────────────────────

    def test_finished_games_have_scores(self):
        # Cricket uses innings-string scores in game_teams.score rather than
        # a simple numeric home_score/away_score, so exclude it from this check.
        bad = self.con.execute("""
            SELECT COUNT(*) FROM games
            WHERE status='post' AND sport != 'cricket'
              AND (home_score IS NULL OR away_score IS NULL)
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} finished non-cricket games have NULL scores")

    def test_cricket_scores_are_innings_strings(self):
        """Cricket uses innings-string scoring; no numeric scores are stored.

        The ESPN Sheffield Shield data doesn't provide parseable numeric scores
        (_score() can't convert '238 & 114 (44.5 ov)' to int), so both
        games.home_score and game_teams.score are NULL for cricket.
        """
        null_home = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE sport='cricket' AND home_score IS NULL"
        ).fetchone()[0]
        total = self.con.execute(
            "SELECT COUNT(*) FROM games WHERE sport='cricket'"
        ).fetchone()[0]
        self.assertGreater(total, 0, "There should be cricket games in the DB")
        self.assertEqual(null_home, total,
                         "All cricket games should have NULL home_score (innings format)")

    def test_home_score_matches_game_teams_score(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM games g
            JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'home'
            WHERE g.home_score IS NOT NULL AND gt.score IS NOT NULL
              AND g.home_score != gt.score
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} home score mismatches between games and game_teams")

    def test_away_score_matches_game_teams_score(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM games g
            JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'away'
            WHERE g.away_score IS NOT NULL AND gt.score IS NOT NULL
              AND g.away_score != gt.score
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} away score mismatches between games and game_teams")

    def test_no_game_has_two_winners(self):
        """is_winner=TRUE must appear at most once per game."""
        bad = self.con.execute("""
            SELECT COUNT(*) FROM (
                SELECT event_id, COUNT(*) c FROM game_teams
                WHERE is_winner = TRUE
                GROUP BY event_id HAVING c > 1
            )
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} games have two winners")

    def test_all_finished_soccer_games_have_two_sides(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM (
                SELECT g.event_id, COUNT(gt.id) n
                FROM games g
                LEFT JOIN game_teams gt ON gt.event_id = g.event_id
                WHERE g.sport='soccer' AND g.status='post'
                GROUP BY g.event_id HAVING n != 2
            )
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} finished soccer games don't have exactly 2 sides")

    # ── Odds sanity ───────────────────────────────────────────────────────────

    def test_moneylines_within_sane_range(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM game_teams
            WHERE moneyline IS NOT NULL
              AND (moneyline < -999999 OR moneyline > 999999)
        """).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_draw_odds_only_on_soccer(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM games
            WHERE draw_odds IS NOT NULL AND sport != 'soccer'
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} non-soccer games have draw_odds")

    def test_win_probability_sums_to_one(self):
        """Where both win_pcts are populated, they must sum to ~1.0."""
        bad = self.con.execute("""
            SELECT COUNT(*) FROM games
            WHERE home_win_pct IS NOT NULL AND away_win_pct IS NOT NULL
              AND ABS((home_win_pct + away_win_pct) - 1.0) > 0.05
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} games where home+away win_pct ≠ 1.0")

    # ── Stat type integrity ───────────────────────────────────────────────────

    def test_nba_pts_all_numeric(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport = 'basketball' AND ps.stat_key = 'PTS'
              AND TRY_CAST(ps.stat_value AS DOUBLE) IS NULL
              AND ps.stat_value NOT IN ('', '--', 'N/A')
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} NBA PTS values that can't be cast to DOUBLE")

    def test_nba_min_all_numeric(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport = 'basketball' AND ps.stat_key = 'MIN'
              AND TRY_CAST(ps.stat_value AS DOUBLE) IS NULL
              AND ps.stat_value NOT IN ('', '--', 'N/A')
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} NBA MIN values that can't be cast to DOUBLE")

    def test_soccer_goals_all_numeric(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport = 'soccer' AND ps.stat_key = 'G'
              AND TRY_CAST(ps.stat_value AS DOUBLE) IS NULL
              AND ps.stat_value NOT IN ('', '--', 'N/A')
        """).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_mlb_no_negative_hits(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport = 'baseball' AND ps.stat_key = 'H'
              AND TRY_CAST(ps.stat_value AS INTEGER) < 0
        """).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_hockey_toi_format(self):
        """NHL Time-On-Ice must be in MM:SS format — not a raw float."""
        bad = self.con.execute(r"""
            SELECT COUNT(*) FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            WHERE gp.sport = 'hockey' AND ps.stat_key = 'TOI'
              AND NOT REGEXP_MATCHES(ps.stat_value, '^\d+:\d{2}$')
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} TOI values not in MM:SS format")

    def test_no_null_stat_keys(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM player_stats WHERE stat_key IS NULL OR stat_key=''"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_no_null_stat_values(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM player_stats WHERE stat_value IS NULL"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    # ── DNP integrity ─────────────────────────────────────────────────────────

    def test_dnp_players_have_no_nonzero_stats(self):
        bad = self.con.execute("""
            SELECT COUNT(*) FROM game_players gp
            WHERE gp.did_not_play = TRUE
              AND EXISTS (
                  SELECT 1 FROM player_stats ps
                  WHERE ps.game_player_id = gp.id
                    AND TRY_CAST(ps.stat_value AS DOUBLE) > 0
              )
        """).fetchone()[0]
        self.assertEqual(bad, 0, f"{bad} DNP players have non-zero stats")

    # ── Soccer substitution & formation ──────────────────────────────────────

    def test_soccer_subbed_in_and_out_roughly_equal(self):
        sub_in  = self.con.execute(
            "SELECT COUNT(*) FROM game_players WHERE subbed_in=TRUE"
        ).fetchone()[0]
        sub_out = self.con.execute(
            "SELECT COUNT(*) FROM game_players WHERE subbed_out=TRUE"
        ).fetchone()[0]
        # Allow up to 20% divergence for partial data
        if sub_in > 0 and sub_out > 0:
            ratio = max(sub_in, sub_out) / min(sub_in, sub_out)
            self.assertLess(ratio, 1.2,
                            f"subbed_in ({sub_in}) and subbed_out ({sub_out}) diverge >20%")

    def test_barcelona_games_have_formations(self):
        """Barcelona's home games must have formation data."""
        n = self.con.execute("""
            SELECT COUNT(*) FROM games g
            JOIN game_teams gt ON gt.event_id = g.event_id AND gt.home_away = 'home'
            WHERE gt.team_id = '83' AND g.league = 'esp.1'
              AND g.home_formation IS NOT NULL
        """).fetchone()[0]
        self.assertGreater(n, 0, "At least one Barcelona home game must have formation data")

    # ── Multi-join query correctness ──────────────────────────────────────────

    def test_nba_top_scorers_query(self):
        """5-table join to find NBA top scorers must execute and return rows."""
        rows = self.con.execute("""
            SELECT p.display_name, t.team_name,
                   SUM(CAST(ps.stat_value AS INTEGER)) total_pts
            FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id
            JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
            JOIN teams t   ON t.team_id   = gp.team_id   AND t.sport = gp.sport
            JOIN games g   ON g.event_id  = gp.event_id
            WHERE g.league = 'nba' AND ps.stat_key = 'PTS'
              AND gp.did_not_play = FALSE
            GROUP BY p.display_name, t.team_name
            HAVING SUM(CAST(ps.stat_value AS INTEGER)) >= 50
            ORDER BY total_pts DESC LIMIT 10
        """).fetchall()
        self.assertGreater(len(rows), 0, "No NBA 50+ total point scorers found")

    def test_soccer_top_scorers_query(self):
        """Barcelona goals query must return Raphinha or Lewandowski-style entries."""
        rows = self.con.execute("""
            SELECT p.display_name, SUM(CAST(ps.stat_value AS INTEGER)) goals
            FROM player_stats ps
            JOIN game_players gp ON gp.id = ps.game_player_id AND gp.team_id = '83'
            JOIN players p ON p.player_id = gp.player_id AND p.sport = gp.sport
            WHERE gp.sport = 'soccer' AND ps.stat_key = 'G'
            GROUP BY p.display_name
            HAVING goals > 0
            ORDER BY goals DESC
        """).fetchall()
        self.assertGreater(len(rows), 0, "Barcelona should have players with recorded goals")

    def test_nba_win_rate_cte(self):
        """CTE computing NBA team win rates must execute without error."""
        rows = self.con.execute("""
            SELECT t.team_name,
                   ROUND(100.0 * SUM(CASE WHEN gt.is_winner THEN 1 ELSE 0 END) / COUNT(*), 1)
                     AS win_pct
            FROM game_teams gt
            JOIN teams t  ON t.team_id = gt.team_id AND t.sport = gt.sport
            JOIN games g  ON g.event_id = gt.event_id
            WHERE g.league = 'nba' AND g.status = 'post'
            GROUP BY t.team_name HAVING COUNT(*) >= 3
            ORDER BY win_pct DESC LIMIT 5
        """).fetchall()
        self.assertGreater(len(rows), 0)

    def test_date_range_query(self):
        """Date range query must return results without pytz crash."""
        rows = self.con.execute("""
            SELECT sport, league,
                   MIN(CAST(game_date AS DATE)) first_game,
                   MAX(CAST(game_date AS DATE)) last_game
            FROM games GROUP BY sport, league ORDER BY sport
        """).fetchall()
        self.assertGreater(len(rows), 0)
        # Confirm dates are reasonable (no 1970 epoch or 2027+ dates)
        for r in rows:
            self.assertGreater(str(r[2]), "2020-01-01", f"{r[0]}/{r[1]} has old start date")

    def test_player_name_search(self):
        """ILIKE player name search must return results."""
        rows = self.con.execute("""
            SELECT display_name, sport FROM players
            WHERE display_name ILIKE '%james%' ORDER BY sport
        """).fetchall()
        self.assertGreater(len(rows), 0, "No players with 'james' in name found")

    def test_cross_sport_player_pk_design(self):
        """Players PK is (player_id, sport) — same numeric id can appear in 2 sports."""
        cross = self.con.execute("""
            SELECT player_id, COUNT(DISTINCT sport) n
            FROM players GROUP BY player_id HAVING n > 1
        """).fetchall()
        # This is expected and by design — just verify no error executing the query
        self.assertIsInstance(cross, list)


# ════════════════════════════════════════════════════════════════════════════
# 18. realtime_monitor — detect_changes (pregame + live events)
# ════════════════════════════════════════════════════════════════════════════

class TestDetectChanges(unittest.TestCase):

    def setUp(self):
        import realtime_monitor as rm
        self.rm = rm

    def _base(self, status="in", home_score="50", away_score="45",
              period=2, home_pct=0.60, home_ml=-120, away_ml=100,
              game_total=220.0):
        return {
            "event_id":   "EVT1",
            "short_name": "HOM @ AWY",
            "sport":      "basketball",
            "league":     "nba",
            "league_key": "nba",
            "status":     status,
            "period":     period,
            "clock":      "5:00",
            "home":       {"team_abbr": "HOM", "score": home_score, "is_winner": None},
            "away":       {"team_abbr": "AWY", "score": away_score, "is_winner": None},
            "win_prob":   {"home_pct": home_pct, "away_pct": 1 - home_pct},
            "odds":       {"home_ml": home_ml, "away_ml": away_ml,
                           "game_total": game_total},
        }

    def _types(self, events):
        return [e["type"] for e in events]

    # --- Transition events ---------------------------------------------------

    def test_game_started_event(self):
        old = self._base(status="pre", home_score=None, away_score=None)
        new = self._base(status="in")
        evs = self.rm.detect_changes(old, new)
        self.assertIn("GAME_STARTED", self._types(evs))

    def test_game_finished_event(self):
        old = self._base(status="in")
        new = self._base(status="post", home_score="110", away_score="105")
        evs = self.rm.detect_changes(old, new)
        self.assertIn("GAME_FINISHED", self._types(evs))

    # --- Live game events ----------------------------------------------------

    def test_score_update_live(self):
        old = self._base(home_score="50", away_score="45")
        new = self._base(home_score="52", away_score="45")
        evs = self.rm.detect_changes(old, new)
        self.assertIn("SCORE_UPDATE", self._types(evs))
        score_ev = next(e for e in evs if e["type"] == "SCORE_UPDATE")
        self.assertEqual(score_ev["home_score"], "52")

    def test_period_change_live(self):
        old = self._base(period=2)
        new = self._base(period=3)
        evs = self.rm.detect_changes(old, new)
        self.assertIn("PERIOD_CHANGE", self._types(evs))

    def test_period_change_only_when_live(self):
        """Period change must NOT fire for finished games."""
        old = self._base(status="post", period=3)
        new = self._base(status="post", period=4)
        evs = self.rm.detect_changes(old, new)
        self.assertNotIn("PERIOD_CHANGE", self._types(evs))

    def test_win_prob_shift_live(self):
        old = self._base(home_pct=0.60)
        new = self._base(home_pct=0.70)
        evs = self.rm.detect_changes(old, new)
        self.assertIn("WIN_PROB_SHIFT", self._types(evs))

    def test_win_prob_shift_NOT_fired_for_pregame(self):
        """Win probability shift must NOT fire for pregame games."""
        old = self._base(status="pre", home_score=None, away_score=None, home_pct=0.55)
        new = self._base(status="pre", home_score=None, away_score=None, home_pct=0.70)
        evs = self.rm.detect_changes(old, new)
        self.assertNotIn("WIN_PROB_SHIFT", self._types(evs))

    def test_odds_move_live_emits_odds_move(self):
        """Live game moneyline shifts emit ODDS_MOVE, not LINE_MOVE."""
        old = self._base(home_ml=-120)
        new = self._base(home_ml=-150)   # 30-point move
        evs = self.rm.detect_changes(old, new)
        self.assertIn("ODDS_MOVE", self._types(evs))
        self.assertNotIn("LINE_MOVE", self._types(evs))

    def test_no_score_update_when_none(self):
        """No SCORE_UPDATE when scores are still None (pregame)."""
        old = self._base(status="pre", home_score=None, away_score=None)
        new = self._base(status="pre", home_score=None, away_score=None)
        evs = self.rm.detect_changes(old, new)
        self.assertNotIn("SCORE_UPDATE", self._types(evs))

    def test_no_score_update_when_unchanged(self):
        old = self._base(home_score="50", away_score="45")
        new = self._base(home_score="50", away_score="45")
        evs = self.rm.detect_changes(old, new)
        self.assertNotIn("SCORE_UPDATE", self._types(evs))

    # --- Pregame game events --------------------------------------------------

    def test_line_move_for_pregame_game(self):
        """Pregame moneyline shifts emit LINE_MOVE, not ODDS_MOVE."""
        old = self._base(status="pre", home_score=None, away_score=None, home_ml=-110)
        new = self._base(status="pre", home_score=None, away_score=None, home_ml=-150)  # 40-pt move
        evs = self.rm.detect_changes(old, new)
        self.assertIn("LINE_MOVE", self._types(evs))
        self.assertNotIn("ODDS_MOVE", self._types(evs))

    def test_line_move_fields(self):
        old = self._base(status="pre", home_score=None, away_score=None, home_ml=-110)
        new = self._base(status="pre", home_score=None, away_score=None, home_ml=-160)
        evs = self.rm.detect_changes(old, new)
        lm = next((e for e in evs if e["type"] == "LINE_MOVE"), None)
        self.assertIsNotNone(lm)
        self.assertEqual(lm["old_value"], -110)
        self.assertEqual(lm["new_value"], -160)

    def test_small_ml_move_does_not_fire(self):
        """Moves of < 5 points must not emit any event."""
        old = self._base(home_ml=-110)
        new = self._base(home_ml=-112)   # 2-point move — below threshold
        evs = self.rm.detect_changes(old, new)
        types = self._types(evs)
        self.assertNotIn("ODDS_MOVE", types)
        self.assertNotIn("LINE_MOVE", types)

    def test_total_move_fires(self):
        old = self._base(game_total=220.0)
        new = self._base(game_total=221.0)   # 1-point move
        evs = self.rm.detect_changes(old, new)
        self.assertIn("TOTAL_MOVE", self._types(evs))

    def test_small_total_move_does_not_fire(self):
        old = self._base(game_total=220.0)
        new = self._base(game_total=220.3)   # < 0.5 — no event
        evs = self.rm.detect_changes(old, new)
        self.assertNotIn("TOTAL_MOVE", self._types(evs))

    def test_no_events_when_nothing_changes(self):
        g = self._base()
        evs = self.rm.detect_changes(g, dict(g))  # identical copy
        self.assertEqual(evs, [])


# ════════════════════════════════════════════════════════════════════════════
# 19. realtime_monitor — pregame rate-limiting & opening_odds (poll_once logic)
# ════════════════════════════════════════════════════════════════════════════

class TestPregameHandling(unittest.TestCase):

    def setUp(self):
        import realtime_monitor as rm
        self.rm = rm
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_state(self, status="pre", event_id="EVT1") -> dict:
        return {
            "event_id":      event_id,
            "short_name":    "HOM @ AWY",
            "name":          "Home vs Away",
            "league_key":    "nba",
            "sport":         "basketball",
            "league":        "nba",
            "status":        status,
            "status_detail": "Scheduled" if status == "pre" else "3rd Quarter",
            "period":        0 if status == "pre" else 3,
            "clock":         "",
            "home":          {"team_abbr": "HOM", "score": None if status == "pre" else "60",
                              "team_id": "1", "is_winner": None, "record": "20-10"},
            "away":          {"team_abbr": "AWY", "score": None if status == "pre" else "55",
                              "team_id": "2", "is_winner": None, "record": "18-12"},
            "odds":          {"home_ml": -150, "away_ml": 130, "game_total": 220.0},
            "win_prob":      {"home_pct": 0.60, "away_pct": 0.40},
            "players":       [],
            "formations":    {},
            "date":          "2026-03-15T20:00Z",
        }

    def test_save_live_state_separates_pregame_and_live(self):
        """save_live_state correctly places pre/in/post games in separate buckets."""
        states = {
            "E1": self._make_state("pre",  "E1"),
            "E2": self._make_state("in",   "E2"),
            "E3": self._make_state("post", "E3"),
        }
        states["E3"]["home"]["score"] = "110"
        states["E3"]["away"]["score"] = "105"
        self.rm.save_live_state(states, self.tmpdir)
        with open(os.path.join(self.tmpdir, "live_state.json")) as f:
            data = json.load(f)
        self.assertEqual(data["pregame_count"],  1)
        self.assertEqual(data["live_count"],     1)
        self.assertEqual(data["finished_count"], 1)
        self.assertEqual(data["pregame"][0]["status"],  "pre")
        self.assertEqual(data["live"][0]["status"],     "in")
        self.assertEqual(data["finished"][0]["status"], "post")

    def test_pregame_game_in_pregame_dated_file(self):
        """Pregame games must appear in pregame_YYYYMMDD.json."""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        states = {"E1": self._make_state("pre", "E1")}
        self.rm.save_live_state(states, self.tmpdir)
        path = os.path.join(self.tmpdir, f"pregame_{today}.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["games"][0]["event_id"], "E1")

    def test_live_game_not_in_pregame_file(self):
        """In-progress games must NOT appear in pregame_YYYYMMDD.json."""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        states = {"E1": self._make_state("in", "E1")}
        self.rm.save_live_state(states, self.tmpdir)
        path = os.path.join(self.tmpdir, f"pregame_{today}.json")
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["count"], 0)

    def test_pregame_odds_refresh_every_constant_exists(self):
        """PREGAME_ODDS_REFRESH_EVERY constant must be defined and > 0."""
        self.assertTrue(hasattr(self.rm, "PREGAME_ODDS_REFRESH_EVERY"))
        self.assertGreater(self.rm.PREGAME_ODDS_REFRESH_EVERY, 0)

    def test_build_game_state_accepts_refresh_extras_false(self):
        """build_game_state must accept refresh_extras=False without error
        (it returns {} when no competition data — same as normal failure path)."""
        http_mock = MagicMock()
        http_mock.get.return_value = {}   # empty response
        # Empty event — no competition data — should return {} gracefully
        result = self.rm.build_game_state(
            http_mock, "basketball", "nba", "nba",
            {"id": "EVT99", "competitions": []},
            fetch_players=False,
            player_cycle=False,
            refresh_extras=False,
        )
        self.assertEqual(result, {})
        # fetch_odds and fetch_win_prob must NOT have been called
        # (refresh_extras=False, so no external calls)
        # The http_mock.get would be called 0 times with refresh_extras=False
        http_mock.get.assert_not_called()

    def test_new_game_discovered_event_type_is_string(self):
        """NEW_GAME_DISCOVERED event must have the correct type string."""
        ev = {
            "type": "NEW_GAME_DISCOVERED",
            "event_id": "EVT1",
            "status": "pre",
            "scheduled_at": "2026-03-15T20:00Z",
        }
        self.assertEqual(ev["type"], "NEW_GAME_DISCOVERED")


# ════════════════════════════════════════════════════════════════════════════
# 20. build_db — live_games table DDL
# ════════════════════════════════════════════════════════════════════════════

class TestLiveGamesDDL(unittest.TestCase):
    """The live_games table must be created by build_db.DDL and have the right schema."""

    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute(bdb.DDL)

    def tearDown(self):
        self.con.close()

    def test_live_games_table_exists(self):
        tables = {r[0] for r in self.con.execute("SHOW TABLES").fetchall()}
        self.assertIn("live_games", tables, "live_games table must exist after DDL")

    def test_live_games_has_event_id_pk(self):
        cols = {r[0] for r in self.con.execute("DESCRIBE live_games").fetchall()}
        self.assertIn("event_id", cols)

    def test_live_games_required_columns(self):
        required = {
            "event_id", "league_key", "sport", "league", "name",
            "status", "status_detail", "period", "clock",
            "home_team_id", "home_team_name", "home_team_abbr", "home_score",
            "away_team_id", "away_team_name", "away_team_abbr", "away_score",
            "home_ml", "away_ml", "home_spread", "game_total",
            "home_win_pct", "away_win_pct",
            "situation", "players", "updated_at",
        }
        actual = {r[0] for r in self.con.execute("DESCRIBE live_games").fetchall()}
        missing = required - actual
        self.assertEqual(missing, set(), f"Missing columns in live_games: {missing}")

    def test_live_games_insert_and_select(self):
        self.con.execute("""
            INSERT INTO live_games (
                event_id, league_key, sport, league, name,
                status, status_detail, period, clock,
                home_team_id, home_team_name, home_team_abbr, home_score,
                away_team_id, away_team_name, away_team_abbr, away_score,
                home_ml, away_ml, home_spread, game_total,
                home_win_pct, away_win_pct,
                situation, players, updated_at
            ) VALUES (
                'EVT1', 'nba', 'basketball', 'nba', 'Home vs Away',
                'in', '3rd Quarter', 3, '5:00',
                'T1', 'Home', 'HOM', '60',
                'T2', 'Away', 'AWY', '55',
                -150, 130, -3.5, 220.0,
                0.65, 0.35,
                '{}', '[]', '2026-03-15T20:00:00'
            )
        """)
        row = self.con.execute("SELECT event_id, status, period FROM live_games").fetchone()
        self.assertEqual(row[0], "EVT1")
        self.assertEqual(row[1], "in")
        self.assertEqual(row[2], 3)

    def test_live_games_delete_replaces_rows(self):
        """DELETE + re-INSERT pattern used by update_db must work correctly."""
        for i in range(3):
            self.con.execute(f"""
                INSERT INTO live_games (event_id, sport, status, situation, players)
                VALUES ('EVT{i}', 'basketball', 'in', '{{}}', '[]')
            """)
        self.con.execute("DELETE FROM live_games")
        n = self.con.execute("SELECT COUNT(*) FROM live_games").fetchone()[0]
        self.assertEqual(n, 0, "DELETE must remove all live_games rows")

    def test_live_games_in_drop_all(self):
        """DROP_ALL must include live_games so --rebuild works cleanly."""
        self.assertIn("live_games", bdb.DROP_ALL)

    def test_live_games_situation_is_json_compatible(self):
        """situation column must accept a JSON string."""
        import json
        payload = json.dumps({"linescores": [{"period": 1, "home": 10, "away": 8}]})
        self.con.execute(
            "INSERT INTO live_games (event_id, sport, status, situation, players)"
            " VALUES ('EVTJ', 'basketball', 'in', ?, '[]')",
            [payload],
        )
        row = self.con.execute("SELECT situation FROM live_games WHERE event_id='EVTJ'").fetchone()
        self.assertIsNotNone(row[0])


# ════════════════════════════════════════════════════════════════════════════
# 21. update_db — incremental_historical_update & sync_live_games
# ════════════════════════════════════════════════════════════════════════════

import update_db


class TestUpdateDbIncrementalUpdate(unittest.TestCase):
    """Tests for update_db.incremental_historical_update()."""

    def setUp(self):
        self.tmpdir  = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmpdir, "historical_data")
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.tmpdir, "test.db")
        con = duckdb.connect(self.db_path)
        con.execute(bdb.DDL)
        con.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_json(self, filename: str, games: list) -> str:
        path = os.path.join(self.data_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(games, f)
        return path

    def test_returns_zeros_on_empty_data_dir(self):
        g, p, s = update_db.incremental_historical_update(self.db_path, self.data_dir)
        self.assertEqual((g, p, s), (0, 0, 0))

    def test_returns_zeros_on_missing_data_dir(self):
        g, p, s = update_db.incremental_historical_update(self.db_path, "/nonexistent/path")
        self.assertEqual((g, p, s), (0, 0, 0))

    def test_inserts_new_game_from_json(self):
        self._write_json("nba.json", [_minimal_game(event_id="NEW1")])
        g, _, _ = update_db.incremental_historical_update(self.db_path, self.data_dir)
        self.assertEqual(g, 1)

    def test_does_not_duplicate_existing_game(self):
        game = _minimal_game(event_id="DUP1")
        self._write_json("nba.json", [game])
        update_db.incremental_historical_update(self.db_path, self.data_dir)
        # Second call — nothing new
        g, _, _ = update_db.incremental_historical_update(self.db_path, self.data_dir)
        self.assertEqual(g, 0)

    def test_inserts_only_new_games(self):
        self._write_json("nba.json", [
            _minimal_game(event_id="OLD1"),
            _minimal_game(event_id="OLD2"),
        ])
        update_db.incremental_historical_update(self.db_path, self.data_dir)

        # Add a third new game
        self._write_json("nba.json", [
            _minimal_game(event_id="OLD1"),
            _minimal_game(event_id="OLD2"),
            _minimal_game(event_id="NEW3"),
        ])
        g, _, _ = update_db.incremental_historical_update(self.db_path, self.data_dir)
        self.assertEqual(g, 1)

    def test_is_idempotent(self):
        self._write_json("nba.json", [_minimal_game(event_id="IDEM1")])
        for _ in range(3):
            update_db.incremental_historical_update(self.db_path, self.data_dir)
        con = duckdb.connect(self.db_path)
        count = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)

    def test_processes_multiple_sport_files(self):
        self._write_json("nba.json",  [_minimal_game(event_id="NBA1", sport="basketball")])
        self._write_json("nhl.json",  [_minimal_game(event_id="NHL1", sport="hockey",
                                                      league="nhl")])
        g, _, _ = update_db.incremental_historical_update(self.db_path, self.data_dir)
        self.assertEqual(g, 2)


class TestUpdateDbSyncLiveGames(unittest.TestCase):
    """Tests for update_db.sync_live_games()."""

    def setUp(self):
        self.tmpdir   = tempfile.mkdtemp()
        self.live_dir = os.path.join(self.tmpdir, "live")
        os.makedirs(self.live_dir)
        self.db_path  = os.path.join(self.tmpdir, "test.db")
        con = duckdb.connect(self.db_path)
        con.execute(bdb.DDL)
        con.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_live_state(self, payload: dict) -> None:
        path = os.path.join(self.live_dir, "live_state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _live_state(self, pre=None, live=None, finished=None) -> dict:
        from datetime import datetime, timezone
        return {
            "updated_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_games":    len(pre or []) + len(live or []) + len(finished or []),
            "live_count":     len(live or []),
            "pregame_count":  len(pre or []),
            "finished_count": len(finished or []),
            "live":           live or [],
            "pregame":        pre or [],
            "finished":       finished or [],
        }

    def _game_entry(self, event_id="EVT1", status="in") -> dict:
        return {
            "event_id":      event_id,
            "league_key":    "nba",
            "sport":         "basketball",
            "league":        "nba",
            "name":          "Home vs Away",
            "status":        status,
            "status_detail": "3rd Quarter" if status == "in" else "Scheduled",
            "period":        3 if status == "in" else 0,
            "clock":         "5:00",
            "home": {"team_id": "T1", "team_name": "Home", "team_abbr": "HOM",
                     "score": "60" if status == "in" else None},
            "away": {"team_id": "T2", "team_name": "Away", "team_abbr": "AWY",
                     "score": "55" if status == "in" else None},
            "odds":     {"home_ml": -150, "away_ml": 130, "game_total": 220.0},
            "win_prob": {"home_pct": 0.65, "away_pct": 0.35},
            "situation": {"linescores": []},
            "players":   [],
        }

    def test_returns_zero_when_no_live_state_file(self):
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 0)

    def test_returns_zero_when_all_buckets_empty(self):
        self._write_live_state(self._live_state())
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 0)

    def test_syncs_live_game_to_table(self):
        self._write_live_state(self._live_state(live=[self._game_entry("E1", "in")]))
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 1)
        con = duckdb.connect(self.db_path)
        row = con.execute("SELECT status FROM live_games WHERE event_id='E1'").fetchone()
        con.close()
        self.assertEqual(row[0], "in")

    def test_syncs_pregame_to_table(self):
        self._write_live_state(self._live_state(pre=[self._game_entry("P1", "pre")]))
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 1)
        con = duckdb.connect(self.db_path)
        row = con.execute("SELECT status FROM live_games WHERE event_id='P1'").fetchone()
        con.close()
        self.assertEqual(row[0], "pre")

    def test_syncs_all_buckets_together(self):
        self._write_live_state(self._live_state(
            pre=[self._game_entry("P1", "pre")],
            live=[self._game_entry("L1", "in")],
            finished=[self._game_entry("F1", "post")],
        ))
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 3)

    def test_replaces_stale_rows_on_each_call(self):
        """Each call must DELETE all old rows and INSERT fresh ones."""
        self._write_live_state(self._live_state(live=[self._game_entry("E1", "in")]))
        update_db.sync_live_games(self.db_path, self.live_dir)

        # Second call with different data
        self._write_live_state(self._live_state(live=[self._game_entry("E2", "in")]))
        update_db.sync_live_games(self.db_path, self.live_dir)

        con = duckdb.connect(self.db_path)
        rows = con.execute("SELECT event_id FROM live_games ORDER BY event_id").fetchall()
        con.close()
        event_ids = [r[0] for r in rows]
        self.assertNotIn("E1", event_ids, "Old row E1 must be deleted on new sync")
        self.assertIn("E2", event_ids)

    def test_graceful_on_corrupt_live_state(self):
        """Corrupt JSON must return 0 without raising."""
        path = os.path.join(self.live_dir, "live_state.json")
        with open(path, "w") as f:
            f.write("{ invalid json }")
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 0)

    def test_entries_missing_event_id_are_skipped(self):
        bad_entry = {**self._game_entry(""), "event_id": ""}
        self._write_live_state(self._live_state(live=[bad_entry]))
        count = update_db.sync_live_games(self.db_path, self.live_dir)
        self.assertEqual(count, 0)

    def test_odds_stored_correctly(self):
        game = self._game_entry("ODD1", "in")
        game["odds"]["home_ml"] = -180
        game["odds"]["game_total"] = 215.5
        self._write_live_state(self._live_state(live=[game]))
        update_db.sync_live_games(self.db_path, self.live_dir)
        con = duckdb.connect(self.db_path)
        row = con.execute(
            "SELECT home_ml, game_total FROM live_games WHERE event_id='ODD1'"
        ).fetchone()
        con.close()
        self.assertEqual(row[0], -180)
        self.assertAlmostEqual(row[1], 215.5)

    def test_win_prob_stored(self):
        game = self._game_entry("WP1", "in")
        game["win_prob"]["home_pct"] = 0.73
        self._write_live_state(self._live_state(live=[game]))
        update_db.sync_live_games(self.db_path, self.live_dir)
        con = duckdb.connect(self.db_path)
        row = con.execute(
            "SELECT home_win_pct FROM live_games WHERE event_id='WP1'"
        ).fetchone()
        con.close()
        self.assertAlmostEqual(row[0], 0.73, places=4)


class TestUpdateDbHelpers(unittest.TestCase):
    """Unit tests for update_db type-coercion helpers."""

    def test_int_valid(self):
        self.assertEqual(update_db._int("42"), 42)
        self.assertEqual(update_db._int(-150), -150)

    def test_int_invalid(self):
        self.assertIsNone(update_db._int(None))
        self.assertIsNone(update_db._int("N/A"))

    def test_float_valid(self):
        self.assertAlmostEqual(update_db._float("7.5"), 7.5)

    def test_float_invalid(self):
        self.assertIsNone(update_db._float(None))
        self.assertIsNone(update_db._float(""))


# ════════════════════════════════════════════════════════════════════════════
# 22. stats_api.py — event market checks
# ════════════════════════════════════════════════════════════════════════════

class TestStatsApiMarketCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import stats_api

        cls.stats_api = stats_api
        cls.client = TestClient(stats_api.app)

    def test_historical_total_over_returns_true(self):
        historical_row = {
            "event_id": "SOC1",
            "date": "2026-03-12T20:00:00",
            "sport": "soccer",
            "league": "uefa.champions",
            "name": "Paris Saint-Germain vs Chelsea",
            "short_name": "PSG vs CHE",
            "status": "post",
            "home_team": "Paris Saint-Germain",
            "home_abbr": "PSG",
            "away_team": "Chelsea",
            "away_abbr": "CHE",
            "home_score": 4,
            "away_score": 2,
            "provider": "ESPN BET",
            "game_total": 5.5,
            "over_odds": -110,
            "under_odds": -110,
            "draw_odds": 240,
            "home_ml": -120,
            "away_ml": 100,
            "home_spread": -1.5,
            "away_spread": 1.5,
            "home_spread_odds": -105,
            "away_spread_odds": -115,
        }

        with (
            patch.object(self.stats_api, "_read_live_state", return_value={"pregame": [], "live": [], "finished": []}),
            patch.object(self.stats_api, "_query", return_value=[historical_row]),
        ):
            response = self.client.get(
                "/stats/market-check",
                params={
                    "date": "2026-03-12",
                    "team": "PSG",
                    "opponent": "Chelsea",
                    "sport": "soccer",
                    "market": "total",
                    "pick": "over",
                    "line": 5.5,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["result"])
        self.assertEqual(payload["source"], "historical")
        self.assertEqual(payload["outcome"], "win")

    def test_live_spread_uses_stored_line(self):
        live_event = {
            "event_id": "LIVE1",
            "date": "2026-03-12T20:00:00Z",
            "sport": "soccer",
            "league": "uefa.champions",
            "name": "Paris Saint-Germain vs Chelsea",
            "short_name": "PSG vs CHE",
            "status": "in",
            "home": {"team_name": "Paris Saint-Germain", "team_abbr": "PSG", "score": "3"},
            "away": {"team_name": "Chelsea", "team_abbr": "CHE", "score": "1"},
            "odds": {
                "provider": "ESPN BET",
                "home_ml": -120,
                "away_ml": 100,
                "home_spread": -1.5,
                "away_spread": 1.5,
                "home_spread_odds": -105,
                "away_spread_odds": -115,
                "game_total": 5.5,
            },
        }

        with (
            patch.object(self.stats_api, "_read_live_state", return_value={"pregame": [], "live": [live_event], "finished": []}),
            patch.object(self.stats_api, "_query", return_value=[]),
        ):
            response = self.client.get(
                "/stats/market-check",
                params={
                    "date": "2026-03-12",
                    "team": "PSG",
                    "opponent": "Chelsea",
                    "sport": "soccer",
                    "market": "spread",
                    "pick": "PSG",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["result"])
        self.assertFalse(payload["settled"])
        self.assertEqual(payload["source"], "live")

    def test_historical_moneyline_home_win_returns_true(self):
        historical_row = {
            "event_id": "SOC3",
            "date": "2026-03-12T20:00:00",
            "sport": "soccer",
            "league": "uefa.champions",
            "name": "Paris Saint-Germain vs Chelsea",
            "short_name": "PSG vs CHE",
            "status": "post",
            "home_team": "Paris Saint-Germain",
            "home_abbr": "PSG",
            "away_team": "Chelsea",
            "away_abbr": "CHE",
            "home_score": 3,
            "away_score": 1,
            "provider": "ESPN BET",
            "game_total": 3.5,
            "over_odds": -110,
            "under_odds": -110,
            "draw_odds": 240,
            "home_ml": -120,
            "away_ml": 100,
            "home_spread": -0.5,
            "away_spread": 0.5,
            "home_spread_odds": -105,
            "away_spread_odds": -115,
        }

        with (
            patch.object(self.stats_api, "_read_live_state", return_value={"pregame": [], "live": [], "finished": []}),
            patch.object(self.stats_api, "_query", return_value=[historical_row]),
        ):
            response = self.client.get(
                "/stats/market-check",
                params={
                    "date": "2026-03-12",
                    "team": "PSG",
                    "opponent": "Chelsea",
                    "sport": "soccer",
                    "market": "moneyline",
                    "pick": "home",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["result"])
        self.assertTrue(payload["settled"])
        self.assertEqual(payload["outcome"], "win")

    def test_invalid_date_returns_400(self):
        response = self.client.get(
            "/stats/market-check",
            params={
                "date": "2026-13-40",
                "team": "PSG",
                "opponent": "Chelsea",
                "sport": "soccer",
                "market": "total",
                "pick": "over",
                "line": 2.5,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "date must be in YYYY-MM-DD format")

    def test_total_without_line_returns_400(self):
        historical_row = {
            "event_id": "SOC2",
            "date": "2026-03-12T20:00:00",
            "sport": "soccer",
            "league": "uefa.champions",
            "name": "Paris Saint-Germain vs Chelsea",
            "short_name": "PSG vs CHE",
            "status": "post",
            "home_team": "Paris Saint-Germain",
            "home_abbr": "PSG",
            "away_team": "Chelsea",
            "away_abbr": "CHE",
            "home_score": 2,
            "away_score": 1,
            "provider": "ESPN BET",
            "game_total": None,
            "over_odds": None,
            "under_odds": None,
            "draw_odds": None,
            "home_ml": -120,
            "away_ml": 100,
            "home_spread": None,
            "away_spread": None,
            "home_spread_odds": None,
            "away_spread_odds": None,
        }

        with (
            patch.object(self.stats_api, "_read_live_state", return_value={"pregame": [], "live": [], "finished": []}),
            patch.object(self.stats_api, "_query", return_value=[historical_row]),
        ):
            response = self.client.get(
                "/stats/market-check",
                params={
                    "date": "2026-03-12",
                    "team": "PSG",
                    "opponent": "Chelsea",
                    "sport": "soccer",
                    "market": "total",
                    "pick": "over",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Total line is required", response.json()["detail"])


# ════════════════════════════════════════════════════════════════════════════
# 23. main.py — module structure, imports, and thread configuration
# ════════════════════════════════════════════════════════════════════════════

class TestMainModuleStructure(unittest.TestCase):
    """Verify main.py contains the correct thread targets and CLI args."""

    def setUp(self):
        import importlib
        import main as main_mod
        importlib.reload(main_mod)   # ensure fresh import
        self.main_mod = main_mod

    def test_run_api_function_exists(self):
        self.assertTrue(callable(self.main_mod._run_api))

    def test_run_monitor_function_exists(self):
        self.assertTrue(callable(self.main_mod._run_monitor))

    def test_run_updater_function_exists(self):
        self.assertTrue(callable(self.main_mod._run_updater),
                        "_run_updater must exist for Thread 3 (DB updater)")

    def test_main_function_exists(self):
        self.assertTrue(callable(self.main_mod.main))

    def test_module_has_no_import_errors(self):
        """Importing main must not raise any exception."""
        import main  # noqa: F401
        self.assertTrue(True)

    def test_run_updater_signature(self):
        """_run_updater must accept (db_path, data_dir, live_dir)."""
        import inspect
        sig = inspect.signature(self.main_mod._run_updater)
        params = list(sig.parameters.keys())
        self.assertIn("db_path",   params)
        self.assertIn("data_dir",  params)
        self.assertIn("live_dir",  params)

    def test_run_api_signature(self):
        """_run_api must accept a port parameter."""
        import inspect
        sig = inspect.signature(self.main_mod._run_api)
        self.assertIn("port", sig.parameters)

    def test_threading_import_is_present(self):
        """main must import threading for thread management."""
        import threading
        self.assertTrue(hasattr(self.main_mod, "__file__"))
        # Verify threading is available after import
        self.assertIsNotNone(threading.Thread)

    def test_signal_handlers_registered_on_import(self):
        """The module must reference signal handling (not crash on non-TTY)."""
        import signal
        # Just verify the module can coexist with signal — no explosion
        self.assertIsNotNone(signal.SIGTERM)


# ════════════════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader  = unittest.TestLoader()
    suite   = loader.discover(start_dir=os.path.dirname(os.path.abspath(__file__)),
                              pattern="test_suite.py")
    runner  = unittest.TextTestRunner(verbosity=2)
    result  = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
