#!/usr/bin/env python3
"""Tests for nova_mina_typescript.py — the 7 house categories (Security, Performance, Retry, Unit,
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

SRC = (SCRIPTS / "nova_mina_typescript.py").read_text()
T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
CASE_ROW = (7, "event_unknown", "sig", "IPS Alert unknown", T0 - timedelta(days=1), T0, "open")


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


ROUTES = {
    "FROM unexplained_events": [CASE_ROW],
    "FROM telemetry.events": [("11", T0 - timedelta(minutes=3), "nova_syslog_server.py", "warn", "ips", "IPS Alert")],
    "FROM syslog_events": [("99", T0 - timedelta(minutes=4), "fw", "snort", 3, "drop | tcp")],
}


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_no_face_data_read(self):
        for sql in M.SOURCES.values():
            self.assertNotRegex(sql, r"face_(presence|unknown|encodings|people)")
        self.assertIn("!~* 'face'", M.SOURCES["telemetry.presence"])
        self.assertIn("!~* 'face'", M.SOURCES["telemetry.events"])

    def test_output_dir_refuses_boot_disk_and_elsewhere(self):
        for bad in ("/tmp/x", str(Path.home() / "typescripts"), "/Volumes/../etc"):
            with self.assertRaises(ValueError):
                M.safe_dir(bad)

    def test_case_id_is_a_parameter(self):
        cur = FakeCur()
        M.load_case(cur, "1; DELETE FROM unexplained_events")
        sql, params = cur.sql[0]
        self.assertNotIn("DELETE", sql)
        self.assertIn("DELETE", params[0])


class TestPerformance(unittest.TestCase):
    def test_collate_10k(self):
        rows = {s: [(str(i), T0 + timedelta(seconds=i), "x", f"m{i}") for i in range(2000)] for s in M.SOURCES}
        t = time.monotonic()
        lines = M.collate(rows)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(len(lines), 10000)
        self.assertTrue(all(a["ts"] <= b["ts"] for a, b in zip(lines, lines[1:])))

    def test_every_source_is_capped(self):
        for sql in M.SOURCES.values():
            self.assertIn("LIMIT %s", sql)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            M.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_query_failure_fails_open(self):
        # RETRY GAP: _q — a failed read is not retried; it degrades to no rows.
        with mock.patch("builtins.print"):
            self.assertEqual(M._q(FakeCur(boom=True), "SELECT 1"), [])
            self.assertIsNone(M.load_case(FakeCur(boom=True), 1))


class TestUnit(unittest.TestCase):
    def test_window_is_four_hours_utc(self):
        s, e = M.window(datetime(2026, 10, 1, 12))
        self.assertEqual(e - s, timedelta(hours=4))
        self.assertEqual(s.tzinfo, timezone.utc)

    def test_line_hash_and_escaping(self):
        r = ("a|b", T0, "x | y", None, "")
        ln = M.line("syslog_events", r)
        self.assertNotIn("|", ln["text"] + ln["row"])
        self.assertEqual(ln["hash"], M.row_hash("syslog_events", r))
        self.assertNotEqual(ln["hash"], M.row_hash("syslog_events", ("a|b", T0, "x | z", None, "")))

    def test_empty_collate_and_render(self):
        self.assertEqual(M.collate({}), [])
        doc = M.render({"id": 1, "kind": "k", "description": "d", "last_seen": T0}, [], None, ["syslog_events"])
        self.assertIn("Lines: 0; capped", doc)
        self.assertNotIn("Perishable", doc)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(M.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_rama_window import expiries, retention", SRC)
        self.assertIn('W.get_config(cur, SERVICE, "out_dir", DEFAULT_OUT)', SRC)

    def test_build_carries_rama_expiry_and_sorted_lines(self):
        text = M.build(FakeCur(ROUTES), dict(zip(("id", "kind", "signature", "description", "first_seen",
                                                  "last_seen", "status"), CASE_ROW)))
        self.assertIn("## Perishable evidence (Rama Window)", text)
        self.assertIn("| scanner_audio |", text)
        self.assertLess(text.index("| syslog_events | 99"), text.index("| telemetry.events | 11"))

    def test_rama_window_matches(self):
        import nova_rama_window as R
        items = R.due([{"id": 1, "kind": "k", "last_seen": T0}], {"x": 10}, T0)
        self.assertEqual(items[0]["window_start"], M.window(T0)[0])


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes=ROUTES, out=None):
        cur = FakeCur(routes)
        with mock.patch.object(M.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(M, "safe_dir", return_value=Path(out or "/nonexistent")), \
                mock.patch("builtins.print"):
            return cur, M.run(7, dry=dry)

    def test_run_writes_append_only_file(self):
        with tempfile.TemporaryDirectory() as d:
            cur, p = self._run(False, out=d)
            self.assertTrue(p.exists())
            body = p.read_text()
            self.assertIn("# Typescript: Buick 8 #7", body)
            self.assertIn("Document sha256:", body)
            with mock.patch.object(M, "safe_dir", return_value=Path(d)):
                M.write(FakeCur(), {"id": 8, "last_seen": T0}, "a", now=T0)
                with self.assertRaises(FileExistsError):   # append-only: never overwrites
                    M.write(FakeCur(), {"id": 8, "last_seen": T0}, "b", now=T0)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("INSERT", "UPDATE", "CREATE")))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cur, text = self._run(True, out=d)
            self.assertEqual(os.listdir(d), [])
        self.assertIn("## Timeline", text)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("INSERT", "UPDATE", "CREATE")))

    def test_unknown_case(self):
        cur, res = self._run(False, routes={})
        self.assertIsNone(res)
        self.assertEqual(M.main.__name__, "main")


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_mina_typescript.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_mina_typescript.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)



class TestMergedWrapper(unittest.TestCase):
    """2026-10-09 (organ audit M13): --case is a thin wrapper onto nova_buick8_log --case."""

    def test_case_routes_to_buick8(self):
        import nova_buick8_log as B8
        with mock.patch.object(B8, "run_case", return_value=0) as rc, mock.patch("builtins.print"):
            self.assertEqual(M.main(["--case", "7", "--dry-run"]), 0)
        rc.assert_called_once_with(7, True)
        self.assertIn("merged into nova_buick8_log.py on 2026-10-09", SRC)


if __name__ == "__main__":
    unittest.main()
