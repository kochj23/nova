#!/usr/bin/env python3
"""Tests for nova_alert_triage.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame), plus the original evidence-stage class (apply_evidence + annotation shape).
triage() opens PG and calls an LLM: both are mocked here. Written by Jordan Koch (via Claude)."""
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
import urllib.request
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("tri", SCRIPTS / "nova_alert_triage.py")
tri = importlib.util.module_from_spec(spec); spec.loader.exec_module(tri)
SRC = (SCRIPTS / "nova_alert_triage.py").read_text()
FAULT = {"verdict": "detector_fault", "note": "queried name ends in .net"}


class TestApplyEvidence(unittest.TestCase):
    def test_fault_on_warning_suppresses(self):
        v, conf, dec, reason = tri.apply_evidence(FAULT, hard=False, level="warning")
        self.assertEqual((v, dec), ("detector_fault", "suppress"))
        self.assertGreaterEqual(conf, 0.9)
        self.assertIn(".net", reason)

    def test_safety_contract_hard_and_critical_still_page(self):
        self.assertIsNone(tri.apply_evidence(FAULT, hard=True, level="warning"))
        self.assertIsNone(tri.apply_evidence(FAULT, hard=False, level="critical"))

    def test_supported_or_missing_evidence_changes_nothing(self):
        self.assertIsNone(tri.apply_evidence({"verdict": "supported"}, False, "warning"))
        self.assertIsNone(tri.apply_evidence(None, False, "warning"))
        self.assertIsNone(tri.apply_evidence("junk", False, "warning"))

    def test_verdict_registered_and_prompt_carries_evidence(self):
        self.assertIn("detector_fault", tri._VERDICTS)
        self.assertIn("EVIDENCE:", SRC)
        self.assertIn('"next_action"', SRC)
        self.assertIn("🛠 Do:", SRC)

    def test_evidence_runs_before_the_llm(self):
        self.assertLess(SRC.index("nova_evidence_check.check("), SRC.index("raw = llm("))


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
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
    def __init__(self, cur): self._cur = cur; self.autocommit = False

    def cursor(self, *a, **k): return self._cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _evidence_module(result=None, raise_=None):
    """A stand-in nova_evidence_check so triage() never touches the real evidence path."""
    m = types.ModuleType("nova_evidence_check")

    def check(oc, **kw):
        if raise_:
            raise raise_
        return result
    m.check = check
    return m


class _Triage:
    """Run triage() offline: PG, evidence, recall and the LLM all mocked; collects what was logged."""
    def __init__(self, llm_out="", evidence=None, evidence_error=None, backoff=None, similar=(), changes=()):
        self.cur = _Cur([("decision='page'", backoff), ("FROM claude_actions", [(c,) for c in changes])])
        self.llm_calls = []
        self._llm_out = llm_out
        self._ev = _evidence_module(evidence, evidence_error)
        self._similar = [{"id": i, "text": t} for i, t in enumerate(similar, 1)]

    def __enter__(self):
        def llm(prompt, **k):
            self.llm_calls.append(prompt); return self._llm_out
        self._stack = ExitStack()
        for cm in (patch.object(tri.psycopg2, "connect", lambda *a, **k: _Conn(self.cur)),
                   patch.dict(sys.modules, {"nova_evidence_check": self._ev}),
                   patch.object(tri, "llm", llm),
                   patch.object(tri, "_recall", lambda q, n=4, source=None: self._similar if source == "incident" else []),
                   redirect_stdout(io.StringIO())):
            self._stack.enter_context(cm)
        return self

    def __exit__(self, *a):
        self._stack.close()

    def logged(self):
        rows = self.cur.executed("INSERT INTO alert_triage_log")
        return rows[0][1] if rows else None


BENIGN = json.dumps({"verdict": "learned_normal", "confidence": 0.9, "likely_cause": "rack idles warm",
                     "reason": "matches baseline", "next_action": ""})
