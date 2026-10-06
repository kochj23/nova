#!/usr/bin/env python3
"""Tests for nova_media_gardener.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Deletion safety: os.remove is mocked in every apply() test; propose() is proven never to delete files
and to refuse outright when Plex watched-state can't be verified."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_media_gardener.py"
SRC = SCRIPT.read_text()

import types  # noqa: E402

# _plex_played_suffixes() does `import nova_plex` at call time; the real module resolves PLEX_URL via PG at
# import, so every test hands it this offline stand-in through patch.dict(sys.modules) (restored after).
FAKE_PLEX = types.ModuleType("nova_plex")
FAKE_PLEX.PLEX_URL = "http://plex.test:32400"
FAKE_PLEX.token = lambda: "t"


def _plex():
    return patch.dict(sys.modules, {"nova_plex": FAKE_PLEX})


def _load():
    spec = importlib.util.spec_from_file_location("nmg_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mg = _load()
OLD = datetime.now(timezone.utc) - timedelta(days=40)


class _Cur:
    def __init__(self, fetch=()):
        self.sql = []; self.fetch = list(fetch)

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.fetch

    def __enter__(self): return self
    def __exit__(self, *a): return False


def _conn(cur):
    c = MagicMock(); c.cursor.side_effect = lambda: cur
    return c


def _xml(body):
    r = MagicMock(); r.read.return_value = body.encode(); return r


SECTIONS = '<MediaContainer><Directory key="7"><Location path="/external3/videos/youtube/"/></Directory></MediaContainer>'


def _videos(n_played, n_unplayed=1):
    v = "".join(f'<Video lastViewedAt="1"><Media><Part file="/external3/videos/youtube/S/p{i}.mp4"/></Media></Video>'
                for i in range(n_played))
    v += "".join(f'<Video><Media><Part file="/x/videos/youtube/S/u{i}.mp4"/></Media></Video>' for i in range(n_unplayed))
    return f"<MediaContainer>{v}</MediaContainer>"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_fstring_sql_is_the_constant_window(self):
        fs = re.findall(r'execute\(f"""(.*?)"""', SRC, re.S)
        self.assertEqual(len(fs), 1)
        self.assertEqual(re.findall(r"\{(\w+)\}", fs[0]), ["WINDOW_DAYS"])
        self.assertIsInstance(mg.WINDOW_DAYS, int)

    def test_candidate_query_has_all_safety_gates(self):
        for gate in ("p.policy = 'rolling_15'", "p.locked", "p.source_type = 'youtube'",
                     "'/videos/youtube/'", "NOT LIKE '%/.grab/%'", "!~* '[.]ts$'"):
            self.assertIn(gate, SRC)

    def test_apply_deletes_only_approved(self):
        cur = _Cur(fetch=[])
        with patch.object(mg.psycopg2, "connect", return_value=_conn(cur)), patch.object(mg.os, "remove") as rm:
            self.assertEqual(mg.apply(), 0)
        self.assertIn("WHERE status='approved'", cur.sql[0][0])
        rm.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_suffix_10k_paths(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            mg._suffix(f"/Volumes/external/videos/youtube/Show/{i}.mp4")
        self.assertLess(time.perf_counter() - t0, 0.2)

    def test_plex_paging_is_bounded(self):
        full = _videos(500, 0)
        with _plex(), \
             patch("urllib.request.urlopen", side_effect=[_xml(SECTIONS)] + [_xml(full)] * 100) as u:
            played = mg._plex_played_suffixes()
        self.assertEqual(u.call_count, 1 + 51)       # sections + 51 pages (stops once pages > 50)
        self.assertEqual(len(played), 500)


class TestRetry(unittest.TestCase):
    def test_plex_down_returns_none_and_propose_refuses(self):
        # RETRY GAP: _plex_played_suffixes()/urlopen — one attempt; failure -> None, and propose() changes NOTHING
        with _plex(), \
             patch("urllib.request.urlopen", side_effect=OSError("plex down")) as u:
            self.assertIsNone(mg._plex_played_suffixes())
        self.assertEqual(u.call_count, 1)
        cur = _Cur()
        with patch.object(mg.psycopg2, "connect", return_value=_conn(cur)), \
             patch.object(mg, "_plex_played_suffixes", return_value=None):
            self.assertEqual(mg.propose(), 0)
        self.assertFalse(any(s.lstrip().startswith(("DELETE", "INSERT")) for s, _ in cur.sql))

    def test_telemetry_failure_non_fatal(self):
        cur = _Cur(fetch=[])
        c2 = MagicMock(); c2.__enter__.return_value.execute.side_effect = RuntimeError("pg")
        conn = MagicMock(); conn.cursor.side_effect = [cur, c2]
        with patch.object(mg.psycopg2, "connect", return_value=conn), \
             patch.object(mg, "_plex_played_suffixes", return_value=set()):
            self.assertEqual(mg.propose(), 0)


class TestUnit(unittest.TestCase):
    def test_suffix(self):
        self.assertEqual(mg._suffix("/Volumes/external/videos/youtube/A/b.mp4"), "videos/youtube/A/b.mp4")
        self.assertEqual(mg._suffix("/external3/videos/youtube/A/b.mp4"), "videos/youtube/A/b.mp4")
        self.assertEqual(mg._suffix("/movies/x.mkv"), "/movies/x.mkv")

    def test_played_set_stops_at_unplayed(self):
        with _plex(), \
             patch("urllib.request.urlopen", side_effect=[_xml(SECTIONS), _xml(_videos(2))]):
            self.assertEqual(mg._plex_played_suffixes(), {"videos/youtube/S/p0.mp4", "videos/youtube/S/p1.mp4"})

    def test_no_youtube_library_is_unknown(self):
        with _plex(), \
             patch("urllib.request.urlopen", return_value=_xml("<MediaContainer/>")):
            self.assertIsNone(mg._plex_played_suffixes())


class TestIntegration(unittest.TestCase):
    def test_watched_kept_unwatched_proposed(self):
        rows = [("/Volumes/external/videos/youtube/S/p0.mp4", "S", OLD),
                ("/Volumes/external/videos/youtube/S/new.mp4", "S", OLD)]
        cur = _Cur(fetch=rows)
        conn = MagicMock(); conn.cursor.side_effect = [cur, MagicMock()]
        with patch.object(mg.psycopg2, "connect", return_value=conn), \
             patch.object(mg, "_plex_played_suffixes", return_value={"videos/youtube/S/p0.mp4"}), \
             patch.object(mg.os, "remove") as rm:
            self.assertEqual(mg.propose(), 1)
        inserts = [(s, p) for s, p in cur.sql if s.startswith("INSERT INTO media_prune_proposals")]
        self.assertIn("'rejected'", inserts[0][0])
        self.assertEqual(inserts[1][1][0], rows[1][0])
        self.assertTrue(inserts[1][1][3])                 # size estimated (file not stat-able)
        rm.assert_not_called()                            # propose never deletes


class TestFunctional(unittest.TestCase):
    def test_apply_golden_path(self):
        cur = _Cur(fetch=[("/v/a.mp4",), ("/v/gone.mp4",), ("/v/locked.mp4",)])
        conn = MagicMock(); conn.cursor.side_effect = [cur, MagicMock()]
        def rm(fp):
            if "locked" in fp:
                raise PermissionError("busy")
        def size(fp):
            if "gone" in fp:
                raise FileNotFoundError(fp)
            return 2_000_000
        with patch.object(mg.psycopg2, "connect", return_value=conn), \
             patch.object(mg.os, "remove", side_effect=rm) as remove, \
             patch.object(mg.os.path, "getsize", side_effect=size):
            self.assertEqual(mg.apply(), 1)
        self.assertEqual(remove.call_count, 2)
        pruned = [p[0] for s, p in cur.sql if s.startswith("UPDATE")]
        self.assertEqual(pruned, ["/v/a.mp4", "/v/gone.mp4"])     # locked file NOT marked pruned

    def test_main_dispatch(self):
        self.assertIn('"--apply" in sys.argv', SRC)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # every invocation hits PG (no --help), so the frame check is an import smoke
        r = subprocess.run([sys.executable, "-c", "import nova_media_gardener"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
