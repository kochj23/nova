#!/usr/bin/env python3
"""Tests for nova_directive_decide.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_directive_decide.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("ndecide", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dd = _load()
ROW = (7, "live", "Always ask first", "fb:a", "Never ask", "fb:b", "deploys")


class _Cur:
    def __init__(self, answers=(), rowcount=1):
        self.answers = list(answers); self.sql = []; self.params = []; self.rowcount = rowcount

    def execute(self, sql, params=None): self.sql.append(sql); self.params.append(params)
    def fetchone(self): return self.answers.pop(0)
    def fetchall(self): return self.answers.pop(0)


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None): return datetime(2026, 1, 1, 10, 0)


class _NightDT(datetime):
    @classmethod
    def now(cls, tz=None): return datetime(2026, 1, 1, 3, 0)


def _slack_stub(resp):
    m = types.ModuleType("nova_slack_answers"); m.slack = MagicMock(return_value=resp)
    return m


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_decide_is_parameterized_and_only_closes_open_rows(self):
        cur = _Cur()
        dd.decide(cur, 5, "x'); DROP TABLE directive_conflicts;--", "jordan")
        self.assertIn("AND status='open'", cur.sql[0])
        self.assertEqual(cur.params[0], ("jordan", "x'); DROP TABLE directive_conflicts;--", 5))
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')

    def test_dsn_overridable_from_env(self):
        self.assertIn('os.environ.get("NOVA_OPS_DSN"', SRC)


class TestPerformance(unittest.TestCase):
    def test_prompt_text_10k_rows(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            dd.prompt_text((i, "latent", "a " * 300, "s", "b " * 300, "s", "w " * 200))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_slack_failure_records_nothing(self):
        # RETRY GAP: post_pending/slack chat.postMessage — one attempt; a not-ok reply skips the
        # slack_prompts insert so tomorrow's run re-offers the same conflict.
        cur = _Cur([(0,), [ROW]])
        with patch.dict(sys.modules, {"nova_slack_answers": _slack_stub({"ok": False})}), \
             patch.object(dd, "datetime", _FixedDT), redirect_stdout(io.StringIO()):
            self.assertEqual(dd.post_pending(cur, dry=False), 0)
        self.assertFalse(any("INSERT" in s for s in cur.sql))

    def test_pg_down_raises_no_partial_state(self):
        with patch.object(dd.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c, \
             patch.object(sys, "argv", ["x", "--list"]):
            with self.assertRaises(psycopg2.OperationalError):
                dd.main()
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 8)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as b:
            dd.selftest()
        self.assertIn("selftest ok", b.getvalue())

    def test_short_edges(self):
        self.assertEqual(dd.short(""), "")
        self.assertEqual(len(dd.short("x" * 500, 10)), 10)
        self.assertTrue(dd.short("x" * 500, 10).endswith("…"))

    def test_decide_reports_not_open(self):
        self.assertFalse(dd.decide(_Cur(rowcount=0), 1, "n", "claude"))

    def test_daily_cap_and_quiet_hours(self):
        self.assertEqual(dd.post_pending(_Cur([(dd.POST_PER_DAY,)]), dry=True), 0)
        with patch.object(dd, "datetime", _NightDT):
            cur = _Cur()
            self.assertEqual(dd.post_pending(cur, dry=False), 0)
        self.assertEqual(cur.sql, [])


class TestIntegration(unittest.TestCase):
    def test_uses_shared_slack_helper_and_records_prompt(self):
        stub = _slack_stub({"ok": True, "ts": "111.2"})
        cur = _Cur([(0,), [ROW]])
        with patch.dict(sys.modules, {"nova_slack_answers": stub}), patch.object(dd, "datetime", _FixedDT), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(dd.post_pending(cur, dry=False), 1)
        self.assertEqual(stub.slack.call_args.kwargs["channel"], dd.CHANNEL)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO slack_prompts" in s]
        self.assertEqual(ins, [("7", dd.CHANNEL, "111.2")])
        self.assertEqual(cur.params[1], (dd.POST_PER_DAY,))       # LIMIT = remaining room


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur):
        conn = MagicMock(); conn.cursor.return_value = cur
        buf = io.StringIO()
        with patch.object(dd.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x"] + argv), \
             redirect_stdout(buf):
            dd.main()
        return buf.getvalue()

    def test_decide_cli(self):
        cur = _Cur()
        out = self._main(["--decide", "12", "--note", "both apply", "--by", "jordan"], cur)
        self.assertEqual(cur.params[0], ("jordan", "both apply", 12))
        self.assertIn("#12 decided", out)

    def test_post_dry_run_prints_without_slack(self):
        cur = _Cur([(0,), [ROW]])
        with patch.dict(sys.modules, {"nova_slack_answers": _slack_stub({"ok": True, "ts": "1"})}) :
            out = self._main(["--post", "--dry-run"], cur)
            self.assertFalse(sys.modules["nova_slack_answers"].slack.called)
        self.assertIn("Directive conflict #7 (live)", out)
        self.assertIn("posted 1", out)

    def test_list_prints_rows(self):
        out = self._main(["--list"], _Cur([[(1, "live", "high", "a", "b")]]))
        self.assertIn("1 | live | high | a | b", out)


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
