#!/usr/bin/env python3
"""Tests for nova_agent_sentinel.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_agent_sentinel.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_agent_sentinel_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sen = _load()
GOOD_CFG = {"agents": {"defaults": {"model": {"primary": "ollama/nova:latest"}},
                       "list": [{"id": "research", "model": "openrouter/qwen/qwen3-235b-a22b-2507"}]},
            "channels": {"modelByChannel": {}, "signal": {"dmPolicy": "allowlist", "groupPolicy": "allowlist"}}}


def _run(coro):
    return asyncio.run(coro)


def _agent():
    a = sen.SecuritySentinel.__new__(sen.SecuritySentinel)  # skip redis client in __init__
    a.infer = AsyncMock(return_value='{"risk_level": "low", "summary": "fine"}')
    a.notify = AsyncMock()
    a.report_to_jordan = AsyncMock()
    return a


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ps = [patch.object(sen, "log"),
                   patch.object(sen.urllib.request, "urlopen", side_effect=OSError("offline")),
                   patch.object(sen, "Path", side_effect=lambda p: Path(self.tmp.name) / "absent.log")]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def privacy(self, cfg, lsof=""):
        cur = MagicMock()
        cur.fetchone.return_value = (json.dumps(cfg),) if cfg is not None else None
        conn = MagicMock(cursor=MagicMock(return_value=cur))
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=lsof if args[0] == "lsof" else "123", stderr="")
        a = _agent()
        with patch("psycopg2.connect", return_value=conn), patch("subprocess.run", side_effect=fake_run):
            res = _run(a.handle({"type": "privacy_monitor"}))
        return a, res, cur, runs


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{12,}['\"]")

    def test_cloud_model_on_non_research_agent_is_a_violation(self):
        cfg = json.loads(json.dumps(GOOD_CFG))
        cfg["agents"]["list"].append({"id": "chat", "model": "openrouter/qwen/qwen3-235b-a22b-2507"})
        a, res, _, _ = self.privacy(cfg)
        self.assertEqual(res["risk_level"], "critical")
        self.assertTrue(any("agent[chat] using OpenRouter" in v for v in res["violations"]))
        a.report_to_jordan.assert_awaited_once()

    def test_open_signal_policy_flagged(self):
        cfg = json.loads(json.dumps(GOOD_CFG))
        cfg["channels"]["signal"]["dmPolicy"] = "open"
        _, res, _, _ = self.privacy(cfg)
        self.assertTrue(any("Signal dmPolicy" in v for v in res["violations"]))

    def test_unexpected_outbound_host_warned(self):
        lsof = ("openclaw 1 u TCP 10.0.0.2:5000->203.0.113.9:443 (ESTABLISHED)\n"
                "openclaw 1 u TCP 10.0.0.2:5001->slack.com:443 (ESTABLISHED)\n")
        _, res, _, _ = self.privacy(GOOD_CFG, lsof)
        self.assertEqual(res["risk_level"], "medium")
        self.assertEqual([w for w in res["warnings"] if "outbound" in w],
                         ["Unexpected outbound connection: 203.0.113.9"])


class TestPerformance(unittest.TestCase):
    def test_parse_response_10k(self):
        a = _agent()
        resp = "<think>" + "x" * 200 + "</think>" + json.dumps({"risk_level": "low", "findings": []})
        t0 = time.perf_counter()
        for _ in range(10_000):
            a._parse_response(resp)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_infer_failure_fails_open(self):
        # RETRY GAP: _analyze_unifi/_analyze_camera/infer — one attempt, None on failure, no alert
        a = _agent()
        a.infer = AsyncMock(side_effect=TimeoutError("slow"))
        self.assertIsNone(_run(a.handle({"type": "unifi_event", "event": "x"})))
        self.assertIsNone(_run(a.handle({"type": "camera_alert", "smart_types": ["person"]})))
        self.assertEqual(a.infer.await_count, 2)
        a.report_to_jordan.assert_not_awaited()

    def test_pg_failure_becomes_warning(self):
        # RETRY GAP: _privacy_monitor/psycopg2.connect — one attempt, degrades to a warning
        a = _agent()
        with patch("psycopg2.connect", side_effect=OSError("pg down")), \
             patch("subprocess.run", side_effect=OSError("no lsof")):
            res = _run(a.handle({"type": "privacy_monitor"}))
        self.assertEqual(res["risk_level"], "medium")
        self.assertTrue(any("pg down" in w for w in res["warnings"]))


class TestUnit(unittest.TestCase):
    def test_parse_response_strips_think(self):
        a = _agent()
        self.assertEqual(a._parse_response('<think>{"no": 1}</think> {"risk_level": "high"}'), {"risk_level": "high"})

    def test_parse_response_garbage(self):
        r = _agent()._parse_response("not json at all")
        self.assertEqual(r["risk_level"], "unknown")
        self.assertFalse(r["flag_jordan"])

    def test_vehicle_only_camera_ignored(self):
        a = _agent()
        self.assertIsNone(_run(a.handle({"type": "camera_alert", "smart_types": ["vehicle", "licensePlate"]})))
        a.infer.assert_not_awaited()

    def test_empty_inputs_return_none(self):
        a = _agent()
        self.assertIsNone(_run(a.handle({"type": "threat_assessment", "signals": []})))
        self.assertIsNone(_run(a.handle({"type": "something", "text": ""})))


class TestIntegration(_Base):
    def test_config_read_from_nova_documents(self):
        _, res, cur, runs = self.privacy(GOOD_CFG)
        sql = cur.execute.call_args[0][0]
        self.assertIn("FROM nova_documents", sql)
        self.assertIn("openclaw.json", sql)
        self.assertEqual(res["risk_level"], "none")
        self.assertEqual(runs[0][0], "pgrep")

    def test_report_routing(self):
        a = _agent()
        _run(a._report_security({"risk_level": "low", "summary": "s"}, "ctx"))
        _run(a._report_security({"risk_level": "critical"}, "ctx"))
        a.notify.assert_awaited_once()
        a.report_to_jordan.assert_awaited_once()
        self.assertIn("CRITICAL", a.report_to_jordan.await_args[0][0])


class TestFunctional(_Base):
    def test_nmap_golden_path_reports(self):
        a = _agent()
        a.infer = AsyncMock(return_value='{"risk_level": "high", "findings": [{"description": "rogue ssh"}]}')
        res = _run(a.handle({"type": "nmap_scan", "devices": [{"ip": "192.168.1.5", "open_ports": [22]}]}))
        self.assertEqual(res["risk_level"], "high")
        self.assertIn("192.168.1.5", a.infer.await_args[0][0])
        self.assertIn("rogue ssh", a.report_to_jordan.await_args[0][0])

    def test_nmap_no_devices_returns_none(self):
        a = _agent()
        self.assertIsNone(_run(a.handle({"type": "nmap_scan"})))
        a.infer.assert_not_awaited()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_daemon(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_agent_sentinel; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
