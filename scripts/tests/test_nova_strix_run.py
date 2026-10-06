#!/usr/bin/env python3
"""Tests for nova_strix_run.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). All ssh/psql/subprocess, the maintenance window, notify, signals
and os._exit are mocked; NO scan is ever launched and NO window is ever really opened.
Written by Jordan Koch (via Claude)."""
import importlib.util
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load("nova_strix_run_t", SCRIPTS / "nova_strix_run.py")
SRC = (SCRIPTS / "nova_strix_run.py").read_text()


class _Exit(Exception):
    def __init__(self, code): self.code = code


def _argv(*extra):
    return ["nova_strix_run.py", "--targets", "http://192.168.1.2:3000", "--label", "grafana", *extra]


def _run_main(argv, ssh_impl, alive_seq=(False,), scorecard="4"):
    alive = iter(alive_seq)
    env = dict(
        ssh=mock.Mock(side_effect=ssh_impl),
        strix_alive=mock.Mock(side_effect=lambda: next(alive, False)),
        force_kill=mock.Mock(),
        wazuh_scorecard=mock.Mock(return_value=scorecard),
        nova_maintenance=mock.Mock(),
        nova_notify=mock.Mock(),
        nova_router=types.SimpleNamespace(base=lambda: "http://router.local:8080"),
    )
    patches = [mock.patch.object(sr, k, v) for k, v in env.items()]
    patches.append(mock.patch.object(sr.signal, "signal"))
    patches.append(mock.patch.object(sr.signal, "alarm"))
    patches.append(mock.patch.object(sr.time, "sleep"))
    patches.append(mock.patch.object(sr.os, "_exit", side_effect=lambda c: (_ for _ in ()).throw(_Exit(c))))
    patches.append(mock.patch.object(sys, "argv", argv))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
         patches[7], patches[8], patches[9], patches[10], patches[11]:
        try:
            sr.main()
            code = None
        except _Exit as e:
            code = e.code
    return env, code


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_recon_only_forbids_exploitation(self):
        env, _ = _run_main(_argv("--recon-only"), lambda cmd, **k: "strix_runs/abc\n")
        launch = next(c.args[0] for c in env["ssh"].call_args_list if "strix" in c.args[0] and "nohup" in c.args[0])
        self.assertIn("-m quick", launch)
        self.assertIn("RECON and vulnerability IDENTIFICATION ONLY", launch)
        self.assertIn("NO exploitation", launch)

    def test_router_key_is_not_a_real_secret(self):
        # the router accepts any key; the harness never ships a cloud credential in source
        self.assertIn("nova-router", SRC)
        self.assertNotRegex(SRC, r"sk-[A-Za-z0-9]{20,}")


class TestPerformance(unittest.TestCase):
    def test_ansi_strip_10k_lines(self):
        blob = ("\x1b[31mstrix_runs/run_x\x1b[0m line\n" * 10_000)
        t0 = time.perf_counter()
        out = sr.ANSI.sub("", blob)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertNotIn("\x1b", out)


class TestRetry(unittest.TestCase):
    def test_ssh_failure_fails_open_to_empty_string(self):
        # RETRY GAP: ssh() — one attempt; any error returns "" so callers degrade, never crash
        with mock.patch.object(sr.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 40)) as r:
            self.assertEqual(sr.ssh("whoami"), "")
        self.assertEqual(r.call_count, 1)

    def test_wazuh_scorecard_failure_returns_question_mark(self):
        with mock.patch.object(sr.subprocess, "run", side_effect=RuntimeError("psql down")):
            self.assertEqual(sr.wazuh_scorecard(45), "?")


class TestUnit(unittest.TestCase):
    def test_strix_alive_parses_yes(self):
        with mock.patch.object(sr, "ssh", return_value="yes\n"):
            self.assertTrue(sr.strix_alive())
        with mock.patch.object(sr, "ssh", return_value=""):
            self.assertFalse(sr.strix_alive())

    def test_ssh_passes_batchmode_flags(self):
        with mock.patch.object(sr.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="ok")) as r:
            self.assertEqual(sr.ssh("echo hi"), "ok")
        cmd = r.call_args.args[0]
        self.assertIn("-o", cmd); self.assertIn("BatchMode=yes", cmd); self.assertEqual(cmd[-1], "echo hi")

    def test_scorecard_query_is_scoped_and_read_only(self):
        with mock.patch.object(sr.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="7\n")) as r:
            self.assertEqual(sr.wazuh_scorecard(30), "7")
        q = r.call_args.args[0][-1]
        self.assertTrue(q.strip().upper().startswith("SELECT"))
        self.assertIn("security_events", q)


class TestIntegration(unittest.TestCase):
    def test_window_opened_and_always_closed(self):
        env, code = _run_main(_argv(), lambda cmd, **k: "strix_runs/run_1\n")
        env["nova_maintenance"].start.assert_called_once()
        env["nova_maintenance"].stop.assert_called()
        env["force_kill"].assert_called()        # cleanup always force-kills the sandbox
        self.assertEqual(code, 0)

    def test_router_base_used_for_llm_endpoint(self):
        env, _ = _run_main(_argv(), lambda cmd, **k: "strix_runs/run_1\n")
        launch = next(c.args[0] for c in env["ssh"].call_args_list if "OPENAI_API_BASE" in c.args[0])
        self.assertIn("http://router.local:8080/v1", launch)


class TestFunctional(unittest.TestCase):
    def test_complete_run_posts_summary(self):
        def ssh_impl(cmd, **k):
            if "vulnerabilities.csv" in cmd:
                return "id,title,sev\n1,Default creds on admin,HIGH\n2,Reflected XSS,MEDIUM\n"
            return "strix_runs/run_ok\n"
        env, code = _run_main(_argv(), ssh_impl, alive_seq=(False,))
        self.assertEqual(code, 0)
        final = env["nova_notify"].notify.call_args_list[-1]
        self.assertIn("COMPLETE", final.args[0])
        self.assertIn("HIGH×1", final.args[0])
        self.assertIn("Wazuh in-window events", final.kwargs["body"])

    def test_failed_start_closes_window_and_returns(self):
        env, code = _run_main(_argv(), lambda cmd, **k: "Traceback (most recent call last): boom\n")
        # never found a rundir and saw an error -> notify failure, stop window, return (no os._exit)
        self.assertIsNone(code)
        env["nova_maintenance"].stop.assert_called_once()
        self.assertTrue(any("failed to start" in c.args[0] for c in env["nova_notify"].notify.call_args_list))

    def test_timeout_force_kills(self):
        # alive stays True until the cap trips; patch time so the deadline is immediately exceeded
        with mock.patch.object(sr.time, "time", side_effect=[0] + [10_000] * 20):
            env, code = _run_main(_argv("--max-min", "1"), lambda cmd, **k: "strix_runs/run_to\n",
                                  alive_seq=(True, True, True))
        self.assertEqual(code, 2)
        self.assertTrue(any("force-killed" in c.args[0] or "TIMED OUT" in c.args[0]
                            for c in env["nova_notify"].notify.call_args_list))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        import os
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_strix_run.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--targets", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
