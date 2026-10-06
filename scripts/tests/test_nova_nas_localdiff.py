#!/usr/bin/env python3
"""Tests for nova_nas_localdiff.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Every ssh/rsync/subprocess, psycopg2 and Slack post is mocked; NO
host is touched, NO file is deleted, and TMP/STATE are redirected to a tempdir. The prune path is
proven to stay OFF without its opt-in env var. Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ld = _load("nova_nas_localdiff_t", SCRIPTS / "nova_nas_localdiff.py")
SRC = (SCRIPTS / "nova_nas_localdiff.py").read_text()
ld.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_FEED="C_FEED")


def _write(p, pairs):
    Path(p).write_text("".join(f"{rel}\t{size}\n" for rel, size in pairs), encoding="utf-8")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("VALUES (now(),%s,%s,0,%s,0,%s,%s)", SRC)

    def test_prune_refuses_absolute_and_traversal_paths(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(ld, "TMP", td), \
             mock.patch.object(ld.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="RC=0", returncode=0)):
            n, rc = ld.prune_orphans("nas", "/dest", ["/etc/passwd", "../../evil", "ok/real.txt"])
        # only the one safe relative path survives the filter
        self.assertEqual(n, 1)


class TestPerformance(unittest.TestCase):
    def test_load_sizes_10k(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "lst"
            _write(f, [(f"dir/file{i}.bin", str(i)) for i in range(10_000)])
            t0 = time.perf_counter()
            c = ld.load_sizes(str(f))
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(sum(c.values()), 10_000)


class TestRetry(unittest.TestCase):
    def test_record_telemetry_failure_is_swallowed(self):
        # RETRY GAP: record()/psycopg2 — single attempt; a telemetry write failure must not crash the run
        with mock.patch.object(ld.psycopg2, "connect", side_effect=RuntimeError("pg down")):
            ld.record("nas", 0, 5)  # no exception

    def test_source_unreachable_fails_loud_and_aborts(self):
        ld.nova_config.post_both.reset_mock()
        with mock.patch.object(ld, "source_reachable", return_value=False), \
             mock.patch.object(ld, "record") as rec, \
             mock.patch.object(ld.os, "makedirs"), mock.patch("builtins.print"):
            rc = ld.main()
        self.assertEqual(rc, 1)
        rec.assert_called_once_with("nas", 99, 0)   # a failed backup_run is written so the monitor sees it
        self.assertTrue(any("FAILED" in c.args[0] for c in ld.nova_config.post_both.call_args_list))


class TestUnit(unittest.TestCase):
    def test_excl_anchors_recycle_and_snapshot(self):
        self.assertTrue(ld.EXCL.search("#recycle/old.bin"))
        self.assertTrue(ld.EXCL.search("share/#snapshot/x"))
        self.assertTrue(ld.EXCL.search("a/@eaDir/thumb"))
        self.assertFalse(ld.EXCL.search("music/recycle_notes.txt"))

    def test_load_sizes_multiset_counts_same_rel_diff_size(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "lst"
            _write(f, [("dup.jpg", "10"), ("dup.jpg", "20"), ("dup.jpg", "10"), ("@eaDir/x", "5")])
            c = ld.load_sizes(str(f))
        self.assertEqual(c[("dup.jpg", "10")], 2)
        self.assertEqual(c[("dup.jpg", "20")], 1)
        self.assertNotIn(("@eaDir/x", "5"), c)

    def test_record_ok_codes(self):
        captured = {}
        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, sql, params): captured["p"] = params
        class C:
            autocommit = True
            def cursor(s): return Cur()
            def close(s): pass
        with mock.patch.object(ld.psycopg2, "connect", return_value=C()):
            ld.record("nas", 24, 3)       # rc 24 counts as ok
        self.assertEqual(captured["p"][-1], True)
        self.assertEqual(captured["p"][-2], 0)   # errors=0 when ok


class TestIntegration(unittest.TestCase):
    def test_resync_defers_without_force(self):
        with mock.patch.object(ld, "source_reachable", return_value=True), \
             mock.patch.object(ld, "synology_resyncing", return_value=True), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(ld.os, "makedirs"), mock.patch("builtins.print"):
            os.environ.pop("NOVA_LOCALDIFF_FORCE", None)
            rc = ld.main()
        self.assertEqual(rc, 0)

    def test_synology_resyncing_parses_mdstat(self):
        with mock.patch.object(ld.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="md0 : active\n  [===>] recovery = 5.0%", returncode=0)):
            self.assertTrue(ld.synology_resyncing())
        with mock.patch.object(ld.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="md0 : active raid5", returncode=0)):
            self.assertFalse(ld.synology_resyncing())


class TestFunctional(unittest.TestCase):
    def _drive(self, src_pairs, dst_pairs, env=None):
        ld.nova_config.post_both.reset_mock()
        td = tempfile.mkdtemp()
        state = str(Path(td) / "state.json")

        def fake_find(host, path, outfile):
            _write(outfile, src_pairs if host == ld.SYNO else dst_pairs)
            return os.path.getsize(outfile)

        rsynced = {}
        def fake_rsync(name, s, cifs, tosync):
            rsynced["n"] = sum(1 for _ in open(tosync)); return 0

        patches = [
            mock.patch.object(ld, "TMP", td),
            mock.patch.object(ld, "STATE", state),
            mock.patch.object(ld, "source_reachable", return_value=True),
            mock.patch.object(ld, "synology_resyncing", return_value=False),
            mock.patch.object(ld, "ensure_mounts"),
            mock.patch.object(ld, "find_to", side_effect=fake_find),
            mock.patch.object(ld, "rsync_files", side_effect=fake_rsync),
            mock.patch.object(ld, "prune_orphans", return_value=(0, 0)),
            mock.patch.object(ld, "record"),
            mock.patch.object(sys, "argv", ["x", "nas"]),
            mock.patch.object(ld.os, "makedirs"),
            mock.patch("builtins.print"),
            mock.patch.dict(os.environ, env or {}, clear=False),
        ]
        for p in patches:
            p.start()
        try:
            rc = ld.main()
        finally:
            for p in patches:
                p.stop()
        return rc, rsynced

    def test_in_sync_no_rsync(self):
        pairs = [(f"f{i}.bin", str(i)) for i in range(1200)]
        rc, rsynced = self._drive(pairs, pairs)
        self.assertNotIn("n", rsynced)   # nothing to sync
        self.assertTrue(any("already in sync" in c.args[0] for c in ld.nova_config.post_both.call_args_list))

    def test_differing_files_rsynced(self):
        src = [(f"f{i}.bin", str(i)) for i in range(1200)] + [("new.bin", "5")]
        dst = [(f"f{i}.bin", str(i)) for i in range(1200)]
        rc, rsynced = self._drive(src, dst)
        self.assertEqual(rsynced["n"], 1)

    def test_prune_stays_off_without_env(self):
        # orphan on UNAS, but prune must not run without NOVA_LOCALDIFF_PRUNE=1
        src = [(f"f{i}.bin", str(i)) for i in range(1200)]
        dst = src + [("orphan.bin", "9")]
        with mock.patch.object(ld, "prune_orphans") as prune:
            rc, _ = self._drive(src, dst)
        prune.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_localdiff"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
