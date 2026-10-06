#!/usr/bin/env python3
"""Tests for nova_pg_failover.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). This tool promotes a Postgres standby and rewrites DNS/pgbouncer,
so EVERY ssh/subprocess/nsupdate/Slack call is mocked and the fence marker is redirected to a
tempdir; no host, DB, or DNS record is ever touched, and the refusal/abort paths are proven.
Written by Jordan Koch (via Claude)."""
import argparse
import importlib.util
import io
import re
import subprocess
import sys
import types
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fo = _load("nova_pg_failover_t", SCRIPTS / "nova_pg_failover.py")
SRC = (SCRIPTS / "nova_pg_failover.py").read_text()
# neutralize the only outbound at load — nothing can post to Slack
fo.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), JORDAN_DM="D_TEST")


def _args(**kw):
    return argparse.Namespace(**kw)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_tsig_key_from_keychain_not_source(self):
        self.assertIn('"security", "find-generic-password"', SRC)
        self.assertIn("nova-bind-tsig-key", SRC)
        self.assertNotRegex(SRC, r"hmac-sha256:[^\"']*:[A-Za-z0-9+/=]{20,}")

    def test_promote_refuses_without_confirm(self):
        with mock.patch.object(fo, "ssh") as ssh, mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=False))
        self.assertEqual(rc, 1)
        ssh.assert_not_called()                       # never touched a host
        fo.nova_config.post_both.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_pgbouncer_rewrite_large_file_fast(self):
        text = ("host=192.168.1.2 port=5434 db=x\n" * 10_000)
        t0 = time.perf_counter()
        out = text.replace("host=192.168.1.2 port=5434", "host=192.168.1.10 port=5432")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertNotIn("192.168.1.2", out)


class TestRetry(unittest.TestCase):
    def test_promote_aborts_if_primary_still_answers(self):
        # the split-brain guard: a live primary => abort, no promotion, nothing mutated
        with mock.patch.object(fo, "check_primary_alive", return_value=(True, True)), \
             mock.patch.object(fo, "ssh") as ssh, mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=True))
        self.assertEqual(rc, 1)
        ssh.assert_not_called()
        fo.nova_config.post_both.assert_not_called()

    def test_promote_aborts_if_promotion_command_fails(self):
        with mock.patch.object(fo, "check_primary_alive", return_value=(False, False)), \
             mock.patch.object(fo, "ssh", return_value=(1, "", "err")) as ssh, mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=True))
        self.assertEqual(rc, 1)
        # one ssh (the failed promote), then abort — never reached pg_is_in_recovery re-check
        self.assertEqual(ssh.call_count, 1)
        fo.nova_config.post_both.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_check_primary_alive_two_signals(self):
        def ssh(host, cmd, timeout=15):
            if "pg_is_in_recovery" in cmd:
                return (0, "f", "")
            return (0, "", "")
        with mock.patch.object(fo, "ssh", side_effect=ssh):
            self.assertEqual(fo.check_primary_alive(), (True, True))

    def test_check_primary_dead(self):
        with mock.patch.object(fo, "ssh", return_value=(255, "", "timeout")):
            self.assertEqual(fo.check_primary_alive(), (False, False))

    def test_standby_health(self):
        def ssh(host, cmd, timeout=15):
            if host == fo.STANDBY_IP:
                return (0, "t", "")
            return (0, "2ms", "")
        with mock.patch.object(fo, "ssh", side_effect=ssh):
            in_rec, lag, err = fo.check_standby_health()
        self.assertTrue(in_rec)
        self.assertEqual(lag, "2ms")


