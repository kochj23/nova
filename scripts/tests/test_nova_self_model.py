#!/usr/bin/env python3
"""Tests for nova_self_model.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_self_model.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load("self_model_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    def __init__(self, routes=None, one=None, fail_on=None):
        self.routes, self.one, self.fail_on = routes or {}, one or {}, fail_on
        self.sql, self.params, self._last, self._p = [], [], "", None

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        self.sql.append(sql); self.params.append(params); self._last, self._p = sql, params

    def _route(self, table, default):
        for k, v in table.items():
            if k in self._last:
                return v(self._p) if callable(v) else v
        return default

    def fetchall(self):
        return self._route(self.routes, [])

    def fetchone(self):
        return self._route(self.one, None)

    def writes(self, needle):
        return [p for s, p in zip(self.sql, self.params) if needle in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


RAW = ("## WORLDVIEW\nEvidence beats performance; most alerts are noise.\n"
       "## HOW I'VE CHANGED\nI thought Marey eradicated recurrence; she documents it faster.\n"
       "## WHAT I'M PREOCCUPIED WITH\nEscapements, and where alerts are born.\n"
       "## MY TASTE\nTerse dashboards. Thin, admittedly.\n"
       "## WHAT I'M BECOMING\nA system that measures itself before it describes itself.\n")
ACTIVE = [("alerts", "most alerts are noise", 0.8)]
ARCS = [("marey", "eradicates recurrence", "documents it faster", 2, date(2026, 9, 10), date(2026, 9, 15))]
PREOCCS = [("escapements", "interest", "coaxial", 4)]
TASTE = [("terse dashboards", "ops", "like", 0.8)]


def _ops_cur(active=ACTIVE, preoccs=PREOCCS):
    return _Cur(routes={"FROM beliefs\n                  WHERE active": active, "GROUP BY topic": ARCS,
                        "FROM preoccupations": preoccs, "FROM taste": TASTE},
                one={"INSERT INTO self_model": (3, datetime(2026, 10, 5, 4, 10))})


def _mem_cur(fail_on=None):
    return _Cur(routes={"FROM memories": lambda p: [(f"{p[0]} memory\nline",)] if p[0] != "research" else []}, fail_on=fail_on)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_interpolates_only_int_constants_source_is_bound(self):
        self.assertNotIn('execute(f"', SRC)
        for m in re.finditer(r'"""\s*%\s*\(?("%s",\s*)?(\w+)(?:,\s*(\w+))?\)?', SRC):
            for name in (m.group(2), m.group(3)):
                self.assertIn(name, (None, "WINDOW_DAYS", "n"))
        mc = _mem_cur()
        sm.gather_memory_texture(mc)
        self.assertEqual(mc.params[0], ("episodic",)); self.assertNotIn("episodic", mc.sql[0])
        self.assertIsInstance(sm.WINDOW_DAYS, int)

    def test_only_write_is_its_own_versioned_row(self):
        tables = set(re.findall(r"(?:INSERT INTO|UPDATE)\s+(\w+)", SRC))
        self.assertEqual(tables, {"self_model"})


class TestPerformance(unittest.TestCase):
    def test_parse_and_prompt_fast_on_10k(self):
        big = "\n".join(f"filler line {i}" for i in range(10_000))
        raw = RAW.replace("## WORLDVIEW\n", "## WORLDVIEW\n" + big + "\n")
        active = [(f"t{i}", "s", 0.5) for i in range(10_000)]
        t0 = time.perf_counter()
        sec = sm.parse_sections(raw); p = sm.build_prompt(active, [], [], [], {})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(sec["worldview"].endswith("most alerts are noise.")); self.assertIn("t9999", p)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(sm.llm("x"), "ok")
        self.assertEqual(len(calls), 3); self.assertTrue(calls[2].startswith(sm.OLLAMA_NODES[2]))

    def test_remember_no_retry_but_main_survives(self):
        # RETRY GAP: remember — one POST; main() wraps it so the row is still saved.
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                sm.remember("t", "s", {})
        oc, mc = _ops_cur(), _mem_cur()
        with mock.patch("psycopg2.connect", side_effect=[_Conn(oc), _Conn(mc)]), mock.patch.object(sm, "llm", return_value=RAW), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")), redirect_stdout(StringIO()):
            self.assertEqual(sm.main(), 0)
        self.assertEqual(len(oc.writes("INSERT INTO self_model")), 1)

    def test_accessor_fails_open(self):
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(sm.current_self_model(), "")


