#!/usr/bin/env python3
"""Tests for nova_review_custody.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_review_custody.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    rc = _load("review_custody_t", SCRIPT)
SRC = SCRIPT.read_text()
POSTS = []
rc.nova_config = types.SimpleNamespace(post_both=lambda m, **k: POSTS.append((m, k)) or True, SLACK_CHAN="C-CHAT")
rc.HAVE_CONFIG = True
rc.psycopg2 = MagicMock()
rc.psycopg2.connect.side_effect = RuntimeError("psycopg2.connect not mocked in test")


class _Cur:
    """Answers fetchone() by the last SQL; records statements."""
    def __init__(self, answers=None, fail=()):
        self.answers = answers or {}; self.fail = fail; self.stmts = []; self._last = ""

    def execute(self, sql, params=None):
        self.stmts.append((sql, params)); self._last = sql
        if any(f in sql for f in self.fail):
            raise RuntimeError("relation does not exist")

    def fetchone(self):
        for k, v in self.answers.items():
            if k in self._last:
                return v
        return None


def _at(offset_days):
    class _D(date):
        @classmethod
        def today(cls):
            return rc.REVIEW_DATE + timedelta(days=offset_days)
    return patch.object(rc, "date", _D)


GOOD_OPS = {"quiet_wake_rate": (0.4, None), "restraint_ledger": (3,)}
GOOD_MEM = {"ILIKE": (2,), "gravel": (5,)}


def _main(argv=(), offset=0, ops=None, mem=None):
    oc = _Cur(GOOD_OPS if ops is None else ops); mc = _Cur(GOOD_MEM if mem is None else mem)
    conns = [MagicMock(cursor=MagicMock(return_value=oc)), MagicMock(cursor=MagicMock(return_value=mc))]
    POSTS.clear()
    with _at(offset), patch.object(rc.psycopg2, "connect", side_effect=conns), patch.object(sys, "argv", ["x", *argv]), \
            redirect_stdout(io.StringIO()) as out:
        code = rc.main()
    return code, oc, out.getvalue()


def _logged(oc):
    return [p for s, p in oc.stmts if s.startswith("INSERT INTO review_custody_log")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_only_write_is_custody_log(self):
        writes = re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)
        self.assertEqual(writes, [("INSERT INTO", "review_custody_log")])


class TestPerformance(unittest.TestCase):
    def test_witness_message_10k(self):
        f = {"review_arrived": True, "review_evidence": ["e"] * 5, "contains_honest_nothings": False,
             "nothing_signals": ["s"] * 5}
        t0 = time.perf_counter()
        for _ in range(10_000):
            rc._witness_message(f, False)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_probe_failures_fail_open(self):
        # RETRY GAP: _look_for_review — every probe is one-shot; a failing table is noted, never raised
        mc = _Cur(fail=("memories",)); oc = _Cur(fail=("turing_scoreboard", "restraint_ledger"))
        f = rc._look_for_review(mc, oc)
        self.assertFalse(f["review_arrived"] or f["contains_honest_nothings"])
        self.assertIn("memory probe failed", f["review_evidence"][0])

    def test_slack_failure_still_records(self):
        # RETRY GAP: post_both — one attempt; failure is logged and the PG record still lands (posted=False)
        with patch.object(rc.nova_config, "post_both", side_effect=RuntimeError("slack down")):
            code, oc, _ = _main(offset=1)
        self.assertEqual(code, 0)
        self.assertFalse(_logged(oc)[0][3])


class TestUnit(unittest.TestCase):
    def test_witness_message_branches(self):
        base = {"review_evidence": [], "nothing_signals": []}
        m = rc._witness_message({**base, "review_arrived": False, "contains_honest_nothings": False}, True)
        self.assertIn("did not arrive", m); self.assertIn("rehearsal", m)
        self.assertIn("no null/fizzle/restraint signals", m)
        m = rc._witness_message({**base, "review_arrived": True, "contains_honest_nothings": False}, False)
        self.assertIn("highlight reel", m)
        m = rc._witness_message({**base, "review_arrived": True, "contains_honest_nothings": True}, False)
        self.assertIn("keeps its own nothings", m)

    def test_honest_nothings_needs_two_signals(self):
        f = rc._look_for_review(_Cur({"gravel": (1,)}), _Cur())
        self.assertFalse(f["contains_honest_nothings"])
        f = rc._look_for_review(_Cur(GOOD_MEM), _Cur(GOOD_OPS))
        self.assertTrue(f["review_arrived"] and f["contains_honest_nothings"])
        self.assertEqual(len(f["nothing_signals"]), 3)


class TestIntegration(unittest.TestCase):
    def test_reads_raw_record_not_reflection_self_report(self):
        mc, oc = _Cur(GOOD_MEM), _Cur(GOOD_OPS)
        rc._look_for_review(mc, oc)
        self.assertEqual(mc.stmts[0][1], (rc.REVIEW_DATE,))
        self.assertIn("FROM memories", mc.stmts[0][0])
        self.assertTrue(any("turing_scoreboard" in s for s, _ in oc.stmts))

    def test_already_witnessed_query(self):
        self.assertTrue(rc._already_witnessed(_Cur({"review_custody_log": (1,)})))
        self.assertFalse(rc._already_witnessed(_Cur()))


class TestFunctional(unittest.TestCase):
    def test_on_review_day_posts_and_records(self):
        code, oc, out = _main(offset=0)
        self.assertEqual(code, 0)
        self.assertEqual(POSTS[0][1], {"slack_channel": "C-CHAT"})
        row = _logged(oc)[0]
        self.assertEqual(row[:4], (rc.REVIEW_DATE, True, True, True))
        self.assertEqual(json.loads(row[4])["mode"], "live")
        self.assertIn("----- WITNESS -----", out)

    def test_gates_dormant_window_witnessed_and_dry_run(self):
        for offset in (-1, rc.WINDOW_DAYS + 1):
            code, oc, _ = _main(offset=offset)
            self.assertEqual((code, POSTS, _logged(oc)), (0, [], []))
        code, oc, _ = _main(offset=2, ops={**GOOD_OPS, "review_custody_log": (1,)})
        self.assertEqual((POSTS, _logged(oc)), ([], []))
        code, oc, _ = _main(["--dry-run"], offset=-30)           # rehearsal: no post, logged as dry
        self.assertEqual(POSTS, [])
        self.assertEqual(json.loads(_logged(oc)[0][4])["mode"], "dry")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_review_custody.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
