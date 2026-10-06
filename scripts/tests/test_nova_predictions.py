#!/usr/bin/env python3
"""Tests for nova_predictions.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("nova_predictions_t", SCRIPTS / "nova_predictions.py")
pr._stamp = lambda: {}                                   # no lineage lookups
pr.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")))
SRC = (SCRIPTS / "nova_predictions.py").read_text()
SOFT = types.SimpleNamespace(calibrate=lambda conf, oc, domain=None: conf)   # identity calibration


class _Cur:
    """Scripted cursor: `answer(sql, params)` returns the rows for each query; records writes."""
    def __init__(self, answer):
        self.answer = answer; self.sql = []; self._rows = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        r = self.answer(sql, params)
        if isinstance(r, Exception):
            raise r
        self._rows = list(r or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def _q():
    return redirect_stdout(io.StringIO())


def _resp(content):
    class R:
        def read(self):
            return json.dumps({"message": {"content": content}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return R()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", pr.OPS_DSN + pr.MEM_DSN)
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_mem_check_text_like_is_a_parameter(self):
        mc = _Cur(lambda s, p: [(0,)])
        evil = "x' OR '1'='1"
        pr.eval_deterministic({"type": "mem_activity", "source": "bambu", "text_like": evil}, mc, "t")
        sql, params = mc.sql[0]
        self.assertNotIn(evil, sql)
        self.assertEqual(params[-1], f"%{evil}%")

    def test_relationship_bet_without_check_is_dropped(self):
        oc = _Cur(lambda s, p: [(1,)] if "RETURNING" in s else [])
        raw = json.dumps([{"statement": "Jordan will text me", "domain": "relationship", "confidence": 0.9}])
        with patch.object(pr, "gather_signals", return_value={"preoccupations": [], "streams": [], "texture": [],
                                                               "jordan_cadence": None}), \
                patch.object(pr, "llm", return_value=raw), patch.dict(sys.modules, {"nova_soft_certainty": SOFT}), _q():
            self.assertEqual(pr.do_predict(oc, None), [])
        self.assertFalse(any("INSERT" in s for s, _ in oc.sql))


class TestPerformance(unittest.TestCase):
    def test_report_10k_rows(self):
        rows = [(0.05 + (i % 19) * 0.05, ("correct", "incorrect", "partial")[i % 3], 0.1) for i in range(10_000)]

        def ans(s, p):
            if "status='resolved'" in s:
                return rows
            if "to_regclass" in s:
                return [(None,)]
            return [(0,)]
        t0 = time.perf_counter()
        with _q():
            r = pr.do_report(_Cur(ans))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(r["resolved"], 10_000)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def uo(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _resp("forecast")
        with patch.object(pr.urllib.request, "urlopen", side_effect=uo):
            self.assertEqual(pr.llm("p"), "forecast")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], pr.OLLAMA_NODES[0] + "/api/chat")

    def test_all_nodes_down_returns_empty(self):
        with patch.object(pr.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(pr.llm("p"), "")
        self.assertEqual(uo.call_count, len(pr.OLLAMA_NODES))

    def test_gateway_accessors_fail_safe(self):
        # RETRY GAP: recent_surprises/calibration_summary/recall — one attempt, safe empty default
        self.assertEqual(pr.recent_surprises(), [])
        self.assertEqual(pr.calibration_summary(), "")
        with patch.object(pr.urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertEqual(pr.recall("q"), [])


class TestUnit(unittest.TestCase):
    def test_parse_json_helpers(self):
        self.assertEqual(pr.parse_json_array('noise [{"a":1}] tail'), [{"a": 1}])
        self.assertEqual(pr.parse_json_array("no array"), [])
        self.assertEqual(pr.parse_json_array("[broken"), [])
        self.assertEqual(pr.parse_json_object('x {"o": 2} y'), {"o": 2})
        self.assertEqual(pr.parse_json_object(None), {})
        self.assertEqual((pr.clamp01("2"), pr.clamp01(-1), pr.clamp01("x")), (1.0, 0.0, 0.5))

    def test_extract_check(self):
        crit = 'prose\n\n```check\n{"type": "mem_activity", "source": "bambu"}\n```'
        self.assertEqual(pr.extract_check(crit)["source"], "bambu")
        self.assertIsNone(pr.extract_check("just prose"))
        self.assertIsNone(pr.extract_check("```check\n{bad}\n```"))

    def test_parse_resolves_by_relative_and_absolute(self):
        before = datetime.now(timezone.utc)
        d = pr.parse_resolves_by("+3d")
        self.assertAlmostEqual((d - before).total_seconds(), 3 * 86400, delta=5)
        self.assertEqual(pr.parse_resolves_by("2030-01-02").tzinfo, timezone.utc)
        g = pr.parse_resolves_by("garbage")
        self.assertAlmostEqual((g - before).total_seconds(), 86400, delta=5)

    def test_deterministic_silence_check(self):
        mc = _Cur(lambda s, p: [(0,)])
        out, hit, why = pr.eval_deterministic({"type": "mem_activity", "source": "bambu", "expect": "silent"}, mc, "t")
        self.assertEqual((out, hit), ("correct", 1.0))
        self.assertIsNone(pr.eval_deterministic({"type": "other"}, mc, "t"))


class TestIntegration(unittest.TestCase):
    def test_predict_attaches_machine_check_only_for_real_streams(self):
        sig = {"preoccupations": [], "streams": [("bambu", 40)], "texture": [], "jordan_cadence": None}
        raw = json.dumps([
            {"statement": "bambu stays active", "domain": "ops", "confidence": 0.8, "resolves_by": "+12h",
             "resolution_criteria": "count rows", "check": {"type": "mem_activity", "source": "bambu"}},
            {"statement": "ghost stays quiet", "domain": "nonsense", "confidence": 0.6,
             "check": {"type": "mem_activity", "source": "ghost"}}])
        oc = _Cur(lambda s, p: [(42,)] if "RETURNING" in s else [])
        with patch.object(pr, "gather_signals", return_value=sig), patch.object(pr, "llm", return_value=raw), \
                patch.dict(sys.modules, {"nova_soft_certainty": SOFT}), _q():
            w = pr.do_predict(oc, None)
        self.assertEqual(len(w), 2)
        inserts = [p for s, p in oc.sql if "INSERT INTO predictions" in s]
        self.assertIn("```check", inserts[0][4])
        self.assertNotIn("```check", inserts[1][4])               # unknown source -> no machine block
        self.assertEqual(inserts[1][1], "self")                   # bad domain normalised

    def test_autonomy_check_resolves_against_trust_table(self):
        oc = _Cur(lambda s, p: [(True, 5, 0)])
        out, hit, why = pr.eval_deterministic({"type": "autonomy_class_earned", "action_class": "restart"},
                                               None, "t", oc)
        self.assertEqual((out, hit), ("correct", 1.0))
        self.assertEqual(oc.sql[0][1], ("restart",))
        self.assertEqual(pr.eval_autonomy({"type": "autonomy_verify"}, None, "t")[0], "unresolvable")


class TestFunctional(unittest.TestCase):
    def test_resolve_scores_and_spawns_curiosity_on_high_surprise(self):
        crit = 'c\n```check\n{"type": "mem_activity", "source": "bambu", "expect": "active"}\n```'
        due = [(1, "bambu stays active", "ops", 0.95, crit, "t0"),
               (2, "vague thing", "self", 0.5, "prose", "t0")]

        def ans(s, p):
            if "FROM predictions" in s and "status='open'" in s:
                return due
            return []
        oc = _Cur(ans)
        mc = _Cur(lambda s, p: [(0,)])                            # stream silent -> confident miss
        with patch.object(pr, "eval_llm", return_value=("unresolvable", None, "no evidence")), \
                patch.object(pr, "remember", return_value=9) as rem, _q():
            res = pr.do_resolve(oc, mc)
        self.assertEqual(res[0][:2], (1, "incorrect"))
        self.assertAlmostEqual(res[0][2], 0.9025)
        self.assertEqual(res[1], (2, "unresolvable", None))
        self.assertEqual(rem.call_count, 2)                       # curiosity + belief-revision candidate
        self.assertTrue(any("reflection_questions" in s for s, _ in oc.sql))
        self.assertTrue(any("expired_unresolvable" in s for s, _ in oc.sql))

    def test_predict_with_no_llm_output_writes_nothing(self):
        oc = _Cur(lambda s, p: [])
        with patch.object(pr, "gather_signals", return_value={}), patch.object(pr, "build_predict_prompt", return_value="p"), \
                patch.object(pr, "llm", return_value=""), _q():
            self.assertEqual(pr.do_predict(oc, None), [])
        self.assertEqual(oc.sql, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_predictions.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--mode", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_predictions"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
