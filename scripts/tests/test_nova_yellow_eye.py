#!/usr/bin/env python3
"""Tests for nova_yellow_eye.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_yellow_eye as Y  # noqa: E402

SRC = (SCRIPTS / "nova_yellow_eye.py").read_text()
BORN = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)


class FakeCur:
    """Routes SQL (str() of it, so psycopg2.sql objects work) by keyword to canned rows."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        s = str(sql)
        self.sql.append((s, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in s), [])

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


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_table_names_are_identifiers_not_text(self):
        cur = FakeCur({"to_regclass": [("x",)], "SELECT EXISTS": [(True,)]})
        evil = 'x"; DROP TABLE births; --'
        self.assertTrue(Y.has_rows(cur, [evil]))
        q = [s for s, _ in cur.sql if "SELECT EXISTS" in s][0]
        self.assertIn("Identifier", q)          # quoted by psycopg2.sql, never pasted

    def test_bad_citation_rejected_before_connecting(self):
        with mock.patch.object(Y.W, "connect") as c, mock.patch("builtins.print"):
            self.assertEqual(Y.sign("t", "x; DROP:1", "me"), 2)
            self.assertEqual(Y.sign("t", "no_colon", "me"), 2)
        c.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_parse_and_match_10k(self):
        text = "@@C 2026-10-08T00:00:00+00:00\n" + "\n".join(f"+  task_{i}:" for i in range(10000))
        corpus = {f"test_nova_{i}.py": "x" for i in range(10000)}
        t = time.monotonic()
        fs = Y.parse_first_seen(text)
        hits = sum(Y.has_test(f"nova_{i}.py", corpus) for i in range(0, 10000, 100))
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual((len(fs), hits), (10000, 100))


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg down")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            Y.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    # RETRY GAP: _git — local read-only git, no retry; it fails open to "".
    def test_git_fails_open(self):
        with mock.patch("subprocess.run", side_effect=OSError("no git")), mock.patch("builtins.print"):
            self.assertEqual(Y._git(SCRIPTS, "log"), "")
            self.assertEqual(Y.parse_first_seen(Y._git(SCRIPTS, "log")), {})

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(Y.signed_tasks(FakeCur(boom=True)), {})
            self.assertFalse(Y.ran_ok(FakeCur(boom=True), "t"))


class TestUnit(unittest.TestCase):
    def test_parse_first_seen(self):
        self.assertEqual(Y.parse_first_seen(""), {})
        fs = Y.parse_first_seen("@@C 2026-10-01T00:00:00+00:00\n+  a:\n+  b: 1\n+    script: x\n"
                                "@@C 2026-10-08T00:00:00+00:00\n+  a:  # moved\n-  c:\n")
        self.assertEqual(list(fs), ["a"])
        self.assertEqual(fs["a"].day, 1)

    def test_declared_tables_and_tests(self):
        self.assertEqual(Y.declared_tables("no tables here"), [])
        self.assertTrue(Y.has_test("nova_affect.py", {"test_affect.py": ""}))
        self.assertFalse(Y.has_test("nova_affect.py", {"test_other.py": "nothing relevant"}))

    def test_state_and_failures(self):
        late = BORN + timedelta(hours=73)
        self.assertEqual(Y.state_of(BORN, BORN + timedelta(hours=71), {}, False), "hypercare")
        self.assertEqual(Y.state_of(BORN, late, {"ran_ok": True, "has_rows": None, "has_test": True}, False), "ready")
        self.assertEqual(Y.failures({"ran_ok": False, "has_rows": None, "has_test": True}), ["ran_ok"])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(Y.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_and_tables(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("CREATE TABLE IF NOT EXISTS births", Y.SCHEMA)
        for col in ("signed_by", "signed_row", "closes_at", "owner"):
            self.assertIn(col, Y.SCHEMA)
        self.assertIn("scheduler_runs", SRC)
        self.assertIn("service='yellow_eye' AND key='owner'", SRC)

    def test_busab_reads_the_same_births(self):
        import nova_busab as B
        self.assertIs(B.scheduler_births, Y.scheduler_births)
        self.assertIs(B.script_births, Y.script_births)

    def test_queue_rolls_one_item(self):
        rows = [{"task": "a", "script": "nova_a.py", "born": BORN, "checks": {"ran_ok": False}, "state": "unattended"},
                {"task": "b", "script": "nova_b.py", "born": BORN, "checks": {}, "state": "ready"}]
        cur = FakeCur({"FROM claude_queue": [(41,)]})
        self.assertEqual(Y.file_queue(cur, rows), 41)
        self.assertTrue(any(s.startswith("UPDATE claude_queue") for s in writes(cur)))
        self.assertFalse(any("INSERT" in s for s in writes(cur)))
        desc, ctx = Y.queue_text(rows)
        self.assertTrue(desc.startswith("Yellow Eye: 1 unattended"))
        self.assertIn("awaiting sign-off: b", ctx)
        self.assertIsNone(Y.file_queue(FakeCur(), rows[1:]))   # nothing unattended -> nothing filed


class TestFunctional(unittest.TestCase):
    def _run(self, dry, hours=72, born=BORN):
        cur = FakeCur({"to_regclass": [("births",)], "FROM births WHERE signed_at": [("b_task", "Little Mister")],
                       "FROM scheduler_runs": [], "SELECT EXISTS": [(True,)], "FROM claude_queue WHERE": [],
                       "RETURNING id": [(7,)]})
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "nova_a.py").write_text("CREATE TABLE IF NOT EXISTS a_rows (x int);")
            with mock.patch.object(Y, "SCRIPTS", Path(d)), \
                    mock.patch.object(Y, "current_tasks", return_value={"a_task": "nova_a.py", "b_task": "nova_b.py",
                                                                         "old_task": "nova_o.py"}), \
                    mock.patch.object(Y, "scheduler_births", return_value={"a_task": born, "b_task": born,
                                                                           "old_task": BORN - timedelta(days=30)}), \
                    mock.patch.object(Y, "_test_corpus", return_value={"test_nova_a.py": ""}), \
                    mock.patch.object(Y.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
                rows = Y.run(dry=dry, hours=hours)
        return cur, rows

    def test_run_records_births_and_files_failures(self):
        cur, rows = self._run(False, hours=0)
        st = {r["task"]: r["state"] for r in rows}
        self.assertEqual(st, {"a_task": "unattended", "b_task": "signed"})   # old_task predates START
        self.assertEqual(sum("INSERT INTO births" in s for s in writes(cur)), 2)
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s][0]
        self.assertIn("a_task", q[1])
        self.assertIn("ran_ok", q[2])

    def test_dry_run_writes_nothing(self):
        cur, rows = self._run(True, hours=0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(writes(cur), [])

    def test_hypercare_files_nothing(self):
        cur, rows = self._run(False, born=datetime.now(timezone.utc))
        self.assertNotIn("unattended", {r["state"] for r in rows})
        self.assertFalse(any("claude_queue" in s for s in writes(cur)))

    def test_sign_refuses_empty_table(self):
        cur = FakeCur({"SELECT script FROM births": [("nova_none.py",)], "to_regclass": [(None,)]})
        with mock.patch.object(Y.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            self.assertEqual(Y.sign("a_task", "a_rows:1", "Little Mister"), 2)
        self.assertEqual(writes(cur), [])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_yellow_eye.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_yellow_eye.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
