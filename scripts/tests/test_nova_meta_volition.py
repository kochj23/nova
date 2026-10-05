#!/usr/bin/env python3
"""Tests for nova_meta_volition.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_meta_volition.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mv = _load("mv", SCRIPT)


class _Cur:
    """Routes each query to a canned answer by SQL substring (first match wins); records every statement."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def _hit(self):
        for key, val in self.routes:
            if key in self._last:
                return val
        return None

    def fetchone(self):
        v = self._hit()
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def fetchall(self):
        v = self._hit()
        return list(v) if isinstance(v, list) else ([] if v is None else [v])

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _mc(lanes, topics=(), laneless=2):
    return _Cur([("(metadata->>'mode') IS NULL", (laneless,)), ("'topic' AS topic", list(topics)), ("AS mode", list(lanes.items()))])


def _oc(volition=(), latest=None, n_active=0):
    return _Cur([("FROM volition_log", list(volition)), ("INSERT INTO attention_policy", (11,)), ("INSERT INTO meta_volition_log", (12,)),
                 ("FROM attention_policy ORDER BY ts DESC LIMIT 1", latest), ("count(*) FILTER", (n_active,))])


BALANCED = {"preoccupation": 10, "thread": 4, "tangent": 2, "tinker": 2, "aspire": 2}
CROWDED = {"preoccupation": 5, "thread": 1, "tangent": 1, "tinker": 10, "aspire": 10}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_interpolation_is_int_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIn('WINDOW_DAYS = int(', SRC)                         # the only % injection is an int-cast env value
        self.assertIsInstance(mv.WINDOW_DAYS, int)
        oc = _oc()
        with patch.object(mv, "llm", lambda *a, **k: "note'); DROP TABLE attention_policy; --"), redirect_stdout(io.StringIO()):
            mv.mode_review(oc, _mc(CROWDED))
        sql, params = oc.ran("INSERT INTO attention_policy")[0]
        self.assertNotIn("DROP", sql); self.assertIn("DROP", params[2])

    def test_never_auto_activates_a_policy(self):
        self.assertNotIn("status='active'", "".join(re.findall(r"(?:INSERT INTO|UPDATE)[^;]*", SRC)).replace("WHERE status='active'", ""))
        self.assertIn("'proposed',%s", SRC)
        self.assertIn("WHERE status='proposed'", SRC)                     # supersede never touches a human-blessed 'active'
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"attention_policy", "meta_volition_log"})


class TestPerformance(unittest.TestCase):
    def test_split_and_diagnosis_fast_on_10k_topics(self):
        topics = [(f"topic {i}", 10_000 - i) for i in range(10_000)]
        t0 = time.perf_counter()
        for _ in range(200):
            shares, passion, sd, total = mv.compute_shares(BALANCED)
            mv.diagnose(shares, passion, sd, topics, total)
            mv._rebalance_toward_baseline(shares)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes_then_succeeds(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "a note"}})
        with patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(mv.llm("q"), "a note")
        self.assertEqual(calls, [n + "/api/chat" for n in mv.OLLAMA_NODES[:3]])
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(mv.llm("q"), "")

    def test_note_falls_back_to_her_own_words_when_the_model_is_silent(self):
        with patch.object(mv, "llm", lambda *a, **k: ""):
            note = mv.write_note("crowded", ["f"], {}, mv.BASELINE_WEIGHTS, None, 0.0)
            self.assertIn("Little Mister", note)
            self.assertIn("40%", mv.write_note("fixated", ["f"], {}, mv.BASELINE_WEIGHTS, "radios", 0.4))
            self.assertIn("well-balanced", mv.write_note("balanced", ["f"], {}, None, None, 0.0))

    def test_notify_and_accessors_fail_open(self):
        # RETRY GAP: notify_jordan (post_both) — one attempt, swallowed; the proposal row is already saved
        with patch.object(mv, "_post_both", MagicMock(side_effect=OSError("slack down"))), patch.object(mv, "DRY_RUN", False), \
             redirect_stdout(io.StringIO()):
            mv.notify_jordan("n", mv.BASELINE_WEIGHTS, "crowded")
        with patch.object(mv.psycopg2, "connect", side_effect=OSError("no pg")):
            self.assertEqual(mv.current_attention_note(), "")
            self.assertIsNone(mv.active_attention_weights())


