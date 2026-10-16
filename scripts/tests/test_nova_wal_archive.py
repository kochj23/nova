#!/usr/bin/env python3
"""Tests for nova_wal_archive.py + nova_wal_archive_cmd.sh — the 7 house categories (Security,
Performance, Retry, Unit, Integration, Functional, Frame). Little Mister, 2026-10-15 (#36).
No Postgres, no NAS: the archive_command runs for real against temp dirs.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_wal_archive.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_wal_archive as w  # noqa: E402

CMD = SCRIPTS / "nova_wal_archive_cmd.sh"
SRC = (SCRIPTS / "nova_wal_archive.py").read_text()
CMD_SRC = CMD.read_text()
TOOLS = all(shutil.which(t) for t in ("bash", "zstd", "timeout", "cmp"))
SEG = "000000010000030700000074"


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wal = self.root / "pg_wal"
        self.wal.mkdir()
        self.arch = self.root / "archive"
        self.gaps = self.root / "gaps.log"

    def seg(self, name=SEG, data=None):
        p = self.wal / name
        p.write_bytes(data if data is not None else os.urandom(4096) + b"\0" * 60000)
        return p

    def run_cmd(self, src, name, **env):
        e = {**os.environ, "NOVA_WAL_ARCHIVE_DIR": str(self.arch), "NOVA_WAL_GAP_LOG": str(self.gaps),
             "NOVA_WAL_TIMEOUT_S": "10", **{k: str(v) for k, v in env.items()}}
        return subprocess.run(["bash", str(CMD), str(src), name], capture_output=True, text=True, env=e, timeout=60)


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_shell_true(self):
        pat = re.compile(r"(password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{12,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_no_hardcoded_ips_or_user_paths(self):
        for text in (SRC, CMD_SRC):
            self.assertIsNone(re.search(r"\b192\.168\.\d+\.\d+\b", text))
            self.assertNotIn("/Users/", text)

    @unittest.skipUnless(TOOLS, "bash/zstd/timeout/cmp required")
    def test_cmd_refuses_names_postgres_would_never_send(self):
        t = _Tmp("setUp"); t.setUp()
        for bad in ("../../etc/passwd", "x; rm -rf /", "", "0000000100000307000000ZZ"):
            r = t.run_cmd(t.seg(), bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(t.arch.exists() and any(t.arch.iterdir()))
        t.tmp.cleanup()

    def test_cleanup_never_touches_history_or_foreign_files(self):
        names = ["00000002.history", "00000002.history.zst", "README", "base_x", ".tmp", SEG + ".zst"]
        self.assertEqual(w.wal_to_delete(names, "000000010000030800000000", time.time()), [SEG + ".zst"])


class TestPerformance(unittest.TestCase):
    def test_cleanup_selection_scales(self):
        names = [f"00000001{i // 256:08X}{i % 256:08X}.zst" for i in range(200_000)]
        t0 = time.perf_counter()
        doomed = w.wal_to_delete(names, "00000001000001F400000000", time.time())
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(doomed), 0x1F4 * 256)


class TestRetry(_Tmp):
    @unittest.skipUnless(TOOLS, "bash/zstd/timeout/cmp required")
    def test_unreachable_archive_returns_nonzero_so_postgres_retries(self):
        blocker = self.root / "file"; blocker.write_text("x")       # a FILE where the dir should be
        self.arch = blocker / "sub"
        r = self.run_cmd(self.seg(), SEG, NOVA_WAL_CAP_GB=1000, NOVA_WAL_MIN_FREE_GB=0)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("retry", r.stderr)
        self.assertFalse(self.gaps.exists())

    @unittest.skipUnless(TOOLS, "bash/zstd/timeout/cmp required")
    def test_safety_valve_drops_and_records_when_disk_threatened(self):
        blocker = self.root / "file"; blocker.write_text("x")
        self.arch = blocker / "sub"
        r = self.run_cmd(self.seg(), SEG, NOVA_WAL_CAP_GB=0, NOVA_WAL_MIN_FREE_GB=0)
        self.assertEqual(r.returncode, 0, r.stderr)                    # primary's disk wins over the chain
        self.assertIn("SAFETY VALVE", r.stderr)
        line = self.gaps.read_text().strip()
        self.assertIn(f"DROPPED {SEG}", line)
        self.assertEqual(len(w.gaps_since([line], None)), 1)

    def test_wait_for_retries_until_true(self):
        calls = iter([Exception("down"), False, True])
        def fn():
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v
        with patch.object(w.time, "sleep"):
            self.assertTrue(w.wait_for(fn, 60, 0))


class TestUnit(unittest.TestCase):
    def test_lsn_to_segment(self):
        self.assertEqual(w.lsn_to_segment("307/74000028"), "000000010000030700000074")
        self.assertEqual(w.lsn_to_segment("0/1000000", 2), "000000020000000000000001")
        self.assertEqual(w.lsn_to_segment("1/FF000000"), "0000000100000001000000FF")

    def test_parse_basebackup_stderr(self):
        txt = "pg_basebackup: initiating base backup\npg_basebackup: write-ahead log start point: 307/74000028 on timeline 1\n"
        self.assertEqual(w.parse_basebackup_stderr(txt), ("307/74000028", 1))
        self.assertEqual(w.parse_basebackup_stderr("nothing"), (None, None))

    def test_wal_to_delete_ordering_and_labels(self):
        names = ["000000010000030700000073.zst", "000000010000030700000074.zst",
                 "000000010000030700000073.00000028.backup.zst", "000000010000030700000075.partial.zst"]
        got = w.wal_to_delete(names, "000000010000030700000074", time.time())
        self.assertEqual(sorted(got), sorted(names[0:1] + names[2:3]))

    def test_wal_to_delete_without_base_uses_age(self):
        now = time.time()
        names = ["000000010000030700000001.zst", "000000010000030700000002.zst"]
        mt = {names[0]: now - 4 * 86400, names[1]: now - 3600}
        self.assertEqual(w.wal_to_delete(names, None, now, mt), names[:1])

    def test_archiver_state(self):
        now = 10_000_000.0
        self.assertEqual(w.archiver_state({"archive_mode": "off"}, now, False)[0], "info")
        self.assertEqual(w.archiver_state({"archive_mode": "off"}, now, True)[0], "critical")
        ok = {"archive_mode": "on", "last_archived_time": now - 120, "last_failed_time": None,
              "archived_count": 5, "failed_count": 0}
        self.assertEqual(w.archiver_state(ok, now, True)[0], "ok")
        slow = dict(ok, last_archived_time=now - 20 * 60)
        self.assertEqual(w.archiver_state(slow, now, True)[0], "warning")
        stalled = dict(ok, last_archived_time=now - 20 * 60, last_failed_time=now - 10, last_failed_wal=SEG)
        level, msg = w.archiver_state(stalled, now, True)
        self.assertEqual(level, "critical"); self.assertIn("STALLED", msg)
        blip = dict(ok, last_failed_time=now - 10, last_failed_wal=SEG)
        self.assertEqual(w.archiver_state(blip, now, True)[0], "warning")

    def test_gaps_since(self):
        lines = ["2026-10-16T10:00:00Z DROPPED A reason=x", "garbage", "2026-10-16T12:00:00Z DROPPED B r"]
        since = w.datetime(2026, 10, 16, 11, tzinfo=w.timezone.utc).timestamp()
        self.assertEqual([n for _, n in w.gaps_since(lines, since)], ["B"])
        self.assertEqual(len(w.gaps_since(lines, None)), 2)

    def test_precheck_restart(self):
        reps = [("a", "streaming", 0.01), ("b", "streaming", 0.2), ("c", "streaming", None)]
        self.assertEqual(w.precheck_restart(reps, [], False, 139.0), (True, []))
        ok, why = w.precheck_restart(reps[:1], ["pg_dump running 1:02:03"], False, 139.0)
        self.assertFalse(ok)
        self.assertTrue(any("streaming replicas" in r for r in why))
        self.assertTrue(any("pg_dump" in r for r in why))
        self.assertFalse(w.precheck_restart(reps, [], True, 139.0)[0])
        self.assertFalse(w.precheck_restart([("a", "streaming", 300), ("b", "streaming", 0)], [], False, 139)[0])
        self.assertFalse(w.precheck_restart(reps, [], False, 20.0)[0])


class TestIntegration(_Tmp):
    def test_install_copies_atomically_and_is_idempotent(self):
        dst = self.root / "libexec" / "cmd.sh"
        with patch.object(w, "LOCAL_CMD", dst):
            self.assertTrue(w.install())
            self.assertFalse(w.install())
        self.assertEqual(dst.read_bytes(), CMD.read_bytes())
        self.assertTrue(os.access(dst, os.X_OK))

    def test_archive_command_points_at_local_copy(self):
        self.assertTrue(w.archive_command().endswith(" %p %f"))
        self.assertFalse(w.archive_command().startswith(str(SCRIPTS)))   # never the NAS-backed repo copy
        self.assertFalse(w.archive_command().startswith("/nova/"))

    def test_cleanup_keeps_newest_two_bases_and_their_wal(self):
        base, arch = self.root / "base", self.root / "arch"
        base.mkdir(); arch.mkdir()
        for i, lsn in enumerate(("307/10000000", "307/20000000", "307/30000000")):
            d = base / f"base_2026101{i}_000000"; d.mkdir()
            (d / "NOVA_OK").write_text(f'{{"start_lsn": "{lsn}", "timeline": 1}}')
        for seg in ("000000010000030700000010", "000000010000030700000015", "000000010000030700000020",
                    "000000010000030700000031"):
            (arch / f"{seg}.zst").write_text("x")
        with patch.object(w, "BASE_DIR", base), patch.object(w, "ARCHIVE_DIR", arch):
            w.cleanup()
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["base_20261011_000000", "base_20261012_000000"])
        self.assertEqual(sorted(p.name for p in arch.iterdir()),
                         ["000000010000030700000020.zst", "000000010000030700000031.zst"])


class TestFunctional(_Tmp):
    @unittest.skipUnless(TOOLS, "bash/zstd/timeout/cmp required")
    def test_archive_roundtrip_and_idempotent_resend(self):
        src = self.seg()
        r = self.run_cmd(src, SEG)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.arch / f"{SEG}.zst"
        self.assertTrue(out.exists())
        back = subprocess.run(["zstd", "-q", "-d", "-c", str(out)], capture_output=True).stdout
        self.assertEqual(back, src.read_bytes())
        self.assertEqual([p.name for p in self.arch.iterdir()], [f"{SEG}.zst"])   # no temp left behind
        self.assertEqual(self.run_cmd(src, SEG).returncode, 0)                     # identical re-send = OK

    @unittest.skipUnless(TOOLS, "bash/zstd/timeout/cmp required")
    def test_never_overwrites_a_different_segment(self):
        self.assertEqual(self.run_cmd(self.seg(), SEG).returncode, 0)
        before = (self.arch / f"{SEG}.zst").read_bytes()
        r = self.run_cmd(self.seg(data=b"different"), SEG)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to overwrite", r.stderr)
        self.assertEqual((self.arch / f"{SEG}.zst").read_bytes(), before)

    @unittest.skipUnless(TOOLS, "bash/zstd/timeout/cmp required")
    def test_history_and_backup_label_names_are_archived(self):
        for n in ("00000002.history", SEG + ".00000028.backup"):
            self.assertEqual(self.run_cmd(self.seg(n, b"label"), n).returncode, 0, n)

    def test_monitor_alarms_on_stall(self):
        cur = MagicMock()
        conn = MagicMock(); conn.cursor.return_value = cur
        st = {"archive_mode": "on", "last_archived_time": time.time() - 3600, "last_failed_time": time.time(),
              "last_failed_wal": SEG, "archived_count": 1, "failed_count": 9}
        with patch.object(w, "connect", return_value=conn), patch.object(w, "archiver_stats", return_value=st), \
             patch.object(w, "is_enabled", return_value=True), patch.object(w, "notify") as n, \
             patch.object(w, "file_sha", return_value="same"), patch.object(w, "kept_bases", return_value=[]), \
             patch.object(w, "q1", return_value=(None,)), patch.object(w, "GAP_LOG", Path("/nonexistent/gaps")):
            self.assertEqual(w.monitor(), 1)
        self.assertEqual(n.call_args[0][2], "critical")


class TestFrame(unittest.TestCase):
    def test_headers_and_main_guard(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("Written by Jordan Koch (via Claude)", SRC)
        self.assertIn("Written by Jordan Koch (via Claude)", CMD_SRC)
        self.assertIn("Little Mister, 2026-10-15", SRC)

    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", "import nova_wal_archive"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_cli_requires_one_mode(self):
        with self.assertRaises(SystemExit):
            w.main([])


if __name__ == "__main__":
    unittest.main()
