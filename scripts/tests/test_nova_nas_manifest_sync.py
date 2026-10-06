#!/usr/bin/env python3
"""Tests for nova_nas_manifest_sync.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Every ssh / tar / mv / PG call is mocked; the delete (quarantine) refusal paths are proven."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nas_manifest_sync.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load("nas_manifest_t", SCRIPT)
SRC = SCRIPT.read_text()
# Nothing in this file may ssh, tar, mv, or page.
ms.subprocess = MagicMock(DEVNULL=-3, PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired)
ms.subprocess.run.side_effect = RuntimeError("subprocess.run not mocked in test")
ms.subprocess.Popen.side_effect = RuntimeError("subprocess.Popen not mocked in test")
ms.alert = MagicMock()
ms.log = lambda m: None

SHARE = ms.SHARES[0]


def _nul(entries):
    return b"".join(f"{s}".encode() + b"\x00" + p.encode() + b"\x00" for p, s in entries)


def _args(**kw):
    d = dict(dry_run=False, share=None, max_delete_pct=5.0, max_delete_abs=500, min_src_ratio=0.5)
    d.update(kw)
    return Namespace(**d)


def _sync(src, dst, prev_src=0, args=None, mount_ok=True, copy_rc=0, quarantined=None):
    """Drive sync_share with fake finds; returns (result, mocks)."""
    def fake_find(host, path, outfile, **k):
        Path(outfile).write_bytes(_nul(src if host == ms.SYNO_HOST else dst))
        return 1
    m = SimpleNamespace(rsync=MagicMock(return_value=copy_rc), quar=MagicMock(return_value=quarantined or 0),
                        prune=MagicMock(), run=MagicMock(), delta=MagicMock())
    ms.alert.reset_mock()
    with patch.object(ms, "ssh_find", side_effect=fake_find), \
            patch.object(ms, "pg_replace_manifest", side_effect=[prev_src, 0]), \
            patch.object(ms, "ensure_mount", return_value=mount_ok), patch.object(ms, "rsync_delta", m.rsync), \
            patch.object(ms, "quarantine_orphans", m.quar), patch.object(ms, "prune_trash", m.prune), \
            patch.object(ms, "record_run", m.run), patch.object(ms, "record_delta", m.delta):
        res = ms.sync_share(MagicMock(), SHARE, "/volume/U/.srv/.unifi-drive", args or _args())
    return res, m


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertIn("BatchMode=yes", ms.SSH_OPTS)

    def test_traversal_refused(self):
        for bad in ("../etc/x", "/abs", "a/../../b", "", "a\x00b"):
            self.assertFalse(ms.safe_relpath(bad), repr(bad))
            with self.assertRaises(ValueError):
                ms.quarantine_dest("/t/.nova-trash", "nas", "d", bad)

    def test_empty_source_never_deletes(self):
        res, m = _sync(src=[], dst=[("a", 1), ("b", 2)], prev_src=1000)
        self.assertEqual(res["mode"], "abort")
        m.quar.assert_not_called(); m.rsync.assert_not_called()
        self.assertTrue(ms.alert.call_args.kwargs["critical"])

    def test_quarantine_only_touches_unas(self):
        with patch.object(ms.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as r, \
                patch.object(ms, "run_with_retry", return_value=SimpleNamespace(stdout="RC=0")) as rr:
            n = ms.quarantine_orphans("nas", "nas/.data", "/v/.nova-trash", ["ok/a", "../evil"], "d")
        self.assertEqual(n, 1)
        hosts = {c.args[0][len(ms.SSH_OPTS) + 1] for c in r.call_args_list + rr.call_args_list}
        self.assertEqual(hosts, {ms.UNAS_HOST})


class TestPerformance(unittest.TestCase):
    def test_diff_100k_is_linear(self):
        src = {f"p{i}": i for i in range(100_000)}
        dst = {f"p{i}": (i if i % 10 else -1) for i in range(5_000, 105_000)}
        t0 = time.perf_counter()
        tc, orp = ms.diff_manifests(src, dst)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(orp), 5_000)


class TestRetry(unittest.TestCase):
    def test_run_with_retry_backs_off_then_succeeds(self):
        seq = [SimpleNamespace(returncode=255, stderr="conn reset", stdout=""), OSError("timeout"),
               SimpleNamespace(returncode=0, stderr="", stdout="ok")]
        with patch.object(ms.subprocess, "run", side_effect=seq) as r, patch.object(ms.time, "sleep") as sl:
            self.assertEqual(ms.run_with_retry(["ssh"], timeout=1, retries=3, backoff=5).stdout, "ok")
        self.assertEqual(r.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [5, 10])

    def test_run_with_retry_gives_up_and_ssh_find_retries_empty(self):
        with patch.object(ms.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="x", stdout="")), \
                patch.object(ms.time, "sleep"):
            with self.assertRaises(RuntimeError):
                ms.run_with_retry(["ssh"], timeout=1, retries=2)
        calls = []
        def fake_run(argv, stdout=None, **k):
            calls.append(1)
            if len(calls) == 3:
                stdout.write(b"5\x00a\x00")
        with tempfile.TemporaryDirectory() as d, patch.object(ms.subprocess, "run", side_effect=fake_run), \
                patch.object(ms.time, "sleep"):
            self.assertEqual(ms.ssh_find(ms.UNAS_HOST, "/p", f"{d}/o"), 4)
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ms.selftest(), 0)
        self.assertIn("selftest OK", out.getvalue())

    def test_guards(self):
        self.assertEqual(ms.evaluate_guards(10, 10, 0, 100, 5, 500, 0.5)[:2], (False, False))   # shrank
        self.assertEqual(ms.evaluate_guards(100, 100_000, 6_000, 100, 5, 500, 0.5)[:2], (True, False))
        self.assertEqual(ms.evaluate_guards(100, 100_000, 4_000, 100, 5, 500, 0.5), (True, True, []))

    def test_clean_to_file_handles_weird_names_and_excludes(self):
        with tempfile.TemporaryDirectory() as d:
            Path(f"{d}/raw").write_bytes(_nul([("a\tb\nc.txt", 3), ("x/@eaDir/t", 1), ("ok", 7)]) + b"zz\x00bad\x00")
            m = ms.clean_to_file(f"{d}/raw", f"{d}/clean")
            self.assertEqual(m, {"a\tb\nc.txt": 3, "ok": 7})
            self.assertIn('"a\tb\nc.txt",3', Path(f"{d}/clean").read_text())


class TestIntegration(unittest.TestCase):
    def test_pg_replace_manifest_scoped_and_locked(self):
        cur = MagicMock(); cur.fetchone.return_value = (42,)
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        with tempfile.NamedTemporaryFile("w", suffix=".csv") as f:
            self.assertEqual(ms.pg_replace_manifest(conn, "syno", "nas", f.name), 42)
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        self.assertTrue(sqls[0].startswith("SELECT pg_advisory_xact_lock"))
        dele = [c for c in cur.execute.call_args_list if c.args[0].startswith("DELETE")][0]
        self.assertEqual(dele.args[1], ("syno", "nas"))
        self.assertIn(ms.MANIFEST_TABLE, dele.args[0])

    def test_record_run_writes_backup_runs_job(self):
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            ms.record_run("nas", 0, 5, 10, 100, 0, True)
        sql, params = cur.execute.call_args.args
        self.assertIn(ms.RUNS_TABLE, sql)
        self.assertEqual(params[0], "nova-backup:nas:manifest-sync")
        with patch("psycopg2.connect", side_effect=OSError("pg down")):
            ms.record_run("nas", 0, 5, 10, 100, 0, True)        # best-effort, never raises


class TestFunctional(unittest.TestCase):
    def test_dry_run_plans_but_touches_nothing(self):
        res, m = _sync(src=[("a", 1), ("b", 2)], dst=[("a", 1), ("z", 9)], args=_args(dry_run=True))
        self.assertEqual((res["mode"], res["to_copy"], res["orphans"]), ("dry-run", 1, 1))
        for mk in (m.rsync, m.quar, m.prune, m.run):
            mk.assert_not_called()

    def test_live_golden_path_copies_quarantines_and_records(self):
        res, m = _sync(src=[("a", 1), ("b", 2)], dst=[("a", 1), ("z", 9)], quarantined=1)
        self.assertTrue(res["ok"])
        m.rsync.assert_called_once()
        self.assertEqual(m.quar.call_args.args[3], ["z"])
        self.assertTrue(m.run.call_args.kwargs["ok"])
        self.assertEqual(m.delta.call_args.kwargs, {"copied": [("b", 2)], "quarantined": [("z", 9)]})

    def test_orphan_flood_skips_quarantine_and_missing_mount_aborts(self):
        res, m = _sync(src=[("a", 1)], dst=[("a", 1)] + [(f"o{i}", 1) for i in range(600)])
        m.quar.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertIn("quarantine SKIPPED", ms.alert.call_args.args[1])
        res, m = _sync(src=[("a", 1)], dst=[("z", 1)], mount_ok=False)
        m.rsync.assert_not_called(); m.quar.assert_not_called()
        self.assertFalse(m.run.call_args.kwargs["ok"])


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_nas_manifest_sync"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
