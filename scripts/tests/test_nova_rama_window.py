#!/usr/bin/env python3
"""Tests for nova_rama_window.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import nova_mina_typescript as M  # noqa: E402
import nova_rama_window as R  # noqa: E402

SRC = (SCRIPTS / "nova_rama_window.py").read_text()
NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

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


def writes(cur):
    return [s for s, _ in cur.sql if any(k in s for k in ("CREATE", "INSERT", "UPDATE", "DELETE"))]


ROUTES = {
    "WHERE status='open'": [(33, "sensor_silence", NOW - timedelta(hours=10)),      # audio due in 12 h
                            (34, "adsb_squawk", NOW - timedelta(hours=250))],        # frigate due in 40 h
    "to_regclass": [(None,)],
}


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_person_footage_past_face_ttl_is_never_suggested(self):
        item = {"source": "frigate", "case_id": 1, "window_start": NOW - timedelta(hours=73)}
        self.assertIn("objects and vehicles only", R.export_cmd(item, NOW, 72))
        fresh = dict(item, window_start=NOW - timedelta(hours=10))
        self.assertIn("must be deleted by 2026-10-11 02:00", R.export_cmd(fresh, NOW, 72))

    def test_snapshot_removal_stays_on_data_volumes(self):
        cur = FakeCur({"to_regclass": [("rama_window",)], "r.status='snapshot'": [(1, "/etc/passwd")]})
        with mock.patch("builtins.print"), mock.patch.object(Path, "unlink") as ul:
            R.expire_snapshots(cur)
        ul.assert_not_called()
        self.assertFalse(any("UPDATE" in s for s, _ in cur.sql))


class TestPerformance(unittest.TestCase):
    def test_due_on_10k_cases(self):
        cases = [{"id": i, "kind": "k", "last_seen": NOW - timedelta(seconds=i)} for i in range(10000)]
        t = time.monotonic()
        items = R.due(cases, R.DEFAULT_RETENTION_H, NOW)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(len(items), 10000)           # only scanner_audio is inside 48 h
        self.assertTrue(all(a["score"] >= b["score"] for a, b in zip(items, items[1:])))


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            R.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_config_and_query_failures_fail_open(self):
        # RETRY GAP: retention/_q — reads are not retried; they fall back to the static table / no rows.
        with mock.patch("builtins.print"):
            self.assertEqual(R.retention(FakeCur(boom=True)), R.DEFAULT_RETENTION_H)
            self.assertEqual(R.open_cases(FakeCur(boom=True)), [])
            self.assertEqual(R.face_ttl(FakeCur(boom=True)), 72.0)


class TestUnit(unittest.TestCase):
    def test_expiries(self):
        self.assertEqual(R.expiries(NOW, {"a": 1.5}), {"a": NOW + timedelta(hours=1.5)})
        self.assertEqual(R.expiries(NOW, {}), {})

    def test_due_edges(self):
        ret = {"x": 24}
        gone = [{"id": 1, "kind": "k", "last_seen": NOW - timedelta(hours=30)}]   # already rotated out
        far = [{"id": 2, "kind": "k", "last_seen": NOW + timedelta(hours=100)}]
        self.assertEqual(R.due(gone, ret, NOW), [])
        self.assertEqual(R.due(far, ret, NOW), [])
        self.assertEqual(R.due([], ret, NOW), [])

    def test_queue_text(self):
        items = R.due([{"id": 5, "kind": "k", "last_seen": NOW}], {"syslog": 10}, NOW)
        desc, ctx = R.queue_text(items, NOW, 72)
        self.assertIn("1 open Buick 8 case(s)", desc)
        self.assertIn("--snapshot 5", ctx)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(R.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_mina_typescript import window", SRC)
        self.assertIn("from nova_face_retention import settings", SRC)

    def test_retention_overrides_from_service_config(self):
        cur = FakeCur({"FROM service_config": [({"syslog": 12},)]})
        ret = R.retention(cur)
        self.assertEqual(ret["syslog"], 12)
        self.assertEqual(ret["frigate"], R.DEFAULT_RETENTION_H["frigate"])
        self.assertEqual(cur.sql[0][1], ("rama_window", "retention_hours"))

    def test_snapshot_is_minas_typescript(self):
        cur = FakeCur({"FROM unexplained_events": [(7, "k", "s", "d", NOW, NOW, "open")]})
        with mock.patch.object(R.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(M, "build", return_value="| 2026-10-08 x |\n") as b, \
                mock.patch.object(M, "write", return_value=Path("/Volumes/Data/t.md")) as w, \
                mock.patch("builtins.print"):
            self.assertEqual(R.snapshot(7), "/Volumes/Data/t.md")
        b.assert_called_once()
        w.assert_called_once()
        self.assertTrue(any("'typescript','snapshot'" in s for s, _ in cur.sql))


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes=ROUTES):
        cur = FakeCur(routes)
        with mock.patch.object(R.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            items = R.run(dry=dry, now=NOW)
        return cur, items

    def test_run_records_and_queues(self):
        cur, items = self._run(False)
        self.assertEqual([(i["case_id"], i["source"]) for i in items], [(33, "scanner_audio"), (34, "frigate")])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS rama_window" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO rama_window" in s for s, _ in cur.sql), 2)
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)
        self.assertIn("objects and vehicles only", q[0][2])
        self.assertTrue(any("INSERT INTO service_config" in s for s, _ in cur.sql))   # seeds the table

    def test_dry_run_writes_nothing(self):
        cur, items = self._run(True)
        self.assertEqual(len(items), 2)
        self.assertEqual(writes(cur), [])

    def test_snapshot_dry_run_writes_nothing(self):
        cur = FakeCur({"FROM unexplained_events": [(7, "k", "s", "d", NOW, NOW, "open")]})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(R.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(M, "safe_dir", return_value=Path(d)), mock.patch("builtins.print"):
            text = R.snapshot(7, dry=True)
            self.assertEqual(os.listdir(d), [])
        self.assertIn("## Timeline", text)
        self.assertEqual(writes(cur), [])

    def test_no_open_cases_queues_nothing(self):
        cur, items = self._run(False, {"to_regclass": [(None,)]})
        self.assertEqual(items, [])
        self.assertFalse(any("claude_queue" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_rama_window.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_rama_window.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--snapshot", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
