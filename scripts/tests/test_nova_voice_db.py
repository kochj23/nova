#!/usr/bin/env python3
"""Tests for nova_voice_db.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import runpy
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_voice_db.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_voice_db_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vdb = _load()
C0 = np.eye(4)[0]
C1 = np.eye(4)[1]
TOPS = [(0, 120, C0), (1, 40, C1)]


class _PG:
    def __init__(self, rows):
        self.rows = list(rows)
        self.cur = mock.MagicMock()
        self.cur.__enter__.return_value = self.cur
        self.cur.fetchone.side_effect = lambda: self.rows.pop(0) if self.rows else None
        self.conn = mock.MagicMock()
        self.conn.cursor.return_value = self.cur

    def sql(self):
        return [c[0] for c in self.cur.execute.call_args_list]


class _Env(unittest.TestCase):
    def setUp(self):
        ps = [mock.patch.object(vdb, "windows", return_value=("P", "S")),
              mock.patch.object(vdb, "diarize", return_value="L"),
              mock.patch.object(vdb, "top_speakers", return_value=TOPS),
              mock.patch("sys.stdout", new_callable=io.StringIO)]
        self.m = [p.start() for p in ps]
        self.out = self.m[3]
        self.addCleanup(lambda: [p.stop() for p in ps])

    def pg(self, rows):
        pg = _PG(rows)
        p = mock.patch.object(vdb.psycopg2, "connect", return_value=pg.conn)
        p.start(); self.addCleanup(p.stop)
        return pg


class TestSecurity(_Env):
    def test_no_hardcoded_credentials_and_no_eval(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"\beval\(")                   # DB-stored embeddings are parsed, never eval'd

    def test_hostile_stored_embedding_is_not_executed(self):
        pg = self.pg([("__import__('os').system('echo pwned')", 1)])
        with self.assertRaises(json.JSONDecodeError):
            vdb.enroll("Alex", "v.mp4", 0, 60)
        self.assertFalse(any("UPDATE" in s for s, _ in pg.sql()))

    def test_sql_parameterized(self):
        pg = self.pg([None])
        vdb.enroll("O'Brien'); --", "v.mp4", 0, 60)
        for sql, params in pg.sql():
            self.assertNotIn("O'Brien", sql)
        self.assertEqual(pg.sql()[-1][1][0], "O'Brien'); --")


class TestPerformance(_Env):
    def test_vec_serialization_10k(self):
        v = np.random.default_rng(0).normal(size=256)
        t0 = time.perf_counter()
        for _ in range(2_000):
            s = vdb._vec(v)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(json.loads(s)), 256)


class TestRetry(_Env):
    def test_memory_post_fails_open(self):
        # RETRY GAP: attribute_video — one /remember POST; failure swallowed, names still returned
        self.pg([("Alex", 0.91), ("Bo", 0.40)])
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(vdb.attribute_video("/v/ep1.mp4"), ["Alex"])
        self.assertEqual(uo.call_count, 1)


class TestUnit(_Env):
    def test_vec_format(self):
        self.assertEqual(vdb._vec([1, 0.5]), "[1.000000,0.500000]")
        self.assertEqual(vdb._vec([]), "[]")

    def test_enroll_rank_out_of_range(self):
        pg = self.pg([])
        vdb.enroll("Alex", "v.mp4", 5, 60)
        self.assertIn("only 2 speakers found", self.out.getvalue())
        self.assertEqual(pg.sql(), [])


class TestIntegration(_Env):
    def test_uses_fingerprint_helpers_and_voiceprints_table(self):
        self.assertIn("from nova_voice_fingerprint import windows, diarize, top_speakers", SRC)
        pg = self.pg([None])
        vdb.enroll("Alex", "v.mp4", 0, 90)
        self.m[0].assert_called_once_with("v.mp4", 90)
        self.assertIn("INSERT INTO voiceprints", pg.sql()[-1][0])
        self.assertEqual(json.loads(pg.sql()[-1][1][1]), [1.0, 0.0, 0.0, 0.0])

    def test_enroll_merges_running_mean(self):
        pg = self.pg([(vdb._vec(C1), 1)])
        vdb.enroll("Alex", "v.mp4", 0, 60)
        sql, params = pg.sql()[-1]
        self.assertIn("UPDATE voiceprints", sql)
        merged = np.array(json.loads(params[0]))
        self.assertTrue(np.allclose(merged, [0.707107, 0.707107, 0, 0], atol=1e-5))
        self.assertEqual(params[1:], (2, "Alex"))


class TestFunctional(_Env):
    def test_identify_prints_named_and_unknown(self):
        self.pg([("Alex", 0.88), ("Bo", 0.50)])
        vdb.identify("v.mp4", 60, 0.75)
        o = self.out.getvalue()
        self.assertIn("Speaker 0 (120s)  ->  Alex   (sim 0.88)", o)
        self.assertIn("Speaker 1 (40s)  ->  UNKNOWN   (closest 'Bo' 0.50)", o)

    def test_attribute_video_stores_speaker_index(self):
        self.pg([("Alex", 0.9), None])
        with mock.patch("urllib.request.urlopen") as uo:
            self.assertEqual(vdb.attribute_video("/v/ep1.mp4"), ["Alex"])
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["source"], "speaker_index")
        self.assertEqual(body["text"], "[ep1.mp4] identified speakers: Alex (+1 unknown voice(s))")

    def test_cli_list(self):
        pg = self.pg([])
        import datetime as _dt
        pg.cur.fetchall.return_value = [("Alex", 3, _dt.datetime(2026, 1, 2, 3, 4))]
        with mock.patch.object(sys, "argv", ["x", "list"]), \
                mock.patch("nova_voice_fingerprint.windows"), mock.patch("psycopg2.connect", return_value=pg.conn):
            runpy.run_path(str(PATH), run_name="__main__")
        self.assertIn("Alex", self.out.getvalue())
        self.assertIn("3 sample(s)", self.out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("enroll", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_voice_db"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=60, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