class TestUnit(unittest.TestCase):
    def test_parse_sections(self):
        sec = sm.parse_sections(RAW)
        self.assertEqual(sec["becoming"], "A system that measures itself before it describes itself.")
        self.assertIn("Marey", sec["changed"])
        curly = RAW.replace("HOW I'VE CHANGED", "HOW I’VE CHANGED").replace("## ", "### ")
        self.assertIn("Marey", sm.parse_sections(curly)["changed"])
        self.assertEqual(sm.parse_sections("no headers")["taste"], "")

    def test_build_prompt_blocks(self):
        p = sm.build_prompt([], [], [], [], {})
        for s in ("(ledger empty)", "(no beliefs revised in the window)", "(none active)", "(no taste recorded yet)", "(thin)"):
            self.assertIn(s, p)
        p = sm.build_prompt(ACTIVE, ARCS, PREOCCS, TASTE, {"episodic": ["x"]})
        self.assertIn("turned over 2x (2026-09-10->2026-09-15)", p); self.assertIn("(valence +0.80)", p)
        self.assertIn("[episodic]\n  · x", p)

    def test_accessor_trims_on_a_line_boundary(self):
        row = _Cur(one={"FROM self_model": ("line one\nline two\nline three",)})
        with mock.patch("psycopg2.connect", return_value=_Conn(row)):
            self.assertEqual(sm.current_self_model(max_chars=14), "line one")
            self.assertEqual(sm.current_self_model(), "line one\nline two\nline three")
        with mock.patch("psycopg2.connect", return_value=_Conn(_Cur())):
            self.assertEqual(sm.current_self_model(), "")


class TestIntegration(unittest.TestCase):
    def test_gatherers_shape_and_skip_a_failing_source(self):
        oc = _ops_cur()
        active, arcs = sm.gather_beliefs(oc)
        self.assertEqual((active, arcs), (ACTIVE, ARCS))
        self.assertEqual(sm.gather_preoccupations(oc), PREOCCS); self.assertEqual(sm.gather_taste(oc), TASTE)
        with redirect_stdout(StringIO()):
            tex = sm.gather_memory_texture(_mem_cur())
        self.assertEqual(set(tex), {"episodic", "association", "unclaimed", "private_notebook", "nova_articles"})
        self.assertEqual(tex["episodic"], ["episodic memory line"])

    def test_gather_to_prompt_to_parse_roundtrip(self):
        oc, mc = _ops_cur(), _mem_cur()
        active, arcs = sm.gather_beliefs(oc)
        p = sm.build_prompt(active, arcs, sm.gather_preoccupations(oc), sm.gather_taste(oc), sm.gather_memory_texture(mc))
        self.assertIn("most alerts are noise", p); self.assertIn("escapements [interest] (returned to 4x)", p)
        self.assertEqual(set(sm.parse_sections(RAW)), {k for _, k in sm._SECTIONS})


class TestFunctional(unittest.TestCase):
    def _run(self, oc, mc, raw=RAW):
        posts = []

        def fake(req, timeout=None):
            posts.append(json.loads(req.data.decode())); return _Resp({"id": "m1"})
        with mock.patch("psycopg2.connect", side_effect=[_Conn(oc), _Conn(mc)]), mock.patch.object(sm, "llm", return_value=raw), \
             mock.patch("urllib.request.urlopen", side_effect=fake), redirect_stdout(StringIO()) as out:
            code = sm.main()
        return code, posts, out.getvalue()

    def test_golden_path_writes_row_and_memory(self):
        oc, mc = _ops_cur(), _mem_cur()
        code, posts, out = self._run(oc, mc)
        self.assertEqual(code, 0)
        ins = oc.writes("INSERT INTO self_model")[0]
        self.assertEqual(ins[4], "A system that measures itself before it describes itself."); self.assertEqual(ins[5], RAW.strip())
        self.assertEqual(posts[0]["source"], "self_model"); self.assertEqual(posts[0]["metadata"]["self_model_id"], 3)
        self.assertIn("WHAT I'M BECOMING", out)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS self_model" in s for s in oc.sql))

    def test_thin_interior_skips_and_short_synthesis_aborts(self):
        oc, mc = _ops_cur(active=[], preoccs=[]), _mem_cur()
        with mock.patch.object(sm, "llm") as llm:
            code, posts, _ = self._run(oc, mc)
        self.assertEqual((code, posts), (0, [])); self.assertEqual(llm.call_count, 0)
        oc, mc = _ops_cur(), _mem_cur()
        code, posts, _ = self._run(oc, mc, raw="too short")
        self.assertEqual((code, posts), (1, [])); self.assertEqual(oc.writes("INSERT INTO self_model"), [])


class TestFrame(unittest.TestCase):
    def test_import_is_clean_in_a_subprocess(self):
        r = subprocess.run([sys.executable, "-c", "import nova_self_model"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_main_is_guarded(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("self_model_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
