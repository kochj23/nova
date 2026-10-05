#!/usr/bin/env python3
"""Tests for nova_affect.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_affect.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aff = _load("affect", SCRIPT)
SRC = SCRIPT.read_text()
NOW = datetime.now(timezone.utc)


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); `tables` drives to_regclass; records every execute."""
    def __init__(self, answers=(), tables=()):
        self.answers = list(answers); self.tables = set(tables); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "to_regclass" in sql:
            self._last = (params[0],) if params[0].split(".")[-1] in self.tables else (None,)
            return
        hit = next((v for k, v in self.answers if k in sql), None)
        if isinstance(hit, Exception):
            raise hit
        self._last = hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False; self.closed = 0

    def cursor(self, *a, **k): return self._cur

    def close(self): self.closed += 1


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _busy_day():
    """An ops cursor describing a loud day: 3x the usual pages, one critical incident, warm words."""
    msgs = [("thanks Nova, that was great",), ("love it",), ("restart the poller",)]
    ts = [(NOW - timedelta(hours=h),) for h in (60, 40, 30, 20, 2)]
    return _Cur([
        ("FILTER (WHERE decision='page') p", (4.0,)),
        ("FILTER (WHERE level ILIKE 'crit%%') c", (1.0,)),
        ("AND decision='page'", (12,)),
        ("AND level ILIKE 'crit%%'", (4,)),
        ("FROM incidents WHERE status <> 'resolved'", (1, "critical")),
        ("FROM deep_healthcheck_log", (8, 0)),
        ("sum(count_24h) FROM contact_sense", (6,)),
        ("user_message FROM gateway_traces", msgs),
        ("created_at FROM gateway_traces", ts),
        ("avg(surprise)", (0.4,)),
        ("count(*) FROM predictions", (3,)),
        ("FROM claude_queue", (50,)),
        ("FROM herd_correspondents", (5,)),
        ("verified AND NOT vetoed", (3,)),
        ("(vetoed OR reverted)", (1,)),
        ("INSERT INTO affect_state", (7, datetime(2026, 10, 5, 9, 0))),
        ("SELECT label, valence, arousal, evidence", ("wired and heavy", -0.3, 0.7, {"summary": ["12 alerts paged", "1 open incident"]})),
    ], tables=("alert_triage_log", "incidents", "deep_healthcheck_log", "contact_sense", "gateway_traces",
               "predictions", "claude_queue", "herd_correspondents", "autonomy_ledger"))


def _mem_cur():
    return _Cur([("source='nova_articles' AND created_at::date=current_date", (2,)),
                 ("coalesce(metadata->>'type','')='pursuit' AND created_at::date=current_date", (3,)),
                 ("FROM memories WHERE source='nova_articles'", (2.0,)),
                 ("FROM memories WHERE source='unclaimed'", (4.0,))], tables=("memories",))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_except_an_int_constant(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        # the only % interpolation is the integer window constant, never user data
        for m in re.finditer(r"\) % (\w+)\)", SRC):
            self.assertEqual(m.group(1), "AUTONOMY_WINDOW_H")
        self.assertIsInstance(aff.AUTONOMY_WINDOW_H, int)

    def test_resonance_evidence_never_leaks_message_text(self):
        secret = "my password is hunter2 and I love you"
        cur = _Cur([("user_message FROM gateway_traces", [(secret,)]),
                    ("created_at FROM gateway_traces", [])], tables=("gateway_traces",))
        out = aff.signal_resonance(cur)
        blob = json.dumps(out)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("password", blob)
        self.assertIn("love", blob)  # only the lexicon hit is cited

    def test_only_write_is_affect_state(self):
        writes = [m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)]
        self.assertEqual(writes, ["INSERT INTO affect_state"])


class TestPerformance(unittest.TestCase):
    def test_tone_score_10k_messages_fast(self):
        msgs = [("thanks that was great" if i % 3 else "this is broken again? ugh") for i in range(10_000)]
        t0 = time.perf_counter()
        scores = [aff.tone_score(m)[0] for m in msgs]
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual({round(s) for s in scores}, {1, -1})

    def test_combine_10k_signals_fast(self):
        sigs = [aff.sig(f"s{i}", i, None, 0.001 * (1 if i % 2 else -1), 0.0001, "n") for i in range(10_000)]
        t0 = time.perf_counter()
        v, a, neutral, mag, usable = aff.combine(sigs)
        self.assertLess(time.perf_counter() - t0, 0.2)
        self.assertEqual(usable, 10_000)
        self.assertLessEqual(a, 1.0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def flaky(req, timeout=60):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "steady"}})
        with patch.object(urllib.request, "urlopen", flaky):
            self.assertEqual(aff.llm("p", "s"), "steady")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls, [n + "/api/chat" for n in aff.OLLAMA_NODES[:3]])

    def test_llm_all_nodes_down_fails_open_to_deterministic_label(self):
        def boom(*a, **k):
            raise OSError("down")
        with patch.object(urllib.request, "urlopen", boom):
            self.assertEqual(aff.llm("p", "s"), "")
            label, how = aff.name_label(0.4, 0.3, [])
        self.assertEqual((label, how), ("quietly satisfied", "fallback (llm unavailable)"))

    def test_current_affect_fails_open_neutral(self):
        # RETRY GAP: current_affect — one connect attempt; PG down returns the neutral dict, never raises
        def boom(*a, **k):
            raise OSError("pg down")
        with patch.object(aff.psycopg2, "connect", boom):
            d = aff.current_affect()
        self.assertEqual(d["label"], "neutral")
        self.assertEqual(d["injection"], "")

    def test_scalar_query_helper_fails_open(self):
        # RETRY GAP: _one — a failed read returns None and the signal reports usable=False
        cur = _Cur([("SELECT 1", RuntimeError("boom"))])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertIsNone(aff._one(cur, "SELECT 1"))
        self.assertIn("query skipped", buf.getvalue())


