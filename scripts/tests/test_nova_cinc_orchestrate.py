#!/usr/bin/env python3
"""Tests for nova_cinc_orchestrate.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
This module changes remote hosts (sudo/apt/ufw/rsync), so EVERY ssh_exec / subprocess.run / DB call is
mocked, and the dry-run (drift) path is proven to issue only read-only probes. notify is stubbed on the
loaded module; LOG_FILE and DOTFILES_SOURCE are tempdirs."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_cinc_orchestrate.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="cinc_"))
MUTATING = re.compile(r"apt-get install|systemctl|\btee\b|sed -i|ufw enable|ufw allow|brew install|mkdir -p|rkhunter --propupd|aideinit")


def _load():
    spec = importlib.util.spec_from_file_location("nova_cinc_orchestrate_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.nova_notify = MagicMock()
    mod.LOG_FILE = TMP / "orch.log"
    mod.print = lambda *a, **k: None
    return mod


co = _load()
NODE = {"node_name": "core5", "node_ip": "192.168.1.10", "ssh_user": "nova", "os_family": "linux",
        "run_list": ["nova_base", "nova_monitoring", "nova_security", "nova_linux", "nova_unknown"]}


class _Ssh:
    """ssh_exec double: probes answer `answer`; records every command."""
    def __init__(self, answer="drift missing inactive"):
        self.answer, self.cmds = answer, []

    def __call__(self, user, host, command, timeout=60):
        self.cmds.append(command)
        if command == "echo ok":
            return 0, "ok", ""
        return 0, self.answer, ""


class _Base(unittest.TestCase):
    def setUp(self):
        co.nova_notify.reset_mock()


class TestSecurity(_Base):
    def test_no_credentials_and_parameterized_sql(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))
        self.assertIsNone(re.search(r"db_(query|exec)\(\s*f[\"']", SRC))

    def test_dry_run_issues_only_read_only_probes(self):
        ssh = _Ssh()
        with patch.object(co, "ssh_exec", side_effect=ssh), patch.object(co, "db_exec"), \
             patch.object(co.subprocess, "run") as run:
            ok, changes, out = co.converge_node(NODE, dry_run=True)
        self.assertTrue(ssh.cmds)
        self.assertEqual([c for c in ssh.cmds if MUTATING.search(c)], [])
        run.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(changes, 12)          # every drifted probe is counted (bug fix 2026-10-05)
        self.assertIn("nova_linux: 3 changes", out)

    def test_ssh_is_batch_mode_argv(self):
        with patch.object(co.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="ok", stderr="")) as run:
            co.ssh_exec("u", "h", "echo ok; touch /tmp/x")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-1], "echo ok; touch /tmp/x")       # one argv element to ssh, no local shell
        self.assertNotIn("shell", run.call_args.kwargs)


class TestPerformance(_Base):
    def test_many_nodes_converge_fast(self):
        nodes = [dict(NODE, node_name=f"n{i}", run_list=["nova_macos", "nova_unknown"]) for i in range(2000)]
        with patch.object(co, "get_nodes", return_value=nodes), patch.object(co, "ssh_exec", side_effect=_Ssh()), \
             patch.object(co, "db_exec"):
            t0 = time.perf_counter()
            co.cmd_converge(SimpleNamespace(node=None, dry_run=True))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertIn("2000/2000 OK", co.nova_notify.call_args.kwargs["body"])


class TestRetry(_Base):
    def test_ssh_timeout_and_unreachable_fail_closed(self):
        # RETRY GAP: ssh_exec()/ssh_test() — one attempt; timeout -> (-1, "", msg); node marked failed
        with patch.object(co.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 10)) as run:
            self.assertEqual(co.ssh_exec("u", "h", "x"), (-1, "", "SSH command timed out"))
            self.assertFalse(co.ssh_test("u", "h"))
        self.assertEqual(run.call_count, 2)
        with patch.object(co, "ssh_test", return_value=False), patch.object(co, "db_exec") as dbx:
            self.assertEqual(co.converge_node(NODE), (False, 0, "SSH unreachable"))
        self.assertIn("'SSH unreachable'", dbx.call_args[0][0])

    def test_db_errors_fail_open(self):
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")) as c:
            self.assertEqual(co.db_query("SELECT 1"), [])
            co.db_exec("SELECT 1")
        self.assertEqual(c.call_count, 2)
        self.assertIn("DB exec error", co.LOG_FILE.read_text())


class TestUnit(_Base):
    def test_recipe_dispatch(self):
        self.assertIn("[skipped] macOS managed locally", co._apply_recipe_shell("u", "h", "macos", "nova_macos", True)[1])
        self.assertIn("no shell handler", co._apply_recipe_shell("u", "h", "linux", "bogus", True)[1])
        self.assertIn("not linux", co._recipe_nova_linux("u", "h", "macos", True)[1])
        self.assertIn("macOS only", co._recipe_nova_dotfiles("u", "h", "linux", True)[1])

    def test_all_ok_probes_report_no_changes(self):
        with patch.object(co, "ssh_exec", side_effect=_Ssh(answer="ok")):
            rc, out, _ = co._recipe_nova_linux("u", "h", "linux", False)
        self.assertEqual(out.count("[ok]"), 3)

    def test_notify_strips_emoji_prefix(self):
        co.notify(":gear: *Title*\nbody")
        self.assertEqual(co.nova_notify.call_args[0][0], "*Title*")


class TestIntegration(_Base):
    def test_converge_applies_and_counts_changes(self):
        ssh = _Ssh()
        with patch.object(co, "ssh_exec", side_effect=ssh), patch.object(co, "db_exec") as dbx:
            ok, changes, out = co.converge_node(NODE, dry_run=False)
        self.assertTrue(ok)
        self.assertGreater(changes, 5)
        self.assertTrue(any(MUTATING.search(c) for c in ssh.cmds))      # the real (mocked) path does mutate
        sqls = [c[0][0] for c in dbx.call_args_list]
        self.assertIn("INSERT INTO deployment_runs", sqls[0])
        self.assertTrue(any("cinc_node_configs" in s for s in sqls))

    def test_dotfiles_dry_run_never_writes(self):
        home = TMP / "home"
        (home / ".vim").mkdir(parents=True, exist_ok=True)
        (home / ".zshrc").write_text("x")
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "shasum":
                return SimpleNamespace(stdout="LOCAL  f\n", returncode=0, stderr="")
            return SimpleNamespace(stdout=">f+++++++++ vimrc\n", returncode=0, stderr="")
        with patch.object(co, "DOTFILES_SOURCE", home), patch.object(co.subprocess, "run", side_effect=run), \
             patch.object(co, "ssh_exec", side_effect=_Ssh(answer="REMOTE exists")):
            rc, out, _ = co._recipe_nova_dotfiles("u", "10.0.0.9", "macos", True)
        rsyncs = [c for c in calls if c[0] == "rsync"]
        self.assertTrue(rsyncs and all("--dry-run" in c for c in rsyncs))
        self.assertIn("[drift] .zshrc differs", out)


class TestFunctional(_Base):
    def test_cli_drift_notifies_summary(self):
        with patch.object(co, "get_nodes", return_value=[NODE]), patch.object(co, "ssh_exec", side_effect=_Ssh()), \
             patch.object(co, "db_exec"), patch.object(sys, "argv", ["x", "drift"]):
            co.main()
        kw = co.nova_notify.call_args.kwargs
        self.assertEqual(kw["dedup_key"], "orchestrate-drift")
        self.assertIn("Resources drifted", kw["body"])

    def test_cli_add_and_status(self):
        with patch.object(co, "db_exec") as dbx, patch.object(sys, "argv", ["x", "add", "10.0.0.5", "box", "linux"]):
            co.main()
        self.assertEqual(dbx.call_args[0][1], ("box", "10.0.0.5", "linux", json.dumps(["nova_base"])))
        with patch.object(co, "db_query", return_value=[]), patch.object(sys, "argv", ["x", "status"]), \
             patch.object(co, "print", create=True) as p:
            co.main()
        self.assertEqual(p.call_args[0][0], "No nodes registered")


class TestFrame(unittest.TestCase):
    def test_help_and_import(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("converge", r.stdout)
        r = subprocess.run([sys.executable, "-c", "import nova_cinc_orchestrate"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
