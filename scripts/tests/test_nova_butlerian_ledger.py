#!/usr/bin/env python3
"""Tests for nova_butlerian_ledger.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_butlerian_ledger as B  # noqa: E402

SRC = (SCRIPTS / "nova_butlerian_ledger.py").read_text()
T = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
WRITES = ("CREATE", "INSERT", "UPDATE", "DELETE")


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last, self.rowcount = routes or {}, boom, [], [], 0
        self._next_id = 100

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        if "RETURNING id" in sql:
            self._next_id += 1
            self._last = [(self._next_id,)]
            return
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None

    def writes(self):
        return [s for s, _ in self.sql if s.lstrip().upper().startswith(WRITES)]


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


DEC = [(1, T - timedelta(hours=4), T, "executed", "jordan", "Jordan via Claude: approved"),
       (2, T, T, "acknowledged", "jordan (slack reply)", "approve all interest skills"),
       (3, T, T, "executed", "jordan", "approved TEMPORARILY, revert later"),
       (4, T, T, "rejected", "jordan", "declined"),
       (5, T, T, "executed", "claude-reviewer", ""),
       (6, T, T, "superseded", "jordan", "")]


def routes(drills_table=True, this_quarter=False):
    return {"FROM coagency_proposals": DEC, "FROM autonomy_ledger": [(T,)], "FROM gateway_traces": [(T,), (T,)],
            "to_regclass": [("butlerian_drills" if drills_table else None,)],
            "WHERE quarter": [(1,)] if this_quarter else []}


class ConfigCur(FakeCur):
    """service_config reads answer by key: consent and runbook_dir."""

    def __init__(self, consent=True, rb_dir=None, **kw):
        super().__init__(**kw)
        self.consent, self.rb_dir = consent, rb_dir

    def execute(self, sql, params=None):
        if "FROM service_config" in sql and params and params[0] == "butlerian":
            self.sql.append((sql, params))
            v = self.consent if params[1] == "aub_drill_consent" else self.rb_dir
            self._last = [] if v is None else [(v,)]
            return
        super().execute(sql, params)


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_ips_or_addresses(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotIn("kochj23" + "@" + "gmail.com", SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_note_stays_a_parameter(self):
        cur = FakeCur()
        evil = "x'); DROP TABLE coagency_proposals; --"
        with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            B.record_drill("find_device", "could", evil)
        for s, p in cur.sql:
            self.assertNotIn(evil, s)
        self.assertTrue(any(p and evil in p for _, p in cur.sql))

    def test_unknown_task_or_result_refused(self):
        with mock.patch.object(B.W, "connect") as c, mock.patch("builtins.print"):
            self.assertEqual(B.record_drill("rm -rf", "could"), 2)
            self.assertEqual(B.record_drill("find_device", "great"), 2)
        c.assert_not_called()

    def test_no_consent_no_offer(self):
        for consent in (None, False, 1, "yes"):
            cur = ConfigCur(consent=consent, routes=routes())
            with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
                self.assertIsNone(B.offer_drill())
            self.assertEqual(cur.writes(), [], consent)

    def test_only_his_requests_counted(self):
        import inspect
        self.assertIn("person = 'jordan'", inspect.getsource(B.gather))

    def test_no_posting(self):
        for bad in ("post_slack", "post_both", "notify(", "chat.postMessage", "urlopen"):
            self.assertNotIn(bad, SRC)


class TestPerformance(unittest.TestCase):
    def test_weekly_10k(self):
        rows = [(i, T - timedelta(hours=i % 50), T - timedelta(days=i % 56), ("executed", "rejected")[i % 2],
                 ("jordan", "claude-reviewer")[i % 3 == 0], "approved") for i in range(10000)]
        t = time.monotonic()
        weeks = B.weekly(rows, [T] * 10000, [T] * 10000)
        B.trend(weeks)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(sum(w["decided"] for w in weeks.values()), 10000)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}
        good = mock.MagicMock()

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg down")
            return good
        fake_pg = mock.MagicMock(connect=flaky)
        with mock.patch.dict(sys.modules, {"psycopg2": fake_pg}):
            self.assertIs(B.W.connect(_sleep=lambda s: None), good)
        self.assertEqual(calls["n"], 3)

    def test_query_failure_contained(self):
        # RETRY GAP: _q — a failed read is not retried; it fails open to "nothing known".
        with mock.patch("builtins.print"):
            self.assertEqual(B.gather(FakeCur(boom=True), T - timedelta(weeks=1), T), {})
            self.assertFalse(B.consented(FakeCur(boom=True)))

    def test_unmounted_runbook_dir_reads_missing(self):
        self.assertEqual(set(B.runbooks("/nonexistent/lagash").values()), {False})


class TestUnit(unittest.TestCase):
    def test_classify(self):
        self.assertTrue(B.classify("executed", "jordan", "approved")["unchanged"])
        self.assertFalse(B.classify("executed", "jordan", "approved but only half")["unchanged"])
        self.assertTrue(B.classify("acknowledged", "jordan (via Claude: yes on anything)", "")["blanket"])
        self.assertEqual(B.classify("rejected", "claude-reviewer x", "")["who"], "delegated")
        self.assertEqual(B.classify("executed", "nova:earned-autonomy", "")["who"], "nova")
        for args in (("superseded", "jordan", ""), ("blocked", "", ""), ("executed", "", ""),
                     ("executed", "someone else", ""), ("pending_human", None, None)):
            self.assertIsNone(B.classify(*args))

    def test_weekly_counts_and_citations(self):
        w = B.weekly(DEC, [T], [T, T])["2026-10-05"]
        self.assertEqual((w["decided"], w["him_accepted"], w["him_unchanged"], w["him_blanket"],
                          w["him_rejected"], w["delegated"]), (5, 3, 2, 1, 1, 1))
        self.assertEqual(w["cited"]["edited"], [3])
        self.assertEqual(w["cited"]["delegated"], [5])
        self.assertEqual(w["dependence"], 0.6)
        self.assertEqual(B.weekly([]), {})
        only_traffic = B.weekly([], [], [T])["2026-10-05"]
        self.assertIsNone(only_traffic["dependence"])
        self.assertIsNone(only_traffic["median_hours_to_accept"])

    def test_trend(self):
        mk = lambda d, n=5: {"decided": n, "dependence": d}  # noqa: E731
        seq = lambda vals: {str(i): mk(v) for i, v in enumerate(vals)}  # noqa: E731
        self.assertEqual(B.trend(seq([.2] * 3 + [.6] * 3)), "rising")
        self.assertEqual(B.trend(seq([.6] * 3 + [.2] * 3)), "falling")
        self.assertEqual(B.trend(seq([.5] * 6)), "steady")
        self.assertEqual(B.trend(seq([.5] * 5)), "insufficient")
        self.assertEqual(B.trend({str(i): mk(.9, 1) for i in range(8)}), "insufficient")

    def test_summary_is_tentative(self):
        s = B.summary(B.weekly(DEC), "rising")
        self.assertIn("may only mean", s)
        self.assertIn("calibration", s)
        self.assertIn("nothing to say", B.summary({}, "insufficient"))

    def test_pick_task_and_quarter(self):
        self.assertEqual(B.pick_task({"a": True, "b": True}, {"a": T}), "b")
        self.assertEqual(B.pick_task({"a": True, "b": True}, {"a": T, "b": T - timedelta(days=1)}), "b")
        self.assertIsNone(B.pick_task({"a": False}, {}))
        self.assertEqual(B.quarter(datetime(2026, 1, 1)), "2026Q1")
        self.assertEqual(B.quarter(datetime(2026, 12, 31)), "2026Q4")

    def test_eight_tasks_and_runbooks(self):
        self.assertEqual(len(B.TASKS), 8)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "find_device.md").write_text("# runbook")
            rb = B.runbooks(d)
        self.assertEqual([t for t, ok in rb.items() if ok], ["find_device"])

    def test_this_monday(self):
        m = B.this_monday(T)
        self.assertEqual((m.weekday(), m.hour), (0, 0))
        self.assertLessEqual(m, T)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(B.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("W.connect()", SRC)
        self.assertIn("W.get_config(cur, SERVICE, \"aub_drill_consent\"", SRC)
        self.assertIn("nova_relationship.quiet_mode", SRC)

    def test_tables_and_config_keys(self):
        for t in ("CREATE TABLE IF NOT EXISTS butlerian_weekly", "CREATE TABLE IF NOT EXISTS butlerian_drills",
                  "quarter text UNIQUE", "week date PRIMARY KEY", "cited jsonb"):
            self.assertIn(t, B.SCHEMA)
        self.assertEqual(B.SERVICE, "butlerian")
        self.assertIn('"aub_drill_consent"', SRC)
        self.assertIn('"runbook_dir"', SRC)

    def test_gather_feeds_weekly(self):
        cur = FakeCur(routes())
        with mock.patch("builtins.print"):
            weeks = B.gather(cur, T - timedelta(weeks=1), T + timedelta(days=1))
        w = weeks["2026-10-05"]
        self.assertEqual((w["autonomous_actions"], w["his_requests"], w["decided"]), (1, 2, 5))
        tables = " ".join(s for s, _ in cur.sql)
        for t in ("coagency_proposals", "autonomy_ledger", "gateway_traces"):
            self.assertIn(t, tables)

    def test_upsert_matches_columns(self):
        self.assertEqual(B.UPSERT.count("%s"), len(B.WEEK_COLS) + 2)
        self.assertIn("ON CONFLICT (week)", B.UPSERT)


class TestFunctional(unittest.TestCase):
    def _run(self, dry):
        cur = FakeCur(routes())
        with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"), \
                mock.patch.object(B, "this_monday", return_value=T + timedelta(days=6)):
            out = B.run(dry=dry)
        return cur, out

    def test_run_writes_weeks(self):
        cur, out = self._run(False)
        self.assertIn("2026-10-05", out["weeks"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS butlerian_weekly" in s for s in cur.writes()))
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO butlerian_weekly" in s]
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1][0], "2026-10-05")

    def test_dry_run_writes_nothing(self):
        cur, out = self._run(True)
        self.assertEqual(cur.writes(), [])
        self.assertEqual(out["trend"], "insufficient")

    def test_offer_with_consent_files_one_question(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dns_failover.md").write_text("# runbook")
            cur = ConfigCur(consent=True, rb_dir=d, routes=routes())
            with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"), \
                    mock.patch.object(B, "quiet_active", return_value=False):
                text = B.offer_drill(now=T)
        self.assertIn("dns_failover.md", text)
        self.assertIn("Either answer is fine", text)
        q = [p for s, p in cur.sql if "INSERT INTO reflection_questions" in s]
        self.assertEqual(len(q), 1)
        self.assertTrue(any("INSERT INTO butlerian_drills" in s and p[0] == "2026Q4" for s, p in cur.sql))

    def test_offer_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dns_failover.md").write_text("# runbook")
            cur = ConfigCur(consent=True, rb_dir=d, routes=routes(drills_table=False))
            with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"), \
                    mock.patch.object(B, "quiet_active", return_value=False):
                self.assertIsNotNone(B.offer_drill(dry=True, now=T))
        self.assertEqual(cur.writes(), [])

    def test_offer_capped_quiet_and_runbookless(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dns_failover.md").write_text("# runbook")
            cases = [(routes(this_quarter=True), d, False), (routes(), d, True), (routes(), "/nonexistent", False)]
            for r, rb, quiet in cases:
                cur = ConfigCur(consent=True, rb_dir=rb, routes=r)
                with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"), \
                        mock.patch.object(B, "quiet_active", return_value=quiet):
                    self.assertIsNone(B.offer_drill(now=T))
                self.assertEqual(cur.writes(), [])

    def test_record_drill_inserts_when_no_offer(self):
        cur = FakeCur()
        with mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            self.assertEqual(B.record_drill("pg_failover", "partly", "needed the runbook twice"), 0)
        self.assertTrue(any("INSERT INTO butlerian_drills (task, ran_at" in s for s, _ in cur.sql))

    def test_pg_down_raises_from_run(self):
        with mock.patch.object(B.W, "connect", side_effect=OSError("down")), mock.patch("builtins.print"):
            with self.assertRaises(OSError):
                B.run(dry=True)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_butlerian_ledger.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_butlerian_ledger.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        for flag in ("--dry-run", "--offer-drill", "--record-drill"):
            self.assertIn(flag, r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertFalse(hasattr(B, "conn"))


if __name__ == "__main__":
    unittest.main()
