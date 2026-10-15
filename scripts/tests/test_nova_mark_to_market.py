#!/usr/bin/env python3
"""Tests for nova_mark_to_market.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mark_to_market.py"
spec = importlib.util.spec_from_file_location("mtm_under_test", SCRIPT)
mtm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mtm)
SRC = SCRIPT.read_text()
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    """Answers by the first rule whose substring is in the SQL; records every statement."""

    def __init__(self, rules=(), default_one=(0,), default_all=()):
        self.rules, self.q, self.args = list(rules), [], []
        self.default_one, self.default_all = default_one, list(default_all)
        self.last = ""
        self.connection = mock.Mock()

    def execute(self, sql, args=None):
        self.q.append(str(sql))
        self.args.append(args)
        self.last = str(sql)

    def _hit(self):
        for sub, res in self.rules:
            if sub in self.last:
                return res
        return None

    def fetchone(self):
        r = self._hit()
        if r is None:
            return self.default_one
        return r[0] if isinstance(r, list) else r

    def fetchall(self):
        r = self._hit()
        if r is None:
            return list(self.default_all)
        return r if isinstance(r, list) else [r]


def _results():
    def c(b, r, wd=()):
        return mtm._class(b, r, [{"item": f"x{i}", "why": "y"} for i in wd])
    return {"beliefs": c(819, 0, range(5)), "feature_wishes": c(28, 26, [42, 43]), "organs": c(90, 66, range(3)),
            "attention": c(720, 33.3, [1]), "preoccupations": c(43, 21, range(4)), "skills": c(31, 23, range(2))}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|app_pass|api_key|token)\s*=\s*['\"][^'\"]+['\"]")

    def test_sql_is_parameterized(self):
        # no f-string SQL carrying values; the one dynamic statement is built with psycopg2.sql.Identifier
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertIn("sql.Identifier(schema)", SRC)

    def test_no_private_addresses_paths_or_emails(self):
        self.assertNotRegex(SRC, r"\b(192\.168|10\.\d+\.\d+)\.\d+")
        self.assertNotIn("/Users/", SRC)
        self.assertNotRegex(SRC, r"[\w.]+@[\w-]+\.(com|net|org)")

    def test_read_only_towards_what_it_measures(self):
        for verb in ("DELETE FROM", "UPDATE beliefs", "UPDATE feature_wishes", "UPDATE preoccupations",
                     "UPDATE nova_skills", "DROP TABLE", "TRUNCATE"):
            self.assertNotIn(verb, SRC)
        writes = re.findall(r"INSERT INTO (\w+)", SRC)
        self.assertEqual(set(writes), {"mark_to_market"})


class TestPerformance(unittest.TestCase):
    def test_grounded_topics_10k(self):
        runs = [(f"t{i % 500}", "noted", False, i % 3) for i in range(10000)]
        threads = [(f"t{i}", "note [1]" if i % 2 else "musing") for i in range(10000)]
        t = time.monotonic()
        g = mtm.grounded_topics(runs, threads)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertGreater(len(g), 500)

    def test_wish_evidence_over_many_scripts(self):
        texts = {f"nova_s{i}.py": "x = 1\n" * 200 for i in range(900)}
        t = time.monotonic()
        for n in range(30):
            mtm.wish_evidence({"id": n, "lineage": {"script": f"nova_s{n}.py"}}, set(texts), texts, set(), set(), {})
        self.assertLess(time.monotonic() - t, 5.0)


class TestRetry(unittest.TestCase):
    # RETRY GAP: repo_files / git_log_text / memory_written — no retry; they fail open to a safe default.
    def test_repo_files_falls_back_when_git_missing(self):
        with mock.patch.object(mtm.subprocess, "run", side_effect=FileNotFoundError("git")):
            files = mtm.repo_files(SCRIPTS.parent)
        self.assertIn("nova_mark_to_market.py", files)

    def test_git_log_fails_open(self):
        with mock.patch.object(mtm.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 60)):
            self.assertEqual(mtm.git_log_text(), "")

    def test_table_probe_timeout_is_unknown_not_fatal(self):
        cur = FakeCursor([("pg_class", (10,)), ("information_schema.columns", [("ts",)]), ("pg_index", (1,))])
        real = cur.execute

        def boom(sql, args=None):
            real(sql, args)
            if "LIMIT 1" in str(sql) and "pg_index" not in str(sql) and "pg_class" not in str(sql):
                raise RuntimeError("canceling statement due to statement timeout")
        cur.execute = boom
        with mock.patch("psycopg2.sql.SQL") as S:
            S.return_value.format.return_value = "SELECT 1 FROM t WHERE ts >= %s AND ts < %s LIMIT 1"
            self.assertIsNone(mtm.table_fresh(cur, "t", NOW - timedelta(days=30), NOW))
        self.assertIn("SET statement_timeout = %s", cur.q[-1])      # timeout restored

    def test_selftest_needs_no_network(self):
        with mock.patch("psycopg2.connect", side_effect=AssertionError("no PG in selftest")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(mtm.main(["--selftest"]), 0)


class TestUnit(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(mtm.norm("nova_comet_tail.py"), "comet_tail")
        self.assertEqual(mtm.norm("nova-core-liveness"), "core_liveness")
        self.assertEqual(mtm.norm(None), "")

    def test_ratio(self):
        self.assertIsNone(mtm.ratio(0, 3))
        self.assertEqual(mtm.ratio(819, 0), 0.0)
        self.assertEqual(mtm.ratio(720, 33.3), round(33.3 / 720, 4))

    def test_wish_ref_is_exact(self):
        p = mtm.wish_ref(42)
        self.assertTrue(p.search("built for wish #42"))
        self.assertTrue(p.search("feature_wishes #42"))
        self.assertFalse(p.search("wish #420"))
        self.assertFalse(p.search("#42 alone is not a wish citation"))

    def test_wish_evidence_kinds(self):
        w = {"id": 77, "lineage": {"script": "nova_weight_of_meaning.py", "agent_doc": "weight-organs",
                                   "schedule": "scheduler-core weight_of_meaning every 6h"}}
        ev = mtm.wish_evidence(w, {"nova_weight_of_meaning.py"}, {}, {"weight_of_meaning"}, {"weight-organs"}, {})
        self.assertEqual(len(ev), 3)
        self.assertEqual(mtm.wish_evidence({"id": 42, "lineage": {"date": "2026-10-02"}}, set(), {}, set(), set(), {}), [])

    def test_wish_evidence_ignores_the_measuring_organs(self):
        texts = {"nova_mark_to_market.py": "wish #42", "nova_flop_slate.py": "wish #42"}
        self.assertEqual(mtm.wish_evidence({"id": 42, "lineage": {}}, set(), texts, set(), set(), {}), [])

    def test_grounded_requires_a_citation(self):
        g = mtm.grounded_topics([("cbt", "noted", False, 0), ("Horology", "fizzled", False, 2)],
                                [("philosophy", "the quietest room"), ("fire", "Truck 11 [2]")])
        self.assertEqual(set(g), {"fire"})

    def test_created_tables(self):
        self.assertEqual(mtm.created_tables('CREATE TABLE IF NOT EXISTS "comet_tail" (id int)'), ["comet_tail"])
        self.assertEqual(mtm.created_tables(None), [])

    def test_commissioned(self):
        self.assertTrue(mtm.commissioned('"""x. Little Mister, 2026-10-15: build the organ."""', "nova_x.py"))
        self.assertFalse(mtm.commissioned('"""Little Mister asked for this organ article"""', "nova_ops_article_x.py"))
        self.assertFalse(mtm.commissioned('"""Little Mister asked"""', "nova_y.py"))

    def test_claimed_minutes_reads_the_policy(self):
        m, cite = mtm.claimed_minutes_per_day(SCRIPTS)
        self.assertEqual(m % 60, 0)
        self.assertIn("hours a day", cite)
        m2, cite2 = mtm.claimed_minutes_per_day(Path("/nonexistent"))
        self.assertEqual(m2, mtm.DEFAULT_CLAIM_HOURS * 60)

    def test_wish_shipped_on(self):
        self.assertEqual(mtm.wish_shipped_on({"lineage": {"shipped": "2026-10-01"}}), date(2026, 10, 1))
        self.assertEqual(mtm.wish_shipped_on({"lineage": {"granted_2026_10_04": "x"}}), date(2026, 10, 4))
        self.assertEqual(mtm.wish_shipped_on({"lineage": {}, "when": datetime(2026, 9, 16, 1)}), date(2026, 9, 16))

    def test_due_and_imported_by(self):
        self.assertTrue(mtm.due(date(2026, 11, 1), False))
        self.assertFalse(mtm.due(date(2026, 11, 9), True))
        self.assertTrue(mtm.imported_by("nova_weight_of_presence.py", "import nova_weight_of_presence as wp\n"))
        self.assertTrue(mtm.imported_by("nova_x.py", "    from nova_x import f\n"))
        self.assertFalse(mtm.imported_by("nova_x.py", "# see nova_x.py\n"))

    def test_measure_attention_and_skills_with_fake_cursor(self):
        cur = FakeCursor([("FROM scheduler_runs", (1800, 1790, 1000.0, 400))])
        r = mtm.measure_attention(cur, NOW - timedelta(days=30), NOW, SCRIPTS)
        self.assertEqual(r["detail"]["minutes_per_day"], 33.3)
        self.assertLess(r["ratio"], 0.1)
        self.assertEqual(len(r["writedowns"]), 1)
        cur = FakeCursor([("FROM nova_skills", [("a", 3, 0, 0), ("b", 0, 2, 2), ("c", 0, 0, 1)])])
        r = mtm.measure_skills(cur)
        self.assertEqual((r["booked"], r["realised"]), (3, 2))
        self.assertIn("skill c", r["writedowns"][0]["item"])


class TestIntegration(unittest.TestCase):
    def test_uses_shared_house_helpers(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("nova_pg_schema.safe_ddl(oc, SCHEMA)", SRC)
        self.assertIn("from nova_notify import notify", SRC)

    def test_organ_inventory_merges_docs_commission_wishes_and_tasks(self):
        cur = FakeCursor([("FROM agent_docs", [("nova-watch-organs", "runs `nova_a.py` and gateway.py")])])
        texts = {"nova_a.py": "", "nova_b.py": '"""Little Mister: the sense organ"""', "nova_c.py": "",
                 "nova_mark_to_market.py": '"""Little Mister organ"""'}
        sched = {"a_task": {"script": "nova_a.py", "enabled": True}, "c_off": {"script": "nova_c.py", "enabled": False}}
        wishes = [{"id": 9, "status": "shipped", "lineage": {"script": "nova_c.py", "memory_source": "c_src"}},
                  {"id": 10, "status": "declined", "lineage": {"script": "nova_d.py"}}]
        inv = mtm.organ_inventory(cur, texts, sched, wishes)
        self.assertEqual(set(inv), {"nova_a.py", "nova_b.py", "nova_c.py"})
        self.assertEqual(inv["nova_a.py"]["tasks"], ["a_task"])
        self.assertEqual(inv["nova_c.py"]["tasks"], [])          # disabled task is not a runner
        self.assertEqual(inv["nova_c.py"]["mem_sources"], ["c_src"])

    def test_unit_signals_chain_runs_events_memories(self):
        runs = {"t1": {"runs": 4, "ok": 4, "ok_out": 0, "last": 1}}
        events = {"x": {"events": 2, "incidents": 1, "acked": 0}}
        mc = FakeCursor([("FROM memories", None)], default_one=None)
        units = {"nova_x.py": {"script": "nova_x.py", "tasks": ["t1"], "mem_sources": []},
                 "nova_y.py": {"script": "nova_y.py", "tasks": [], "mem_sources": ["y_src"]}}
        sigs = mtm.unit_signals(FakeCursor(), mc, units, {}, NOW - timedelta(days=30), NOW, runs=runs, events=events)
        self.assertTrue(mtm.realised(sigs["nova_x.py"]))
        self.assertEqual(sigs["nova_x.py"]["incidents"], 1)
        self.assertFalse(mtm.realised(sigs["nova_y.py"]))
        self.assertIn("y_src", [a[0] for a in mc.args if a])      # mem source probed by index

    def test_load_scheduler_reads_both_files_and_comments(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d, "scheduler-core.yaml").write_text(
                "tasks:\n  a:\n    script: nova_a.py\n  b:\n    script: nova_b.py\n    enabled: false  # retired (Jordan)\n")
            Path(d, "scheduler.yaml").write_text("tasks:\n  b:\n    script: nova_b.py\n    enabled: false\n")
            s = mtm.load_scheduler(Path(d))
        self.assertTrue(s["a"]["enabled"])
        self.assertFalse(s["b"]["enabled"])
        self.assertEqual(s["b"]["disabled_note"], "retired (Jordan)")


class TestFunctional(unittest.TestCase):
    def _run(self, argv, measure_side=None):
        oc = FakeCursor([("to_regclass('public.mark_to_market')", (False,))])
        conn = mock.Mock()
        conn.cursor.return_value = oc
        with mock.patch.object(mtm, "connect", return_value=conn), \
                mock.patch.object(mtm, "measure", side_effect=measure_side, return_value=_results()), \
                mock.patch("nova_pg_schema.safe_ddl") as ddl, \
                mock.patch("nova_notify.notify", return_value=True) as note, \
                redirect_stdout(io.StringIO()) as out:
            rc = mtm.main(argv)
        return rc, oc, ddl, note, out.getvalue()

    def test_scheduled_run_writes_six_rows_and_one_digest(self):
        rc, oc, ddl, note, _ = self._run(["--scheduled"])
        self.assertEqual(rc, 0)
        ddl.assert_called_once()
        inserts = [a for q, a in zip(oc.q, oc.args) if "INSERT INTO mark_to_market" in q]
        self.assertEqual(sorted(a[1] for a in inserts), sorted(mtm.CLASSES))
        self.assertEqual(inserts[0][0], date.today().replace(day=1))
        note.assert_called_once()
        self.assertEqual(note.call_args.kwargs["category"], "mark_to_market")
        self.assertIn("beliefs: 0 of 819 realised = 0%", note.call_args.kwargs["body"])

    def test_dry_run_writes_and_notifies_nothing(self):
        rc, oc, ddl, note, out = self._run(["--dry-run"])
        self.assertEqual(rc, 0)
        ddl.assert_not_called()
        note.assert_not_called()
        self.assertFalse(any("INSERT" in q for q in oc.q))
        self.assertIn("write-downs", out.lower())

    def test_measure_failure_writes_nothing(self):
        with self.assertRaises(RuntimeError):
            self._run(["--scheduled"], measure_side=RuntimeError("pg gone"))

    def test_render_shows_last_month(self):
        body = mtm.render(_results(), date(2026, 11, 1), {"beliefs": 0.0, "organs": 0.7})
        self.assertIn("(last month 70%)", body)
        self.assertNotRegex(body, r"192\.168\.|/Users/")


class TestFrame(unittest.TestCase):
    def test_selftest_subprocess(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_help_and_import_do_not_run(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--scheduled", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_docstring_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("ENRON", SRC)


if __name__ == "__main__":
    unittest.main()
