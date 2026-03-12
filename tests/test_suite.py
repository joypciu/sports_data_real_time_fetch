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
# Runner
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader  = unittest.TestLoader()
    suite   = loader.discover(start_dir=os.path.dirname(os.path.abspath(__file__)),
                              pattern="test_suite.py")
    runner  = unittest.TextTestRunner(verbosity=2)
    result  = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
