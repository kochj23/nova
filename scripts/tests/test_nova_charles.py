#!/usr/bin/env python3
"""Tests for nova_charles.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_charles as C  # noqa: E402

SRC = (SCRIPTS / "nova_charles.py").read_text()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
WIN = timedelta(minutes=5)
_URL = mock.patch("urllib.request.urlopen", side_effect=OSError("offline"))


def setUpModule():
    _URL.start()


def tearDownModule():
    _URL.stop()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def scattered(now, n=8):
    """Anomaly rows at days/hours with no daily rhythm, and a BB action 2 min after each."""
    ts = [now - timedelta(days=d, hours=d) for d in range(1, n + 1)]
    rows = [("sensor_silence", t, []) for t in ts]
    acts = [{"ts": t + timedelta(minutes=2), "kind": "restart", "text": "→ Restarted feed",
             "producer": "big-brother restarted feed"} for t in ts]
    return rows, acts


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_kind_stays_a_parameter(self):
        evil = "x'; DROP TABLE claude_queue; --"
        cur = FakeCur({"RETURNING id": [(9,)]})
        C.file_question(cur, {"kind": evil, "producer": "p", "n": 3, "baseline": 0.1, "p": 0.01}, 5, 30)
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO claude_queue" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertIn(evil, ins[1][1])

    def test_question_never_gives_a_verdict_or_names_people(self):
        _desc, ctx = C.question({"kind": "k", "producer": "p", "n": 3, "baseline": 0.1, "p": 0.01}, 5, 30)
        self.assertIn("not a verdict", ctx)
        self.assertIn("never set its cause", ctx)
        self.assertEqual(set(C.SOURCES), {"big_brother", "remediation", "claude_actions", "autonomy", "reach"})

    def test_never_resolves_buick8(self):
        self.assertNotRegex(SRC, r"(?<!\.)\bresolve\(|nova_buick8_log")
        self.assertNotIn("post_both", SRC)


class TestPerformance(unittest.TestCase):
    def test_analyse_10k_actions(self):
        acts = [{"ts": T0 + timedelta(minutes=4 * i), "source": "big_brother", "producer": f"p{i % 7}", "text": ""}
                for i in range(10000)]
        pts = [(T0 + timedelta(minutes=37 * i), "k") for i in range(1000)]
        t = time.monotonic()
        r = C.analyse(pts, acts, T0, T0 + timedelta(days=28), WIN, shuffles=20)
        self.assertLess(time.monotonic() - t, 10.0)
        self.assertEqual(r["points"], 1000)


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            C.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    # RETRY GAP: own_actions (PG reads) — a failed read is not retried; it fails open to "no actions".
    def test_failed_reads_fail_open(self):
        with mock.patch.object(C, "bb_files", return_value=[]), mock.patch("builtins.print"):
            self.assertEqual(C.own_actions(FakeCur(boom=True), T0, T0 + timedelta(days=1)), [])

    def test_settings_unreadable_gives_defaults(self):
        with mock.patch("builtins.print"):
            self.assertEqual(C.settings(FakeCur(boom=True)), C.DEFAULTS)


class TestUnit(unittest.TestCase):
    def test_anomaly_points_episodes_and_coarse_keys(self):
        rows = [("k", T0 + timedelta(minutes=m), [{"occurrence_key": "20261001"}]) for m in (0, 2, 4, 30)]
        pts = C.anomaly_points(rows, T0, T0 + timedelta(days=1), WIN)
        self.assertEqual([p[0] for p in pts], [T0, T0 + timedelta(minutes=30)])
        self.assertEqual(C.anomaly_points([], T0, T0, WIN), [])
        self.assertEqual(C.anomaly_points([("k", T0 - timedelta(days=2), None)], T0, T0 + timedelta(days=1)), [])

    def test_minute_key_is_local_time(self):
        pts = C.anomaly_points([("k", T0, [{"occurrence_key": "2026-10-01 09:00"}])], T0, T0 + timedelta(days=1))
        self.assertIn((datetime(2026, 10, 1, 9, 0, tzinfo=C.W.TZ), "k"), pts)

    def test_keys_near(self):
        acts = [{"ts": T0, "source": "s", "producer": "p"}]
        self.assertEqual(C.keys_near(T0 + timedelta(minutes=6), "k", [T0], acts, WIN), set())
        self.assertEqual(C.keys_near(T0 + timedelta(minutes=5), "k", [T0], acts, WIN),
                         {("all",), ("source", "s"), ("pair", "k", "p")})

    def test_daily_job_is_not_evidence(self):
        pts = [(T0 + timedelta(days=d, hours=3), "k") for d in range(1, 9)]
        acts = [{"ts": T0 + timedelta(days=d, hours=3, minutes=1), "source": "s", "producer": "daily", "text": ""}
                for d in range(0, 10)]
        r = C.analyse(pts, acts, T0, T0 + timedelta(days=10), WIN, shuffles=50)
        self.assertGreater(r["p"], 0.5)          # same time every day: chance explains it

    def test_question_description_is_stable(self):
        a = C.question({"kind": "k", "producer": "p", "n": 3, "baseline": 0.1, "p": 0.01}, 5, 30)[0]
        b = C.question({"kind": "k", "producer": "p", "n": 9, "baseline": 2.0, "p": 0.2}, 5, 30)[0]
        self.assertEqual(a, b)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(C.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_action_audit(self):
        self.assertIn("import nova_action_audit as A", SRC)
        self.assertIn("A.observe_bb(", SRC)
        self.assertIn("A.observe_remediations(", SRC)
        self.assertIs(C._q, C.A._rows)

    def test_own_actions_merges_sources_in_order(self):
        bb = [{"ts": T0 + timedelta(minutes=3), "kind": "restart", "text": "→ Restarted x", "producer": "bb x"}]
        cur = FakeCur({"FROM claude_actions": [(T0 + timedelta(minutes=1), "command", "launchctl kickstart x")],
                       "FROM autonomy_ledger": [(T0 + timedelta(minutes=2), "observe:x", "observe:x done")]})
        with mock.patch.object(C.A, "observe_bb", return_value=bb), mock.patch.object(C, "bb_files", return_value=[]):
            acts = C.own_actions(cur, T0, T0 + timedelta(hours=1), ("big_brother", "claude_actions", "autonomy"))
        self.assertEqual([a["source"] for a in acts], ["claude_actions", "autonomy", "big_brother"])
        ca = [p for s, p in cur.sql if "FROM claude_actions" in s][0]
        self.assertEqual(ca[2], C.QUIET_TYPES)

    def test_bb_files_skips_stale_rotations(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("nova.jsonl", "nova.jsonl.1", "nova.jsonl.2", "nova.jsonl.gz"):
                (Path(d) / n).write_text("")
            old = time.time() - 40 * 86400
            os.utime(Path(d) / "nova.jsonl.2", (old, old))
            with mock.patch.object(C.A, "LOG_DIR", Path(d)):
                got = [p.name for p in C.bb_files(datetime.now(timezone.utc) - timedelta(days=30))]
        self.assertEqual(got, ["nova.jsonl", "nova.jsonl.1"])

    def test_seldon_imports_the_shared_lookup(self):
        seldon = (SCRIPTS / "nova_seldon_axioms.py").read_text()
        self.assertIn("from nova_charles import _q, own_actions", seldon)
        self.assertNotIn("def own_actions", seldon)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes=None, boom=False):
        now = datetime.now(timezone.utc)
        rows, bb = scattered(now)
        cur = FakeCur(routes if routes is not None else {
            "FROM service_config": [({"shuffles": 100},)], "FROM unexplained_events": rows,
            "FROM claude_queue": [], "RETURNING id": [(42,)]}, boom=boom)
        with mock.patch.object(C.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(C, "bb_files", return_value=[]), \
                mock.patch.object(C.A, "observe_bb", return_value=bb), mock.patch("builtins.print"):
            res = C.run(dry=dry)
        return cur, res

    def test_run_writes_and_files_question(self):
        cur, res = self._run(False)
        self.assertEqual(res["coincident"], 8)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS charles_runs" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO charles_runs" in s for s, _ in cur.sql), 1)
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)
        self.assertIn("Was this Charles?", q[0][1])

    def test_dry_run_writes_nothing(self):
        cur, res = self._run(True)
        self.assertEqual(res["points"], 8)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))

    def test_question_not_refiled_within_a_week(self):
        now = datetime.now(timezone.utc)
        rows, _ = scattered(now)
        cur, _ = self._run(False, {"FROM service_config": [({"shuffles": 100},)], "FROM unexplained_events": rows,
                                   "FROM claude_queue": [(7,)]})
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))

    def test_pg_errors_degrade_to_empty(self):
        with mock.patch.object(C, "ensure_schema"):
            cur, res = self._run(True, boom=True)
        self.assertEqual(res["points"], 0)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_charles.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_charles.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotIn("main()", inspect.getsource(C).split('if __name__ == "__main__":')[0].split("def main")[0])


if __name__ == "__main__":
    unittest.main()
