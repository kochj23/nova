#!/usr/bin/env python3
"""Tests for nova_experiment_tracker.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_experiment_tracker.py"
SRC = SCRIPT.read_text()


def _stubs():
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.SLACK_NOTIFY = "C_N"
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    return {"nova_config": cfg, "nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()):        # notify bus + Slack stubbed at import; restored after
        spec.loader.exec_module(mod)
    return mod


et = _load("et_mod", SCRIPT)


def _ps(stdout, rc=0):
    return types.SimpleNamespace(stdout=stdout, returncode=rc)


DOCKER = ("plex\t3 days ago\tplexinc/pms\n"
          "scratch-redis\t2 days ago\tredis:7\n"
          "quick-test\t3 hours ago\tpython:3\n"
          "old-llm\t2 weeks ago\tollama/ollama\n"
          "weird line\n")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_ssh_is_argv_list_with_fixed_remote_command(self):
        with patch.object(et.subprocess, "run", return_value=_ps("")) as sp:
            et.check_host({"name": "h", "ip": "192.168.1.10", "sudo": True})
        argv = sp.call_args[0][0]
        self.assertEqual(argv[:3], ["ssh", "-o", "ConnectTimeout=10"])
        self.assertEqual(argv[3], "kochj@192.168.1.10")
        self.assertTrue(argv[4].startswith("sudo docker ps --format"))
        self.assertNotIn("shell=True", SRC)

    def test_container_names_are_data_not_commands(self):
        with patch.object(et.subprocess, "run", return_value=_ps("$(reboot)\t5 days ago\timg\n")):
            ex = et.check_host(et.HOSTS[0])
        self.assertEqual(ex[0]["container"], "$(reboot)")


class TestPerformance(unittest.TestCase):
    def test_10k_container_lines_under_bound(self):
        big = "".join(f"c{i}\t{i % 30 + 2} days ago\timg\n" for i in range(10_000))
        with patch.object(et.subprocess, "run", return_value=_ps(big)):
            t0 = time.perf_counter()
            ex = et.check_host(et.HOSTS[0])
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(ex), 10_000)


class TestRetry(unittest.TestCase):
    def test_ssh_failure_fails_open_to_empty_list(self):
        # RETRY GAP: check_host()/subprocess.run(ssh) — one attempt per host; timeout/exception → [] (daily rerun)
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise subprocess.TimeoutExpired("ssh", 15)
        with patch.object(et.subprocess, "run", side_effect=boom), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(et.check_host(et.HOSTS[0]), [])
        self.assertEqual(len(attempts), 1)
        self.assertIn("Failed to check nova-core5", out.getvalue())

    def test_nonzero_rc_is_empty_list(self):
        with patch.object(et.subprocess, "run", return_value=_ps("boom", rc=255)):
            self.assertEqual(et.check_host(et.HOSTS[0]), [])


class TestUnit(unittest.TestCase):
    def test_parse_running_time(self):
        # docker ps {{.RunningFor}} renders "3 days ago" (the "Up ..." form is {{.Status}})
        self.assertEqual(et._parse_running_time("3 days ago"), 72)
        self.assertEqual(et._parse_running_time("5 hours ago"), 5)
        self.assertEqual(et._parse_running_time("20 minutes ago"), 0)
        self.assertEqual(et._parse_running_time("2 weeks ago"), 336)
        self.assertEqual(et._parse_running_time("About an hour ago"), 0)
        self.assertEqual(et._parse_running_time("x days"), 0)
        self.assertEqual(et._parse_running_time(""), 0)

    def test_check_host_filters_protected_and_short_runs(self):
        with patch.object(et.subprocess, "run", return_value=_ps(DOCKER)):
            ex = et.check_host({"name": "h", "ip": "1.2.3.4"})
        self.assertEqual([e["container"] for e in ex], ["scratch-redis", "old-llm"])
        self.assertEqual(ex[0]["running_hours"], 48)
        self.assertEqual(ex[1]["image"], "ollama/ollama")

    def test_threshold_is_strict(self):
        with patch.object(et.subprocess, "run", return_value=_ps("c\t24 hours ago\timg\nd\t25 hours ago\timg\n")):
            ex = et.check_host(et.HOSTS[0])
        self.assertEqual([e["container"] for e in ex], ["d"])


class TestIntegration(unittest.TestCase):
    def test_notify_bus_used_not_direct_slack(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("post_both(", SRC)
        self.assertIn('dedup_key="experiment-tracker-forgotten-containers"', SRC)

    def test_protected_list_covers_core_services(self):
        for name in ("plex", "searxng", "homebridge"):
            self.assertIn(name, et.PROTECTED_CONTAINERS)


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_one_warning(self):
        et.notify.reset_mock()
        with patch.object(et.subprocess, "run", return_value=_ps(DOCKER)), redirect_stdout(io.StringIO()) as out:
            et.main()
        et.notify.assert_called_once()
        args, kw = et.notify.call_args
        self.assertEqual(args[0], "Experiment Tracker — 2 forgotten container(s)")
        self.assertEqual((kw["level"], kw["category"]), ("warning", "docker"))
        self.assertIn("`scratch-redis` on nova-core5 — 2 days ago (redis:7)", kw["body"])
        self.assertIn("docker stop <name>", kw["body"])
        self.assertIn("Posted nag for 2", out.getvalue())

    def test_nothing_found_posts_nothing(self):
        et.notify.reset_mock()
        with patch.object(et.subprocess, "run", return_value=_ps("plex\t9 days ago\timg\n")), redirect_stdout(io.StringIO()) as out:
            et.main()
        et.notify.assert_not_called()
        self.assertIn("No forgotten experiments", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        snippet = ("import sys, types; from unittest.mock import MagicMock\n"
                   "nn = types.ModuleType('nova_notify'); nn.notify = MagicMock(); sys.modules['nova_notify'] = nn\n"
                   "import subprocess; subprocess.run = MagicMock(side_effect=AssertionError('ssh at import'))\n"
                   "import nova_experiment_tracker as m; assert m.NAG_AFTER_HOURS == 24")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
