#!/usr/bin/env python3
"""Tests for nova_notifier.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_notifier.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_FEED, cfg.SLACK_ALERTS, cfg.SLACK_DIGEST, cfg.SLACK_EMAIL = "C_FEED", "C_ALERTS", "C_DIGEST", "C_EMAIL"
    cfg.post_both = MagicMock()
    corr = types.ModuleType("nova_correlator")
    corr.correlate = MagicMock(return_value={"action": "standalone", "suppress": False, "incident_id": None})
    corr.llm_summarize = MagicMock(return_value=("summary", "qwen"))
    rem = types.ModuleType("nova_remediation"); rem.propose_for_incident = MagicMock()
    maint = types.ModuleType("nova_maintenance"); maint.SECURITY_CATEGORIES = frozenset({"security"}); maint.is_active = lambda: False
    return {"nova_config": cfg, "nova_correlator": corr, "nova_remediation": rem, "nova_maintenance": maint}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):           # the daemon binds its stubs at import; sys.modules is restored after
        spec.loader.exec_module(mod)
    return mod


nf = _load("nf", SCRIPT)


class _Cur:
    """RealDictCursor stand-in: `events` are the 'new' rows; `prior` answers the dedup lookup."""
    def __init__(self, events, prior=None):
        self.events = events; self.prior = prior; self.sql = []; self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def fetchall(self):
        return [dict(e) for e in self.events]

    def fetchone(self):
        return self.prior if "status='sent'" in self._last else None

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _ev(**kw):
    base = {"id": 1, "title": "UNAS storage low", "body": "1.4TB free", "level": "warning", "category": "storage",
            "source": "nova_unas.py", "dedup_key": "unas-low", "meta": None, "ts": 0}
    base.update(kw)
    return base


def _triage_stub(result=None, exc=None, hang=0.0):
    mod = types.ModuleType("nova_alert_triage")

    def triage(*a, **k):
        if hang:
            time.sleep(hang)
        if exc:
            raise exc
        return result
    mod.triage = triage
    return mod


def _drain(events, prior=None, triage=None, verbose=False):
    cur = _Cur(events, prior)
    nf.nova_config.post_both = MagicMock()
    nf.nova_correlator.correlate = MagicMock(return_value={"action": "standalone", "suppress": False, "incident_id": None})
    nf.nova_correlator.llm_summarize = MagicMock(return_value=("summary", "qwen"))
    nf.nova_remediation.propose_for_incident = MagicMock()
    with patch.object(nf, "_connect", lambda: _Conn(cur)), patch.dict(sys.modules, {"nova_alert_triage": triage or _triage_stub(None)}), \
         patch.object(nf, "_mesh_relay", MagicMock()) as mesh, redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        n = nf.drain(verbose=verbose)
    return n, cur, mesh, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", nf.DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute\([^)]*%\s*\(', SRC))
        n, cur, _, _ = _drain([_ev(dedup_key="k'; DROP TABLE telemetry.events; --")])
        for sql, params in cur.sql:
            self.assertNotIn("DROP", sql)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.events", "telemetry.incidents"})

    def test_mesh_relay_truncates_and_never_raises(self):
        with patch("urllib.request.urlopen") as u:
            nf._mesh_relay("t" * 150, "b" * 150)
        self.assertEqual(len(json.loads(u.call_args[0][0].data)["text"]), 200)


class TestPerformance(unittest.TestCase):
    def test_policy_functions_fast_on_10k_events(self):
        evs = [_ev(id=i, level=("info", "warning", "critical")[i % 3], category=("syslog", "email", "x")[i % 3],
                   meta=json.dumps({"dedup_window_s": i}) if i % 2 else None) for i in range(10_000)]
        t0 = time.perf_counter()
        for e in evs:
            nf._route(e["level"], e["category"]); nf._dedup_window(e); nf._fmt(e)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_triage_fails_open_on_exception_timeout_and_junk(self):
        # RETRY GAP: _triage_event — one attempt; any failure returns None so the alert posts exactly as before triage existed
        with patch.dict(sys.modules, {"nova_alert_triage": _triage_stub(exc=RuntimeError("brain down"))}):
            self.assertIsNone(nf._triage_event(_ev()))
        with patch.dict(sys.modules, {"nova_alert_triage": _triage_stub(result={"decision": "page"}, hang=1.0)}), patch.object(nf, "_TRIAGE_TIMEOUT_S", 0.2):
            t0 = time.perf_counter()
            self.assertIsNone(nf._triage_event(_ev()))
            self.assertLess(time.perf_counter() - t0, 0.8)
        with patch.dict(sys.modules, {"nova_alert_triage": _triage_stub(result={"decision": "page", "verdict": "x"})}):
            self.assertEqual(nf._triage_event(_ev())["decision"], "page")
        n, cur, _, _ = _drain([_ev()], triage=_triage_stub(result="junk"))
        self.assertEqual(n, 1)                                           # junk verdict -> posted normally

    def test_mesh_and_db_failures_are_swallowed(self):
        # RETRY GAP: _mesh_relay — best effort, one attempt
        with patch("urllib.request.urlopen", side_effect=OSError("radio off")), redirect_stderr(io.StringIO()):
            nf._mesh_relay("t", None)
        with patch.object(nf, "_connect", MagicMock(side_effect=OSError("no pg"))), redirect_stderr(io.StringIO()):
            self.assertEqual(nf.drain(), 0)

    def test_slack_failure_marks_error_and_continues(self):
        # RETRY GAP: nova_config.post_both — one attempt; the row goes to status='error' instead of being lost
        cur = _Cur([_ev(id=1), _ev(id=2, dedup_key="other")])
        nf.nova_config.post_both = MagicMock(side_effect=[OSError("slack 500"), True])
        with patch.object(nf, "_connect", lambda: _Conn(cur)), patch.dict(sys.modules, {"nova_alert_triage": _triage_stub(None)}), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(nf.drain(), 1)
        self.assertEqual(cur.ran("SET status='error'")[0][1], (1,))
        self.assertEqual(cur.ran("SET status='sent'")[0][1], ("C_ALERTS", 2))


class TestUnit(unittest.TestCase):
    def test_route(self):
        self.assertEqual(nf._route("info", "syslog"), "C_DIGEST")
        self.assertEqual(nf._route("warning", "syslog"), "C_ALERTS")       # a warning in a digest category still alerts
        self.assertEqual(nf._route("critical", "security_news"), "C_FEED")  # hard override wins over level
        self.assertEqual(nf._route("info", "email"), "C_EMAIL")
        self.assertEqual(nf._route("info", None), "C_FEED")
        self.assertEqual(nf._route("bogus", "x"), "C_FEED")

    def test_dedup_window(self):
        self.assertEqual(nf._dedup_window(_ev(level="info")), nf.DEDUP_WINDOW_S)
        self.assertEqual(nf._dedup_window(_ev(level="warning")), 86400)
        self.assertEqual(nf._dedup_window(_ev(meta={"dedup_window_s": 60})), 60)
        self.assertEqual(nf._dedup_window(_ev(meta='{"dedup_window_s": "120"}')), 120)
        self.assertEqual(nf._dedup_window(_ev(level="critical", meta="not json")), 86400)
        self.assertEqual(nf._dedup_window(_ev(level="info", meta={"dedup_window_s": "x"})), nf.DEDUP_WINDOW_S)

    def test_fmt(self):
        self.assertEqual(nf._fmt(_ev()), ":warning: *UNAS storage low*\n1.4TB free\n_storage · nova_unas.py_")
        self.assertEqual(nf._fmt(_ev(level="critical", body=None, category=None, source=None)), ":rotating_light: *UNAS storage low*")


class TestIntegration(unittest.TestCase):
    def test_detector_fault_is_suppressed_before_correlation(self):
        t = _triage_stub(result={"verdict": "detector_fault", "decision": "suppress", "reason": "attester.apple-dns.net"})
        n, cur, mesh, out = _drain([_ev(level="critical", category="security")], triage=t, verbose=True)
        self.assertEqual(n, 0)
        sql, params = cur.ran("SET status='suppressed'")[0]
        self.assertIn("channel='detector-fault'", sql); self.assertEqual(params, (1,))
        nf.nova_correlator.correlate.assert_not_called()               # never opens an incident, never a qwen narrative
        nf.nova_config.post_both.assert_not_called(); mesh.assert_not_called()
        self.assertIn("detector-fault #1 [security] — attester.apple-dns.net", out)

    def test_other_suppress_verdicts_still_go_through_correlation(self):
        t = _triage_stub(result={"verdict": "chronic", "decision": "suppress", "reason": "r"})
        n, cur, _, _ = _drain([_ev()], triage=t)
        self.assertEqual(n, 0)
        nf.nova_correlator.correlate.assert_called_once()
        self.assertIn("channel='triage-suppressed'", cur.ran("SET status='suppressed'")[0][0])

    def test_downgrade_and_annotation_reach_the_feed(self):
        t = _triage_stub(result={"verdict": "benign", "decision": "downgrade", "annotation": "likely cause: DHCP renewal"})
        n, cur, _, _ = _drain([_ev()], triage=t)
        msg, kw = nf.nova_config.post_both.call_args[0][0], nf.nova_config.post_both.call_args[1]
        self.assertTrue(msg.startswith("(downgraded) :warning:")); self.assertIn("likely cause: DHCP renewal", msg)
        self.assertEqual(kw["slack_channel"], "C_FEED")
        self.assertEqual(cur.ran("SET status='sent'")[0][1], ("C_FEED", 1))

    def test_maintenance_window_mutes_security_only(self):
        with patch.object(nf, "_maint_active", lambda: True):
            n, cur, _, _ = _drain([_ev(id=1, category="security"), _ev(id=2, category="storage", dedup_key="s")])
        self.assertEqual(n, 1)
        self.assertEqual(cur.ran("channel='maintenance-muted'")[0][1], (1,))


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_and_marks_sent(self):
        n, cur, mesh, _ = _drain([_ev()])
        self.assertEqual(n, 1)
        self.assertEqual(cur.sql[0][0], "SELECT * FROM telemetry.events WHERE status='new' ORDER BY ts ASC LIMIT 200")
        self.assertEqual(cur.ran("make_interval")[0][1], ("unas-low", 86400))
        nf.nova_config.post_both.assert_called_once_with(nf._fmt(_ev()), slack_channel="C_ALERTS")
        self.assertEqual(cur.ran("SET status='sent'")[0][1], ("C_ALERTS", 1))
        mesh.assert_not_called()
        self.assertEqual(nf.nova_correlator.correlate.call_args[0][1], _ev())

    def test_critical_also_goes_over_the_mesh(self):
        n, cur, mesh, _ = _drain([_ev(level="critical")])
        mesh.assert_called_once_with("UNAS storage low", "1.4TB free")

    def test_dedup_collapses_a_repeat(self):
        n, cur, _, _ = _drain([_ev(id=9)], prior={"id": 4})
        self.assertEqual(n, 0)
        sql, params = cur.ran("collapsed_into")[0]
        self.assertEqual(params, (4, 4, 9))
        self.assertEqual(cur.ran("dispatch_count + 1")[0][1], (4,))
        nf.nova_config.post_both.assert_not_called()

    def test_missing_dedup_key_is_derived_and_persisted(self):
        n, cur, _, _ = _drain([_ev(dedup_key=None, title="Disk 97.5% full on nova-core3 after 12 days")])
        sql, params = cur.ran("SET dedup_key=%s")[0]
        self.assertEqual(params, ("auto:nova_unas.py:storage:Disk #full on nova-core#after #days", 1))

    def test_opened_incident_posts_a_summary_and_proposes_remediation(self):
        cur = _Cur([_ev(level="critical")])
        nf.nova_config.post_both = MagicMock()
        nf.nova_correlator.correlate = MagicMock(return_value={"action": "opened", "suppress": False, "incident_id": 55, "role": "root"})
        nf.nova_correlator.llm_summarize = MagicMock(return_value=("the disk died", "qwen3"))
        nf.nova_remediation.propose_for_incident = MagicMock()
        with patch.object(nf, "_connect", lambda: _Conn(cur)), patch.dict(sys.modules, {"nova_alert_triage": _triage_stub(None)}), \
             patch.object(nf, "_mesh_relay", MagicMock()):
            self.assertEqual(nf.drain(), 1)
        msg = nf.nova_config.post_both.call_args[0][0]
        self.assertTrue(msg.startswith(":rotating_light: *Incident #55: UNAS storage low*\nthe disk died"))
        self.assertIn("summary by qwen3", msg)
        nf.nova_remediation.propose_for_incident.assert_called_once()
        self.assertEqual(cur.ran("UPDATE telemetry.incidents SET slack_ts='posted'")[0][1], (55,))

    def test_symptom_folded_into_incident_is_not_posted(self):
        cur = _Cur([_ev()])
        nf.nova_config.post_both = MagicMock()
        nf.nova_correlator.correlate = MagicMock(return_value={"action": "folded", "suppress": True, "incident_id": 55, "role": "symptom"})
        with patch.object(nf, "_connect", lambda: _Conn(cur)), patch.dict(sys.modules, {"nova_alert_triage": _triage_stub(None)}):
            self.assertEqual(nf.drain(), 0)
        nf.nova_config.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_runs_and_import_never_starts_the_loop(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--daemon", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
