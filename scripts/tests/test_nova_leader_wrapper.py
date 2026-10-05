#!/usr/bin/env python3
"""Tests for nova_leader_wrapper.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_leader_wrapper.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lw = _load("lw", SCRIPT)
ARGV = ["nova_leader_wrapper.py", "--name", "nova-test-svc", "--", "sleep", "1"]


# ── stubs: a PG session, a child process, a clock ─────────────────────────────
class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.conn.dead:
            raise RuntimeError("server closed the connection unexpectedly")
        self.conn.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        locks = self.conn.locks
        return (locks.pop(0) if len(locks) > 1 else locks[0],)


class _Conn:
    """A PG session stub. `locks` is the sequence of pg_try_advisory_lock answers (the last repeats)."""
    def __init__(self, locks=(False,)):
        self.locks = list(locks); self.sql = []; self.dead = False; self.closed = False; self.autocommit = False

    def cursor(self):
        return _Cur(self)

    def close(self):
        self.closed = True

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _DyingConn(_Conn):
    """The session survives the leader's first heartbeat and dies before the next cursor is opened."""
    def cursor(self):
        if any("leader_standby" in s for s, _ in self.sql):
            self.dead = True
        return _Cur(self)


class _Child:
    """Popen stand-in: poll() answers None `ticks` times, then the exit code."""
    def __init__(self, cmd, ticks=1, rc=0):
        self.cmd = cmd; self.pid = 4242; self.ticks = ticks; self._rc = rc; self.returncode = None
        self.terminated = False; self.killed = False; self.signals = []

    def poll(self):
        if self.ticks > 0:
            self.ticks -= 1
            return None
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self._rc

    def send_signal(self, sig):
        self.signals.append(sig)


class _Clock:
    """Deterministic `time`: sleep advances the clock instead of blocking."""
    def __init__(self, start=1000.0):
        self.now = start; self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s); self.now += s


class _InterruptingClock(_Clock):
    """Ctrl-C the standby loop after `after` sleeps so main() can return."""
    def __init__(self, after=3):
        super().__init__(); self.after = after

    def sleep(self, s):
        super().sleep(s)
        if len(self.sleeps) >= self.after:
            raise KeyboardInterrupt


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", lw.DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIn("pg_try_advisory_lock(hashtext(%s))", SRC)
        self.assertEqual(lw.BEAT.count("%s"), 4)
        conn = _Conn([True])
        lw.try_lock(conn, "x'; DROP TABLE leader_standby; --")
        sql, params = conn.sql[0]
        self.assertNotIn("DROP", sql)                     # the name travels as a parameter, never in the SQL
        self.assertEqual(params, ("x'; DROP TABLE leader_standby; --",))

    def test_only_writes_its_own_table(self):
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"leader_standby"})


class TestPerformance(unittest.TestCase):
    def test_parse_and_beat_fast_on_10k(self):
        argv = ["--name", "svc", "--"] + [f"arg{i}" for i in range(10_000)]
        conn = _Conn()
        t0 = time.perf_counter()
        for _ in range(100):
            lw.parse(argv)
        for i in range(10_000):
            lw.beat(conn, "svc", "standby", i)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(conn.sql), 10_000)


class TestRetry(unittest.TestCase):
    def test_main_retries_pg_until_it_connects(self):
        conn = _Conn([True])
        connect = MagicMock(side_effect=[OSError("refused"), OSError("refused"), conn])
        clock = _Clock()
        with patch.object(lw, "connect", connect), patch.object(lw, "time", clock), \
             patch.object(lw, "run_leader", MagicMock(return_value=0)), patch.object(sys, "argv", ARGV):
            with self.assertRaises(SystemExit) as cm:
                lw.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(clock.sleeps, [lw.RETRY_S, lw.RETRY_S])   # constant backoff between candidates

    def test_beat_fails_open_when_the_session_is_gone(self):
        # RETRY GAP: beat — one shot per cadence tick; a failed beat is the lost-session SIGNAL, not retried
        conn = _Conn(); conn.dead = True
        self.assertFalse(lw.beat(conn, "svc", "leader", 1))


