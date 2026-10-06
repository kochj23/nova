#!/usr/bin/env python3
"""Tests for nova_backup_restore_test.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_backup_restore_test.py").read_text()
DROPDB = "drop" + "db"


def _load():
    saved = {k: os.environ.get(k) for k in ("PGHOST", "PGPORT")}
    try:
        spec = importlib.util.spec_from_file_location("nbrt_under_test", SCRIPTS / "nova_backup_restore_test.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:   # the module setdefaults PGHOST/PGPORT; don't leak that into other tests
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


rt = _load()
rt.notify = MagicMock()          # stub the outbound alert at module load


def _ok(out="", rc=0, err=""):
    return SimpleNamespace(stdout=out, stderr=err, returncode=rc)


class _Base(unittest.TestCase):
    def setUp(self):
        rt.PROBLEMS.clear()
        rt.notify.reset_mock()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_subprocess_uses_arg_lists_not_shell(self):
        self.assertNotIn("shell=True", SRC)

    def test_drops_only_scratch_databases(self):
        drops = re.findall(r'\["' + DROPDB + r'"[^\]]*\]', SRC)
        self.assertEqual(len(drops), 4)
        for d in drops:
            self.assertTrue("SCRATCH" in d or "scratch" in d, d)
        self.assertTrue(rt.SCRATCH.endswith("_restoretest"))


class TestPerformance(_Base):
    def test_nas_sample_is_bounded_on_10k_files(self):
        local, nas = self.root / "l", self.root / "n"
        fake = [local / f"f{i}" for i in range(10_000)]
        with patch.object(Path, "rglob", return_value=fake), patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1)):
            t0 = time.perf_counter()
            ok, n = rt.nas_sample_check(local, nas)
        self.assertEqual((ok, n), (rt.NAS_SAMPLE, rt.NAS_SAMPLE))
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_createdb_failure_fails_open(self):
        # RETRY GAP: pg_restore_test()/createdb — one attempt; failure recorded as a PROBLEM, returns None
        calls = []
        def fake(args, **kw):
            calls.append(args[0])
            return _ok(rc=1 if args[0] == "createdb" else 0, err="boom")
        with patch.object(rt, "sh", side_effect=fake):
            self.assertIsNone(rt.pg_restore_test(Path("/nonexistent/dump")))
        self.assertEqual(calls.count("createdb"), 1)
        self.assertIn("createdb scratch failed", rt.PROBLEMS[0])

    def test_live_count_unreadable_returns_none(self):
        # RETRY GAP: live_count()/psql — single attempt, non-digit output -> None
        with patch.object(rt, "sh", return_value=_ok("ERROR", 2)) as sh:
            self.assertIsNone(rt.live_count())
        self.assertEqual(sh.call_count, 1)


class TestUnit(_Base):
    def test_latest_dump_picks_newest_and_handles_missing(self):
        for n in ("nova_memories_20260101", "nova_memories_20260301", "nova_ops_20260401"):
            (self.root / n).mkdir()
        self.assertEqual(rt.latest_dump(self.root).name, "nova_memories_20260301")
        self.assertEqual(rt.latest_dump(self.root, "nova_ops").name, "nova_ops_20260401")
        self.assertIsNone(rt.latest_dump(self.root / "missing"))
        self.assertIsNone(rt.latest_dump(self.root, "nothing"))

    def test_first_existing_falls_back_to_first(self):
        self.assertEqual(rt._first_existing("/no/a", "/no/b"), Path("/no/a"))
        self.assertEqual(rt._first_existing("/no/a", str(self.root)), self.root)

    def test_nas_check_size_drift_and_missing(self):
        local, nas = self.root / "l", self.root / "n"
        local.mkdir(); nas.mkdir()
        (local / "a").write_text("12345"); (nas / "a").write_text("1")
        (local / "b").write_text("x")
        ok, n = rt.nas_sample_check(local, nas)
        self.assertEqual((ok, n), (0, 2))
        self.assertTrue(any("size drift" in p for p in rt.PROBLEMS))
        self.assertTrue(any("missing file" in p for p in rt.PROBLEMS))
        rt.PROBLEMS.clear()
        self.assertEqual(rt.nas_sample_check(local, None), (0, 0))
        self.assertIn("NAS copy missing", rt.PROBLEMS[0])


class TestIntegration(_Base):
    def test_restore_always_cleans_scratch(self):
        seq = []
        def fake(args, **kw):
            seq.append(args[0])
            if args[0] == "psql" and "SELECT count(*) FROM memories" in args:
                return _ok("42")
            return _ok()
        with patch.object(rt, "sh", side_effect=fake):
            self.assertEqual(rt.pg_restore_test(Path("/x/dump")), 42)
        self.assertEqual(seq[0], DROPDB); self.assertEqual(seq[-1], DROPDB)
        self.assertEqual(seq.count("pg_restore"), 2)       # pre-data then data

    def test_nova_ops_restore_limits_to_control_plane_tables(self):
        (self.root / "nova_ops_1").mkdir()
        got = []
        def fake(args, **kw):
            got.append(args)
            return _ok("7") if args[0] == "psql" else _ok()
        with patch.object(rt, "LOCAL_DIR", self.root), patch.object(rt, "sh", side_effect=fake):
            self.assertEqual(rt.nova_ops_restore_test(), 7)
        restore = next(a for a in got if a[0] == "pg_restore")
        self.assertIn("claude_memories", restore)
        self.assertNotIn("syslog_events", restore)
        self.assertEqual(got[-1][0], DROPDB)


class TestFunctional(_Base):
    def test_golden_path_notifies_passed(self):
        dump = self.root / "nova_memories_1"; dump.mkdir(); (dump / "f").write_text("x")
        nas = self.root / "nas"; (nas / "nova_memories_1").mkdir(parents=True)
        (nas / "nova_memories_1" / "f").write_text("x")
        (self.root / "nova_ops_1").mkdir()
        def fake(args, **kw):
            if args[0] == "psql" and "-tA" in args:
                return _ok("100")
            return _ok()
        with patch.object(rt, "LOCAL_DIR", self.root), patch.object(rt, "NAS_DIR", nas), \
             patch.object(rt, "sh", side_effect=fake):
            with self.assertRaises(SystemExit) as cm:
                rt.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("PASSED", rt.notify.call_args[0][0])

    def test_no_dump_is_critical(self):
        with patch.object(rt, "LOCAL_DIR", self.root / "none"), patch.object(rt, "sh") as sh:
            with self.assertRaises(SystemExit) as cm:
                rt.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(rt.notify.call_args.kwargs["level"], "critical")
        sh.assert_not_called()

    def test_drift_is_flagged(self):
        (self.root / "nova_memories_1").mkdir()
        def fake(args, **kw):
            if args[0] == "psql" and args[args.index("-d") + 1] == "nova_memories":
                return _ok("1000")
            if args[0] == "psql" and "-tA" in args:
                return _ok("10")
            return _ok()
        with patch.object(rt, "LOCAL_DIR", self.root), patch.object(rt, "NAS_DIR", self.root / "nas"), \
             patch.object(rt, "sh", side_effect=fake):
            with self.assertRaises(SystemExit) as cm:
                rt.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("DRIFT", rt.notify.call_args.kwargs["body"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_backup_restore_test"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
