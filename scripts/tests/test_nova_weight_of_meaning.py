#!/usr/bin/env python3
"""Tests for nova_weight_of_meaning.py — wish #77 "Weight of Meaning". The 7 house categories
(Security, Performance, Retry, Unit, Integration, Functional, Frame). Both databases and the memory
server are mocked; no real PG or HTTP is reachable from this file.
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
from datetime import date
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


wm = _load("wm", SCRIPTS / "nova_weight_of_meaning.py")
SRC = (SCRIPTS / "nova_weight_of_meaning.py").read_text()
TODAY = date(2026, 10, 13)
d = date
CALLED = [("m1", "nova_articles", 545, d(2026, 9, 11), "Twenty-Nine Uncommitted Sins"),
          ("m2", "conversation", 512, d(2026, 10, 8), "Jordan: Or Dylan's room? Nova: ops_query")]
OWN = [("conversation", 377, 234, None), ("quiet_sensor", 7, 0, "q1")]
ALL = [("scanner", 60552, 850), ("philosophy", 57979, 1330), ("conversation", 362, 200)]


class _Mem:
    """nova_memories, read-only."""

    def __init__(self, fail=()):
        self.fail, self.sql, self._all = set(fail), [], []
        self.connection = self

    def rollback(self):
        pass

    def set_session(self, **k):
        self.session = k

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s)
        for f in self.fail:
            if f in s:
                raise RuntimeError("canceling statement due to statement timeout")
        self._all = (CALLED if "access_count > 0 ORDER BY" in s else
                     OWN if "array_agg" in s else ALL if "GROUP BY source" in s else [])

    def fetchall(self):
        return self._all


class _Ops:
    autocommit = False

    def __init__(self, seen=None):
        self.sql, self.params, self.seen, self._one = [], [], seen, None

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s); self.params.append(params)
        self._one = self.seen if "FROM service_config" in s else None

    def fetchone(self):
        return self._one

    def inserts(self):
        return {(p[0], p[1]) for s, p in zip(self.sql, self.params) if s.startswith("INSERT INTO service_config")}


def _run(argv, mem, ops, fail_remember=False):
    posted = []

    def rem(text, meta, source=None, **_):
        if fail_remember:
            raise OSError("memory server down")
        posted.append((source, text, meta))

    def connect(dsn, **k):
        return mem if "nova_memories" in dsn else ops
    ps = [patch.object(psycopg2, "connect", connect), patch.object(wm.ec, "remember", rem),
          patch.object(wm.ec, "_stamp", dict)]
    for p in ps:
        p.start()
    try:
        with redirect_stdout(io.StringIO()) as buf:
            rc = wm.main(argv)
    finally:
        for p in reversed(ps):
            p.stop()
    return rc, posted, buf.getvalue()


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            wm.demo()

    def test_uncalled_needs_rows(self):
        self.assertEqual(wm.uncalled([("dream", 0, 0, None)]), [])

    def test_own_voice_is_never_the_shadow(self):
        sh = wm.shadow([("conversation", 10**6, 0), ("x", 5, 0)], [("conversation", 10, 5, None)])
        self.assertEqual([x["source"] for x in sh["loud"]], ["x"])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|passwd|secret|token)\s*=\s*['\"]", SRC, re.I))

    def test_memories_read_only(self):
        code = SRC.replace("DO UPDATE SET", "")
        for verb in ("UPDATE ", "DELETE ", "TRUNCATE", "DROP ", "ALTER "):
            self.assertFalse(verb in code, verb)
        mem = _Mem()
        _run([], mem, _Ops())
        self.assertEqual(mem.session, {"readonly": True, "autocommit": True})
        self.assertTrue(all(s.startswith(("SELECT", "SET statement_timeout")) for s in mem.sql))

    def test_his_words_never_excerpted(self):
        rc, posted, _ = _run([], _Mem(), _Ops())
        self.assertNotIn("Dylan", posted[0][1])
        self.assertIn("a conversation with him", posted[0][1])

    def test_queries_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f\"")


class TestPerformance(unittest.TestCase):
    def test_pure_logic_fast_on_many_sources(self):
        rows = [(f"s{i}", 1000 + i, i, f"id{i}") for i in range(5000)]
        allr = [(f"s{i}", 1000 + i, i) for i in range(5000)]
        t = time.time()
        c = wm.called([(f"m{i}", "dream", i, TODAY, "x" * 400) for i in range(10000)])
        wm.meaning_text(wm.findings(c, wm.uncalled(rows), wm.shadow(allr, rows)), TODAY)
        self.assertLess(time.time() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_no_memories_db_is_fail_open(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wm.main([]), 0)

    def test_a_timed_out_read_hides_nothing_else(self):
        mem = _Mem(fail={"array_agg"})
        c, o, a = wm.gather(mem)
        self.assertEqual((len(c), o, len(a)), (2, [], 3))

    def test_remember_retries(self):
        calls = []

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{}'

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) < 2:
                raise OSError("blip")
            return _R()
        with patch.object(urllib.request, "urlopen", flaky):
            wm.ec.remember("t", {}, _sleep=lambda s: None, source=wm.SOURCE)
        self.assertEqual(len(calls), 2)


class TestIntegration(unittest.TestCase):
    def test_full_run_writes_memory_and_state(self):
        ops = _Ops()
        rc, posted, _ = _run([], _Mem(), ops)
        self.assertEqual((rc, [p[0] for p in posted]), (0, ["weight_of_meaning"]))
        self.assertEqual(ops.inserts(), {("nova_weight_of_meaning", "high_water"), ("nova_weight_of_meaning", "latest")})
        self.assertEqual(posted[0][2]["findings"], ["called", "uncalled", "shadow"])

    def test_unchanged_meaning_is_not_restated(self):
        rc, posted, _ = _run([], _Mem(), _Ops())
        sig = posted[0][2]["sig"]
        rc, posted2, out = _run([], _Mem(), _Ops(seen=({"seen": {sig: "2099-01-01"}},)))
        self.assertEqual(posted2, [])
        self.assertIn("meaning unchanged", out)

    def test_memory_server_down_raises_for_the_scheduler(self):
        # merged 2026-10-15: run() still raises; via memory_anchor's 'meaning' section the scheduler sees exit 1
        rc, posted, _ = _run([], _Mem(), _Ops(), fail_remember=True)
        self.assertEqual((rc, posted), (1, []))
        ps = [patch.object(psycopg2, "connect", lambda dsn, **k: _Mem() if "nova_memories" in dsn else _Ops()),
              patch.object(wm.ec, "remember", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))),
              patch.object(wm.ec, "_stamp", dict)]
        for p in ps:
            p.start()
        try:
            with self.assertRaises(OSError), redirect_stdout(io.StringIO()):
                wm.run(False)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_wrapper_delegates_to_memory_anchor_meaning(self):
        import nova_memory_anchor as anchor
        with patch.object(anchor, "main", return_value=0) as m, redirect_stdout(io.StringIO()):
            self.assertEqual(wm.main(["--dry-run"]), 0)
        m.assert_called_once_with(["--meaning", "--dry-run"])


class TestFunctional(unittest.TestCase):
    def test_every_line_cited(self):
        rc, posted, _ = _run([], _Mem(), _Ops())
        text = posted[0][1]
        for line in [ln for ln in text.splitlines() if re.match(r"\s+\d+\.", ln)]:
            self.assertIn("I notice", line)
            self.assertRegex(line, r"\[[^\]]+\]$")
        self.assertIn("memories#m1", text)
        self.assertIn("recalled=0)->#q1", text)
        self.assertIn("not only his", text)                                         # honest about the gauge


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_weight_of_meaning.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dry_run_writes_nothing(self):
        ops = _Ops()
        rc, posted, out = _run(["--dry-run"], _Mem(), ops)
        self.assertEqual((rc, posted, ops.sql), (0, [], []))
        self.assertIn("Weight of meaning,", out)

    def test_docstring(self):
        for s in ("wish #77", "Written by Jordan Koch (via Claude).", "--dry-run", "--selftest", "weight_of_meaning"):
            self.assertIn(s, wm.__doc__)


if __name__ == "__main__":
    unittest.main()
