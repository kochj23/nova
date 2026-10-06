#!/usr/bin/env python3
"""Tests for nova_app_watchdog.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error    # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_app_watchdog.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_app_watchdog_test_"))


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("naw", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


aw = _load()
aw.STATE_FILE = TMP / "state.json"
aw.notify = MagicMock(return_value=True)
aw.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_NOTIFY="C_TEST")
aw.vector_remember = MagicMock()


def _reset():
    aw.STATE_FILE.unlink(missing_ok=True)
    aw.notify.reset_mock(); aw.vector_remember.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_restart_never_uses_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("os.system", SRC)
        with patch.object(aw.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stderr="")
            aw.restart_app("X", "Evil; rm -rf /")
        self.assertEqual(run.call_args[0][0], ["open", "-a", "Evil; rm -rf /"])   # argv, not a shell string

    def test_probes_are_loopback_only(self):
        with patch.object(aw.urllib.request, "urlopen", side_effect=urllib.error.URLError("x")) as uo:
            aw.check_port(37400)
        self.assertTrue(uo.call_args[0][0].full_url.startswith("http://127.0.0.1:"))


class TestPerformance(unittest.TestCase):
    def test_restart_budget_prune_on_10k_entries(self):
        now = time.time()
        state = {"restarts": [{"ts": now - (i % 7200)} for i in range(10_000)]}
        t0 = time.perf_counter()
        n = aw.count_recent_restarts(state)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(n, len(state["restarts"]))
        self.assertLess(n, 10_000)                 # entries older than an hour were pruned


class TestRetry(unittest.TestCase):
    def test_vector_remember_fails_open(self):
        # RETRY GAP: vector_remember — one POST, failure swallowed so a crash alert is never lost
        fresh = _load()
        with patch.object(fresh.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertIsNone(fresh.vector_remember("x"))
        self.assertEqual(uo.call_count, 1)

    def test_check_port_refused_is_one_shot_and_down(self):
        # RETRY GAP: check_port — a refused connection is one attempt; next cron cycle is the retry
        with patch.object(aw.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")) as uo:
            alive, info, _ = aw.check_port(1)
        self.assertFalse(alive); self.assertEqual(info, "connection refused"); self.assertEqual(uo.call_count, 1)

    def test_restart_exception_returns_false(self):
        with patch.object(aw.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(aw.restart_app("X", "X"))
            self.assertFalse(aw.restart_infra("Ollama", "open -a Ollama"))


class TestUnit(unittest.TestCase):
    def test_load_state_defaults_and_corrupt(self):
        _reset()
        self.assertEqual(aw.load_state(), {"apps": {}, "restarts": []})
        aw.STATE_FILE.write_text("{not json")
        self.assertEqual(aw.load_state(), {"apps": {}, "restarts": []})

    def test_check_port_http_error_on_root_counts_as_alive(self):
        calls = [ValueError("no status"), urllib.error.HTTPError("u", 404, "nf", {}, None)]
        with patch.object(aw.urllib.request, "urlopen", side_effect=calls):
            alive, info, _ = aw.check_port(1)
        self.assertTrue(alive); self.assertEqual(info, "responding")

    def test_check_infra_port(self):
        with patch.object(aw.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 500, "e", {}, None)):
            self.assertEqual(aw.check_infra_port(11434), (True, "responding"))
        with patch.object(aw.urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertEqual(aw.check_infra_port(11434), (False, "down"))


class TestIntegration(unittest.TestCase):
    def test_uses_shared_notify_bus_and_config(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("VECTOR_URL = nova_config.VECTOR_URL", SRC)
        self.assertNotIn("chat.postMessage", SRC)

    def test_state_round_trip(self):
        _reset()
        aw.save_state({"apps": {"1": {"alive": True}}, "restarts": []})
        self.assertEqual(aw.load_state()["apps"]["1"]["alive"], True)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset()
        self.p = [patch.object(aw, "MONITORED_APPS", [(37400, "NovaControl", "NovaControl", True)]),
                  patch.object(aw, "INFRA_SERVICES", []),
                  patch.object(aw, "capture_diagnostics", return_value="/tmp/diag"),
                  patch.object(aw.time, "sleep")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_down_critical_app_alerts_and_restarts_once(self):
        with patch.object(aw, "check_port", side_effect=[(False, "refused", 0.1), (True, "1.0", 0.1)]), \
             patch.object(aw, "restart_app", return_value=True) as ra:
            aw.main()
        ra.assert_called_once_with("NovaControl", "NovaControl")
        self.assertEqual(aw.notify.call_args.kwargs["level"], "critical")
        self.assertIn("confirmed back up", aw.notify.call_args.kwargs["body"])
        st = json.loads(aw.STATE_FILE.read_text())
        self.assertFalse(st["apps"]["37400"]["alive"])
        self.assertEqual(len(st["restarts"]), 1)

    def test_restart_budget_exhausted_alerts_without_restart(self):
        aw.save_state({"apps": {}, "restarts": [{"ts": time.time()}] * aw.MAX_RESTARTS_PER_HOUR})
        with patch.object(aw, "check_port", return_value=(False, "refused", 0.1)), \
             patch.object(aw, "restart_app") as ra:
            aw.main()
        ra.assert_not_called()
        self.assertIn("is DOWN", aw.notify.call_args.kwargs["body"])

    def test_recovery_posts_info(self):
        aw.save_state({"apps": {"37400": {"alive": False, "last_alert": 0}}, "restarts": []})
        with patch.object(aw, "check_port", return_value=(True, "1.0", 0.1)):
            aw.main()
        self.assertEqual(aw.notify.call_args.kwargs["level"], "info")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--status", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(aw.main))
        aw.notify.reset_mock()
        _load()
        aw.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