class TestUnit(unittest.TestCase):
    def test_selftest_resonance_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            aff.selftest_resonance()
        self.assertIn("passed", buf.getvalue())

    def test_clamp_and_words(self):
        self.assertEqual(aff.clamp(5, 0, 1), 1)
        self.assertEqual(aff.clamp(-5, 0, 1), 0)
        self.assertEqual(aff.clamp(0.5, 0, 1), 0.5)
        self.assertEqual(aff._valence_word(0.5), "clearly positive (good)")
        self.assertEqual(aff._valence_word(0.0), "neutral / even")
        self.assertEqual(aff._valence_word(-0.5), "clearly negative (heavy/bad)")
        self.assertEqual(aff._arousal_word(0.7), "activated / wired")
        self.assertEqual(aff._arousal_word(0.1), "calm")

    def test_fallback_label_quadrants(self):
        self.assertEqual(aff._fallback_label(0.5, 0.7), "energised")
        self.assertEqual(aff._fallback_label(0.5, 0.3), "quietly satisfied")
        self.assertEqual(aff._fallback_label(-0.5, 0.7), "wired and heavy")
        self.assertEqual(aff._fallback_label(-0.5, 0.3), "heavy")
        self.assertEqual(aff._fallback_label(0.0, 0.7), "keyed-up but even")
        self.assertEqual(aff._fallback_label(0.0, 0.3), "steady")

    def test_combine_neutral_guard(self):
        thin = [aff.sig("a", 1, None, 0.01, 0, "n"), aff.sig("b", 1, None, 0.0, 0.0, "n")]
        v, a, neutral, mag, usable = aff.combine(thin)
        self.assertTrue(neutral)
        self.assertEqual(usable, 1)
        self.assertEqual(a, aff.RESTING_AROUSAL)
        loud = [aff.sig("a", 1, None, -0.4, 0.2, "n"), aff.sig("b", 1, None, -0.3, 0.1, "n")]
        v, a, neutral, mag, usable = aff.combine(loud)
        self.assertFalse(neutral)
        self.assertEqual(v, -0.7)

    def test_name_label_rejects_a_label_that_inverts_valence(self):
        with patch.object(aff, "llm", lambda p, s: "heavy and grim"):
            self.assertEqual(aff.name_label(0.4, 0.3, [])[1], "fallback (llm label 'heavy and grim' contradicted valence sign)")
        with patch.object(aff, "llm", lambda p, s: "content"):
            self.assertTrue(aff.name_label(-0.4, 0.3, [])[1].startswith("fallback"))
        with patch.object(aff, "llm", lambda p, s: '"Wired but steady."\nignored'):
            self.assertEqual(aff.name_label(0.0, 0.7, []), ("wired but steady", "llm-named"))
        with patch.object(aff, "llm", lambda p, s: "   "):
            self.assertEqual(aff.name_label(0.0, 0.3, [])[1], "fallback (empty llm)")

    def test_signals_report_absent_tables_as_unusable(self):
        cur = _Cur(tables=())
        for fn in (aff.signal_alerts, aff.signal_incidents, aff.signal_infra, aff.signal_social,
                   aff.signal_resonance, aff.signal_surprise, aff.signal_unresolved, aff.signal_autonomy):
            for s in fn(cur):
                self.assertFalse(s["usable"], s)
                self.assertEqual((s["dv"], s["da"]), (0, 0))
        self.assertFalse(aff.signal_creative(cur, None)[0]["usable"])

    def test_evidence_strings_rank_influential_first(self):
        state = {"signals": [aff.sig("a", 1, None, 0.01, 0, "small"), aff.sig("b", 1, None, -0.4, 0.1, "big"),
                             aff.sig("c", None, None, 0, 0, "unusable", False)]}
        self.assertEqual(aff._evidence_strings(state), ["big", "small"])
        self.assertEqual(aff._evidence_strings(state, top=1), ["big"])

    def test_demo_neutral_runs_without_db(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(aff.demo_neutral(), 0)
        self.assertIn("LABEL: 'neutral'", buf.getvalue())


class TestIntegration(unittest.TestCase):
    def test_compute_affect_folds_every_signal_from_the_right_tables(self):
        oc, mc = _busy_day(), _mem_cur()
        with patch.object(aff, "llm", lambda p, s: "wired but steady"):
            st = aff.compute_affect(oc, mc)
        names = [s["signal"] for s in st["signals"]]
        self.assertEqual(names, ["alert_paging", "criticals", "open_incidents", "infra_health", "creative_output",
                                 "social_contact", "resonance", "silence", "surprise", "unresolved_load", "autonomy"])
        self.assertFalse(st["neutral"])
        self.assertEqual(st["labelled_by"], "llm-named")
        by = {s["signal"]: s for s in st["signals"]}
        self.assertLess(by["alert_paging"]["dv"], 0)        # 12 vs 4 typical: elevated
        self.assertEqual(by["social_contact"]["value"], 6)  # contact_sense preferred over gateway_traces
        self.assertIn("contact_sense", by["social_contact"]["note"])
        self.assertGreater(by["creative_output"]["dv"], 0)
        self.assertGreater(by["resonance"]["dv"], 0)
        self.assertEqual(by["autonomy"]["value"], 2)
        self.assertTrue(any("FROM alert_triage_log" in s for s, _ in oc.sql))
        self.assertTrue(any("FROM memories" in s for s, _ in mc.sql))

    def test_store_writes_evidence_and_lineage_as_json(self):
        oc = _busy_day()
        with patch.object(aff, "llm", lambda p, s: ""):
            st = aff.compute_affect(oc, None)
        row = aff.store(oc, st)
        self.assertEqual(row[0], 7)
        sql, params = oc.executed("INSERT INTO affect_state")[0]
        self.assertEqual(params[:3], (st["valence"], st["arousal"], st["label"]))
        self.assertIsInstance(params[3], aff.Json)
        self.assertIn("weights", params[3].adapted)
        self.assertEqual(params[3].adapted["summary"], aff._evidence_strings(st))

    def test_current_affect_composes_the_gateway_injection(self):
        oc = _busy_day()
        conn = _Conn(oc)
        with patch.object(aff.psycopg2, "connect", lambda *a, **k: conn):
            d = aff.current_affect()
        self.assertEqual(d["injection"], "Today I'm running wired and heavy — 12 alerts paged; 1 open incident.")
        self.assertEqual(conn.closed, 1)
        with patch.object(aff.psycopg2, "connect", lambda *a, **k: _Conn(_Cur([("SELECT label", ("neutral", 0, 0.35, {})) ]))):
            self.assertIn("insufficient signal", aff.current_affect()["injection"])
        with patch.object(aff.psycopg2, "connect", lambda *a, **k: _Conn(_Cur([("SELECT label", None)]))):
            self.assertEqual(aff.current_affect()["evidence"], [])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, oc, mem_ok=True):
        conns = []

        def connect(dsn, **k):
            if dsn == aff.MEM_DSN and not mem_ok:
                raise OSError("mem down")
            c = _Conn(_mem_cur() if dsn == aff.MEM_DSN else oc); conns.append(c); return c
        buf = io.StringIO()
        with patch.object(aff.psycopg2, "connect", connect), patch.object(aff, "llm", lambda p, s: "wired but steady"), \
                patch.object(sys, "argv", ["nova_affect.py", *argv]), redirect_stdout(buf):
            rc = aff.main()
        return rc, buf.getvalue()

    def test_golden_path_stores_and_prints_injection(self):
        oc = _busy_day()
        rc, out = self._main([], oc)
        self.assertEqual(rc, 0)
        self.assertEqual(len(oc.executed("CREATE TABLE IF NOT EXISTS affect_state")), 1)
        self.assertEqual(len(oc.executed("INSERT INTO affect_state")), 1)
        self.assertIn("affect_state #7 written", out)
        self.assertIn("LABEL   = 'wired but steady'  [llm-named]", out)
        self.assertIn("VALENCE = +0.051", out)   # 12 vs 4 pages dragged down, creative + warm words lifted: net just above even
        self.assertIn("gateway injection → Today I'm running wired and heavy", out)

    def test_dry_run_writes_nothing(self):
        oc = _busy_day()
        rc, out = self._main(["--dry-run"], oc)
        self.assertEqual(rc, 0)
        self.assertEqual(oc.executed("INSERT INTO affect_state"), [])
        self.assertNotIn("gateway injection", out)

    def test_error_path_memories_db_down_skips_creative_only(self):
        oc = _busy_day()
        rc, out = self._main(["--dry-run"], oc, mem_ok=False)
        self.assertEqual(rc, 0)
        self.assertIn("memories db unavailable", out)
        self.assertRegex(out, r"creative_output\s+value=None")
        self.assertIn("VALENCE =", out)


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(aff.__name__, "affect")


if __name__ == "__main__":
    unittest.main()
