#!/usr/bin/env python3
"""Tests for nova_preflight_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG and ssh are mocked. Written by Jordan Koch (via Claude)."""
import contextlib
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pf = _load("nova_preflight_check_t", SCRIPTS / "nova_preflight_check.py")
SRC = (SCRIPTS / "nova_preflight_check.py").read_text()


class _Cur:
    def __init__(self, conn): self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params)); self.last = sql

    def fetchone(self):
        return self.conn.snapshot if "capacity_snapshots" in self.last else (self.conn.issues,)


class _Conn:
    def __init__(self, snapshot=None, issues=0):
        self.snapshot = snapshot; self.issues = issues; self.sql = []

    def cursor(self): return _Cur(self)
    def close(self): pass


def _ssh(stdout="", rc=0):
    return mock.Mock(return_value=types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=""))


def _pre(host, snapshot, issues=0, docker=""):
    conn = _Conn(snapshot, issues)
    with mock.patch.object(pf.psycopg2, "connect", return_value=conn), \
         mock.patch.object(pf.subprocess, "run", _ssh(docker)) as run:
        ok, msgs = pf.run_preflight(host)
    return ok, msgs, conn, run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_host_name_is_bound_not_interpolated(self):
        evil = "x'; SELECT 1; --"
        _, _, conn, _ = _pre(evil, None)
        for sql, params in conn.sql:
            self.assertNotIn(evil, sql)
        self.assertEqual(conn.sql[0][1], (evil,))
        self.assertEqual(conn.sql[1][1], (f"%{evil}%",))

    def test_unknown_host_never_sshes(self):
        _, msgs, _, run = _pre("not-a-host", None)
        run.assert_not_called()
        self.assertIn("Unknown host", msgs[0])


class TestPerformance(unittest.TestCase):
    def test_10k_container_lines(self):
        lines = "\n".join(json.dumps({"Name": f"c{i}", "CPUPerc": "1.0%"}) for i in range(10_000))
        t0 = time.perf_counter()
        with mock.patch.object(pf.subprocess, "run", _ssh(lines)):
            out = pf.check_docker_resources("10.0.0.1")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 10_000)


class TestRetry(unittest.TestCase):
    def test_ssh_timeout_fails_open(self):
        # RETRY GAP: check_docker_resources()/ssh — single attempt; timeout => [] (no docker info)
        with mock.patch.object(pf.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 15)) as r:
            self.assertEqual(pf.check_docker_resources("10.0.0.1"), [])
        self.assertEqual(r.call_count, 1)

    def test_incident_query_failure_is_swallowed(self):
        calls = {"n": 0}
        def connect(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise pf.psycopg2.OperationalError("down")
            return _Conn((80, 80, 10, "OK"))
        with mock.patch.object(pf.psycopg2, "connect", side_effect=connect), \
             mock.patch.object(pf.subprocess, "run", _ssh("")):
            ok, _ = pf.run_preflight("nova-core")
        self.assertTrue(ok)


class TestUnit(unittest.TestCase):
    def test_bad_json_lines_skipped(self):
        with mock.patch.object(pf.subprocess, "run", _ssh('{"Name":"a"}\nnot json\n{"Name":"b"}')):
            self.assertEqual([c["Name"] for c in pf.check_docker_resources("x")], ["a", "b"])

    def test_nonzero_ssh_is_empty(self):
        with mock.patch.object(pf.subprocess, "run", _ssh('{"Name":"a"}', rc=255)):
            self.assertEqual(pf.check_docker_resources("x"), [])

    def test_headroom_none_when_no_row(self):
        with mock.patch.object(pf.psycopg2, "connect", return_value=_Conn(None)):
            self.assertIsNone(pf.get_current_headroom("h"))


class TestIntegration(unittest.TestCase):
    def test_thresholds_per_host_applied(self):
        ok, msgs, _, _ = _pre("nova-core5", (29.0, 80.0, 50.0, "OK"))   # core5 min_cpu=30
        self.assertFalse(ok)
        self.assertTrue(any(m.startswith("BLOCK: CPU headroom 29.0%") for m in msgs))
        ok2, _, _, _ = _pre("mac-studio", (29.0, 80.0, 50.0, "OK"))      # studio min_cpu=20
        self.assertTrue(ok2)


class TestFunctional(unittest.TestCase):
    def test_healthy_host_passes_with_info(self):
        docker = "\n".join(json.dumps(c) for c in ({"Name": "web", "CPUPerc": "75%"}, {"Name": "db", "CPUPerc": "2%"}))
        ok, msgs, _, _ = _pre("nova-core", (80.0, 70.0, 40.0, "OK"), issues=5, docker=docker)
        self.assertTrue(ok)
        self.assertIn("INFO: 2 Docker containers running", msgs)
        self.assertIn("WARN: High CPU containers: web", msgs)
        self.assertTrue(any("5 recent issues" in m for m in msgs))

    def test_critical_and_full_disk_block(self):
        ok, msgs, _, _ = _pre("nova-core", (80.0, 70.0, 95.0, "CRIT"))
        self.assertFalse(ok)
        self.assertTrue(any("CRITICAL" in m for m in msgs))
        self.assertTrue(any(m.startswith("BLOCK: Disk free 5.0%") for m in msgs))

    def test_main_json_exit_code(self):
        out = io.StringIO()
        with mock.patch.object(pf, "run_preflight", return_value=(False, ["BLOCK: x"])), \
             mock.patch.object(sys, "argv", ["x", "--host", "nova-core", "--json"]), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                pf.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(json.loads(out.getvalue()), {"passed": False, "messages": ["BLOCK: x"]})


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_preflight_check.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--host", r.stdout)


if __name__ == "__main__":
    unittest.main()
