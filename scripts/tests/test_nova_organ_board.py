#!/usr/bin/env python3
"""Tests for nova_organ_board.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_organ_board.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ob = _load("ob", SCRIPT)
NOW = datetime.now(timezone.utc)
ORGANS = ("affect", "time_sense", "attention_focus", "core_liveness", "security_organ", "presence", "contact_sense")


class _Cur:
    def __init__(self, rows=(), boom=False):
        self.rows = list(rows); self.boom = boom; self.sql = []
        self.connection = MagicMock()

    def execute(self, sql, params=None):
        if self.boom and "FROM organ_board" in sql:
            raise RuntimeError('relation "organ_board" does not exist')
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


def _rows(entered=None, conf=0.8):
    return [("affect", NOW, "calm", {"valence": 0.2, "arousal": 0.1}),
            # entered_at is measured against the wall clock inside someone_just_arrived(), so it must be
            # computed at call time — a module-level NOW drifted to 6 min by the end of a full-suite run.
            ("presence", NOW, "jordan@office", {"confidence": conf, "entered_at": entered or (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()}),
            ("contact_sense", NOW, "quiet", None)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertTrue(SRC.count('os.environ.get("NOVA_OPS_DSN"') == 1)

    def test_read_only_over_the_world(self):
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "TRUNCATE"):
            self.assertNotIn(verb, SRC)
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertTrue(ob.VIEW.strip().startswith("CREATE OR REPLACE VIEW organ_board AS"))


class TestPerformance(unittest.TestCase):
    def test_board_and_arrival_fast_on_10k(self):
        rows = [(f"organ{i}", NOW, "s", {"i": i}) for i in range(10_000)] + _rows()[1:2]
        t0 = time.perf_counter()
        b = ob.board(_Cur(rows))
        for _ in range(10_000):
            ob.someone_just_arrived(b)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(b), 10_001)


class TestRetry(unittest.TestCase):
    def test_board_fails_open_and_rolls_back(self):
        # RETRY GAP: board — one SELECT; a missing view yields {} (an organ that cannot read the others still speaks)
        cur = _Cur(boom=True)
        self.assertEqual(ob.board(cur), {})
        cur.connection.rollback.assert_called_once()

    def test_pg_down_is_not_retried(self):
        # main (psycopg2.connect) — the CLI retries 3x (2 s / 4 s), then the error escapes to the caller
        with patch.object(ob.psycopg2, "connect", side_effect=OSError("no pg")) as c, patch("time.sleep"), \
                patch("sys.stderr"):
            with self.assertRaises(OSError):
                ob.main()
        self.assertEqual(c.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            ob.selftest()

    def test_arrival_edges(self):
        b = ob.board(_Cur(_rows()))
        self.assertEqual(ob.someone_just_arrived(b), ("jordan", 3))              # ISO string entered_at parses
        self.assertIsNone(ob.someone_just_arrived(ob.board(_Cur(_rows(entered=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())))))   # future = clock skew, not an arrival
        self.assertIsNone(ob.someone_just_arrived(ob.board(_Cur(_rows(conf=None)))))
        self.assertIsNone(ob.someone_just_arrived({"presence": {"ts": NOW, "state": "x", "detail": {}}}))
        self.assertEqual(ob.someone_just_arrived(b, within_min=1), None)
        self.assertEqual(ob.someone_just_arrived(b, within_min=4), ("jordan", 3))

    def test_board_shape_tolerates_null_detail(self):
        b = ob.board(_Cur(_rows()))
        self.assertEqual(b["contact_sense"], {"ts": NOW, "state": "quiet", "detail": {}})
        self.assertEqual(ob.board(_Cur([])), {})


class TestIntegration(unittest.TestCase):
    def test_view_covers_every_organ_and_security_organ_consumes_it(self):
        for o in ORGANS:
            self.assertIn(f"'{o}'", ob.VIEW)
        self.assertIn("from nova_organ_board import board, someone_just_arrived", (SCRIPTS / "nova_security_organ.py").read_text())
        cur = _Cur()
        ob.ensure_view(cur)
        self.assertTrue(cur.sql[0].startswith("CREATE OR REPLACE VIEW organ_board"))

    def test_board_feeds_arrival(self):
        cur = _Cur(_rows())
        b = ob.board(cur)
        self.assertEqual(cur.sql, ["SELECT organ, ts, state, detail FROM organ_board"])
        self.assertEqual(set(b), {"affect", "presence", "contact_sense"})
        self.assertEqual(ob.someone_just_arrived(b)[0], "jordan")


class TestFunctional(unittest.TestCase):
    def test_main_prints_the_board_and_the_arrival(self):
        out = io.StringIO()
        with patch.object(ob.psycopg2, "connect", return_value=_Conn(_Cur(_rows()))), redirect_stdout(out):
            ob.main()
        lines = out.getvalue().splitlines()
        self.assertEqual([l.split()[0] for l in lines[:3]], ["affect", "contact_sense", "presence"])   # sorted by organ
        self.assertIn('"valence": 0.2', lines[0])
        self.assertEqual(lines[-1], "arrival: ('jordan', 3)")

    def test_main_error_path_missing_view_prints_no_arrival(self):
        out = io.StringIO()
        with patch.object(ob.psycopg2, "connect", return_value=_Conn(_Cur(boom=True))), redirect_stdout(out):
            ob.main()
        self.assertEqual(out.getvalue().strip(), "arrival: None")


class TestFrame(unittest.TestCase):
    def test_selftest_runs_and_import_is_guarded(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