class TestUnit(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(lw.parse(["--name", "x", "--", "sleep", "1"]), ("x", ["sleep", "1"]))
        self.assertEqual(lw.parse(["--name", "x", "--", "python", "-c", "--name"])[1], ["python", "-c", "--name"])
        with self.assertRaises(SystemExit):
            lw.parse(["--name", "x", "sleep"])                 # no separator
        with self.assertRaises(SystemExit):
            lw.parse(["--", "sleep"])                          # no name

    def test_try_lock_returns_pg_answer(self):
        self.assertTrue(lw.try_lock(_Conn([True]), "a"))
        self.assertFalse(lw.try_lock(_Conn([False]), "a"))

    def test_beat_writes_role_pid_and_node(self):
        conn = _Conn()
        self.assertTrue(lw.beat(conn, "svc", "leader", 77))
        sql, params = conn.sql[0]
        self.assertIn("INSERT INTO leader_standby", sql)
        self.assertEqual(params, ("svc", lw.NODE, "leader", 77))
        self.assertGreater(lw.BEAT_S, 0); self.assertGreater(lw.RETRY_S, 0)


class TestIntegration(unittest.TestCase):
    def test_standby_never_starts_the_child_while_the_lock_is_held_elsewhere(self):
        conn = _Conn([False])                                   # another node holds pg_advisory_lock
        popen = MagicMock()
        clock = _InterruptingClock(after=3)
        with patch.object(lw, "connect", MagicMock(return_value=conn)), patch.object(lw, "time", clock), \
             patch.object(lw.subprocess, "Popen", popen), patch.object(sys, "argv", ARGV):
            with self.assertRaises(SystemExit) as cm:
                lw.main()
        self.assertEqual(cm.exception.code, 0)
        popen.assert_not_called()
        self.assertEqual(len(conn.ran("pg_try_advisory_lock")), 3)
        self.assertEqual([p[2] for _, p in conn.ran("leader_standby")], ["standby"] * 3)

    def test_standby_takes_over_once_the_lock_frees(self):
        conn = _Conn([False, False, True])
        run_leader = MagicMock(return_value=0)
        clock = _Clock()
        with patch.object(lw, "connect", MagicMock(return_value=conn)), patch.object(lw, "time", clock), \
             patch.object(lw, "run_leader", run_leader), patch.object(sys, "argv", ARGV):
            with self.assertRaises(SystemExit):
                lw.main()
        run_leader.assert_called_once_with(conn, "nova-test-svc", ["sleep", "1"])
        self.assertEqual(clock.sleeps, [lw.RETRY_S, lw.RETRY_S])

    def test_leader_stops_child_when_its_pg_session_dies(self):
        conn = _DyingConn()
        child = _Child(["sleep", "1"], ticks=10_000)
        clock = _Clock()
        with patch.object(lw.subprocess, "Popen", MagicMock(return_value=child)), \
             patch.object(lw, "signal", MagicMock()), patch.object(lw, "time", clock):
            rc = lw.run_leader(conn, "svc", ["sleep", "1"])
        self.assertEqual(rc, 75)                                 # EX_TEMPFAIL -> systemd restarts as candidate
        self.assertTrue(child.terminated)
        self.assertEqual(len(clock.sleeps), lw.BEAT_S)           # one liveness check per BEAT_S seconds


class TestFunctional(unittest.TestCase):
    def _run_main(self, child):
        conn = _Conn([True])
        popen = MagicMock(return_value=child)
        with patch.object(lw, "connect", MagicMock(return_value=conn)), patch.object(lw, "time", _Clock()), \
             patch.object(lw.subprocess, "Popen", popen), patch.object(lw, "signal", MagicMock()), \
             patch.object(sys, "argv", ARGV):
            with self.assertRaises(SystemExit) as cm:
                lw.main()
        return conn, popen, cm.exception.code

    def test_leader_golden_path(self):
        conn, popen, code = self._run_main(_Child(["sleep", "1"], ticks=2, rc=0))
        self.assertEqual(code, 0)
        popen.assert_called_once_with(["sleep", "1"])
        beats = [p for _, p in conn.ran("leader_standby")]
        self.assertEqual(beats[0][2:], ("leader", 4242))
        self.assertEqual(beats[-1][2:], ("standby", None))      # hands the lock back on the way out
        self.assertTrue(conn.ran("pg_advisory_unlock_all"))

    def test_child_exit_code_propagates(self):
        _, _, code = self._run_main(_Child(["sleep", "1"], ticks=1, rc=3))
        self.assertEqual(code, 3)

    def test_bad_argv_exits_with_usage(self):
        with patch.object(sys, "argv", ["nova_leader_wrapper.py", "sleep"]):
            with self.assertRaises(SystemExit) as cm:
                lw.main()
        self.assertIn("--name", str(cm.exception.code))


class TestFrame(unittest.TestCase):
    ENV = {**os.environ, "NOVA_TEST_QUIET": "1"}

    def test_import_is_side_effect_free_and_main_is_guarded(self):
        # --selftest needs a live PG, so the frame check is: import cleanly, never run main() on import
        r = subprocess.run([sys.executable, "-c", "import nova_leader_wrapper"], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=30, env=self.ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_no_args_prints_usage_and_fails(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, env=self.ENV)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--name", r.stderr)


if __name__ == "__main__":
    unittest.main()