REAL = json.dumps({"verdict": "real_actionable", "confidence": 0.8, "likely_cause": "disk", "reason": "new",
                   "next_action": "check df"})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        writes = [m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)]
        self.assertEqual(writes, ["INSERT INTO alert_triage_log"])

    def test_hard_critical_regex_covers_the_contract(self):
        for t in ("nightly backup failed", "data loss on pg", "primary is down", "no standby left",
                  "split-brain detected", "exposed secret in repo", "all hosts down"):
            self.assertTrue(tri._HARD_CRITICAL.search(t), t)
        self.assertFalse(tri._HARD_CRITICAL.search("replica fenced for maintenance"))

    def test_hard_critical_always_pages_whatever_the_model_says(self):
        with _Triage(llm_out=BENIGN) as t:
            d = tri.triage("Nightly backup failed", level="warning", category="backup")
        self.assertEqual((d["decision"], d["verdict"], d["confidence"], d["hard_override"]), ("page", "real_actionable", 1.0, True))
        self.assertEqual(t.llm_calls, [])          # the model is never even consulted
        self.assertTrue(t.logged()[11])            # hard_override column


class TestPerformance(unittest.TestCase):
    def test_hard_critical_and_evidence_gate_fast_on_10k(self):
        texts = [f"task 'job{i}' is STALE on host{i % 50}" for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(1 for x in texts if tri._HARD_CRITICAL.search(x))
        for _ in range(10_000):
            tri.apply_evidence(FAULT, hard=False, level="warning")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(hits, 0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def flaky(req, timeout=45):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "{}"}})
        with patch.object(urllib.request, "urlopen", flaky):
            self.assertEqual(tri.llm("p"), "{}")
        self.assertEqual(calls, [n + "/api/chat" for n in tri.OLLAMA_NODES[:3]])

    def test_llm_all_down_fails_open_to_a_page(self):
        def boom(*a, **k):
            raise OSError("down")
        with patch.object(urllib.request, "urlopen", boom):
            self.assertEqual(tri.llm("p"), "")
        with _Triage(llm_out="") as t:
            d = tri.triage("Disk 91% on nova-core", level="warning")
        self.assertEqual(d["decision"], "page")
        self.assertIn("triage parse failed", d["reason"])

    def test_recall_and_backoff_fail_open(self):
        # RETRY GAP: _recall — one GET, errors become [] (triage proceeds without context)
        # RETRY GAP: _backoff_status — one read, errors return (False, "") so the caller pages
        def boom(*a, **k):
            raise OSError("down")
        with patch.object(urllib.request, "urlopen", boom):
            self.assertEqual(tri._recall("q"), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tri._backoff_status(_Cur([("decision='page'", RuntimeError("pg"))]), "k"), (False, ""))
            self.assertEqual(tri._recent_changes(_Cur([("FROM claude_actions", RuntimeError("pg"))])), [])


class TestUnit(unittest.TestCase):
    def test_backoff_ladder(self):
        self.assertEqual(tri._backoff_status(_Cur(), None), (False, ""))
        self.assertEqual(tri._backoff_status(_Cur([("decision='page'", (0, None))]), "k"), (False, ""))
        sup, why = tri._backoff_status(_Cur([("decision='page'", (1, 600))]), "k")
        self.assertTrue(sup); self.assertIn("window 1h", why); self.assertIn("paged 10m ago", why)
        sup, why = tri._backoff_status(_Cur([("decision='page'", (2, 5000))]), "k")
        self.assertTrue(sup); self.assertIn("window 4h", why)
        self.assertEqual(tri._backoff_status(_Cur([("decision='page'", (2, 15000))]), "k"), (False, ""))   # 4h elapsed
        self.assertEqual(tri._backoff_status(_Cur([("decision='page'", (9, 90000))]), "k"), (False, ""))   # ladder caps at 24h
        self.assertLessEqual(len(why), 200)

    def test_recent_changes_shape(self):
        self.assertEqual(tri._recent_changes(_Cur([("FROM claude_actions", [("10:00 restart",), ("09:00 failback",)])])),
                         ["10:00 restart", "09:00 failback"])

    def test_verdict_outside_the_set_is_coerced_to_real(self):
        with _Triage(llm_out=json.dumps({"verdict": "whatever", "confidence": 0.99})) as t:
            d = tri.triage("x is odd", level="warning")
        self.assertEqual((d["verdict"], d["decision"]), ("real_actionable", "page"))


