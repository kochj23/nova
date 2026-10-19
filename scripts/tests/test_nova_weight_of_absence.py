#!/usr/bin/env python3
"""Tests for nova_weight_of_absence.py — wish #80 "the weight of absence". The 7 house categories
(Security, Performance, Retry, Unit, Integration, Functional, Frame). DB and memory server are mocked;
no real PG or HTTP is reachable from this file.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
_GUARDS = []


def _offline(*a, **k):
    raise RuntimeError("offline test: real PG / HTTP blocked")


def setUpModule():
    for t in (patch.object(psycopg2, "connect", _offline), patch.object(urllib.request, "urlopen", _offline)):
        t.start(); _GUARDS.append(t)


def tearDownModule():
    while _GUARDS:
        _GUARDS.pop().stop()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wa = _load("wa", SCRIPTS / "nova_weight_of_absence.py")
SRC = (SCRIPTS / "nova_weight_of_absence.py").read_text()
TODAY = date(2026, 10, 13)
d = date


class _World:
    """Answers each read by what it asks for; records every statement."""

    def __init__(self, fail=None, seen=None):
        self.fail, self.sql, self.params, self.seen = fail or set(), [], [], seen
        self._all, self._one = [], None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s); self.params.append(params)
        for f in self.fail:
            if f in s:
                raise RuntimeError(f"relation {f} missing")
        rows, one = [], None
        if "FROM herd_correspondents" in s:
            rows = [("O.C.", d(2026, 10, 8), 8), ("Marey", d(2026, 9, 15), 6), ("Sam", None, 6)]
        elif "FROM analytics_pageviews" in s:
            rows = [("/research/2026-09-21-read-one/",)]
        elif "FROM affect_state" in s:
            rows = [(d(2026, 10, 1), 10, 12), (d(2026, 10, 2), 13, 15), (d(2026, 10, 5), 16, 18)]
        elif "FROM telemetry.events" in s:
            rows = [("nova_local_situation.py", 19, d(2026, 10, 8), 263070)]
        elif "FROM service_config" in s:
            one = self.seen
        self._all, self._one = rows, one

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


def _pages(tmp):
    root = Path(tmp)
    (root / "research").mkdir(parents=True)
    (root / "digests").mkdir()
    for name, draft in (("2026-09-20-on-gaps.md", False), ("2026-09-21-read-one.md", False),
                        ("2026-09-22-a-draft.md", True)):
        (root / "research" / name).write_text(f"---\ntitle: x\ndraft: {'true' if draft else 'false'}\n---\nbody\n")
    (root / "digests" / "2026-09-20-digest.md").write_text("---\n---\n")
    return root


def _run(argv, world, pages_root, fail_remember=False):
    posted = []

    def rem(text, meta, source=None, **_):
        if fail_remember:
            raise OSError("memory server down")
        posted.append((source, text, meta))
    real_gather = wa.gather
    ps = [patch.object(psycopg2, "connect", lambda *a, **k: _Conn(world)), patch.object(wa.ec, "remember", rem),
          patch.object(wa.ec, "_stamp", dict),
          patch.object(wa, "gather", lambda cur, today: real_gather(cur, TODAY, pages_root))]
    for p in ps:
        p.start()
    try:
        with redirect_stdout(io.StringIO()) as buf:
            rc = wa.main(argv)
    finally:
        for p in reversed(ps):
            p.stop()
    return rc, posted, buf.getvalue()


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            wa.demo()

    def test_silent_relative_to_the_herd(self):
        q, n = wa.silent_names([("a", d(2026, 10, 1), 0), ("b", d(2026, 9, 20), 2)], TODAY)
        self.assertEqual(([x["name"] for x in q], n), ([], []))                    # 11d lag < 14: not silent

    def test_gap_runs_cite_neighbours(self):
        g = wa.record_gaps([(d(2026, 10, 1), 1, 2), (d(2026, 10, 4), 7, 9)], TODAY)
        self.assertEqual([(r["days"], r["before"], r["after"]) for r in g], [(2, 2, 7)])

    def test_unread_pages_skip_drafts_and_other_sections(self):
        u, n = wa.unread_pages([("research", "2026-09-20-x.md", True), ("digests", "2026-09-20-y.md", False)], set(), TODAY)
        self.assertEqual((u, n), ([], 0))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|passwd|secret|token)\s*=\s*['\"]", SRC, re.I))

    def test_read_only_over_the_world(self):
        code = SRC.replace("DO UPDATE SET", "")                                      # the upsert of its own row
        for verb in ("UPDATE ", "DELETE ", "TRUNCATE", "DROP ", "ALTER "):
            self.assertFalse(verb in code, verb)
        self.assertEqual(SRC.count("INSERT INTO"), 1)                                  # its own service_config row
        self.assertIn("INSERT INTO service_config", SRC)

    def test_table_identifiers_only_from_the_constant(self):
        w = _World()
        wa.gather(w, TODAY, Path("/nonexistent"))
        tables = {t for t, _ in wa.DIARIES}
        for s in w.sql:
            m = re.search(r"FROM (\w+) WHERE \w+ > now\(\)", s)
            if m and "::date, min(id)" in s:
                self.assertIn(m.group(1), tables)

    def test_never_posts_or_prints_addresses(self):
        self.assertNotIn("chat.postMessage", SRC)
        self.assertNotIn("email", wa.gather.__code__.co_consts.__str__())


class TestPerformance(unittest.TestCase):
    def test_pure_logic_fast_at_scale(self):
        pages = [("research", f"{d(2026, 8, 20) + timedelta(days=i % 50)}-p{i}.md", False) for i in range(20000)]
        rows = [(d(2026, 9, 14) + timedelta(days=i), i, i) for i in range(29) if i % 7]
        t = time.time()
        u, n = wa.unread_pages(pages, set(), TODAY)
        wa.record_gaps(rows, TODAY)
        wa.absence_text(wa.findings(([], []), (u, n), [], [], TODAY), TODAY)
        self.assertLess(time.time() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_remember_retries_via_the_shared_helper(self):
        calls = []

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"id":"x"}'

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("blip")
            return _R()
        with patch.object(urllib.request, "urlopen", flaky):
            wa.ec.remember("t", {}, _sleep=lambda s: None, source=wa.SOURCE)
        self.assertEqual(len(calls), 3)

    def test_no_pg_is_fail_open(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wa.main([]), 0)                                           # connect blocked -> 0

    def test_one_source_failing_hides_nothing_else(self):
        w = _World(fail={"herd_correspondents", "analytics_pageviews"})
        names, pages, gaps, senses = wa.gather(w, TODAY, Path("/nonexistent"))
        self.assertEqual((names, pages), (([], []), ([], 0)))
        self.assertTrue(gaps and senses)


class TestIntegration(unittest.TestCase):
    def test_full_run_writes_memory_and_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            w = _World()
            rc, posted, _ = _run([], w, _pages(tmp))
        self.assertEqual(rc, 0)
        self.assertEqual([p[0] for p in posted], ["weight_of_absence"])
        self.assertEqual(w.inserts(), {("nova_weight_of_absence", "high_water"), ("nova_weight_of_absence", "latest")})
        meta = posted[0][2]
        self.assertEqual(meta["findings"], ["silent_names", "never_spoke", "unread_pages", "record_gap", "quiet_senses"])
        self.assertIn("page:/research/2026-09-20-on-gaps/", meta["cites"])

    def test_unchanged_picture_is_not_restated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = _pages(tmp)
            rc, posted, _ = _run([], _World(), root)
            sig = posted[0][2]["sig"]
            rc, posted2, out = _run([], _World(seen=({"seen": {sig: TODAY.isoformat()}},)), root)
        self.assertEqual(posted2, [])
        self.assertIn("nothing newly absent", out)

    def test_memory_server_down_raises_for_the_scheduler(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                _run([], _World(), _pages(tmp), fail_remember=True)


class TestFunctional(unittest.TestCase):
    def test_every_line_is_cited_and_tentative(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, posted, _ = _run([], _World(), _pages(tmp))
        text = posted[0][1]
        for line in [ln for ln in text.splitlines() if re.match(r"\s+\d+\.", ln)]:
            self.assertIn("I notice", line)
            self.assertRegex(line, r"\[[^\]]+\]$")
        self.assertIn(wa.CLOSE, text)
        self.assertNotRegex(text, r"(diagnos|clearly|definitely|must)")

    def test_draft_and_read_pages_are_not_silent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, posted, _ = _run([], _World(), _pages(tmp))
        cites = posted[0][2]["cites"]
        self.assertNotIn("page:/research/2026-09-21-read-one/", cites)
        self.assertNotIn("page:/research/2026-09-22-a-draft/", cites)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_weight_of_absence.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("assertions passed", r.stdout)

    def test_dry_run_writes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            w = _World()
            rc, posted, out = _run(["--dry-run"], w, _pages(tmp))
        self.assertEqual((rc, posted, w.inserts()), (0, [], set()))
        self.assertIn("Weight of absence,", out)

    def test_docstring_names_the_wish_author_and_modes(self):
        doc = wa.__doc__
        for s in ("wish #80", "Written by Jordan Koch (via Claude).", "--dry-run", "--selftest", "weight_of_absence"):
            self.assertIn(s, doc)


if __name__ == "__main__":
    unittest.main()
