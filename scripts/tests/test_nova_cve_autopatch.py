#!/usr/bin/env python3
"""Tests for nova_cve_autopatch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cve_autopatch.py"
SRC = SCRIPT.read_text()

import nova_config   # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cve = _load("cve_autopatch_under_test", SCRIPT)

# Every outbound door is shut for the life of this module: PG, ssh (subprocess) and Slack/Discord.
_PATCHES = [
    patch.object(cve.psycopg2, "connect", MagicMock(side_effect=AssertionError("offline: psycopg2.connect must be stubbed"))),
    patch.object(cve.subprocess, "run", MagicMock(side_effect=AssertionError("offline: subprocess.run must be stubbed"))),
    patch.object(cve.nova_config, "post_both", MagicMock()),
]
_REAL_RUN = subprocess.run     # cve.subprocess IS the global module; keep the real runner for TestFrame


def setUpModule():
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


def _ticket(tid, host, cve_id, pkg):
    return (tid, f"SECURITY: L13 alert on {host} — {cve_id} affects {pkg}")


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []; self.closed = False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True

    def updates(self):
        return [(s, p) for s, p in self.sql if s.startswith("UPDATE claude_queue")]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class _Fleet:
    """ssh stand-in: answers per command fragment. `upgradable` = packages Ubuntu has a fix for;
    `install_fail` = packages whose apt-get install fails; `update_fail` = hosts whose apt update fails."""
    def __init__(self, upgradable=(), install_fail=(), update_fail=(), raise_on=None):
        self.upgradable, self.install_fail, self.update_fail, self.raise_on = set(upgradable), set(install_fail), set(update_fail), raise_on
        self.calls = []

    def __call__(self, argv, capture_output, text, timeout):
        ip, cmd = argv[-2].split("@")[1], argv[-1]
        self.calls.append((ip, cmd, timeout))
        if self.raise_on and self.raise_on in cmd:
            raise subprocess.TimeoutExpired(argv, timeout)
        r = MagicMock(returncode=0, stdout="", stderr="")
        if "apt-get update" in cmd:
            if ip in self.update_fail:
                r.returncode, r.stderr = 100, "Could not resolve archive.ubuntu.com"
        elif cmd.startswith("apt list --upgradable"):
            pkg = re.search(r"grep -F '(.+)/'", cmd).group(1)
            r.stdout = f"{pkg}/noble-security 1.2 amd64 [upgradable from: 1.1]\n" if pkg in self.upgradable else ""
        elif "apt-get install --only-upgrade" in cmd:
            pkg = cmd.rsplit(" ", 1)[1]
            if pkg in self.install_fail:
                r.returncode, r.stderr = 100, f"E: Unable to locate package {pkg}"
        return r


def _main(rows, fleet=None):
    cur = _Cur(rows); conn = _Conn(cur); fleet = fleet or _Fleet()
    cve.nova_config.post_both.reset_mock()
    out = io.StringIO()
    with patch.object(cve.psycopg2, "connect", return_value=conn), patch.object(cve.subprocess, "run", fleet), redirect_stdout(out):
        cve.main()
    return cur, conn, fleet, out.getvalue()


ROWS = [_ticket(1, "nova-core", "CVE-2026-1001", "openssl"), _ticket(2, "nova-core", "CVE-2026-1002", "openssl"),
        _ticket(3, "nova-core3", "CVE-2026-2001", "linux-image-generic"), _ticket(4, "nova-core5", "CVE-2026-3001", "curl"),
        _ticket(5, "nova-core99", "CVE-2026-4001", "bash"), (6, "SECURITY: L13 alert on nova-core2 — malformed")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", cve.DSN)
        self.assertIn("BatchMode=yes", SRC)                        # key auth only; ssh may never prompt for a password

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"execute\([^)]*%\s*\(", SRC))
        cur, *_ = _main([_ticket(1, "nova-core", "CVE-2026-1", "openssl'; DROP TABLE claude_queue; --")], _Fleet(upgradable=()))
        for sql, params in cur.sql:
            self.assertNotIn("DROP", sql)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"claude_queue"})

    def test_never_reboots_and_never_uses_a_local_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertIsNone(re.search(r"\b(reboot|shutdown)\b\s*[\"']", SRC))
        self.assertNotIn('"sudo reboot', SRC)
        _, _, fleet, _ = _main(ROWS, _Fleet(upgradable={"openssl", "linux-image-generic", "curl"}))
        self.assertFalse(any("reboot" in c for _, c, _ in fleet.calls))
        self.assertTrue(all(ip.startswith("192.168.1.") for ip, _, _ in fleet.calls))
        self.assertTrue(all(ip.startswith("192.168.1.") for ip in cve.HOSTS.values()))

    def test_package_names_are_single_tokens_from_the_ticket_regex(self):
        # TICKET_RE binds host/CVE/package as \S+ — a crafted ticket can't smuggle a second host or a space-separated command
        self.assertIsNone(cve.TICKET_RE.search("SECURITY: L13 alert on nova-core — CVE-2026-1 affects"))
        m = cve.TICKET_RE.search("SECURITY: L13 alert on nova-core — CVE-2026-1 affects openssl extra words")
        self.assertEqual(m.group(3), "openssl")


class TestPerformance(unittest.TestCase):
    def test_grouping_10k_tickets_is_fast(self):
        # unknown host -> every group is skipped before ssh, so this times the regex + grouping path of main() alone
        rows = [_ticket(i, "nova-core42", f"CVE-2026-{i}", f"pkg{i % 500}") for i in range(10_000)]
        t0 = time.perf_counter()
        cur, conn, fleet, out = _main(rows)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(fleet.calls, [])
        self.assertEqual(out.count("unknown host nova-core42"), 500)
        self.assertEqual(cve.nova_config.post_both.call_count, 1)


class TestRetry(unittest.TestCase):
    def test_apt_update_failure_fails_open_per_host_and_the_pass_continues(self):
        # RETRY GAP: ssh()/apt-get update — one attempt; a failing host lands in *Failed* and the other hosts still get patched
        cur, conn, fleet, out = _main(ROWS, _Fleet(upgradable={"openssl", "curl"}, update_fail={"192.168.1.2"}))
        self.assertIn("nova-core: apt update failed: Could not resolve", out)
        report = cve.nova_config.post_both.call_args[0][0]
        self.assertIn("*Failed* (1): nova-core/openssl: apt update failed", report)
        self.assertIn("*Patched* (1): nova-core5/curl", report)
        self.assertEqual(len(cur.updates()), 1)
        self.assertTrue(conn.closed and cur.closed)

    def test_install_failure_leaves_the_ticket_open(self):
        # RETRY GAP: ssh()/apt-get install — one attempt; rc!=0 means no resolve, no retry, stderr clipped to 200 chars
        fleet = _Fleet(upgradable={"openssl"}, install_fail={"openssl"})
        cur, conn, fleet, out = _main(ROWS[:2], fleet)
        self.assertEqual(cur.updates(), [])
        self.assertIn("*Failed* (1): nova-core/openssl: E: Unable to locate package openssl", cve.nova_config.post_both.call_args[0][0])

    def test_ssh_timeout_is_not_caught(self):
        # RETRY GAP: ssh() — subprocess.TimeoutExpired propagates out of main(): a hung host aborts the weekly pass
        # (remaining hosts unpatched, no Slack report). Documented, not fixed here.
        fleet = _Fleet(upgradable={"openssl"}, raise_on="apt-get install")
        with self.assertRaises(subprocess.TimeoutExpired):
            _main(ROWS[:2], fleet)
        cve.nova_config.post_both.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_ticket_regex(self):
        m = cve.TICKET_RE.search("SECURITY: L13 alert on nova-core3 — CVE-2025-40909 affects linux-image-6.8.0-60-generic")
        self.assertEqual(m.groups(), ("nova-core3", "CVE-2025-40909", "linux-image-6.8.0-60-generic"))
        self.assertIsNone(cve.TICKET_RE.search("SECURITY: L12 alert on nova-core — something else"))
        self.assertIsNone(cve.TICKET_RE.search("SECURITY: L13 alert on nova-core - CVE-1 affects x"))   # ASCII dash is not the Wazuh em dash

    def test_kernel_classification(self):
        for pkg in ("linux-image-generic", "linux-headers-6.8.0-60", "linux-modules-extra-6.8", "linux-generic"):
            self.assertTrue(any(pkg.startswith(p) for p in cve.KERNEL_PREFIXES), pkg)
        for pkg in ("openssl", "linux-firmware", "util-linux", "linux-libc-dev"):
            self.assertFalse(any(pkg.startswith(p) for p in cve.KERNEL_PREFIXES), pkg)

    def test_ssh_argv_and_return_shape(self):
        run = MagicMock(return_value=MagicMock(returncode=3, stdout="o", stderr="e"))
        with patch.object(cve.subprocess, "run", run):
            self.assertEqual(cve.ssh("192.168.1.5", "uptime", timeout=7), (3, "o", "e"))
        argv, kw = run.call_args[0][0], run.call_args[1]
        self.assertEqual(argv, ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "kochj@192.168.1.5", "uptime"])
        self.assertEqual((kw["timeout"], kw["capture_output"], kw["text"]), (7, True, True))
        with patch.object(cve.subprocess, "run", run):
            cve.ssh("192.168.1.5", "x")
        self.assertEqual(run.call_args[1]["timeout"], 120)

    def test_log_prefix(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cve.log("hello")
        self.assertEqual(out.getvalue(), "[cve-autopatch] hello\n")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_config_and_the_right_queue_rows(self):
        self.assertIs(cve.nova_config, nova_config)
        self.assertNotIn("def post_both", SRC)
        self.assertIn("dbname=nova_ops", cve.DSN)
        cur, *_ = _main([])
        sql, params = cur.sql[0]
        self.assertEqual(sql, "SELECT id, description FROM claude_queue WHERE status='queued' AND description LIKE 'SECURITY: L13 alert%'")
        self.assertTrue(set(cve.HOSTS) <= {"nova-core", "nova-core2", "nova-core3", "nova-core4", "nova-core5"})

    def test_two_cves_on_one_package_collapse_into_one_upgrade_and_one_resolve(self):
        cur, conn, fleet, out = _main(ROWS[:2], _Fleet(upgradable={"openssl"}))
        installs = [c for _, c, _ in fleet.calls if "apt-get install" in c]
        self.assertEqual(installs, ["sudo apt-get install --only-upgrade -y openssl"])
        (sql, params), = cur.updates()
        self.assertEqual(params, ("Auto-patched via nova_cve_autopatch.py. CVEs: CVE-2026-1001, CVE-2026-1002", [1, 2]))
        self.assertIn("SET status='resolved', completed_at=now(), outcome=%s WHERE id = ANY(%s)", sql)
        self.assertTrue(conn.autocommit)

    def test_report_goes_to_the_notify_channel(self):
        _main(ROWS[:2], _Fleet(upgradable={"openssl"}))
        self.assertEqual(cve.nova_config.post_both.call_args[1], {"slack_channel": nova_config.SLACK_NOTIFY})


class TestFunctional(unittest.TestCase):
    def test_golden_path_userspace_kernel_and_no_fix(self):
        fleet = _Fleet(upgradable={"openssl", "linux-image-generic"})
        cur, conn, fleet, out = _main(ROWS, fleet)
        # userspace: patched + resolved; kernel: installed, resolved as REBOOT PENDING, never rebooted; curl: no fix, ticket left open
        ups = {tuple(p[1]): p[0] for _, p in cur.updates()}
        self.assertEqual(set(ups), {(1, 2), (3,)})
        self.assertTrue(ups[(3,)].startswith("Auto-patched linux-image-generic via nova_cve_autopatch.py — REBOOT PENDING"))
        self.assertIn("CVE-2026-2001", ups[(3,)])
        kernel_install = next(c for c in fleet.calls if c[1].endswith("linux-image-generic") and "install" in c[1])
        self.assertEqual((kernel_install[0], kernel_install[2]), ("192.168.1.5", 300))
        self.assertEqual(sum(1 for c in fleet.calls if "apt-get update" in c[1]), 3)        # nova-core, core3, core5 only
        report = cve.nova_config.post_both.call_args[0][0]
        self.assertTrue(report.startswith(":shield: *Weekly CVE auto-patch report* — 4 package(s) reviewed\n"))
        self.assertIn("*Patched* (1): nova-core/openssl", report)
        self.assertIn("*Kernel patched, REBOOT PENDING* (1): nova-core3/linux-image-generic", report)
        self.assertIn("*No fix available yet from Ubuntu* (1): nova-core5/curl", report)
        self.assertNotIn("*Failed*", report)
        self.assertIn("unknown host nova-core99 — skipping (bash)", out)
        self.assertIn("done: 1 patched, 1 reboot-pending, 1 no-fix-yet, 0 failed", out)
        self.assertTrue(conn.closed and cur.closed)

    def test_no_tickets_posts_nothing_and_closes_cleanly(self):
        cur, conn, fleet, out = _main([])
        self.assertIn("no open L13 tickets — nothing to do", out)
        cve.nova_config.post_both.assert_not_called()
        self.assertEqual(fleet.calls, [])
        self.assertTrue(conn.closed and cur.closed)

    def test_error_path_pg_down_raises_before_any_ssh(self):
        fleet = _Fleet()
        with patch.object(cve.psycopg2, "connect", side_effect=OSError("pg-primary unreachable")), patch.object(cve.subprocess, "run", fleet):
            with self.assertRaises(OSError):
                cve.main()
        self.assertEqual(fleet.calls, [])
        cve.nova_config.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest and a bare run would open PG + ssh the fleet, so the frame check is import-only
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = _REAL_RUN([sys.executable, "-c", "import nova_cve_autopatch"], cwd=str(SCRIPTS), capture_output=True, text=True,
                      timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
