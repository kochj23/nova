#!/usr/bin/env python3
"""Tests for nova_flop_slate.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_flop_slate.py"
spec = importlib.util.spec_from_file_location("flop_slate_under_test", SCRIPT)
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)
mtm = fs.mtm
SRC = SCRIPT.read_text()


class FakeCursor:
    def __init__(self, rules=(), default_one=(0,), default_all=()):
        self.rules, self.q, self.args = list(rules), [], []
        self.default_one, self.default_all = default_one, list(default_all)
        self.last = ""

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
        return self.default_one if r is None else (r[0] if isinstance(r, list) else r)

    def fetchall(self):
        r = self._hit()
        return list(self.default_all) if r is None else (r if isinstance(r, list) else [r])


def _sig(**kw):
    s = {"tasks": ["t"], "runs": 10, "ok": 10, "ok_out": 10, "events": 0, "incidents": 0, "acked": 0,
         "memories": False, "tables": [], "queue": 0}
    s.update(kw)
    return s


SLATE = ([{"initiative": f"task:h{i}", "class": "hit", "reason": "r", "evidence": {"reason": "r"}} for i in range(3)]
         + [{"initiative": "task:d1", "class": "dud", "reason": "always errors (2 runs, 0 succeeded)",
             "evidence": {"reason": "always errors"}},
            {"initiative": "task:old", "class": "retired", "reason": "disabled: retired", "evidence": {}}])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|app_pass|api_key|token)\s*=\s*['\"][^'\"]+['\"]")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_never_retires_or_edits_anything(self):
        for verb in ("DELETE FROM", "UPDATE ", "DROP TABLE", "TRUNCATE", "enabled: false", "yaml.dump", "write_text"):
            self.assertNotIn(verb, SRC.replace("DO UPDATE SET", ""))
        self.assertEqual(set(re.findall(r"INSERT INTO (\w+) \(", SRC)), {"flop_slate"})

    def test_quarter_input_is_validated(self):
        for bad in ("2026Q5", "26Q1", "2026Q1; DROP TABLE x", ""):
            with self.assertRaises(ValueError):
                fs.quarter_bounds(bad)

    def test_no_private_addresses_or_paths(self):
        self.assertNotRegex(SRC, r"\b192\.168\.\d+\.\d+")
        self.assertNotIn("/Users/", SRC)


class TestPerformance(unittest.TestCase):
    def test_classify_and_stats_10k(self):
        sigs = [_sig(incidents=i % 7 == 0, ok=0 if i % 13 == 0 else 10) for i in range(10000)]
        t = time.monotonic()
        classes = [fs.classify(s)[0] for s in sigs]
        st = fs.flop_stats(classes)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(st["total"], 10000)


class TestRetry(unittest.TestCase):
    # RETRY GAP: yaml_added / files_added (git) — no retry; they fail open to "unknown".
    def test_yaml_added_fails_open(self):
        with mock.patch.object(fs.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(fs.yaml_added("x"))

    def test_files_added_fails_open(self):
        with mock.patch.object(fs.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 120)):
            self.assertEqual(fs.files_added(), {})

    def test_notify_failure_does_not_fail_the_run(self):
        rc, *_ = TestFunctional()._run(["--scheduled", "--quarter", "2026Q3"], notify_ok=False)
        self.assertEqual(rc, 0)


class TestUnit(unittest.TestCase):
    def test_quarter_math(self):
        self.assertEqual(fs.quarter_label(date(2026, 10, 15)), "2026Q4")
        self.assertEqual(fs.quarter_bounds("2026Q3"), (date(2026, 7, 1), date(2026, 10, 1)))
        self.assertEqual(fs.quarter_bounds("2026q4"), (date(2026, 10, 1), date(2027, 1, 1)))
        self.assertEqual(fs.previous_quarter(date(2026, 10, 1)), "2026Q3")
        self.assertEqual(fs.previous_quarter(date(2027, 1, 2)), "2026Q4")

    def test_classify_boxes(self):
        self.assertEqual(fs.classify(_sig(runs=0, ok=0, ok_out=0))[0], "dud")
        self.assertIn("always errors", fs.classify(_sig(ok=0, ok_out=0))[1])
        self.assertEqual(fs.classify(_sig(ok_out=0))[0], "dud")                       # silent success
        self.assertEqual(fs.classify(_sig(ok_out=0), shared_fresh=True)[0], "quiet")
        self.assertEqual(fs.classify(_sig(queue=2))[0], "hit")
        self.assertEqual(fs.classify(_sig(incidents=1, acked=1))[1],
                         "1 incident(s) opened from its notifications, 1 acknowledged")
        self.assertEqual(fs.classify(_sig())[0], "quiet")
        self.assertEqual(fs.classify(_sig(), retired_note="disabled: retired")[0], "retired")
        self.assertEqual(fs.classify({"tasks": [], "via_host": "nova_h.py"})[0], "quiet")

    def test_flop_stats_and_flag(self):
        st = fs.flop_stats(["hit"] * 50 + ["quiet"] * 10 + ["dud"])
        self.assertAlmostEqual(st["flop_rate"], round(1 / 61, 4))
        self.assertEqual(st["flag"], "grade inflation or no real risk-taking")
        self.assertIsNone(fs.flop_stats(["hit"] * 40)["flag"])                  # too few to judge
        self.assertIsNone(fs.flop_stats([])["flop_rate"])

    def test_best_and_written_tables(self):
        self.assertEqual(fs.best(["quiet", "dud", "hit"]), "hit")
        self.assertEqual(fs.best([]), "dud")
        self.assertEqual(fs.written_tables("insert into Foo (a)"), ["foo"])

    def test_render(self):
        body = fs.render("2026Q3", fs.flop_stats(["hit"] * 60), [], {"flop_rate": 0.1})
        self.assertIn("FLAG", body)
        self.assertIn("(last quarter 10.0%)", body)
        self.assertIn("Nobody knows anything", body)


class TestIntegration(unittest.TestCase):
    def test_shares_organ_one_helpers(self):
        self.assertIn("import nova_mark_to_market as mtm", SRC)
        for fn in ("def norm", "def realised", "def unit_signals", "def organ_inventory", "def load_scheduler"):
            self.assertNotIn(fn, SRC)

    def test_queue_counts_normalise_session_ids(self):
        cur = FakeCursor([("FROM claude_queue", [("task-sentinel", 5), ("nova-core-liveness", 2), ("", 9)])])
        q = fs.queue_counts(cur, datetime(2026, 7, 1), datetime(2026, 10, 1))
        self.assertEqual(q, {"task_sentinel": 5, "core_liveness": 2})

    def test_build_slate_composes_inventory_signals_and_wishes(self):
        sched = {"live": {"script": "nova_live.py", "enabled": True, "files": ["scheduler-core.yaml"],
                          "disabled_note": None},
                 "broken": {"script": "nova_broken.py", "enabled": True, "files": ["scheduler-core.yaml"],
                            "disabled_note": None},
                 "old": {"script": "nova_old.py", "enabled": False, "files": ["scheduler.yaml"],
                         "disabled_note": "retired 2026-07-30 (Jordan)"},
                 "future": {"script": "nova_future.py", "enabled": True, "files": ["scheduler-core.yaml"],
                            "disabled_note": None}}
        wishes = [{"id": 42, "title": "Echo Memory", "status": "shipped", "lineage": {"date": "2026-10-02"},
                   "when": datetime(2026, 9, 3)},
                  {"id": 36, "title": "Attention Focus", "status": "shipped",
                   "lineage": {"script": "nova_live.py", "shipped": "2026-09-28"}, "when": None}]
        runs = {"live": {"runs": 5, "ok": 5, "ok_out": 5, "last": 1}, "broken": {"runs": 3, "ok": 0, "ok_out": 0, "last": 1}}
        oc = FakeCursor([("FROM agent_docs", [])])
        q3_end_ms = mtm.ms(datetime(2026, 10, 1).astimezone())
        with mock.patch.object(mtm, "load_scheduler", return_value=sched), \
                mock.patch.object(mtm, "script_texts", return_value={"nova_live.py": "", "nova_broken.py": ""}), \
                mock.patch.object(mtm, "load_wishes", return_value=wishes), \
                mock.patch.object(mtm, "organ_inventory", return_value={}), \
                mock.patch.object(mtm, "run_stats", return_value=runs), \
                mock.patch.object(mtm, "event_stats", return_value={"live": {"events": 3, "incidents": 2, "acked": 0}}), \
                mock.patch.object(mtm, "repo_files", return_value={"nova_live.py"}), \
                mock.patch.object(mtm, "git_log_text", return_value=""), \
                mock.patch.object(fs, "queue_counts", return_value={}), \
                mock.patch.object(fs, "first_runs", return_value={"live": 0, "broken": 0, "future": q3_end_ms + 1}), \
                mock.patch.object(fs, "files_added", return_value={}):
            slate = fs.build_slate(oc, None, "2026Q3", now=datetime(2026, 10, 15).astimezone())
        got = {r["initiative"]: r["class"] for r in slate}
        self.assertEqual(got, {"task:live": "hit", "task:broken": "dud", "task:old": "retired",
                               "wish:42": "dud", "wish:36": "hit"})


class TestFunctional(unittest.TestCase):
    def _run(self, argv, notify_ok=True, have=False, slate=None):
        oc = FakeCursor([("to_regclass('public.flop_slate')", (have,)), ("FROM flop_slate WHERE quarter = %s", (1,))])
        conn = mock.Mock()
        conn.cursor.return_value = oc
        with mock.patch.object(mtm, "connect", return_value=conn), \
                mock.patch.object(fs, "build_slate", return_value=list(slate or SLATE)) as bs, \
                mock.patch("nova_pg_schema.safe_ddl") as ddl, \
                mock.patch("nova_notify.notify", return_value=notify_ok) as note, \
                redirect_stdout(io.StringIO()) as out:
            rc = fs.main(argv)
        return rc, oc, ddl, note, bs, out.getvalue()

    def test_scheduled_quarter_writes_rows_and_digest(self):
        rc, oc, ddl, note, bs, _ = self._run(["--scheduled", "--quarter", "2026Q3"])
        self.assertEqual(rc, 0)
        ddl.assert_called_once()
        rows = [a for q, a in zip(oc.q, oc.args) if "INSERT INTO flop_slate" in q]
        self.assertEqual(len(rows), len(SLATE))
        self.assertTrue(all(r[0] == "2026Q3" for r in rows))
        kw = note.call_args.kwargs
        self.assertEqual(kw["category"], "flop_slate")
        self.assertEqual(kw["meta"]["flop_rate"], 0.4)
        self.assertIn("task:d1", kw["body"])
        self.assertIn("nothing is retired automatically", kw["body"])

    def test_default_quarter_is_the_last_completed(self):
        *_, bs, _ = self._run(["--dry-run"])
        self.assertEqual(bs.call_args[0][2], fs.previous_quarter(date.today()))

    def test_already_done_quarter_is_skipped(self):
        rc, oc, ddl, note, bs, out = self._run(["--scheduled", "--quarter", "2026Q3"], have=True)
        self.assertEqual(rc, 0)
        bs.assert_not_called()
        note.assert_not_called()
        self.assertIn("already on the slate", out)

    def test_wrapper_says_merged_and_runs_the_slate(self):
        err = __import__("io").StringIO()
        with mock.patch.object(fs, "run_slate", return_value=0) as rs, mock.patch("sys.stderr", new=err):
            self.assertEqual(fs.main(["--report", "2"]), 0)
        rs.assert_called_once_with(["--report", "2"])
        self.assertIn("merged into nova_mark_to_market.py --slate", err.getvalue())

    def test_survivor_slate_mode_reaches_run_slate(self):
        with mock.patch.dict(sys.modules, {"nova_flop_slate": fs}), \
                mock.patch.object(fs, "run_slate", return_value=0) as rs:
            self.assertEqual(mtm.main(["--slate", "--scheduled", "--quarter", "2026Q3"]), 0)
        rs.assert_called_once_with(["--scheduled", "--quarter", "2026Q3"])

    def test_bad_quarter_is_an_argument_error(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", new=io.StringIO()):
            fs.main(["--quarter", "Q9"])


class TestFrame(unittest.TestCase):
    def test_selftest_subprocess(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--quarter", r.stdout)

    def test_docstring_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("Nobody knows anything", SRC)
        self.assertIn("MERGED 2026-10-15", SRC)
        self.assertIn("def run_slate(", SRC)


if __name__ == "__main__":
    unittest.main()