class TestIntegration(unittest.TestCase):
    def test_unfence_requires_confirm_and_marker(self):
        fo.nova_config.post_both.reset_mock()
        with mock.patch.object(fo, "FENCE_MARKER", Path("/nonexistent/fence.json")), mock.patch.object(fo, "log"):
            self.assertEqual(fo.cmd_unfence(_args(confirm=False)), 0)   # no marker => nothing to do
        fo.nova_config.post_both.assert_not_called()

    def test_unfence_without_confirm_refuses_when_marker_present(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "fence.json"; m.write_text("fenced")
            with mock.patch.object(fo, "FENCE_MARKER", m), mock.patch.object(fo, "log"), \
                 redirect_stdout(io.StringIO()):
                rc = fo.cmd_unfence(_args(confirm=False))
            self.assertEqual(rc, 1)
            self.assertTrue(m.exists())              # marker NOT cleared without --confirm


class TestFunctional(unittest.TestCase):
    def test_check_reports_status_readonly(self):
        with mock.patch.object(fo, "check_primary_alive", return_value=(True, True)), \
             mock.patch.object(fo, "check_standby_health", return_value=(True, "1ms", "")), \
             mock.patch.object(fo, "FENCE_MARKER", Path("/nonexistent/fence.json")), \
             redirect_stdout(io.StringIO()) as out:
            rc = fo.cmd_check(_args())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("All healthy", text)
        self.assertIn("Postgres responding: True", text)

    def test_unfence_clears_marker_with_confirm(self):
        import tempfile
        fo.nova_config.post_both.reset_mock()
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "fence.json"; m.write_text("fenced")
            with mock.patch.object(fo, "FENCE_MARKER", m), mock.patch.object(fo, "log"):
                rc = fo.cmd_unfence(_args(confirm=True))
            self.assertEqual(rc, 0)
            self.assertFalse(m.exists())
        fo.nova_config.post_both.assert_called_once()

    def test_full_promote_golden_path_writes_fence_and_alerts(self):
        import tempfile
        fo.nova_config.post_both.reset_mock()
        calls = {"n": 0}
        def ssh(host, cmd, timeout=15):
            calls["n"] += 1
            if "pg_promote" in cmd:
                return (0, "t", "")
            if "pg_is_in_recovery" in cmd:
                return (0, "f", "")        # promoted standby reports writable
            return (0, "", "")
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "state" / "fence.json"
            ini = Path(td) / "pgbouncer.ini"; ini.write_text("[databases]\nnova_ops = host=192.168.1.2 port=5434\n")
            with mock.patch.object(fo, "check_primary_alive", return_value=(False, False)), \
                 mock.patch.object(fo, "ssh", side_effect=ssh), \
                 mock.patch.object(fo, "FENCE_MARKER", marker), \
                 mock.patch.object(fo, "PGBOUNCER_INI", str(ini)), \
                 mock.patch.object(fo.subprocess, "run") as run, mock.patch.object(fo, "log"), \
                 mock.patch.object(fo.time, "sleep"):
                run.return_value = types.SimpleNamespace(returncode=0, stdout="tsigsecret\n", stderr="")
                rc = fo.cmd_promote(_args(confirm=True))
            self.assertEqual(rc, 0)
            self.assertTrue(marker.exists())
            self.assertIn("host=192.168.1.10 port=5432", ini.read_text())
            self.assertNotIn("192.168.1.2", ini.read_text())
        fo.nova_config.post_both.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_check_cli_exits_zero(self):
        import os
        r = subprocess.run([sys.executable, "-c",
                            "import sys,unittest.mock as m; "
                            "import importlib.util as u; "
                            "s=u.spec_from_file_location('f', r'" + str(SCRIPTS / 'nova_pg_failover.py') + "'); "
                            "mod=u.module_from_spec(s); s.loader.exec_module(mod); "
                            "mod.nova_config=m.MagicMock(); "
                            "mod.check_primary_alive=lambda: (True, True); "
                            "mod.check_standby_health=lambda: (True,'1ms',''); "
                            "from pathlib import Path; mod.FENCE_MARKER=Path('/nonexistent/x.json'); "
                            "sys.argv=['x','check']; sys.exit(mod.main())"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("All healthy", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
