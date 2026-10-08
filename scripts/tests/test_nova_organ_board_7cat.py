#!/usr/bin/env python3
"""7-category gap tests for nova_organ_board.py — the CLI connect retry added here and the
2026-10-08 presence change (arrivals read the presence organ's entered_at + confidence).
PG is mocked. Base suite: test_nova_organ_board.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_organ_board_7cat.py
"""
import importlib.util
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_organ_board.py"
spec = importlib.util.spec_from_file_location("organ_board_7cat", SCRIPT)
ob = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ob)
NOW = datetime.now(timezone.utc)


def presence(mins_ago, conf=0.8, state="jordan@office"):
    return {"presence": {"ts": NOW, "state": state,
                         "detail": {"confidence": conf, "entered_at": NOW - timedelta(minutes=mins_ago)}}}


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch("time.sleep"); self.sleep = p.start(); self.addCleanup(p.stop)
        r = redirect_stderr(io.StringIO()); self.err = r.__enter__(); self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_board_query_is_static(self):
        cur = MagicMock(); cur.fetchall.return_value = []
        ob.board(cur)
        self.assertEqual(cur.execute.call_args.args, ("SELECT organ, ts, state, detail FROM organ_board",))


class TestPerformance(_Base):
    def test_connect_timeout_and_bounded_backoff(self):
        with patch.object(ob.psycopg2, "connect", side_effect=OSError("x")) as c, self.assertRaises(OSError):
            ob.main()
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 8)
        self.assertLessEqual(sum(a.args[0] for a in self.sleep.call_args_list), 10)


class TestRetry(_Base):
    def test_connect_recovers(self):
        cur = MagicMock(); cur.fetchall.return_value = []
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(ob.psycopg2, "connect", side_effect=[OSError("blip"), conn]), redirect_stdout(io.StringIO()):
            ob.main()
        self.assertIn("retry 1", self.err.getvalue())
        self.sleep.assert_called_once_with(2)


class TestUnit(_Base):
    def test_arrival_window_and_confidence(self):
        self.assertEqual(ob.someone_just_arrived(presence(3))[0], "jordan")
        self.assertIsNone(ob.someone_just_arrived(presence(30)))
        self.assertIsNone(ob.someone_just_arrived(presence(1, conf=0.1)))


class TestIntegration(_Base):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            ob.selftest()
        self.assertIn("selftest ok", out.getvalue())


class TestFunctional(_Base):
    def test_cli_prints_board_and_arrival(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("presence", NOW, "jordan@office", {"confidence": 0.9, "entered_at": NOW})]
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(ob.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            ob.main()
        self.assertIn("arrival: ('jordan', 0)", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
