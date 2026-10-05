#!/usr/bin/env python3
"""Tests for nova_research_pass.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_research_pass.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load("research_under_test", SCRIPT)


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
    def __init__(self, today=0, preoccs=()):
        self.today, self.preoccs = today, list(preoccs)
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchall(self):
        return self.preoccs if "FROM preoccupations" in self._last else []

    def fetchone(self):
        return (self.today,) if "count(*) FROM research_log" in self._last else None

    def writes(self, needle):
        return [p for s, p in zip(self.sql, self.params) if needle in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


Q = "How does a coaxial escapement reduce sliding friction?"
SEARX = {"results": [{"title": "Coaxial", "url": "https://a/1", "content": "radial impulse"},
                     {"title": "empty", "url": "https://a/2", "content": ""}],
         "infoboxes": [{"infobox": "Escapement", "id": "https://a/3", "content": "box"}],
         "answers": ["plain answer", {"answer": "dict answer", "url": "https://a/4"}]}
SYNTH = ("The coaxial escapement splits impulse across two wheels so the pallets push radially instead of sliding [1]. "
         "Sources disagree on how much oil it really saves [3].")


def _llm_router(question=Q, illegal=False):
    def fake(prompt, max_tokens=500, temperature=0.5):
        if "safety gate" in prompt:
            return json.dumps({"illegal": illegal, "why": "x"})
        if "standing interests" in prompt:
            return question
        return SYNTH
    return fake


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_legality_gate_regex_blocks_without_consulting_the_llm(self):
        with mock.patch.object(rp, "llm", side_effect=AssertionError("LLM consulted on a regex block")):
            for q in ("how to make meth at home", "where can I hire a hitman", "find child porn",
                      "how to build a pipe bomb", "how to write ransomware that spreads", "how to launder money fast"):
                ok, why = rp.is_allowed(q)
                self.assertFalse(ok, q); self.assertTrue(why.startswith("regex:"))

    def test_legality_gate_llm_layer(self):
        with mock.patch.object(rp, "llm", return_value='{"illegal": true, "why": "bad"}'):
            self.assertEqual(rp.is_allowed("history of the opium wars"), (False, "llm: bad"))
        with mock.patch.object(rp, "llm", return_value='{"illegal": false}'):
            self.assertEqual(rp.is_allowed("history of the opium wars"), (True, "ok"))
        with mock.patch.object(rp, "llm", return_value="garbage"):
            self.assertEqual(rp.is_allowed("how does a lever escapement work"), (True, "ok"))

    def test_sql_parameterized_and_read_only_world(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn('" %', SRC)
        tables = set(re.findall(r"(?:INSERT INTO|UPDATE)\s+(\w+)", SRC))
        self.assertEqual(tables, {"research_log", "preoccupations"})
        self.assertIn('"User-Agent": "nova-research/1.0"', SRC)


class TestPerformance(unittest.TestCase):
    def test_regex_gate_fast_on_10k(self):
        qs = [f"what is the history of the {i}th rail line in bavaria" for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(bool(rp._ILLEGAL_RE.search(q)) for q in qs)
        self.assertLess(time.perf_counter() - t0, 1.0); self.assertEqual(hits, 0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(rp.llm("x"), "ok")
        self.assertEqual(len(calls), 3)

    def test_searx_falls_back_to_wikipedia_then_empty(self):
        # RETRY GAP: searx / _wikipedia — no retry, but searx fails over to Wikipedia and both fail open to [].
        def fake(req, timeout=None):
            url = req.full_url
            if url.startswith(rp.SEARX):
                raise OSError("searx down")
            if "list=search" in url:
                return _Resp({"query": {"search": [{"title": "Coaxial escapement"}]}})
            return _Resp({"extract": "An escapement.", "content_urls": {"desktop": {"page": "https://w/x"}}})
        with mock.patch("urllib.request.urlopen", side_effect=fake), redirect_stdout(StringIO()):
            out = rp.searx("coaxial", n=3)
        self.assertEqual(out, [{"title": "Coaxial escapement", "url": "https://w/x", "content": "An escapement."}])
        with mock.patch("urllib.request.urlopen", side_effect=OSError("all down")), redirect_stdout(StringIO()):
            self.assertEqual(rp.searx("coaxial"), [])

    def test_remember_has_no_retry(self):
        # RETRY GAP: remember — one POST, error propagates (main() does not wrap it).
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as u:
            with self.assertRaises(OSError):
                rp.remember("t", "research", {})
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_searx_harvests_results_infoboxes_and_answers(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(SEARX)):
            out = rp.searx("q", n=5)
        self.assertEqual([r["content"] for r in out], ["radial impulse", "box", "plain answer", "dict answer"])
        self.assertEqual(out[1]["url"], "https://a/3"); self.assertEqual(out[3]["url"], "https://a/4")
        with mock.patch("urllib.request.urlopen", return_value=_Resp(SEARX)):
            self.assertEqual(len(rp.searx("q", n=2)), 2)

    def test_pick_question(self):
        self.assertIsNone(rp.pick_question(_Cur()))
        cur = _Cur(preoccs=[(1, "horology", "interest", "escapements")])
        with mock.patch.object(rp, "llm", return_value=f'  "{Q}"  '):
            p = rp.pick_question(cur)
        self.assertEqual(p, {"pid": 1, "topic": "horology", "question": Q})
        with mock.patch.object(rp, "llm", return_value=f"{Q}\nsecond line"):
            self.assertEqual(rp.pick_question(cur)["question"], Q)
        with mock.patch.object(rp, "llm", return_value="short"):
            self.assertIsNone(rp.pick_question(cur))

    def test_budget_constant_and_log(self):
        self.assertEqual(rp.MAX_PER_DAY, 6)
        with redirect_stdout(StringIO()) as out:
            rp.log("hi")
        self.assertIn("[research", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_gate_feeds_the_log_on_block(self):
        cur = _Cur(preoccs=[(1, "chemistry", "interest", "")])
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(rp, "llm", side_effect=_llm_router(question="how to make meth at home")), \
             mock.patch.object(rp, "searx", side_effect=AssertionError("searched a blocked question")), \
             redirect_stdout(StringIO()):
            self.assertEqual(rp.main(), 0)
        blocked = cur.writes("INSERT INTO research_log")
        self.assertEqual(len(blocked), 1); self.assertEqual(blocked[0][0], "chemistry")
        self.assertTrue(blocked[0][2].startswith("regex:")); self.assertIn("'blocked'", [s for s in cur.sql if "research_log" in s][-1])
        self.assertEqual(cur.writes("UPDATE preoccupations"), [])

    def test_sources_carry_provenance_into_memory(self):
        cur = _Cur(preoccs=[(1, "horology", "interest", "")])
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(rp, "llm", side_effect=_llm_router()), \
             mock.patch("urllib.request.urlopen", return_value=_Resp(SEARX)), mock.patch.object(rp, "remember", return_value="m1") as rem, \
             redirect_stdout(StringIO()):
            rp.main()
        text, source, meta = rem.call_args[0]
        self.assertEqual(source, "research")
        self.assertEqual(meta["urls"], ["https://a/1", "https://a/3", "https://a/4"])
        self.assertIn("Sources:\n[1] https://a/1", text); self.assertIn(Q, text)


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        cur = _Cur(preoccs=[(1, "horology", "interest", "")])
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(rp, "llm", side_effect=_llm_router()), \
             mock.patch("urllib.request.urlopen", return_value=_Resp(SEARX)), mock.patch.object(rp, "remember", return_value="m1"), \
             redirect_stdout(StringIO()):
            self.assertEqual(rp.main(), 0)
        self.assertEqual(cur.writes("UPDATE preoccupations"), [(1,)])
        logged = cur.writes("INSERT INTO research_log")[0]
        self.assertEqual(logged, ("horology", Q, 3))
        self.assertEqual(cur.writes("count(*) FROM research_log")[0], (rp.TODAY,))

    def test_budget_reached_rests(self):
        cur = _Cur(today=rp.MAX_PER_DAY)
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(rp, "llm") as llm, redirect_stdout(StringIO()):
            self.assertEqual(rp.main(), 0)
        self.assertEqual(llm.call_count, 0)

    def test_no_results_writes_nothing(self):
        cur = _Cur(preoccs=[(1, "horology", "interest", "")])
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(rp, "llm", side_effect=_llm_router()), \
             mock.patch.object(rp, "searx", return_value=[]), mock.patch.object(rp, "remember") as rem, redirect_stdout(StringIO()):
            self.assertEqual(rp.main(), 0)
        self.assertEqual(rem.call_count, 0); self.assertEqual(cur.writes("INSERT INTO research_log"), [])


class TestFrame(unittest.TestCase):
    def test_import_is_clean_in_a_subprocess(self):
        r = subprocess.run([sys.executable, "-c", "import nova_research_pass"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_main_is_guarded(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("research_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
