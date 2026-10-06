#!/usr/bin/env python3
"""Tests for nova_wazuh_daily_summary.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The module reads the indexer password from Keychain AT IMPORT, so it is loaded with subprocess.run patched
to a fake `security` (the real Keychain is never read). The Wazuh indexer (os_query) and nova_notify are
mocked at module load."""
import base64
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_wazuh_daily_summary.py"
SRC = SCRIPT.read_text()


def _fake_security(pw):
    def run(argv, **kw):
        assert argv[0] == "security", argv
        return subprocess.CompletedProcess(argv, 0 if pw else 44, pw + "\n" if pw else "", "")
    return run


def _load(pw="fixture-indexer-pw"):
    spec = importlib.util.spec_from_file_location("wazuh_summary_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(subprocess, "run", _fake_security(pw)):
        spec.loader.exec_module(mod)
    return mod


wz = _load()
# module-level stubs: no indexer HTTP, no notifications
wz.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=wz.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))
wz.notify = MagicMock()
wz.log = MagicMock()


def _answers(levels=None, vulns=None, sca=None, active=7, rootcheck=0, fim=10):
    levels = levels if levels is not None else {3: 900, 5: 50}
    vulns = vulns if vulns is not None else {"High": 2, "Low": 4}
    sca = sca if sca is not None else {"nova-core": 80, "mac": 60}
    def q(index, body):
        aggs = body.get("aggs", {})
        if index.startswith("wazuh-monitoring"):
            return {"aggregations": {"active": {"agents": {"value": active}}}}
        if index.startswith("wazuh-states-vulnerabilities"):
            return {"aggregations": {"by_severity": {"buckets": [{"key": k, "doc_count": v} for k, v in vulns.items()]}}}
        if "rootcheck" in aggs:
            return {"aggregations": {"rootcheck": {"count": {"value": rootcheck}}, "fim": {"count": {"value": fim}}}}
        if "by_agent" in aggs and "latest" in aggs["by_agent"].get("aggs", {}):
            return {"aggregations": {"by_agent": {"buckets": [
                {"key": a, "latest": {"hits": {"hits": [{"_source": {"data": {"sca": {"score": s}}}}]}}}
                for a, s in sca.items()]}}}
        lv = {k: v for k, v in levels.items() if k >= 8} if "must" in json.dumps(body.get("query", {})) else levels
        return {"hits": {"total": {"value": sum(lv.values())}},
                "aggregations": {"by_level": {"buckets": [{"key": k, "doc_count": v} for k, v in lv.items()]},
                                 "top_rules": {"buckets": [{"key": "sshd brute force", "doc_count": 3}] if lv else []},
                                 "by_agent": {"buckets": [{"key": "nova-core", "doc_count": 1200}]},
                                 "auth_failed": {"count": {"value": 4}}, "auth_success": {"count": {"value": 1500}}}}
    return q


class TestSecurity(unittest.TestCase):
    def test_password_comes_from_keychain_not_source(self):
        self.assertEqual(base64.b64decode(wz.WAZUH_CREDS).decode(), "admin:fixture-indexer-pw")
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-wazuh-indexer-password"', SRC)

    def test_keychain_miss_falls_back_to_docker_default(self):
        # SECURITY GAP (reported, not changed): with no Keychain/shim entry the module falls back to the
        # Wazuh docker demo default password rather than failing closed. This test pins that behaviour.
        m = _load(pw="")
        self.assertEqual(base64.b64decode(m.WAZUH_CREDS).decode(), "admin:SecretPassword")

    def test_basic_auth_header_and_indexer_target(self):
        cm = MagicMock(); cm.read.return_value = b"{}"
        with patch.object(wz.urllib.request, "urlopen", return_value=cm) as m:
            wz.os_query("wazuh-alerts-*", {"size": 0})
        req = m.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), f"Basic {wz.WAZUH_CREDS}")
        self.assertTrue(req.full_url.startswith("https://192.168.1.2:9200/"))


class TestPerformance(unittest.TestCase):
    def test_build_summary_with_large_aggregations(self):
        levels = {i % 16: 10_000 for i in range(16)}
        t0 = time.perf_counter()
        with patch.object(wz, "os_query", side_effect=_answers(levels=levels)):
            s = wz.build_summary()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("160,000 total", s)


