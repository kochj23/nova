#!/usr/bin/env python3
"""Tests for nova_correlator.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_correlator.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


co = _load("correlator_under_test", SCRIPT)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda *a, **k: cur)


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


EV = {"id": 10, "level": "warning", "category": "ollama", "title": "Ollama down", "body": "", "meta": {"host": "nova-core"}}
OPEN_GPU = [(1, "GPU wedged", "critical", 5, 3, [1.0, 0.0], "gpu")]
ROOT_ROW = ("Suspicious DNS", "client 192.168.1.43 query attester.gateway.icloud.com", "warning", "suspicious_dns",
            "nova_syslog_server.py", "syslog-threat-suspicious_dns-192.168.1.2", None)
EVIDENCE = types.SimpleNamespace(check=mock.MagicMock(return_value={"text": "RAW client 192.168.1.43 Amys-iPhone; RECHECK fails; VERDICT detector_fault"}))


def _summary_cur():
    return _Cur([("SELECT host, title, severity, member_count FROM telemetry.incidents WHERE id", ("nova-core", "Suspicious DNS", "warning", 3)),
                 ("FROM telemetry.events WHERE incident_id", [("warning", "suspicious_dns", "Suspicious DNS", "body text", "root")]),
                 ("JOIN telemetry.events e ON e.id = i.root_event", ROOT_ROW)])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r'execute\([^)]*%\s*\(')
        self.assertEqual(SRC.count("make_interval(secs => %s)"), 1)

    def test_llm_is_local_and_told_to_stay_inside_the_evidence(self):
        self.assertEqual(co.OLLAMA, "http://127.0.0.1:11434")
        self.assertIn("never assert compromise, exfiltration or malware", SRC)
        self.assertIn('"think": False', SRC)

    def test_meta_string_is_parsed_not_evaluated(self):
        self.assertEqual(co._host_of({"meta": '{"host": "nova-core"}'}), "nova-core")
        self.assertIsNone(co._host_of({"meta": "__import__('os')", "title": "", "body": ""}))


class TestPerformance(unittest.TestCase):
    def test_host_and_cosine_fast(self):
        evs = [{"title": f"thing {i} on 192.168.1.{i % 250}", "body": "x", "meta": None} for i in range(10_000)]
        a, b = [float(i % 7) for i in range(768)], [float(i % 5) for i in range(768)]
        t0 = time.perf_counter()
        hosts = [co._host_of(e) for e in evs]
        for _ in range(500):
            co._cosine(a, b)
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(hosts[7], "192.168.1.7")


class TestRetry(unittest.TestCase):
    def test_embed_fails_open_to_none(self):
        # RETRY GAP: _http/embed — one urlopen attempt, None on failure
        with mock.patch.object(co.urllib.request, "urlopen", side_effect=OSError("ollama down")) as uo:
            self.assertIsNone(co.embed("x"))
        self.assertEqual(uo.call_count, 1)

    def test_llm_summarize_falls_back_to_template(self):
        # RETRY GAP: llm_summarize — one chat attempt; failure stores the templated fallback
        cur = _summary_cur()
        with mock.patch.object(co, "_http", side_effect=OSError("ollama down")) as h, \
             mock.patch.dict(sys.modules, {"nova_evidence_check": EVIDENCE}):
            txt, model = co.llm_summarize(_conn(cur), 1)
        self.assertEqual(h.call_count, 1)
        self.assertIsNone(model)
        self.assertTrue(txt.startswith("Suspicious DNS on nova-core: 3 correlated events."))
        (sql, p), = cur.stmts("SET summary=%s WHERE id=%s")
        self.assertNotIn("llm_model", sql)
        self.assertEqual(p, (txt, 1))


class TestUnit(unittest.TestCase):
    def test_cosine_edges(self):
        self.assertEqual(co._cosine([], [1.0]), 0.0)
        self.assertEqual(co._cosine([1.0, 0.0], [1.0]), 0.0)
        self.assertEqual(co._cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertAlmostEqual(co._cosine([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_host_of_sources(self):
        self.assertEqual(co._host_of({"meta": {"host": "unas"}}), "unas")
        self.assertEqual(co._host_of({"title": "pg down on 10.0.0.9", "body": ""}), "10.0.0.9")
        self.assertEqual(co._host_of({"title": "", "body": "Office-M4-2 wedged"}), "Office-M4-2")
        self.assertIsNone(co._host_of({"title": "nothing here", "body": "", "meta": "not json"}))

    def test_topology_and_meta(self):
        self.assertTrue(co._is_symptom_of("host_down", "anything"))
        self.assertTrue(co._is_symptom_of("gpu", "ollama"))
        self.assertFalse(co._is_symptom_of("gpu", "backup"))
        self.assertFalse(co._is_symptom_of("unknown", "x"))
        self.assertTrue(co.is_meta_category("incident_recurring"))
        self.assertFalse(co.is_meta_category(None))

    def test_http_parses_json(self):
        uo = mock.MagicMock(return_value=_Resp({"embedding": [1, 2]}))
        with mock.patch.object(co.urllib.request, "urlopen", uo):
            self.assertEqual(co.embed("hello"), [1, 2])
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, co.OLLAMA + "/api/embeddings")
        self.assertEqual(json.loads(req.data)["model"], co.EMBED_MODEL)


class TestIntegration(unittest.TestCase):
    def test_summary_prompt_carries_the_evidence_block(self):
        cur = _summary_cur()
        EVIDENCE.check.reset_mock()
        h = mock.MagicMock(return_value={"message": {"content": "Root cause: detector fault (.ga substring). Symptoms: none real. Action: fix the rule."}})
        with mock.patch.object(co, "_http", h), mock.patch.dict(sys.modules, {"nova_evidence_check": EVIDENCE}):
            txt, model = co.llm_summarize(_conn(cur), 1)
        self.assertEqual(model, co.SUMMARY_MODEL)
        path, payload, timeout = h.call_args[0]
        self.assertEqual(path, "/api/chat")
        prompt = payload["messages"][1]["content"]
        self.assertIn("EVIDENCE for the root event (raw rows, who, history, re-check):", prompt)
        self.assertIn("VERDICT detector_fault", prompt)
        self.assertIn("Amys-iPhone", prompt)
        kw = EVIDENCE.check.call_args.kwargs
        self.assertEqual((kw["title"], kw["category"], kw["source"], kw["dedup_key"], kw["file_bug_row"]),
                         ("Suspicious DNS", "suspicious_dns", "nova_syslog_server.py", ROOT_ROW[5], False))
        (sql, p), = cur.stmts("SET summary=%s, llm_model=%s")
        self.assertEqual(p, (txt, co.SUMMARY_MODEL, 1))

    def test_evidence_failure_is_fail_open(self):
        cur = _summary_cur()
        boom = types.SimpleNamespace(check=mock.MagicMock(side_effect=RuntimeError("evidence down")))
        h = mock.MagicMock(return_value={"message": {"content": "<think>hmm</think>Root cause: X. Symptom: Y. Action: Z."}})
        with mock.patch.object(co, "_http", h), mock.patch.dict(sys.modules, {"nova_evidence_check": boom}):
            txt, model = co.llm_summarize(_conn(cur), 1)
        self.assertNotIn("EVIDENCE", h.call_args[0][1]["messages"][1]["content"])
        self.assertEqual(txt, "Root cause: X. Symptom: Y. Action: Z.")

    def test_meta_and_public_safety_never_open_incidents(self):
        cur = _Cur()
        for cat in ("incident_recurring", "incident", "traffic_watch", "chp"):
            r = co.correlate(_conn(cur), {**EV, "category": cat})
            self.assertEqual(r["action"], "standalone")
        self.assertEqual(cur.sql, [])


class TestFunctional(unittest.TestCase):
    def test_symptom_attaches_to_open_root_incident(self):
        cur = _Cur([("FROM telemetry.incidents i WHERE status='open'", OPEN_GPU)])
        with mock.patch.object(co, "embed", side_effect=AssertionError("topology must win before embedding")):
            r = co.correlate(_conn(cur), EV)
        self.assertEqual(r, {"action": "attached", "incident_id": 1, "role": "symptom", "suppress": True})
        self.assertEqual(cur.params[0], ("nova-core", co.CORRELATION_WINDOW_S))
        self.assertEqual(cur.stmts("member_count = member_count + 1")[0][1], ("warning", 1))
        self.assertEqual(cur.stmts("status='suppressed'")[0][1], (1, "symptom", 10))

    def test_semantic_match_then_fresh_incident(self):
        storage = [(2, "disk", "warning", 7, 1, [1.0, 0.0], "storage")]
        cur = _Cur([("FROM telemetry.incidents i WHERE status='open'", storage)])
        with mock.patch.object(co, "embed", return_value=[1.0, 0.0]):
            r = co.correlate(_conn(cur), EV)
        self.assertEqual((r["action"], r["incident_id"], r["role"]), ("attached", 2, "member"))
        cur = _Cur([("FROM telemetry.incidents i WHERE status='open'", storage), ("INSERT INTO telemetry.incidents", (9,))])
        with mock.patch.object(co, "embed", return_value=[0.0, 1.0]):
            r = co.correlate(_conn(cur), EV)
        self.assertEqual(r, {"action": "opened", "incident_id": 9, "role": "root", "suppress": False})
        (_, p), = cur.stmts("INSERT INTO telemetry.incidents")
        self.assertEqual(p, ("warning", "nova-core", "Ollama down", 10, [0.0, 1.0]))
        self.assertEqual(cur.stmts("corr_role='root'")[0][1], (9, 10))

    def test_info_and_hostless_events_are_standalone(self):
        cur = _Cur()
        self.assertEqual(co.correlate(_conn(cur), {**EV, "level": "info"})["action"], "standalone")
        self.assertEqual(co.correlate(_conn(cur), {**EV, "meta": None, "title": "vague", "body": ""})["action"], "standalone")
        self.assertEqual(cur.sql, [])

    def test_summary_error_paths(self):
        self.assertEqual(co.llm_summarize(_conn(_Cur()), 404), (None, None))
        cur = _summary_cur()
        with mock.patch.object(co, "_http", return_value={"message": {"content": "meh"}}), \
             mock.patch.dict(sys.modules, {"nova_evidence_check": EVIDENCE}):
            txt, model = co.llm_summarize(_conn(cur), 1)
        self.assertIsNone(model)
        self.assertIn("correlated events", txt)
        self.assertFalse(cur.stmts("SET summary"))          # a too-short reply stores nothing


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_correlator"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_library_module_has_no_entry_point(self):
        self.assertNotIn("__main__", SRC)
        self.assertNotIn("def main", SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("import must not call out")):
            m = _load("correlator_frame_probe", SCRIPT)
        self.assertTrue(callable(m.correlate))


if __name__ == "__main__":
    unittest.main()
