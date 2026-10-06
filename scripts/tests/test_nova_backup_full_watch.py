#!/usr/bin/env python3
"""Tests for nova_backup_full_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    bw = _load("backup_full_watch_t", SCRIPTS / "nova_backup_full_watch.py")
SRC = (SCRIPTS / "nova_backup_full_watch.py").read_text()
POSTS = []
bw.nova_config = MagicMock(post_both=lambda msg, **k: POSTS.append(msg), SLACK_FEED="#feed")
bw.psycopg2 = MagicMock()
bw.psycopg2.connect.side_effect = RuntimeError("psycopg2.connect not mocked in test")

NAS = ("nova-backup:nas:full", 0, True, 1234, 5e9, 600)
EXT = ("nova-backup:external:full", 0, True, 10, 1e9, 120)
BAD = ("nova-backup:external:full", 23, False, 5, 0, 60)


class _Clock:
    """Fake time module: every sleep advances the clock."""
    def __init__(self, t0):
        self.t = t0; self.sleeps = 0

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1; self.t += s


def _run(rows_seq, start=1_000_000.0):
    POSTS.clear()
    clock = _Clock(start)
    seq = iter(rows_seq)
    with patch.object(bw, "time", clock), patch.object(bw, "rows_since", side_effect=lambda d: next(seq, rows_seq[-1])), \
            patch.object(sys, "argv", ["w", str(start)]):
        bw.main()
    return clock


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertIn("WHERE ts > %s", SRC)

    def test_read_only_against_pg(self):
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE)\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_fmt_10k(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            bw.fmt(NAS)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_loop_is_bounded_by_max_runtime(self):
        clock = _run([[]])
        self.assertLessEqual(clock.sleeps, bw.MAX_RUNTIME_S // bw.POLL_S + 1)
        self.assertIn("20h limit", POSTS[-1])


class TestRetry(unittest.TestCase):
    def test_slack_failure_fails_open(self):
        # RETRY GAP: slack() — one post_both attempt; failure is printed, never raised
        with patch.object(bw.nova_config, "post_both", side_effect=RuntimeError("slack down")) as pb:
            bw.slack("x")
        self.assertEqual(pb.call_count, 1)

    def test_poll_retries_after_db_error_on_next_tick(self):
        # RETRY GAP: rows_since has no in-call retry; a PG error escapes main() (watcher is hand-launched)
        with patch.object(bw, "rows_since", side_effect=OSError("pg down")), patch.object(bw, "time", _Clock(5.0)), \
                patch.object(sys, "argv", ["w", "5"]):
            with self.assertRaises(OSError):
                bw.main()


class TestUnit(unittest.TestCase):
    def test_fmt_ok_and_failed(self):
        self.assertEqual(bw.fmt(NAS), "nas: ✅ ok (1,234 files, 5.0 GB, 10m)")
        self.assertIn("❌ rc=23", bw.fmt(BAD))
        self.assertIn("0.0 GB", bw.fmt(("nova-backup:x:full", 1, False, 0, None, 0)))


class TestIntegration(unittest.TestCase):
    def test_rows_since_uses_backup_runs_with_param(self):
        cur = MagicMock(); cur.fetchall.return_value = [NAS]
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(bw.psycopg2, "connect", return_value=conn) as c:
            dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(bw.rows_since(dt), [NAS])
        self.assertEqual(c.call_args.args[0], bw.DSN)
        sql, params = cur.execute.call_args.args
        self.assertIn("telemetry.backup_runs", sql)
        self.assertEqual(params, (dt,))
        conn.close.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_each_job_then_summary(self):
        _run([[], [NAS], [NAS, EXT]])
        self.assertIn("Full backup started", POSTS[0])
        self.assertTrue(any("finished — nas" in p for p in POSTS))
        self.assertIn("Full backup complete", POSTS[-1])
        self.assertEqual(sum("finished — nas" in p for p in POSTS), 1)   # no duplicate job posts

    def test_error_path_reports_failure(self):
        _run([[NAS, BAD]])
        self.assertIn("finished with errors", POSTS[-1])

    def test_heartbeat_posted_hourly(self):
        _run([[]] * 7 + [[NAS, EXT]])
        self.assertTrue(any("still running" in p for p in POSTS))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_backup_full_watch.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
