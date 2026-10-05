#!/usr/bin/env python3
"""Tests for nova_answer_own.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_answer_own.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ao = _load("answer_own", SCRIPT)
SRC = SCRIPT.read_text()
Q_ROW = (2, "What was the 1911 train wreck near Burbank?", "local_history", "an excerpt about the wreck")
HIM_ROW = (1, "Jordan, were you referring to the Sumerians?", "x", "")
GAP_ROW = (9, "I keep returning to 'aviation ref' (17x) with thin understanding", "thin_coverage")
HITS = [{"title": "1911 wreck", "snippet": "A train derailed", "url": "https://example.org/wreck"},
        {"title": "History", "snippet": "More", "url": "https://example.org/more"}]
GOOD = json.dumps({"answer": "It was a derailment on the SP line. [1]", "confidence": "high"})


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = next((v for k, v in self.answers if k in sql), None)

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False

    def cursor(self, *a, **k): return self._cur


def _cur(questions=(), gaps=()):
    return _Cur([("FROM reflection_questions\n", list(questions)), ("FROM learning_gaps WHERE status='open'", list(gaps))])


def _offline(llm=None, searx=None, remember=None, allowed=(True, "ok")):
    """Patch the research_pass helpers the organ imported: LLM, SearXNG, memory POST, legality gate."""
    calls = {"llm": [], "searx": [], "remember": []}

    def _llm(prompt, **k):
        calls["llm"].append(prompt)
        return (llm(prompt) if callable(llm) else llm) if llm is not None else ("wreck 1911 burbank train" if "search query" in prompt else GOOD)

    def _searx(q, n=5):
        calls["searx"].append(q); return HITS if searx is None else searx

    def _remember(text, source, metadata):
        calls["remember"].append((text, source, metadata)); return 1
    ps = [patch.object(ao, "llm", _llm), patch.object(ao, "searx", _searx),
          patch.object(ao, "remember", remember or _remember), patch.object(ao, "is_allowed", lambda q: allowed)]
    return ps, calls


class _With:
    def __init__(self, ps): self.ps = ps

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_table_name_is_a_two_literal_whitelist_and_values_are_bound(self):
        # the only f-string SQL chooses between two literal table names; every value travels as %s
        fstrings = re.findall(r'execute\(f"([^"]+)"', SRC)
        self.assertEqual(len(fstrings), 2)
        for f in fstrings:
            self.assertRegex(f, r"\{(table|'reflection_questions' if item\['kind'\]=='question' else 'learning_gaps')\}")
            self.assertNotIn("{item", f)
        self.assertIn("WHERE id=%s", SRC)

    def test_legality_gate_sees_query_and_context_and_retires_the_row(self):
        cur = _Cur()
        ps, calls = _offline(allowed=(False, "regex: matched illegal/harmful category"))
        seen = []
        ps[3] = patch.object(ao, "is_allowed", lambda q: seen.append(q) or (False, "regex"))
        with _With(ps), redirect_stdout(io.StringIO()):
            r = ao.answer_one(cur, {"kind": "gap", "id": 9, "query": "innocent", "context": "synthesis of GHB", "source": "s"})
        self.assertEqual(r, "skipped")
        self.assertEqual(seen, ["innocent synthesis of GHB"])
        self.assertEqual(calls["searx"], [])
        sql, params = cur.executed("UPDATE learning_gaps")[0]
        self.assertEqual(params, (ao.MAX_ATTEMPTS, 9))

    def test_questions_for_jordan_stay_his(self):
        self.assertEqual(ao.pick(_cur([HIM_ROW, Q_ROW]))["id"], 2)
        self.assertIs(ao.ABOUT_HIM_RE, __import__("nova_ask_one").ABOUT_HIM_RE)


class TestPerformance(unittest.TestCase):
    def test_pick_scans_10k_rows_fast(self):
        rows = [(i, "Jordan, did you see the garage camera?", "x", "") for i in range(10_000)] + [Q_ROW]
        t0 = time.perf_counter()
        it = ao.pick(_cur(rows))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(it["id"], 2)

    def test_regexes_fast_on_10k(self):
        t0 = time.perf_counter()
        n = sum(bool(ao._DICT_RE.search(f"https://www.merriam-webster.com/dictionary/w{i}")) for i in range(10_000))
        n += sum(bool(ao._QUOTED.search(f"I keep returning to 'topic {i}'")) for i in range(10_000))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(n, 20_000)


class TestRetry(unittest.TestCase):
    def test_llm_down_falls_back_to_the_question_as_the_query(self):
        # RETRY GAP: llm (nova_research_pass) — node failover inside, but an all-down "" is accepted here
        ps, calls = _offline(llm="")
        with _With(ps):
            ans, conf, urls = ao.research({"kind": "question", "id": 2, "query": "What was the wreck?", "context": "", "source": "s"})
        self.assertEqual(calls["searx"], ["What was the wreck?"])
        self.assertEqual((ans, conf), ("", "low"))     # unparseable answer -> low, never written

    def test_no_search_hits_fails_open_low(self):
        # RETRY GAP: searx — a failed/empty search returns [] and the question is left unresolved, not guessed
        ps, calls = _offline(searx=[])
        with _With(ps):
            self.assertEqual(ao.research({"kind": "question", "id": 2, "query": "q", "context": "", "source": "s"}), ("", "low", []))
        self.assertEqual(len(calls["llm"]), 1)        # the answer prompt is never sent without sources

    def test_remember_failure_escapes_after_the_row_is_written(self):
        # RETRY GAP: remember (nova_research_pass) — no retry and no catch; the answer row is already
        # committed (autocommit) when the memory POST fails, so a rerun will not re-answer it
        cur = _Cur()

        def boom(*a, **k):
            raise OSError("memory server down")
        ps, _ = _offline(remember=boom)
        with _With(ps), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                ao.answer_one(cur, {"kind": "question", "id": 2, "query": "q", "context": "", "source": "s"})
        self.assertEqual(len(cur.executed("UPDATE reflection_questions SET answer=")), 1)


class TestUnit(unittest.TestCase):
    def test_pick_prefers_questions_then_gaps_then_none(self):
        self.assertEqual(ao.pick(_cur([Q_ROW], [GAP_ROW]))["kind"], "question")
        g = ao.pick(_cur([], [GAP_ROW]))
        self.assertEqual((g["kind"], g["id"], g["source"]), ("gap", 9, "thin_coverage"))
        self.assertTrue(g["query"].startswith("aviation ref: "))
        self.assertIsNone(ao.pick(_cur([HIM_ROW], [])))
        g2 = ao.pick(_cur([], [(3, "no quotes here", "curiosity")]))
        self.assertTrue(g2["query"].startswith("no quotes here: "))

    def test_pick_query_binds_max_attempts_and_skip_sources(self):
        cur = _cur([Q_ROW])
        ao.pick(cur)
        sql, params = cur.sql[0]
        self.assertEqual(params, (ao.MAX_ATTEMPTS, list(ao.SKIP_SOURCES)))
        self.assertIn("self_attempts < %s", sql)
        self.assertIn("NOT ILIKE 'I predicted with%%'", sql)

    def test_research_parses_json_and_downgrades_dictionary_hits(self):
        ps, _ = _offline()
        with _With(ps):
            ans, conf, urls = ao.research({"kind": "question", "id": 2, "query": "q", "context": "c", "source": "s"})
        self.assertEqual((ans, conf, urls), ("It was a derailment on the SP line. [1]", "high", [h["url"] for h in HITS]))
        dicts = [{"title": "purpose", "snippet": "n.", "url": f"https://www.merriam-webster.com/dictionary/{i}"} for i in range(3)]
        ps, _ = _offline(searx=dicts)
        with _With(ps):
            self.assertEqual(ao.research({"kind": "question", "id": 2, "query": "q", "context": "", "source": "s"})[1], "low")

    def test_research_tolerates_garbage_json(self):
        ps, _ = _offline(llm=lambda p: "short query" if "search query" in p else "not json {broken")
        with _With(ps):
            ans, conf, _ = ao.research({"kind": "question", "id": 2, "query": "q", "context": "", "source": "s"})
        self.assertEqual((ans, conf), ("", "low"))

    def test_regexes(self):
        self.assertEqual(ao._QUOTED.search("I keep returning to 'aviation ref' (17x)").group(1), "aviation ref")
        self.assertTrue(ao._DICT_RE.search("https://www.merriam-webster.com/dictionary/purpose"))
        self.assertFalse(ao._DICT_RE.search("https://en.wikipedia.org/wiki/Purpose"))

    def test_selftest_passes_with_the_gate_offline(self):
        with patch.object(ao, "is_allowed", lambda q: (False, "regex") if "GHB" in q else (True, "ok")), redirect_stdout(io.StringIO()):
            ao.selftest()


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported_not_reimplemented(self):
        self.assertIn("from nova_ask_one import ABOUT_HIM_RE, SKIP_SOURCES", SRC)
        self.assertIn("from nova_research_pass import OPS_DSN, is_allowed, llm, remember, searx", SRC)
        self.assertNotIn("def llm(", SRC)
        self.assertNotIn("def searx(", SRC)
        self.assertEqual(ao.SOURCE, "self_answer")

    def test_pick_research_write_chain_for_a_question(self):
        cur = _cur([Q_ROW])
        ps, calls = _offline()
        with _With(ps), redirect_stdout(io.StringIO()):
            item = ao.pick(cur)
            self.assertEqual(ao.answer_one(cur, item), "answered")
        sql, params = cur.executed("UPDATE reflection_questions SET answer=")[0]
        self.assertIn("answered_at=now()", sql)
        self.assertTrue(params[0].startswith(f"[self-researched {date.today().isoformat()}, high confidence] It was a derailment"))
        self.assertIn("Sources: https://example.org/wreck, https://example.org/more", params[0])
        self.assertEqual(params[1], 2)
        text, source, meta = calls["remember"][0]
        self.assertEqual(source, "self_answer")
        self.assertEqual((meta["kind"], meta["ref_id"], meta["confidence"], meta["origin"]), ("question", 2, "high", "local_history"))
        self.assertIn("I asked myself: What was the 1911 train wreck", text)

    def test_gap_chain_marks_studied(self):
        cur = _cur([], [GAP_ROW])
        ps, calls = _offline()
        with _With(ps), redirect_stdout(io.StringIO()):
            self.assertEqual(ao.answer_one(cur, ao.pick(cur)), "answered")
        sql, params = cur.executed("UPDATE learning_gaps SET status='studied'")[0]
        self.assertEqual(params, (9,))
        self.assertEqual(calls["remember"][0][2]["kind"], "gap")

    def test_schema_adds_the_attempt_counters(self):
        cur = _Cur()
        ao.ensure_schema(cur)
        self.assertEqual(len(cur.executed("ADD COLUMN IF NOT EXISTS self_attempts")), 2)


class TestFunctional(unittest.TestCase):
    def _main(self, cur, argv=(), **offline):
        ps, calls = _offline(**offline)
        buf = io.StringIO()
        with patch.object(ao.psycopg2, "connect", lambda *a, **k: _Conn(cur)), _With(ps), \
                patch.object(sys, "argv", ["nova_answer_own.py", *argv]), redirect_stdout(buf):
            rc = ao.main()
        return rc, buf.getvalue(), calls

    def test_golden_path_answers_and_files_a_memory(self):
        cur = _cur([Q_ROW])
        rc, out, calls = self._main(cur)
        self.assertEqual(rc, 0)
        self.assertIn("result: answered", out)
        self.assertEqual(len(cur.executed("UPDATE reflection_questions SET answer=")), 1)
        self.assertEqual(len(calls["remember"]), 1)

    def test_dry_run_prints_and_writes_nothing(self):
        cur = _cur([Q_ROW])
        rc, out, calls = self._main(cur, ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("A (high): It was a derailment", out)
        self.assertEqual(cur.executed("UPDATE"), [])
        self.assertEqual(calls["remember"], [])

    def test_low_confidence_bumps_attempts_instead_of_writing(self):
        cur = _cur([Q_ROW])
        rc, out, calls = self._main(cur, llm=lambda p: "q" if "search query" in p else json.dumps({"answer": "maybe", "confidence": "low"}))
        self.assertEqual(rc, 0)
        self.assertIn("result: unresolved", out)
        self.assertEqual(cur.executed("UPDATE reflection_questions SET self_attempts = self_attempts + 1")[0][1], (2,))
        self.assertEqual(cur.executed("SET answer="), [])
        self.assertEqual(calls["remember"], [])

    def test_nothing_open(self):
        rc, out, _ = self._main(_cur())
        self.assertEqual(rc, 0)
        self.assertIn("nothing open", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        # --selftest is NOT used here: its legality-gate probe reaches the LLM when the regex does not match
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(selftest() if "--selftest" in sys.argv else main())', SRC)
        self.assertEqual(ao.__name__, "answer_own")


if __name__ == "__main__":
    unittest.main()
