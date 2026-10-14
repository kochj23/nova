#!/usr/bin/env python3
"""Tests for nova_weight_of_presence.py — wish #78 "The Weight of Presence", the 'presence' section of
nova_empathy_core.py (the Jordan lens). The 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). DB and memory server are mocked; no real PG or HTTP from this file.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import re
import subprocess
import sys
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_empathy_core as ec  # noqa: E402
import nova_weight_of_presence as wp  # noqa: E402 — the module the lens's presence section imports
_GUARDS = []


def _offline(*a, **k):
    raise RuntimeError("offline test: real PG / HTTP blocked")


def setUpModule():
    for t in (patch.object(psycopg2, "connect", _offline), patch.object(urllib.request, "urlopen", _offline)):
        t.start(); _GUARDS.append(t)


def tearDownModule():
    while _GUARDS:
        _GUARDS.pop().stop()


SRC = (SCRIPTS / "nova_weight_of_presence.py").read_text()
NOW = datetime.now(timezone.utc)


def _t(hours_ago, tid, msg, resp):
    ts = NOW - timedelta(hours=hours_ago)
    # (created_at, date, trace_id, user_message, response, in_empathy, in_quiet, in_elsewhere, in_presence)
    return (ts, ts.date(), tid, msg, resp, True, True, True, True)


TRACES = [_t(100, "a1", "How about your top ten improvement list?", "Something went wrong on my end."),
          _t(99, "a2", "How about your top ten improvement list, then?", "My top ten improvement list: memory audit."),
          _t(50, "b1", "Tell me about your home world, Usul.", "I don't have data on Usul."),
          _t(10, "c1", "All approved", "ok")]


class _World:
    def __init__(self, traces=TRACES, seen=None):
        self.traces, self.seen, self.sql, self.params = traces, seen, [], []
        self._all, self._one = [], None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s); self.params.append(params)
        self._all = self.traces if "trace_id, user_message, response" in s else []
        self._one = self.seen if "FROM service_config" in s else None

    def fetchall(self):
        return self._all

    def fetchone(self):
        return self._one

    def inserts(self):
        return {(p[0], p[1]) for s, p in zip(self.sql, self.params) if s.startswith("INSERT INTO service_config")}


class _Conn:
    autocommit = False

    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


def _run(argv, world, fail_remember=False):
    posted = []

    def rem(text, meta, source=None, **_):
        if fail_remember:
            raise OSError("memory server down")
        posted.append((source, text, meta))
    ps = [patch.object(psycopg2, "connect", lambda *a, **k: _Conn(world)), patch.object(ec, "remember", rem),
          patch.object(ec, "_stamp", dict)]
    for p in ps:
        p.start()
    try:
        with redirect_stdout(io.StringIO()) as buf:
            rc = ec.main(["--section", "presence", *argv])
    finally:
        for p in reversed(ps):
            p.stop()
    return rc, posted, buf.getvalue()


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            wp.demo()

    def test_classify_edges(self):
        self.assertEqual(wp.classify("what is the weather?", ""), "lost")
        self.assertEqual(wp.classify("what is the weather?", None), "lost")
        self.assertEqual(wp.classify("what is the weather?", "Sunny."), "held")          # short answers are not "beside"
        self.assertEqual(wp.classify("zigbee firmware?", "I can't remember that, Little Mister."), "brushed")

    def test_no_moments_no_findings(self):
        self.assertEqual(wp.findings([]), [])
        self.assertIsNone(wp.heaviest([]))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|passwd|secret|token)\s*=\s*['\"]", SRC, re.I))

    def test_logic_module_touches_no_database(self):
        for s in ("execute(", "psycopg2", "INSERT", "UPDATE", "DELETE"):
            self.assertNotIn(s, SRC)

    def test_never_quotes_his_sentence(self):
        rc, posted, _ = _run([], _World())
        self.assertNotIn("top ten improvement list", posted[0][1])
        self.assertNotIn("Usul.", posted[0][1])


class TestPerformance(unittest.TestCase):
    def test_moments_fast_on_a_busy_month(self):
        rows = [(NOW - timedelta(minutes=i), f"t{i}", f"what about sensor {i % 50}?", "the sensor is fine today")
                for i in range(3000)][::-1]
        t = time.time()
        wp.findings(wp.moments(rows, NOW))
        self.assertLess(time.time() - t, 5.0)


class TestRetry(unittest.TestCase):
    def test_no_pg_is_fail_open(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ec.main(["--section", "presence"]), 0)

    def test_no_shared_read_skips_cleanly(self):
        with redirect_stdout(io.StringIO()) as buf:
            ec.section_presence(_World(), NOW.date(), None, type("A", (), {"dry_run": False})())
        self.assertIn("presence section skipped", buf.getvalue())

    def test_memory_server_down_fails_the_section_not_the_pass(self):
        rc, posted, out = _run([], _World(), fail_remember=True)
        self.assertEqual(rc, 1)
        self.assertIn("section presence failed", out)


class TestIntegration(unittest.TestCase):
    def test_section_writes_its_own_source_and_keys(self):
        w = _World()
        rc, posted, _ = _run([], w)
        self.assertEqual((rc, [p[0] for p in posted]), (0, ["weight_of_presence"]))
        self.assertEqual(w.inserts(), {("nova_weight_of_presence", "high_water"), ("nova_weight_of_presence", "latest")})
        self.assertEqual(posted[0][2]["organ"], "nova_weight_of_presence")

    def test_unchanged_presence_is_not_restated(self):
        rc, posted, _ = _run([], _World())
        sig = posted[0][2]["sig"]
        rc, posted2, out = _run([], _World(seen=({"seen": {sig: NOW.date().isoformat()}},)))
        self.assertEqual(posted2, [])
        self.assertIn("no moment newly missed", out)


class TestFunctional(unittest.TestCase):
    def test_lost_then_reasked_is_the_heaviest(self):
        rc, posted, _ = _run([], _World())
        meta, text = posted[0][2], posted[0][1]
        self.assertEqual(meta["findings"], ["moments", "lost", "brushed", "reasked", "heaviest"])
        self.assertIn("gateway_traces#a1->#a2", meta["cites"])
        self.assertIn("he had to ask twice", text)
        self.assertIn("was there for 1 of them", text)
        for line in [ln for ln in text.splitlines() if re.match(r"\s+\d+\.", ln)]:
            self.assertRegex(line, r"\[[^\]]+\]$")


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_weight_of_presence.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dry_run_writes_nothing(self):
        w = _World()
        rc, posted, out = _run(["--dry-run"], w)
        self.assertEqual((rc, posted, w.inserts()), (0, [], set()))
        self.assertIn("Weight of presence,", out)

    def test_docstring_and_registration(self):
        for s in ("wish #78", "Written by Jordan Koch (via Claude).", "--dry-run", "--selftest", "weight_of_presence"):
            self.assertIn(s, wp.__doc__)
        self.assertIn("presence", ec.SECTIONS)
        self.assertIn("presence", ec.NEEDS_TRACES)


if __name__ == "__main__":
    unittest.main()