class TestIntegration(unittest.TestCase):
    def test_evidence_runs_first_and_suppresses_a_detector_fault_without_the_llm(self):
        ev = {"verdict": "detector_fault", "note": "queried name ends in .net", "raw": [{"message": "client 1.2.3.4 query x.net"}],
              "who": {"192.168.1.43": "Amys-iPhone"}, "advice": "fix the TLD list"}
        with _Triage(llm_out=BENIGN, evidence=ev) as t:
            d = tri.triage("Suspicious DNS", level="warning", category="suspicious_dns", source="syslog", dedup_key="k1")
        self.assertEqual((d["decision"], d["verdict"], d["confidence"]), ("suppress", "detector_fault", 0.95))
        self.assertEqual(t.llm_calls, [])
        self.assertIn("🧾 Evidence: client 1.2.3.4 query x.net · 192.168.1.43 = Amys-iPhone", d["annotation"])
        self.assertIn("🛠 Do: fix the TLD list", d["annotation"])
        row = t.logged()
        self.assertEqual((row[0], row[4], row[5], row[7]), ("Suspicious DNS", "k1", "detector_fault", "suppress"))

    def test_evidence_failure_falls_through_to_the_llm_with_context(self):
        with _Triage(llm_out=BENIGN, evidence_error=RuntimeError("no evidence"), similar=("last time it was the fan",),
                     changes=("10:00 restarted snmp",)) as t:
            d = tri.triage("Rack temp high", level="warning", dedup_key="k2")
        self.assertEqual(len(t.llm_calls), 1)
        self.assertIn("(no source rows / no re-check for this category)", t.llm_calls[0])
        self.assertIn("last time it was the fan", t.llm_calls[0])
        self.assertIn("10:00 restarted snmp", t.llm_calls[0])
        self.assertEqual(d["decision"], "suppress")       # learned_normal @0.9 on a warning
        self.assertEqual(d["similar"], ["1"])
        self.assertIn("Similar past incident", d["annotation"])

    def test_evidence_fault_on_critical_still_pages_but_is_annotated(self):
        ev = {"verdict": "detector_fault", "note": "rule bug", "raw": [], "who": {}, "advice": "fix rule"}
        with _Triage(llm_out=REAL, evidence=ev) as t:
            d = tri.triage("Something critical", level="critical")
        self.assertEqual(d["decision"], "page")
        self.assertEqual(len(t.llm_calls), 1)
        self.assertIn("🛠 Do: fix rule", d["annotation"])  # evidence advice beats the model's next_action


class TestFunctional(unittest.TestCase):
    def test_cli_golden_path_prints_the_decision_json(self):
        buf = io.StringIO()
        with _Triage(llm_out=REAL) as t, patch.object(sys, "argv", ["x", "Disk 91% on nova-core", "--level", "warning", "--category", "disk"]):
            with redirect_stdout(buf):
                rc = tri.main()
        self.assertEqual(rc, 0)
        d = json.loads(buf.getvalue())
        self.assertEqual((d["decision"], d["verdict"], d["likely_cause"]), ("page", "real_actionable", "disk"))
        self.assertIn("🛠 Do: check df", d["annotation"])
        self.assertEqual(t.logged()[2], "disk")

    def test_rule_a_info_never_pages(self):
        with _Triage(llm_out=REAL) as t:
            d = tri.triage("Scheduler heartbeat", level="info")
        self.assertEqual(d["decision"], "downgrade")
        self.assertTrue(d["reason"].startswith("info→feed (RULE A)"))

    def test_rule_b_backoff_suppresses_a_repeat_page(self):
        with _Triage(llm_out=REAL, backoff=(1, 600)) as t:
            d = tri.triage("Disk 91% on nova-core", level="warning", dedup_key="disk:nova-core")
        self.assertEqual(d["decision"], "suppress")
        self.assertTrue(d["reason"].startswith("backoff: paged 10m ago"))

    def test_critical_downgrade_becomes_warning_and_log_write_failure_is_non_fatal(self):
        with _Triage(llm_out=json.dumps({"verdict": "expected_change", "confidence": 0.9})) as t:
            t.cur.answers.append(("INSERT INTO alert_triage_log", RuntimeError("log table gone")))
            d = tri.triage("Replica lag", level="critical")
        self.assertEqual((d["decision"], d["level"]), ("page", "critical"))   # critical always pages
        with _Triage(llm_out=json.dumps({"verdict": "expected_change", "confidence": 0.9})) as t:
            d = tri.triage("Replica lag", level="warning")
        self.assertEqual((d["decision"], d["level"]), ("downgrade", "warning"))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_alert_triage.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--level", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(tri.__name__, "tri")


if __name__ == "__main__":
    unittest.main()