class TestUnit(unittest.TestCase):
    def test_compute_shares(self):
        shares, passion, sd, total = mv.compute_shares({})
        self.assertEqual((passion, sd, total), (0.0, 0.0, 0)); self.assertEqual(set(shares), set(mv.ALL_LANES))
        shares, passion, sd, total = mv.compute_shares({**BALANCED, "quiet": 99})
        self.assertEqual(total, 20); self.assertAlmostEqual(passion + sd, 1.0); self.assertAlmostEqual(passion, 0.8)

    def test_diagnose_verdicts(self):
        self.assertEqual(mv.diagnose({}, 0, 0, [], mv.MIN_SAMPLE - 1)[0], "thin")
        shares, p, sd, total = mv.compute_shares(BALANCED)
        self.assertEqual(mv.diagnose(shares, p, sd, [("a", 4), ("b", 3), ("c", 3)], total)[0], "balanced")
        self.assertEqual(mv.diagnose(shares, p, sd, [("a", 5), ("b", 5)], total)[0], "fixated")   # 50% of passion-hours on one topic
        v, findings, proposed, budget = mv.diagnose(shares, p, sd, [("radios", 9), ("b", 1)], total)
        self.assertEqual(v, "fixated"); self.assertAlmostEqual(sum(proposed.values()), 1.0); self.assertEqual(budget, mv.BASELINE_BUDGET)
        self.assertLess(proposed["preoccupation"], mv.BASELINE_WEIGHTS["preoccupation"])
        shares, p, sd, total = mv.compute_shares(CROWDED)
        v, findings, proposed, _ = mv.diagnose(shares, p, sd, [], total)
        self.assertEqual(v, "crowded"); self.assertLess(proposed["tinker"], shares["tinker"]); self.assertGreater(proposed["tinker"], mv.BASELINE_WEIGHTS["tinker"])

    def test_rebalance_moves_halfway_and_normalises(self):
        out = mv._rebalance_toward_baseline({l: 0.0 for l in mv.ALL_LANES})
        self.assertAlmostEqual(sum(out.values()), 1.0, places=2)
        for l in mv.ALL_LANES:
            self.assertAlmostEqual(out[l], mv.BASELINE_WEIGHTS[l], places=2)
        self.assertEqual(mv._one_line("  a\n b  ", 3), "a b")

    def test_accessors_read_the_right_rows(self):
        cur = _Cur([("SELECT note FROM attention_policy", ("I'd rather spend more of me on radios.",))])
        with patch.object(mv.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(mv.current_attention_note(10), "I'd rather")
        cur = _Cur([("SELECT note FROM attention_policy", None), ("SELECT proposal FROM meta_volition_log", ("[balanced] fine",))])
        with patch.object(mv.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(mv.current_attention_note(), "[balanced] fine")
        cur = _Cur([("WHERE status='active'", (json.dumps({"tinker": 0.5}),))])
        with patch.object(mv.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(mv.active_attention_weights(), {"tinker": 0.5})


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported(self):
        import nova_lineage, nova_config
        self.assertIs(mv.lineage_stamp, nova_lineage.lineage_stamp)
        self.assertIs(mv._post_both, nova_config.post_both)
        self.assertEqual(mv.SLACK_CHAN, nova_config.SLACK_CHAN)

    def test_gather_then_shares_then_diagnose(self):
        mc = _mc(CROWDED, topics=[("radios", 4), ("clocks", 1)], laneless=7)
        oc = _oc(volition=[("tinker", 9)])
        lanes, laneless, vol, topics = mv.gather_split(oc, mc)
        self.assertEqual((lanes, laneless, vol, topics), (CROWDED, 7, {"tinker": 9}, [("radios", 4), ("clocks", 1)]))
        self.assertIn(f"interval '{mv.WINDOW_DAYS} days'", mc.sql[0][0])
        self.assertEqual(mv.diagnose(*mv.compute_shares(lanes)[:3], topics, mv.compute_shares(lanes)[3])[0], "crowded")


class TestFunctional(unittest.TestCase):
    def test_review_crowded_writes_a_proposal_and_notifies(self):
        oc = _oc()
        post = MagicMock()
        out = io.StringIO()
        with patch.object(mv, "llm", lambda *a, **k: "I have been over-indexing on tinkering."), patch.object(mv, "_post_both", post), \
             patch.object(mv, "DRY_RUN", False), redirect_stdout(out):
            self.assertEqual(mv.mode_review(oc, _mc(CROWDED)), 0)
        self.assertTrue(oc.ran("CREATE TABLE IF NOT EXISTS attention_policy"))
        self.assertTrue(oc.ran("SET status='superseded' WHERE status='proposed'"))
        sql, params = oc.ran("INSERT INTO attention_policy")[0]
        self.assertIn("'proposed'", sql); self.assertEqual(params[1], mv.BASELINE_BUDGET); self.assertAlmostEqual(sum(json.loads(params[0]).values()), 1.0, places=2)
        self.assertEqual(oc.ran("INSERT INTO meta_volition_log")[0][1][1], "[policy #11, proposed] I have been over-indexing on tinkering.")
        self.assertIn("meta-volition (crowded)", post.call_args[0][0]); self.assertIn("nothing auto-activated", post.call_args[0][0])
        self.assertIn("PROPOSAL (her words)", out.getvalue())

    def test_review_balanced_logs_only(self):
        oc = _oc()
        post = MagicMock()
        with patch.object(mv, "llm", lambda *a, **k: ""), patch.object(mv, "_post_both", post), patch.object(mv, "DRY_RUN", False), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(mv.mode_review(oc, _mc(BALANCED, topics=[("a", 4), ("b", 3), ("c", 3)])), 0)
        self.assertEqual(oc.ran("INSERT INTO attention_policy"), [])
        self.assertTrue(oc.ran("INSERT INTO meta_volition_log")[0][1][1].startswith("[balanced] I looked at how I spent"))
        post.assert_not_called()

    def test_review_error_path_slack_down_still_saves(self):
        oc = _oc()
        with patch.object(mv, "llm", lambda *a, **k: ""), patch.object(mv, "_post_both", MagicMock(side_effect=OSError("slack"))), \
             patch.object(mv, "DRY_RUN", False), redirect_stdout(io.StringIO()):
            self.assertEqual(mv.mode_review(oc, _mc(CROWDED)), 0)
        self.assertEqual(len(oc.ran("INSERT INTO attention_policy")), 1)

    def test_main_report_mode(self):
        oc = _oc(volition=[("preoccupation", 3)], latest=(4, datetime(2026, 10, 1, 9, 0), {"preoccupation": 0.5, "thread": 0.2, "tangent": 0.1, "tinker": 0.1, "aspire": 0.1}, 20, "proposed", "a note"))
        out = io.StringIO()
        with patch.object(mv.psycopg2, "connect", side_effect=[_Conn(oc), _Conn(_mc(BALANCED))]), \
             patch.object(sys, "argv", ["nova_meta_volition.py", "--mode", "report"]), redirect_stdout(out):
            self.assertEqual(mv.main(), 0)
        self.assertIn("Latest policy: #4 [proposed] 2026-10-01 09:00  budget=20", out.getvalue())
        self.assertIn("none (defaults in force)", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_runs_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--mode", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
