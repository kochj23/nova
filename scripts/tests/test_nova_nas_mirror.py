#!/usr/bin/env python3
"""Tests for nova_nas_mirror.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The mirror copies and DELETES on the replica: rsync and the remote delete loop are mocked everywhere,
and both safety guards (MIN_FILES, MAX_DELETE_FRACTION) plus --dry-run are proven to execute nothing."""
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nm = _load("nova_nas_mirror_t", SCRIPTS / "nova_nas_mirror.py")
_TMP = Path(tempfile.mkdtemp())
nm.WORK = str(_TMP / "work")
nm.LOG = str(_TMP / "mirror.log")
MASTER = _TMP / "master"
MASTER.mkdir()
# refusing stub at load: nothing can reach rsync/ssh unmocked
nm.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")))
SRC = (SCRIPTS / "nova_nas_mirror.py").read_text()


def _m(n, prefix="f", size=10):
    return {f"{prefix}{i}": size for i in range(n)}


def _sync(master, replica, dry=False, force=False, run_rc=0, del_rc=0):
    run = MagicMock(return_value=types.SimpleNamespace(returncode=run_rc, stderr=""))
    remote = MagicMock(return_value=types.SimpleNamespace(returncode=del_rc, stdout="DELETED=1"))
    with patch.object(nm, "manifest_local", return_value=master), \
            patch.object(nm, "manifest_remote", return_value=replica), \
            patch.object(nm, "run", run), patch.object(nm.subprocess, "run", remote), \
            redirect_stdout(io.StringIO()) as out:
        rc = nm.sync_share("nas", str(MASTER), "nas", dry, force)
    return rc, run, remote, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("BatchMode=yes", nm.SSH)                  # key auth only, never a password prompt

    def test_tiny_master_refuses_everything(self):
        rc, run, remote, out = _sync(_m(10), _m(5000))
        self.assertEqual(rc, 1)
        run.assert_not_called()
        remote.assert_not_called()
        self.assertIn("dropped mount", out)

    def test_delete_fraction_guard_blocks_mass_delete(self):
        master = _m(2000)
        replica = dict(master, **_m(3000, prefix="gone"))       # 60% of replica would be deleted
        rc, run, remote, out = _sync(master, replica)
        remote.assert_not_called()
        self.assertIn("REFUSING DELETE", out)
        lst = Path(nm.WORK, "nas_to_delete.txt").read_text().splitlines()
        self.assertEqual(len(lst), 3000)                         # evidence still written

    def test_force_overrides_delete_ceiling(self):
        master = _m(2000)
        replica = dict(master, **_m(3000, prefix="gone"))
        rc, run, remote, out = _sync(master, replica, force=True)
        self.assertEqual(remote.call_count, 1)

    def test_dry_run_executes_nothing(self):
        rc, run, remote, out = _sync(_m(2000), {"x": 1}, dry=True)
        self.assertEqual(rc, 0)
        run.assert_not_called()
        remote.assert_not_called()
        self.assertIn("DRY RUN", out)


class TestPerformance(unittest.TestCase):
    def test_load_and_diff_10k(self):
        p = _TMP / "big.tsv"
        p.write_text("".join(f"dir/file{i}.mkv\t{i}\n" for i in range(10_000)))
        t0 = time.perf_counter()
        d = nm.load(str(p))
        _sync(d, d, dry=True)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(d), 10_000)


class TestRetry(unittest.TestCase):
    def test_rsync_failure_reports_rc_without_raising(self):
        # RETRY GAP: sync_share rsync / remote delete — one attempt each; nightly schedule is the retry
        master = _m(2000)
        replica = dict(_m(1990), gone=1)
        rc, run, remote, out = _sync(master, replica, run_rc=12)
        self.assertEqual(rc, 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("copy rsync rc=12", out)

    def test_rsync_partial_transfer_rc24_is_ok(self):
        rc, run, *_ = _sync(_m(2000), _m(1999), run_rc=24)
        self.assertEqual(rc, 0)


class TestUnit(unittest.TestCase):
    def test_load_parses_tabs_and_skips_garbage(self):
        p = _TMP / "m.tsv"
        p.write_text("a\tb\t5\nnosize\t\nweird\tx\n\tnopath\nok.txt\t7\n")
        self.assertEqual(nm.load(str(p)), {"a\tb": 5, "ok.txt": 7})
        self.assertEqual(nm.load(str(_TMP / "missing.tsv")), {})

    def test_unmounted_master_skipped(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nm.sync_share("nas", str(_TMP / "nope"), "nas", False, False), 1)
        self.assertIn("unmounted?", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_remote_manifest_runs_find_on_replica(self):
        r = types.SimpleNamespace(stdout="a.txt\t3\n", returncode=0)
        with patch.object(nm.subprocess, "run", return_value=r) as sr:
            d = nm.manifest_remote("/dst", str(_TMP / "r.tsv"))
        self.assertEqual(d, {"a.txt": 3})
        argv = sr.call_args[0][0]
        self.assertEqual(argv[:len(nm.SSH)], nm.SSH)
        self.assertIn(nm.UNAS_HOST, argv)
        self.assertTrue(argv[-1].startswith('find "/dst"'))


class TestFunctional(unittest.TestCase):
    def test_copy_and_delete_by_explicit_list(self):
        master = _m(2000)
        master["f5"] = 99                                        # size change -> copy
        replica = dict(_m(2000), stale=4)
        rc, run, remote, out = _sync(master, replica)
        self.assertEqual(rc, 0)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["rsync", "-lt", "--partial", "--files-from"])
        self.assertEqual(Path(argv[4]).read_text(), "f5\n")
        payload = remote.call_args[1]["input"]
        self.assertEqual(payload, "stale\n")                     # trailing newline: last entry not dropped
        self.assertIn("while IFS= read -r f", remote.call_args[0][0][-1])

    def test_main_share_filter(self):
        with patch.object(nm, "sync_share", return_value=0) as ss, \
                patch.object(sys, "argv", ["m", "--share", "external", "--dry-run"]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(nm.main(), 0)
        ss.assert_called_once_with("external", "/volume1/external", "External", True, False)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_nas_mirror.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_mirror"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
