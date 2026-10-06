#!/usr/bin/env python3
"""Tests for nova_pkg_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pkg_audit.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="pkg-audit-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("pkg_audit_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(subprocess, "run", side_effect=AssertionError("subprocess at import")):
        spec.loader.exec_module(mod)
    return mod


pa = _load()

BREW_LIST = "aws-c-common 0.14.4\nbat 0.24.0\njq 1.7\n"
BREW_OUTDATED = "aws-c-common (0.14.4) < 0.14.5\nbat (0.24.0) < 0.25.0\nnot a match line\n"
APT_UPGRADABLE = ("Listing...\nalsa-ucm-conf/resolute-updates 1.2.15.3-1ubuntu1.5 all [upgradable from: 1.2.15.3-1ubuntu1.4]\n"
                  "openssl/resolute-security 3.0.13-0ubuntu3.5 arm64 [upgradable from: 3.0.13-0ubuntu3.4]\n")


def _sp(outputs):
    """subprocess.run stub: pops (rc, stdout) per call."""
    outs = list(outputs)

    def run(argv, **kw):
        rc, out = outs.pop(0)
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return MagicMock(side_effect=run)


class _Cur:
    def __init__(self, hosts):
        self.hosts = hosts; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.hosts

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(hosts, sp):
    cur = _Cur(hosts)
    with patch.object(pa.psycopg2, "connect", return_value=_Conn(cur)), patch.object(subprocess, "run", sp), \
         redirect_stdout(io.StringIO()) as out:
        rc = pa.main()
    return rc, cur, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_shell_true(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"shell\s*=\s*True\s*[,)]", SRC))   # (the docstring merely says "No shell=True")

    def test_sh_c_only_ever_receives_in_file_constant_lines(self):
        # every _run() call site passes a literal (or BREW-prefixed literal) — never data from PG or a host
        for line in re.findall(r"(?<!def )_run\((.*)\)", SRC):
            arg = line.split(",")[0].strip()
            self.assertTrue(arg.startswith(('"', "f\"{BREW}")), arg)
        sp = _sp([(0, BREW_LIST), (0, BREW_OUTDATED)])
        with patch.object(subprocess, "run", sp):
            pa.collect("mac-studio", "192.168.1.6", "macos")
        self.assertEqual(sp.call_args_list[0][0][0][:2], ["/bin/sh", "-c"])
        self.assertEqual(sp.call_args_list[0][0][0][2], f"{pa.BREW} list --versions 2>/dev/null")

    def test_remote_hosts_use_batchmode_ssh_argv_and_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        sp = _sp([(0, "5\n"), (0, "")])
        with patch.object(subprocess, "run", sp):
            pa.collect("pi", "192.168.1.9; rm -rf /", "linux")
        argv = sp.call_args_list[0][0][0]
        self.assertEqual(argv[:2], ["ssh", "-o"]); self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-2], "kochj@192.168.1.9; rm -rf /")          # one token, not a shell string
        evil = ("h'; DROP TABLE package_audit; --", "192.168.1.6", "macos")
        rc, cur, _ = _run([evil], _sp([(0, BREW_LIST), (0, BREW_OUTDATED)]))
        self.assertTrue(all("DROP" not in s for s, _ in cur.sql))
        self.assertEqual(cur.ran("DELETE FROM package_audit")[0][1], (evil[0],))


class TestPerformance(unittest.TestCase):
    def test_parse_10k_outdated_lines_fast(self):
        out = "".join(f"pkg{i} (1.{i}) < 2.{i}\n" for i in range(10_000))
        t0 = time.perf_counter()
        with patch.object(subprocess, "run", _sp([(0, BREW_LIST), (0, out)])):
            installed, outdated, ok = pa.collect("m", "192.168.1.6", "macos")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(outdated), 10_000)
        self.assertEqual(outdated[7], ("pkg7", "1.7", "2.7", "homebrew"))


class TestRetry(unittest.TestCase):
    def test_run_is_one_shot_and_returns_the_sentinel(self):
        # RETRY GAP: _run — a single subprocess attempt; any exception becomes (-1, "") so the host is UNREACHABLE
        sp = MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 90))
        with patch.object(subprocess, "run", sp):
            self.assertEqual(pa._run("x", "192.168.1.9"), (-1, ""))
        self.assertEqual(sp.call_count, 1)

    def test_unreachable_host_is_never_fabricated_as_zero_outdated(self):
        with patch.object(subprocess, "run", _sp([(-1, ""), (-1, "")])):
            self.assertEqual(pa.collect("mac", "192.168.1.6", "macos"), (0, [], False))
            self.assertEqual(pa.collect("pi", "192.168.1.9", "linux"), (0, [], False))


class TestUnit(unittest.TestCase):
    def test_brew_parse(self):
        with patch.object(subprocess, "run", _sp([(0, BREW_LIST), (0, BREW_OUTDATED)])):
            installed, outdated, ok = pa.collect("m", "192.168.1.6", "macos")
        self.assertEqual((installed, ok), (3, True))
        self.assertEqual(outdated, [("aws-c-common", "0.14.4", "0.14.5", "homebrew"), ("bat", "0.24.0", "0.25.0", "homebrew")])

    def test_apt_parse(self):
        with patch.object(subprocess, "run", _sp([(0, " 1842 \n"), (0, APT_UPGRADABLE)])):
            installed, outdated, ok = pa.collect("pi", "192.168.1.9", "linux")
        self.assertEqual((installed, ok), (1842, True))
        self.assertEqual(outdated[0], ("alsa-ucm-conf", "1.2.15.3-1ubuntu1.4", "1.2.15.3-1ubuntu1.5", "apt"))
        self.assertEqual(outdated[1][0], "openssl")

    def test_apt_host_with_packages_but_failed_apt_list_is_still_reachable(self):
        with patch.object(subprocess, "run", _sp([(0, "12\n"), (1, "")])):
            self.assertEqual(pa.collect("pi", "192.168.1.9", "linux"), (12, [], True))
        with patch.object(subprocess, "run", _sp([(0, "garbage"), (1, "")])):
            self.assertEqual(pa.collect("pi", "192.168.1.9", "linux"), (0, [], False))

    def test_log_prefix(self):
        with redirect_stdout(io.StringIO()) as out:
            pa.log("hi")
        self.assertRegex(out.getvalue(), r"^\[pkg_audit \d\d:\d\d:\d\d\] hi\n$")


class TestIntegration(unittest.TestCase):
    def test_hosts_come_from_the_orchestrators_node_table(self):
        cur = _Cur([("a", "1.2.3.4", "linux")])
        with patch.object(pa.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(pa.get_hosts(), [("a", "1.2.3.4", "linux")])
        self.assertEqual(cur.sql[0][0], "SELECT node_name, node_ip, os_family FROM cinc_node_configs ORDER BY node_name")
        self.assertIn("FROM cinc_node_configs", (SCRIPTS / "nova_cinc_orchestrate.py").read_text())

    def test_ensure_tables_creates_both_audit_tables(self):
        cur = _Cur([])
        pa.ensure_tables(cur)
        self.assertIn("CREATE TABLE IF NOT EXISTS package_audit (", cur.sql[0][0])
        self.assertIn("CREATE TABLE IF NOT EXISTS package_audit_hosts (", cur.sql[0][0])


class TestFunctional(unittest.TestCase):
    def test_golden_path_replaces_rows_for_reachable_hosts_and_flags_unreachable(self):
        hosts = [("mac-studio", "192.168.1.6", "macos"), ("pi", "192.168.1.9", "linux")]
        sp = _sp([(0, BREW_LIST), (0, BREW_OUTDATED), (-1, ""), (-1, "")])
        rc, cur, out = _run(hosts, sp)
        self.assertEqual(rc, 0)
        self.assertEqual(cur.ran("DELETE FROM package_audit")[0][1], ("mac-studio",))
        ins = [p for _, p in cur.ran("INSERT INTO package_audit (host_name")]
        self.assertEqual(ins, [("mac-studio", "aws-c-common", "0.14.4", "0.14.5", "homebrew"),
                               ("mac-studio", "bat", "0.24.0", "0.25.0", "homebrew")])
        roll = [p for _, p in cur.ran("INSERT INTO package_audit_hosts")]
        self.assertEqual(roll, [("mac-studio", 3, 2, True, "brew"), ("pi", 0, 0, False, "apt")])
        self.assertIn("pi: UNREACHABLE", out); self.assertIn("done: 2 outdated packages", out)

    def test_collect_exception_is_recorded_as_unreachable_not_crash(self):
        hosts = [("weird", "192.168.1.50", "linux")]
        rc, cur, out = _run(hosts, MagicMock(side_effect=ValueError("boom")))
        self.assertEqual(rc, 0)
        self.assertEqual(cur.ran("INSERT INTO package_audit_hosts")[0][1], ("weird", 0, 0, False, "apt"))
        self.assertEqual(cur.ran("DELETE"), [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        boot = ("import sys, unittest.mock as um, psycopg2, subprocess, runpy; "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "subprocess.run = um.MagicMock(side_effect=AssertionError('subprocess at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
