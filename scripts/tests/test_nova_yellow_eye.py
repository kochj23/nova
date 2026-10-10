#!/usr/bin/env python3
"""Tests for nova_yellow_eye.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
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

    def test_bad_burial_name_rejected_before_connecting(self):
        with mock.patch.object(Y.W, "connect") as c, mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                Y.main(["--bury", "a b;rm -rf"])
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

    def test_selftest(self):   # also runs the three absorbed organs' selftests
        with mock.patch("builtins.print"):
            self.assertEqual(Y.selftest(), 0)

    def test_scan_reads_each_source_once(self):
        sc, n = Y.Scan(), {"n": 0}

        def reader(p):
            n["n"] += 1
            return "tasks:\n  a:\n    script: nova_a.py\n"
        with mock.patch.object(Y, "_read_text", side_effect=reader):
            self.assertEqual(sc.tasks(), {"a": "nova_a.py"})
            self.assertEqual(sc.read(Y.SCHED), sc.read(str(Y.SCHED)))
        self.assertEqual(n["n"], 1)

    def test_current_tasks_from_text_and_scheduler_births_from_tasks(self):
        with mock.patch("builtins.print"):
            self.assertEqual(Y.current_tasks(Path("x"), ""), {})
            self.assertEqual(Y.current_tasks(Path("x"), "tasks:\n  b: {script: nova_b.py}\n"), {"b": "nova_b.py"})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.yaml"
            f.write_text("x")
            with mock.patch.object(Y, "_git", return_value=""), mock.patch.object(Y, "current_tasks") as ct:
                born = Y.scheduler_births(f, tasks={"t1": "nova_t.py"})
            ct.assert_not_called()
            self.assertEqual(list(born), ["t1"])


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

    def test_absorbed_modules_load_lazily_without_a_cycle(self):
        code = ("import sys; sys.path.insert(0, %r); import nova_yellow_eye as Y; "
                "assert not {'nova_busab', 'nova_earth_boxes', 'nova_valdemar'} & set(sys.modules); "
                "import nova_busab as B; assert B.scheduler_births is Y.scheduler_births; "
                "assert Y.organ('nova_busab') is B; print('ok')") % str(SCRIPTS)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_each_mode_keeps_its_table_and_queue_session(self):
        import nova_busab as B
        import nova_earth_boxes as E
        import nova_valdemar as V
        self.assertEqual({Y.QUEUE_SESSION, E.QUEUE_SESSION, V.QUEUE_SESSION, B.QUEUE_SESSION},
                         {"nova-yellow-eye", "nova-earth-boxes", "nova-valdemar", "nova-busab"})
        for mod, table in ((Y, "births"), (E, "earth_box_burials"), (V, "valdemar_holds"), (B, "busab_weekly")):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", mod.SCHEMA)
        self.assertIn("CREATE SEQUENCE IF NOT EXISTS earth_box_epoch", E.SCHEMA)
        for mod in (Y, E, V, B):   # sessions are registered before any claude_queue insert (2026-10-09 fix)
            src = Path(mod.__file__).read_text()
            self.assertLess(src.index("INSERT INTO claude_sessions"), src.index("INSERT INTO claude_queue"))

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

    def test_modes_dispatch_to_the_absorbed_organs(self):
        import nova_busab as B
        import nova_earth_boxes as E
        import nova_valdemar as V
        with mock.patch.object(E, "run") as er, mock.patch.object(V, "run") as vr, \
                mock.patch.object(V, "seven_months") as vo, mock.patch.object(B, "run") as br, \
                mock.patch.object(Y, "run") as yr, mock.patch.object(Y, "script_births", return_value={"s": BORN}), \
                mock.patch.object(Y, "scheduler_births", return_value={"t": BORN}), \
                mock.patch.object(E, "local_sources", return_value=[("studio", "p", "x")]):
            self.assertEqual(Y.main(["--burials", "--dry-run"]), 0)
            er.assert_called_once_with(dry=True, sources=[("studio", "p", "x")])
            self.assertEqual(Y.main(["--holds"]), 0)
            vr.assert_called_once_with(dry=False)
            self.assertEqual(Y.main(["--oldest", "--dry-run"]), 0)        # --oldest implies --holds
            self.assertEqual(Y.main(["--holds", "--oldest"]), 0)
            self.assertEqual([c.kwargs for c in vo.call_args_list], [{"dry": True}, {"dry": False}])
            self.assertEqual(vr.call_count, 1)
            self.assertEqual(Y.main(["--pace", "--week", "2026-10-05", "--dry-run"]), 0)
            br.assert_called_once_with(dry=True, week=date(2026, 10, 5), scripts={"s": BORN}, entries={"t": BORN})
            self.assertEqual(Y.main(["--run"]), 0)
            self.assertFalse(yr.call_args.kwargs["dry"])

    def test_births_and_pace_share_one_scan(self):
        import nova_busab as B
        cur = FakeCur({"FROM scheduler_runs": [], "FROM agent_docs": [], "to_regclass": [(None,)]})
        with mock.patch.object(Y, "current_tasks", return_value={"a_task": "nova_a.py"}) as ct, \
                mock.patch.object(Y, "scheduler_births", return_value={"a_task": BORN}) as sb, \
                mock.patch.object(Y, "script_births", return_value={"nova_a.py": BORN}) as scb, \
                mock.patch.object(B, "script_births") as b_scb, mock.patch.object(B, "scheduler_births") as b_sb, \
                mock.patch.object(Y, "_test_corpus", return_value={}), \
                mock.patch.object(Y.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            self.assertEqual(Y.main(["--births", "--pace", "--dry-run", "--week", "2026-10-05"]), 0)
        self.assertEqual((ct.call_count, sb.call_count, scb.call_count), (1, 1, 1))
        b_scb.assert_not_called()
        b_sb.assert_not_called()
        self.assertEqual(writes(cur), [])

    def test_one_failing_mode_does_not_stop_the_others(self):
        import nova_valdemar as V
        with mock.patch.object(Y, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(V, "run") as vr, mock.patch("builtins.print"):
            self.assertEqual(Y.main(["--births", "--holds"]), 1)
        vr.assert_called_once()

    def test_bury_and_show_forward(self):
        import nova_busab as B
        import nova_earth_boxes as E
        cur = FakeCur()
        with mock.patch.object(Y.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(E, "script_hash", return_value=None), mock.patch("builtins.print"):
            self.assertEqual(Y.main(["--bury", "sentinel"]), 0)
        ins = [p for s, p in cur.sql if "INSERT INTO earth_box_burials" in s][0]
        self.assertEqual((ins[0], ins[1], ins[2], ins[5]), ("sentinel", "studio", "subagent", "jordan"))
        with mock.patch.object(B, "show", return_value=0) as bs, mock.patch.object(Y, "show", return_value=0) as ys:
            Y.main(["--show", "--pace"])
            Y.main(["--show"])
        bs.assert_called_once()
        ys.assert_called_once()

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
        for mode in ("--births", "--burials", "--holds", "--oldest", "--pace", "--bury", "--week"):
            self.assertIn(mode, r.stdout)

    def test_no_mode_prints_help(self):
        with mock.patch.object(Y.W, "connect") as c, mock.patch("sys.stdout"):
            self.assertEqual(Y.main([]), 0)
        c.assert_not_called()

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
