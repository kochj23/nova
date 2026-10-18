#!/usr/bin/env python3
"""Tests for nova_nas_localdiff_reverse.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). ssh/find/rsync, PG and Slack are all mocked: no NAS is
touched. Proves dry-run, the resync gate, additive copies, the incremental marker (advances only on
success), the settle window, the shared lock + stale guard, soft-delete move/purge, the anti-wipe
abort and the #nova_deleted exclusion. The Synology-side mover runs for real against temp dirs.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nas_localdiff_reverse.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_nas_localdiff_reverse_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nr = _load()
NOW = 1_800_000_000.0
OLD = NOW - 3600


def _full(entries, clock=NOW):
    """find_to output: clock record then 'size\\tmtime\\tctime\\trel'."""
    return f"{clock}\0".encode() + b"".join(
        f"{s}\t{m}\t{m}\t{p}\0".encode() for p, s, m in entries)


def _incr(entries, clock=NOW):
    """incremental find output: clock then 'type\\tmtime\\tctime\\tsize\\trel'."""
    return f"{clock}\0".encode() + b"".join(
        f"{t}\t{m}\t{m}\t{s}\t{p}\0".encode() for t, p, s, m in entries)


UNAS = [("a.txt", 10, OLD), ("b.txt", 20, OLD), ("#recycle/old", 5, OLD), ("new.txt", 7, OLD),
        ("hot.txt", 9, NOW - 5)]
SYNO = [("a.txt", 10, OLD), ("b.txt", 99, OLD), ("stale.txt", 3, OLD),
        ("#nova_deleted/2026-01-01/x", 1, OLD)]
STATS = ("Number of regular files transferred: 2\nTotal transferred file size: 1,234 bytes\n")


class _Fake:
    """Answers every ssh call by content; records (host, remote) pairs."""

    def __init__(self, resync=False, rsync_rc=0, unas=UNAS, syno=SYNO, incr=(), mover="",
                 mount_ok=True, ps="", trash_dirs=""):
        self.calls, self.resync, self.rsync_rc = [], resync, rsync_rc
        self.unas, self.syno, self.incr, self.mover = unas, syno, incr, mover
        self.mount_ok, self.ps, self.trash_dirs = mount_ok, ps, trash_dirs

    def __call__(self, cmd, **kw):
        host, remote = cmd[-2], cmd[-1]
        self.calls.append((host, remote))
        cp = lambda out="", rc=0: subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
        if "-newermt" in remote:
            kw["stdout"].write(_incr(self.incr)); kw["stdout"].flush()
            return cp()
        if "find ." in remote:
            kw["stdout"].write(_full(self.unas if host == nr.UNAS else self.syno)); kw["stdout"].flush()
            return cp()
        if "mdstat" in remote:
            return cp("md2 : active\n [>....] resync = 3.2%" if self.resync else "md2 : active")
        if remote.startswith("rsync"):
            return cp(f"{STATS}RC={self.rsync_rc}\n")
        if remote.startswith("ps -eo"):
            return cp(self.ps)
        if "/proc/mounts" in remote:
            return cp("a\nb\n" if self.mount_ok else "", 0 if self.mount_ok else 1)
        if remote.startswith("python3 -"):
            return cp(self.mover)
        if remote.startswith("ls -1"):
            return cp(self.trash_dirs)
        if "read -r -d" in remote:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return cp()

    def remotes(self, prefix):
        return [r for _h, r in self.calls if r.startswith(prefix)]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = {}
        self.cfg = {}
        self.ps = [patch.object(nr, "TMP", self.tmp.name),
                   patch.object(nr.nova_config, "post_both"),
                   patch.object(nr.psycopg2, "connect"),
                   patch.object(nr, "get_marker", side_effect=lambda n: self.marker.get(n)),
                   patch.object(nr, "set_marker", side_effect=lambda n, ts, info: self.marker.__setitem__(n, ts)),
                   patch.object(nr, "get_config", side_effect=lambda k, d=None: self.cfg.get(k, d)),
                   patch.object(nr, "put_config", side_effect=lambda k, v: self.cfg.__setitem__(k, v)),
                   patch.object(nr, "alert")]
        _, self.post, self.pg, _, _, _, _, self.alert = [p.start() for p in self.ps]
        nr.QUIET["on"] = False
        self._r = redirect_stdout(io.StringIO())
        self.out = self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, argv, fake):
        with patch.object(nr.subprocess, "run", side_effect=fake):
            rc = nr.main(argv)
        return rc, [c[0][0] for c in self.post.call_args_list]

    def records(self):
        cur = self.pg.return_value.cursor.return_value.__enter__.return_value
        return [c[0][1] for c in cur.execute.call_args_list if "backup_runs" in c[0][0]]


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")
        self.assertIn("BatchMode=yes", SRC)

    def test_default_is_dry_run_no_rsync(self):
        fake = _Fake()
        rc, posts = self.run_main([], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.remotes("rsync"), [])
        self.assertTrue(any("DRY RUN, nothing changed" in p for p in posts))
        self.assertEqual(self.records(), [])

    def test_resync_refuses_to_write(self):
        fake = _Fake(resync=True)
        rc, posts = self.run_main(["--apply"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.remotes("rsync"), [])
        self.assertIn("deferred", posts[0])

    def test_rsync_never_deletes_and_excludes_trash(self):
        fake = _Fake()
        self.run_main(["--apply", "--allow-deletes", "nas"], fake)
        self.assertEqual(len(fake.remotes("rsync")), 1)
        cmd = fake.remotes("rsync")[0]
        self.assertNotIn("--delete", cmd)
        self.assertIn("--exclude='/#nova_deleted/'", cmd)
        self.assertEqual(fake.remotes("python3 -"), [])     # no soft-delete without the flag

    def test_unas_only_ever_gets_read_commands(self):
        fake = _Fake(incr=[("f", "x.txt", 1, OLD)])
        self.run_main(["--apply", "nas"], fake)
        self.run_main(["--apply", "--mode", "incremental", "nas"], fake)
        unas = [r for h, r in fake.calls if h == nr.UNAS]
        self.assertTrue(unas)
        for r in unas:
            self.assertRegex(r, r"^date \+%s\.%N; printf '\\0'; cd '.*' && nice -n 19 ionice -c3 find \.")
            self.assertNotRegex(r, r"\b(rm|mv|rsync|touch|mkdir|tee)\b|>\s*/")

    def test_mover_refuses_absolute_dotdot_and_trash_paths(self):
        with tempfile.TemporaryDirectory() as d:
            dst, cifs, trash = (Path(d) / x for x in ("dst", "cifs", "trash"))
            for p in (dst, cifs):
                p.mkdir()
            (dst / "keep").write_text("k")
            lst = Path(d) / "l"
            lst.write_bytes(b"/etc/passwd\0../keep\0#nova_deleted/a\0")
            r = subprocess.run([sys.executable, "-", str(lst), str(dst), str(cifs), str(trash)],
                               input=nr.MOVER, capture_output=True, text=True, timeout=30)
            self.assertIn("MOVED=0", r.stdout)
            self.assertTrue((dst / "keep").exists())


class TestPerformance(_Base):
    def test_load_sizes_100k(self):
        p = Path(self.tmp.name) / "big.lst"
        p.write_bytes(_full([(f"dir{i % 100}/file{i}.bin", i, OLD) for i in range(100_000)]))
        t0 = time.perf_counter()
        d = nr.load_sizes(str(p))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(d), 100_000)

    def test_soft_delete_guard_is_constant_time(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            nr.soft_delete_guard(5_000_000, 5_100_000, 1000, True, 5_000_000)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_record_fails_open(self):
        # RETRY GAP: record()/psycopg2.connect — single attempt; telemetry failure never breaks the run
        self.pg.side_effect = OSError("pg down")
        nr.record("nas", 0, 3)
        self.assertIn("telemetry write failed", self.out.getvalue())

    def test_rsync_without_rc_marker_is_99(self):
        # RETRY GAP: rsync_files — one attempt; unparseable output -> rc 99, retried by the next pass
        lst = Path(self.tmp.name) / "to.lst"
        lst.write_bytes(b"a\0")
        with patch.object(nr.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, stdout="", stderr="")) as run:
            self.assertEqual(nr.rsync_files("nas", "/src", "/dst", str(lst)), (99, None, None))
        self.assertEqual(run.call_count, 2)

    def test_failed_incremental_retries_same_window(self):
        self.marker["nas"] = OLD
        fake = _Fake(incr=[("f", "x.txt", 1, OLD)], rsync_rc=12)
        with patch.object(nr.subprocess, "run", side_effect=fake):
            ok, _ = nr.run_incremental(*nr.job_of("nas"), apply=True)
            self.assertFalse(ok)
            self.assertEqual(self.marker["nas"], OLD)          # not advanced
            fake.rsync_rc = 0
            ok, _ = nr.run_incremental(*nr.job_of("nas"), apply=True)
        self.assertTrue(ok)
        scans = [r for h, r in fake.calls if "-newermt" in r]
        self.assertEqual(len(set(scans)), 1)                   # second pass rescanned the same window
        self.assertEqual(self.marker["nas"], NOW - nr.SETTLE_S)


class TestUnit(_Base):
    def test_exclusions(self):
        for bad in ("#recycle/x", "a/#recycle/x", "x/@eaDir/y", "a/.DS_Store", "Foo.app/Contents/x",
                    "#snapshot/1", "#nova_deleted/2026-10-17/a.txt", "sub/#nova_deleted/x"):
            self.assertTrue(nr.EXCL.search(bad), bad)
        for good in ("photos/recycle_bin.jpg", "#nova_deletedness.txt", "notes/nova_deleted.txt"):
            self.assertIsNone(nr.EXCL.search(good), good)

    def test_scan_records_skip_clock_tabs_and_exclusions(self):
        p = Path(self.tmp.name) / "s.lst"
        p.write_bytes(_full([("weird\tname.txt", 42, OLD), ("#recycle/x", 1, OLD),
                             ("#nova_deleted/d/y", 1, OLD), ("line\nbreak.txt", 3, OLD)]))
        self.assertEqual(nr.load_sizes(str(p)), {"weird\tname.txt": "42", "line\nbreak.txt": "3"})

    def test_iter_records_across_chunk_boundary(self):
        p = Path(self.tmp.name) / "c.lst"
        p.write_bytes(b"abc\0defgh\0ij")
        self.assertEqual(list(nr.iter_records(str(p), chunk=4)), ["abc", "defgh", "ij"])

    def test_parse_stats(self):
        self.assertEqual(nr.parse_stats(STATS), (2, 1234))
        self.assertEqual(nr.parse_stats("garbage"), (None, None))

    def test_guard_reasons(self):
        g = nr.soft_delete_guard
        self.assertIn("empty", g(0, 10, 0, True))
        self.assertIn("short", g(80, 100, 0, True, prev_src=100))
        self.assertIn("short", g(40, 100, 0, True))
        self.assertIn("CIFS", g(100, 100, 1, False, 100))
        self.assertIn("exceed", g(10_000, 10_000, 501, True, 10_000))
        self.assertIn("exceed", g(1_000_000, 1_000_000, 10_001, True, 1_000_000))
        self.assertIsNone(g(1_000_000, 1_000_000, 10_000, True, 1_000_000))   # 1% allowed
        self.assertIsNone(g(1000, 1000, 500, True, 1000))                      # 500 floor

    def test_purge_selects_only_old_dated_folders(self):
        fake = _Fake(trash_dirs="2026-09-01\n2026-09-20\n2026-10-17\nnot-a-date\n2026-13-45\n")
        with patch.object(nr.subprocess, "run", side_effect=fake):
            old = nr.purge_deleted("/volume1/nas", 30, apply=True, today=date(2026, 10, 17))
        self.assertEqual(old, ["2026-09-01"])
        rms = [r for _h, r in fake.calls if r.startswith("rm -rf")]
        self.assertEqual(rms, ["rm -rf -- /volume1/nas/'#nova_deleted'/2026-09-01"])

    def test_purge_dry_run_removes_nothing(self):
        fake = _Fake(trash_dirs="2026-01-01\n")
        with patch.object(nr.subprocess, "run", side_effect=fake):
            self.assertEqual(nr.purge_deleted("/volume1/nas", 30, apply=False, today=date(2026, 10, 17)),
                             ["2026-01-01"])
        self.assertFalse([r for _h, r in fake.calls if r.startswith("rm")])


class TestIntegration(_Base):
    def test_record_writes_backup_runs_parameterized(self):
        nr.record("nas", 23, 5, 1234, 60.7, kind="reverse-incr")
        sql = self.pg.return_value.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
        self.assertIn("INSERT INTO telemetry.backup_runs", sql)
        self.assertNotIn("nas", sql)
        self.assertEqual(self.records()[-1], ("nova-backup:nas:reverse-incr", 23, 60, 5, 1234, 1, False))

    def test_marker_lives_in_service_config(self):
        for p in self.ps[3:5]:
            p.stop()
        try:
            cur = self.pg.return_value.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = ({"ts": 123.5},)
            self.assertEqual(nr.get_marker("nas"), 123.5)
            nr.set_marker("nas", 456.0, {"files": 1})
            sql, params = cur.execute.call_args[0]
            self.assertIn("service_config", sql)
            self.assertEqual(params[:2], ("nas_replicate", "marker:nas"))
            self.assertEqual(json.loads(params[2])["ts"], 456.0)
        finally:
            self.ps[3].start(); self.ps[4].start()

    def test_lock_excludes_a_second_process(self):
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT); "
             "fcntl.flock(fd,fcntl.LOCK_EX); print('held',flush=True); time.sleep(30)",
             os.path.join(self.tmp.name, "lock_nas")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(nr.Locked):
                with nr.ShareLock("nas", "test", wait_s=0.3, poll_s=0.1):
                    pass
        finally:
            holder.kill(); holder.wait()
        with nr.ShareLock("nas", "test"):              # released when the holder died
            self.assertEqual(json.loads(Path(self.tmp.name, "lock_nas").read_text())["mode"], "test")

    def test_stale_lock_holder_is_terminated_only_if_ours(self):
        lk = nr.ShareLock("nas", "t", wait_s=0.2, poll_s=0.05)
        stale = {"pid": 4242, "mode": "full", "since": time.time() - nr.LOCK_STALE_S - 10}
        with patch.object(nr.fcntl, "flock", side_effect=BlockingIOError), \
             patch.object(lk, "holder", return_value=stale), patch.object(nr.os, "kill") as kill:
            with patch.object(nr, "_cmdline", return_value="python3 nova_nas_localdiff_reverse.py --apply"):
                with self.assertRaises(nr.Locked):
                    lk.__enter__()
            kill.assert_called_with(4242, 15)
            kill.reset_mock()
            with patch.object(nr, "_cmdline", return_value="/usr/bin/something_else"):
                with self.assertRaises(nr.Locked):
                    lk.__enter__()
            kill.assert_not_called()

    def test_remote_guard_orphan_rsync(self):
        young = _Fake(ps="  77 120 rsync -rlt --files-from=/tmp/reverse_nas_watch_9.lst a b\n")
        with patch.object(nr.subprocess, "run", side_effect=young):
            with self.assertRaises(nr.Locked):
                nr.remote_guard("nas")
            nr.remote_guard("external")                 # other share: not blocked
        old = _Fake(ps=f"  78 {nr.ORPHAN_S + 5} rsync --files-from=/tmp/reverse_nas_full_1.lst\n")
        with patch.object(nr.subprocess, "run", side_effect=old):
            nr.remote_guard("nas")
        self.assertIn("kill 78", [r for _h, r in old.calls])


class TestFunctional(_Base):
    def test_full_apply_copies_delta_and_records_real_bytes(self):
        fake = _Fake()
        rc, posts = self.run_main(["--apply", "nas"], fake)
        self.assertEqual(rc, 0)
        delta = [r for r in nr.iter_records(str(Path(self.tmp.name) / "rev_to_nas.lst"))]
        self.assertEqual(sorted(delta), ["b.txt", "new.txt"])        # hot.txt is still settling
        self.assertTrue(any("2 to copy" in p and "1 settling" in p and "1 replica-only" in p for p in posts))
        self.assertTrue(any("--soft-deletes is OFF" in p for p in posts))
        self.assertEqual(self.records()[-1][:6], ("nova-backup:nas:reverse", 0, 0, 2, 1234, 0))
        self.assertEqual(self.cfg["srccount:nas"]["n"], 4)

    def test_full_failed_copy_exits_1(self):
        rc, posts = self.run_main(["--apply", "nas"], _Fake(rsync_rc=12))
        self.assertEqual(rc, 1)
        self.assertIn("failed", posts[-1])

    def test_incremental_marker_advances_only_on_success(self):
        self.marker["nas"] = OLD
        incr = [("f", "new.txt", 7, OLD + 10), ("f", "hot.txt", 9, NOW - 5), ("f", "#recycle/z", 1, OLD),
                ("d", "newdir", 0, OLD + 10)]
        fake = _Fake(incr=incr)
        with patch.object(nr.subprocess, "run", side_effect=fake):
            ok, s = nr.run_incremental(*nr.job_of("nas"), apply=False)
        self.assertTrue(ok)
        self.assertEqual((s["files"], s["deferred"], s["dirs_changed"]), (1, 1, 1))
        self.assertEqual(self.marker["nas"], OLD)                    # dry-run never moves it
        self.assertEqual(fake.remotes("rsync"), [])
        self.assertIn(f"-newermt @{int(OLD) - 2}", [r for _h, r in fake.calls if "-newermt" in r][0])
        with patch.object(nr.subprocess, "run", side_effect=fake):
            ok, s = nr.run_incremental(*nr.job_of("nas"), apply=True)
        self.assertTrue(ok)
        self.assertEqual(self.marker["nas"], NOW - nr.SETTLE_S)
        self.assertEqual(self.records()[-1][0], "nova-backup:nas:reverse-incr")

    def test_incremental_unas_scan_failure_keeps_marker(self):
        self.marker["nas"] = OLD
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 255)
        with patch.object(nr.subprocess, "run", side_effect=fake):
            ok, s = nr.run_incremental(*nr.job_of("nas"), apply=True)
        self.assertFalse(ok)
        self.assertEqual(self.marker["nas"], OLD)
        self.assertIn("scan failed", s["error"])

    def test_soft_delete_report_only_moves_nothing(self):
        fake = _Fake()
        rc, posts = self.run_main(["--soft-deletes", "nas"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.remotes("python3 -"), [])
        self.assertTrue(any("soft-delete REPORT (report only)" in p and "would move 1" in p for p in posts))

    def test_soft_delete_apply_runs_mover(self):
        fake = _Fake(mover="MOVED=1 SKIPPED=0 MISSING=0 ERRORS=0\n")
        rc, posts = self.run_main(["--apply", "--soft-deletes", "nas"], fake)
        self.assertEqual(rc, 0)
        mv = fake.remotes("python3 -")
        self.assertEqual(len(mv), 1)
        self.assertIn(f"/volume1/nas/#nova_deleted/{date.today().isoformat()}", mv[0])
        self.assertTrue(any("soft-delete MOVED" in p for p in posts))

    def test_anti_wipe_abort_alerts_and_moves_nothing(self):
        unas = [("a.txt", 10, OLD)]
        syno = [(f"gone{i}", 1, OLD) for i in range(600)] + [("a.txt", 10, OLD)]
        fake = _Fake(unas=unas, syno=syno)
        self.cfg["srccount:nas"] = {"n": 1}
        rc, posts = self.run_main(["--apply", "--soft-deletes", "nas"], fake)
        self.assertEqual(fake.remotes("python3 -"), [])
        self.alert.assert_called()
        title, body = self.alert.call_args[0][:2]
        self.assertIn("ABORTED", title)
        self.assertIn("exceed", body)
        self.assertTrue(any("soft-delete ABORTED" in p for p in posts))

    def test_dead_mount_aborts_soft_delete(self):
        fake = _Fake(mount_ok=False)
        self.cfg["srccount:nas"] = {"n": 4}
        self.run_main(["--apply", "--soft-deletes", "nas"], fake)
        self.assertEqual(fake.remotes("python3 -"), [])
        self.assertIn("CIFS", self.alert.call_args[0][1])

    def test_mover_moves_into_dated_trash_and_skips_live_files(self):
        with tempfile.TemporaryDirectory() as d:
            dst, cifs = Path(d) / "dst", Path(d) / "cifs"
            trash = dst / "#nova_deleted" / "2026-10-17"
            (dst / "sub").mkdir(parents=True)
            (cifs / "sub").mkdir(parents=True)
            (dst / "sub" / "gone.txt").write_text("g")
            (dst / "sub" / "still.txt").write_text("s")
            (cifs / "sub" / "still.txt").write_text("s")     # still on the UNAS
            lst = Path(d) / "l"
            lst.write_bytes(b"sub/gone.txt\0sub/still.txt\0sub/missing.txt\0")
            r = subprocess.run([sys.executable, "-", str(lst), str(dst), str(cifs), str(trash)],
                               input=nr.MOVER, capture_output=True, text=True, timeout=30)
            self.assertIn("MOVED=1 SKIPPED=1 MISSING=1 ERRORS=0", r.stdout)
            self.assertTrue((trash / "sub" / "gone.txt").exists())
            self.assertFalse((dst / "sub" / "gone.txt").exists())
            self.assertTrue((dst / "sub" / "still.txt").exists())
            self.assertFalse(lst.exists())

    def test_copy_batch_filters_and_locks(self):
        fake = _Fake()
        with patch.object(nr.subprocess, "run", side_effect=fake):
            res = nr.copy_batch("nas", ["a.txt", "a.txt", "#nova_deleted/x", "/etc/passwd", "../up", "b.txt"])
        self.assertTrue(res["ok"])
        self.assertEqual((res["n"], res["files"], res["bytes"]), (2, 2, 1234))
        self.assertEqual(len(fake.remotes("rsync")), 1)

    def test_locked_share_exits_3(self):
        with patch.object(nr, "run_full", side_effect=nr.Locked("busy")):
            rc, _ = self.run_main(["--apply", "nas"], _Fake())
        self.assertEqual(rc, 3)

    def test_empty_find_skips_job(self):
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        rc, posts = self.run_main(["external"], fake)
        self.assertEqual(rc, 0)
        self.assertTrue(any("find produced no output" in p for p in posts))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_localdiff_reverse; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--soft-deletes", r.stdout)


if __name__ == "__main__":
    unittest.main()
