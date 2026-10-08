#!/usr/bin/env python3
"""7-category tests for nova_big_brother.py's single-instance guard (2026-10-08: per-domain flock,
'all' takes both, main() exits 0 when held). Complements tests/test_big_brother.py, which already
covers the daemon's own 7 categories and the cross-process lock behaviour.

Locks live in a tempdir; main() is driven with log/signal/pid/threads stubbed — no daemon starts,
nothing is posted. Written by Jordan Koch (via Claude)."""
import fcntl
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_big_brother as bb  # noqa: E402

SRC = (SCRIPTS / "nova_big_brother.py").read_text()


def _open_fds():
    return len(os.listdir("/dev/fd"))


class _LockCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bb-lock-")
        self._before = list(bb._INSTANCE_LOCKS)

    def tearDown(self):
        for fd in bb._INSTANCE_LOCKS[len(self._before):]:
            os.close(fd)
        del bb._INSTANCE_LOCKS[len(self._before):]

    def hold(self, name):
        """Hold a domain lock through a separate open file description (flock conflicts in-process too)."""
        fd = os.open(os.path.join(self.dir, f"big-brother-{name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd


class TestSecurity(_LockCase):
    def test_lock_files_are_owner_writable_only_and_hold_just_the_pid(self):
        self.assertIsNone(bb._acquire_single_instance("all", self.dir))
        for name in ("service", "system"):
            p = Path(self.dir) / f"big-brother-{name}.lock"
            self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o022, 0)      # not group/world writable
            self.assertEqual(p.read_text(), str(os.getpid()))

    def test_locks_stay_in_the_run_dir(self):
        self.assertEqual(bb.PID_FILE.parent.name, "run")
        self.assertIsNone(bb._acquire_single_instance("service", self.dir))
        self.assertEqual(sorted(os.listdir(self.dir)), ["big-brother-service.lock"])

    def test_domain_names_are_a_fixed_set_for_the_shipped_domains(self):
        for dom in ("all", "service", "system"):
            self.assertTrue(set(bb._domain_lock_names(dom)) <= {"service", "system"})


class TestPerformance(_LockCase):
    def test_acquire_is_fast_and_nonblocking_under_contention(self):
        self.hold("system")
        t0 = time.perf_counter()
        for _ in range(200):
            self.assertEqual(bb._acquire_single_instance("all", self.dir), "system")
        self.assertLess(time.perf_counter() - t0, 1.0)      # LOCK_NB: never waits on the holder

    def test_no_fd_leak_on_contention(self):
        self.hold("system")
        before = _open_fds()
        for _ in range(50):
            bb._acquire_single_instance("all", self.dir)
        self.assertEqual(_open_fds(), before)


class TestRetry(_LockCase):
    """Retry N/A by design: a held lock means another LIVE instance; retrying would recreate the
    duplicate. The contract is one non-blocking try, then a loud exit 0 (launchd won't respawn-loop)."""
    def test_one_nonblocking_attempt_no_sleep(self):
        self.hold("service")
        with patch.object(bb.time, "sleep") as sl, patch.object(bb.fcntl, "flock", wraps=fcntl.flock) as fl:
            self.assertEqual(bb._acquire_single_instance("service", self.dir), "service")
        sl.assert_not_called()
        self.assertEqual(fl.call_count, 1)
        self.assertTrue(fl.call_args[0][1] & fcntl.LOCK_NB)

    def test_lock_is_reacquirable_once_the_holder_releases(self):
        fd = os.open(os.path.join(self.dir, "big-brother-system.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(bb._acquire_single_instance("system", self.dir), "system")
        os.close(fd)                                        # holder exits -> kernel drops the lock
        self.assertIsNone(bb._acquire_single_instance("system", self.dir))


class TestUnit(_LockCase):
    def test_domain_lock_names(self):
        self.assertEqual(bb._domain_lock_names("all"), ["service", "system"])
        self.assertEqual(bb._domain_lock_names("service"), ["service"])
        self.assertEqual(bb._domain_lock_names("system"), ["system"])

    def test_default_domain_and_run_dir(self):
        with patch.object(bb, "BB_DOMAIN", "system"), patch.object(bb, "PID_FILE", Path(self.dir) / "bb.pid"):
            self.assertIsNone(bb._acquire_single_instance())
        self.assertTrue((Path(self.dir) / "big-brother-system.lock").exists())

    def test_creates_missing_run_dir(self):
        d = Path(self.dir) / "nested" / "run"
        self.assertIsNone(bb._acquire_single_instance("service", d))
        self.assertTrue(d.is_dir())


class TestIntegration(_LockCase):
    def test_all_partial_failure_releases_what_it_took(self):
        self.hold("system")
        self.assertEqual(bb._acquire_single_instance("all", self.dir), "system")
        self.assertEqual(bb._INSTANCE_LOCKS, self._before)                 # nothing retained
        self.assertIsNone(bb._acquire_single_instance("service", self.dir))  # service fd was released

    def test_success_retains_fds_for_process_life(self):
        self.assertIsNone(bb._acquire_single_instance("all", self.dir))
        self.assertEqual(len(bb._INSTANCE_LOCKS) - len(self._before), 2)
        self.assertEqual(bb._acquire_single_instance("service", self.dir), "service")


class TestFunctional(unittest.TestCase):
    def _main(self, held):
        threads = MagicMock()
        with patch.object(bb, "_acquire_single_instance", return_value=held), \
                patch.object(bb, "_write_pid") as wp, patch.object(bb, "log") as lg, \
                patch.object(bb.signal, "signal"), patch.object(bb.threading, "Thread", threads), \
                patch.object(bb, "_load_metrics", side_effect=SystemExit("reached startup")):
            try:
                bb.main()
                started = False
            except SystemExit:
                started = True
        return started, wp, lg, threads

    def test_held_domain_exits_quietly_without_pid_or_threads(self):
        started, wp, lg, threads = self._main("service")
        self.assertFalse(started)
        wp.assert_not_called()
        threads.assert_not_called()
        msg = lg.call_args[0][0]
        self.assertIn("'service' already owned by another instance", msg)
        self.assertEqual(lg.call_args.kwargs["level"], bb.LOG_WARN)

    def test_free_domain_proceeds_to_startup(self):
        started, wp, lg, threads = self._main(None)
        self.assertTrue(started)
        wp.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_guard_symbols_present_and_main_guarded(self):
        for sym in ("_acquire_single_instance", "_domain_lock_names", "_INSTANCE_LOCKS", "main"):
            self.assertTrue(hasattr(bb, sym), sym)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
