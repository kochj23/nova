#!/usr/bin/env python3
"""Tests for nova_cinc_daily.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This script runs apt/brew upgrades over SSH: every subprocess / PG / Slack path is replaced at load
by a refusing stub, and each test installs its own mock on top."""
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

import psycopg2.extras

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cd = _load("nova_cinc_daily_t", SCRIPTS / "nova_cinc_daily.py")
# refusing stubs at load: nothing unmocked can reach SSH, PG or Slack
cd.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")),
                                      TimeoutExpired=subprocess.TimeoutExpired)
cd.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")), extras=psycopg2.extras)
cd.nova_config = types.SimpleNamespace(post_both=MagicMock())
SRC = (SCRIPTS / "nova_cinc_daily.py").read_text()

LINUX = {"node_name": "nova-core", "node_ip": "192.0.2.2/32", "ssh_user": "u", "os_family": "linux"}
MAC = {"node_name": "mac-studio", "node_ip": "127.0.0.1/32", "ssh_user": "u", "os_family": "macos"}


def _pg():
    conn = MagicMock(); cur = conn.cursor.return_value
    return conn, cur


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", cd.DB_DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_brew_upgrade_pins_critical_infra_first(self):
        seen = []
        with patch.object(cd, "ssh_cmd", side_effect=lambda h, u, c, timeout=60: (seen.append(c), (0, "", ""))[1]), _quiet():
            cd.apply_updates(MAC)
        self.assertTrue(seen[0].index("brew pin") < seen[0].index("brew upgrade"))
        for keg in ("postgresql@17", "pgbouncer", "node"):
            self.assertIn(keg, seen[0])


class TestPerformance(unittest.TestCase):
    def test_send_report_10k_results_fast(self):
        results = [{"node": f"n{i}", "inventory_count": 3, "updates": [{"action": "a"}],
                    "converge_ok": i % 2 == 0, "drift": [{"type": "service_down", "service": "sshd"}]}
                   for i in range(10_000)]
        t0 = time.perf_counter()
        cd.send_report(results)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_ssh_timeout_and_error_fail_open(self):
        # RETRY GAP: ssh_cmd — one subprocess attempt; timeout/error become rc -1, never raise
        with patch.object(cd.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 1)) as run:
            self.assertEqual(cd.ssh_cmd("192.0.2.9", "u", "true"), (-1, "", "timeout"))
        self.assertEqual(run.call_count, 1)
        with patch.object(cd.subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(cd.ssh_cmd("192.0.2.9", "u", "true"), (-1, "", "nope"))

    def test_slack_failure_is_swallowed(self):
        # RETRY GAP: send_report/post_both — one attempt, logged
        with patch.object(cd.nova_config, "post_both", side_effect=RuntimeError("slack down")), _quiet() as out:
            cd.send_report([])
        self.assertIn("Slack post failed", out.getvalue())

    def test_converge_exception_returns_false(self):
        with patch.object(cd.subprocess, "run", side_effect=OSError("x")), _quiet():
            self.assertFalse(cd.run_converge(LINUX))


class TestUnit(unittest.TestCase):
    def test_ssh_cmd_local_vs_remote(self):
        ok = types.SimpleNamespace(returncode=0, stdout="o", stderr="")
        with patch.object(cd.subprocess, "run", return_value=ok) as run:
            cd.ssh_cmd("127.0.0.1", "u", "ls")
            self.assertEqual(run.call_args[0][0], ["bash", "-lc", "ls"])
            cd.ssh_cmd("192.0.2.4", "u", "ls")
            self.assertEqual(run.call_args[0][0][0], "ssh")
            self.assertIn("u@192.0.2.4", run.call_args[0][0])

    def test_inet_suffix_stripped_and_dpkg_parsed(self):
        conn, cur = _pg()
        with patch.object(cd, "ssh_cmd", return_value=(0, "bash|5.1|amd64\ncurl|7.8\n", "")) as sc, \
                patch.object(cd, "pg_connect", return_value=conn), _quiet():
            pk = cd.collect_inventory(LINUX)
        self.assertEqual(sc.call_args[0][0], "192.0.2.2")
        self.assertEqual([p["name"] for p in pk], ["bash", "curl"])
        self.assertIsNone(pk[1]["arch"])
        self.assertEqual(cur.execute.call_count, 2)

    def test_unreachable_node_reports_once_not_per_service(self):
        with patch.object(cd, "ssh_cmd", return_value=(255, "", "Connection refused")), \
                patch.object(cd, "pg_connect", return_value=_pg()[0]), _quiet():
            d = cd.detect_drift(LINUX)
        self.assertEqual([x["type"] for x in d], ["node_unreachable"])


class TestIntegration(unittest.TestCase):
    def test_converge_delegates_to_orchestrator_and_records_run(self):
        conn, cur = _pg()
        r = types.SimpleNamespace(returncode=0, stdout="Resources updated: 4 of 9\n", stderr="")
        with patch.object(cd.subprocess, "run", return_value=r) as run, \
                patch.object(cd, "pg_connect", return_value=conn), _quiet():
            self.assertTrue(cd.run_converge(LINUX))
        self.assertTrue(run.call_args[0][0][1].endswith("nova_cinc_orchestrate.py"))
        ins = cur.execute.call_args_list[0][0]
        self.assertIn("INSERT INTO cinc_runs", ins[0])
        self.assertEqual(ins[1][3], 4)
        conn.commit.assert_called_once()

    def test_drift_written_to_shared_observations(self):
        conn, cur = _pg()
        answers = {"true": 0, "systemctl is-active wazuh-agent 2>/dev/null": 3}
        with patch.object(cd, "ssh_cmd", side_effect=lambda h, u, c, timeout=60: (answers.get(c, 0), "", "")), \
                patch.object(cd, "pg_connect", return_value=conn), _quiet():
            d = cd.detect_drift(LINUX)
        self.assertEqual(d, [{"type": "service_down", "service": "wazuh-agent"}])
        self.assertIn("shared_observations", cur.execute.call_args_list[1][0][0])


class TestFunctional(unittest.TestCase):
    def setUp(self):
        cd.nova_config.post_both.reset_mock()

    def _main(self, nodes, ssh):
        with patch.object(cd, "get_nodes", return_value=nodes), patch.object(cd, "ssh_cmd", side_effect=ssh), \
                patch.object(cd, "run_converge", return_value=True), \
                patch.object(cd, "pg_connect", return_value=_pg()[0]), \
                patch.object(cd.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="git 2.4\n")), \
                _quiet():
            cd.main()
        return cd.nova_config.post_both.call_args[0][0]

    def test_main_golden_path_reports_fleet(self):
        txt = self._main([LINUX, MAC], lambda h, u, c, timeout=60: (0, "x|1\n" if "dpkg" in c else c, ""))
        self.assertIn("*Fleet:* 2 nodes converged", txt)
        self.assertIn("*Inventory:* 2 packages", txt)
        self.assertIn("*Drift detected:* 0 items", txt)

    def test_all_remote_unreachable_collapses_to_one_diagnostic(self):
        other = dict(LINUX, node_name="nas", node_ip="192.0.2.3/32")
        txt = self._main([LINUX, other, MAC], lambda h, u, c, timeout=60: (255, "", "Permission denied"))
        self.assertIn("cinc_ssh_context_broken", txt)
        self.assertNotIn("node_unreachable", txt)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_cinc_daily"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