class TestRetry(unittest.TestCase):
    def test_indexer_down_every_dimension_fails_to_default(self):
        # RETRY GAP: os_query — one attempt per dimension, no retry; each scorer returns its neutral default
        self.assertEqual(wz.score_agent_coverage(), (0, 0))
        self.assertEqual(wz.score_vulnerabilities()[0], 50)
        self.assertEqual(wz.score_compliance(), (50, {}))
        self.assertEqual(wz.score_threat_activity(), (100, {}))
        self.assertEqual(wz.score_rootkit_fim()[0], 80)
        self.assertIsNone(wz.get_alert_summary())

    def test_main_still_posts_when_indexer_down(self):
        wz.notify.reset_mock()
        wz.main()
        wz.notify.assert_called_once()
        self.assertIn("0/7 reporting", wz.notify.call_args[1]["body"])


class TestUnit(unittest.TestCase):
    def test_vulnerability_penalty(self):
        with patch.object(wz, "os_query", side_effect=_answers(vulns={"Critical": 5, "High": 10, "Medium": 7, "Low": 8})):
            score, d = wz.score_vulnerabilities()
        self.assertEqual((score, d["total"]), (11, 30))          # penalty 5*10 + 10*3 + 7*1 + 8*0.25 = 89
        with patch.object(wz, "os_query", side_effect=_answers(vulns={"High": 2, "Low": 4})):
            self.assertEqual(wz.score_vulnerabilities()[0], 93)

    def test_threat_and_rootkit_scoring(self):
        with patch.object(wz, "os_query", side_effect=_answers(levels={12: 1, 10: 1, 8: 5})):
            score, d = wz.score_threat_activity()
        self.assertEqual((score, d["critical"], d["high"], d["elevated"]), (45, 1, 1, 5))
        with patch.object(wz, "os_query", side_effect=_answers(rootcheck=200, fim=200_000)):
            self.assertEqual(wz.score_rootkit_fim()[0], 40)

    def test_coverage_caps_at_100(self):
        with patch.object(wz, "os_query", side_effect=_answers(active=9)):
            self.assertEqual(wz.score_agent_coverage(), (100, 9))
        self.assertAlmostEqual(sum(wz.WEIGHTS.values()), 1.0)


class TestIntegration(unittest.TestCase):
    def test_composite_combines_all_dimensions(self):
        with patch.object(wz, "os_query", side_effect=_answers()):
            s = wz.build_summary()
        self.assertIn("Agent Coverage: 100/100 (7/7 reporting)", s)
        self.assertIn("Compliance (SCA): 70/100 (nova-core:80%, mac:60%)", s)
        self.assertIn("Auth: 1,500 success, 4 failed", s)
        self.assertRegex(s, r"Security Posture: \d+/100 — (Strong|Moderate)")


class TestFunctional(unittest.TestCase):
    def test_main_posts_one_deduped_notification(self):
        wz.notify.reset_mock()
        with patch.object(wz, "os_query", side_effect=_answers(levels={12: 2, 3: 10})):
            wz.main()
        args, kw = wz.notify.call_args
        self.assertTrue(args[0].startswith("Wazuh SIEM Daily Summary"))
        self.assertNotIn(":shield:", args[0])
        self.assertEqual((kw["category"], kw["dedup_key"]), ("security", "wazuh-daily-summary"))
        self.assertIn("sshd brute force", kw["body"])

    def test_low_score_graded_at_risk(self):
        bad = _answers(active=0, vulns={"Critical": 20}, sca={"x": 10}, levels={12: 5}, rootcheck=100)
        with patch.object(wz, "os_query", side_effect=bad):
            self.assertIn("At Risk", wz.build_summary())


class TestFrame(unittest.TestCase):
    def test_import_with_stubbed_keychain_never_runs_main(self):
        # no --help/--selftest, and import reads Keychain — so the child process stubs subprocess.run first
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import subprocess, runpy, sys\n"
                "subprocess.run = lambda a, **k: subprocess.CompletedProcess(a, 0, 'x\\n', '')\n"
                "m = runpy.run_path(sys.argv[1], run_name='wazuh_smoke')\n"
                "print(m['EXPECTED_AGENTS'])\n")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "7")


if __name__ == "__main__":
    unittest.main()
