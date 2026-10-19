#!/usr/bin/env python3
"""Tests for nova_journal_publish_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime as dt
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal_publish_check.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="publish_check_test_"))

_spec = importlib.util.spec_from_file_location("publish_check_under_test", SCRIPT)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _run(status="failure", code=1, err="", out=""):
    return (status, code, err, out, dt.datetime.now())


def _item(**kw):
    base = {"task": "journal_security", "script": "nova_journal_security.py", "args": [],
            "schedule": "cron 0 9 * * *", "timeout": 600, "owner": "core",
            "due": dt.datetime(2026, 10, 19, 9, 0)}
    base.update(kw)
    return base


def _quiet():
    return redirect_stdout(io.StringIO())


class _Cur:
    """Cursor stand-in for main(): advisory lock, rerun claim/update, alert lookups."""
    def __init__(self, lock=True, claimed=True, alerted=False):
        self.lock, self.claimed, self.alerted, self.sql = lock, claimed, alerted, []
        self._next = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self._next = (self.lock,)
        elif "RETURNING task_id" in sql:
            self._next = ("x",) if self.claimed else None
        elif sql.strip().startswith("SELECT 1 FROM journal_publish_reruns"):
            self._next = (1,) if self.alerted else None
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_sql_is_parameterized_and_no_shell(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        self.assertNotIn("shell=True", SRC)
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_rerun_passes_scheduler_args_as_argv_never_a_shell_string(self):
        hostile = "--days; rm -rf / `id`"
        with patch.object(pc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")) as r, _quiet():
            rc, tail = pc.rerun(_item(script="nova_friends.py", args=["--subject", hostile]))
        argv = r.call_args[0][0]
        self.assertEqual(argv[-2:], ["--subject", hostile])        # one literal argv element
        self.assertTrue(argv[1].endswith("/nova_friends.py"))
        self.assertNotIn("shell", r.call_args[1])
        self.assertEqual((rc, tail), (0, "ok"))


class TestPerformance(unittest.TestCase):
    def test_expected_over_many_tasks_is_fast(self):
        tasks = [{"task": f"t{i}", "script": "x.py", "args": [], "schedule": f"cron {i % 60} {i % 24} * * *",
                  "timeout": 600, "owner": "core"} for i in range(60)]
        t0 = time.monotonic()
        out = pc.expected(dt.datetime(2026, 10, 19, 23, 0), tasks, 26)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertGreater(len(out), 50)          # a few are still inside their grace window

    def test_learn_signatures_on_10k_index_is_bounded(self):
        index = [(1_000_000 + i * 60, f"content/watches/2026-10-{i % 28 + 1:02d}-watches-and-friends-item-{i}.md")
                 for i in range(10_000)]
        runs = [(1_000_000 + k * 6000, 1_000_000 + k * 6000 + 30) for k in range(30)]
        t0 = time.monotonic()
        pc.learn_signatures(runs, index)
        self.assertLess(time.monotonic() - t0, 5.0)


class TestRetry(unittest.TestCase):
    def test_git_fetch_failure_fails_open(self):
        # RETRY GAP: git fetch — one attempt; on failure the check judges the last-fetched origin/main
        cur = _Cur()
        with patch.object(pc, "git", return_value=subprocess.CompletedProcess([], 128, "", "network down")), \
             patch.object(pc, "_pg", return_value=_Conn(cur)), patch.object(pc, "check", return_value=[]), _quiet() as out:
            self.assertEqual(pc.main([]), 0)
        self.assertIn("git fetch failed", out.getvalue())

    def test_slack_failure_never_raises(self):
        # RETRY GAP: post_alert — nova_config.post_both owns its own urlopen retry; a hard failure returns False
        with patch.dict(sys.modules, {"nova_config": MagicMock(post_both=MagicMock(side_effect=OSError("slack down")),
                                                               SLACK_ALERTS="C0")}), _quiet():
            self.assertFalse(pc.post_alert("x"))

    def test_rerun_timeout_is_reported_not_raised(self):
        with patch.object(pc.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)), _quiet():
            rc, tail = pc.rerun(_item())
        self.assertEqual(rc, 124)
        self.assertIn("timed out", tail)


class TestUnit(unittest.TestCase):
    def test_parse_cron_fields(self):
        m = pc.parse_cron("cron 0 12 * * 2,4,6")
        self.assertTrue(m(dt.datetime(2026, 10, 20, 12, 0)))      # Tuesday
        self.assertFalse(m(dt.datetime(2026, 10, 19, 12, 0)))     # Monday
        sun = pc.parse_cron("cron 10 5 * * 7")                     # 7 == Sunday
        self.assertTrue(sun(dt.datetime(2026, 10, 18, 5, 10)))
        step = pc.parse_cron("cron */15 8-9 * * *")
        self.assertTrue(step(dt.datetime(2026, 10, 19, 9, 45)))
        self.assertFalse(step(dt.datetime(2026, 10, 19, 10, 0)))
        monthly = pc.parse_cron("cron 0 9 1 * *")
        self.assertTrue(monthly(dt.datetime(2026, 11, 1, 9, 0)))
        self.assertFalse(monthly(dt.datetime(2026, 11, 2, 9, 0)))
        vixie = pc.parse_cron("cron 0 9 1 * 1")                    # dom AND dow restricted -> either
        self.assertTrue(vixie(dt.datetime(2026, 10, 19, 9, 0)))   # a Monday, not the 1st
        self.assertIsNone(pc.parse_cron("every 30m"))

    def test_last_due_and_grace(self):
        now = dt.datetime(2026, 10, 19, 10, 0)
        self.assertEqual(pc.last_due("cron 15 21 * * *", now - dt.timedelta(hours=26), now),
                         dt.datetime(2026, 10, 18, 21, 15))
        self.assertIsNone(pc.last_due("cron 0 9 * * 3", now - dt.timedelta(hours=26), now))
        self.assertEqual(pc.grace_minutes(60), 30)
        self.assertEqual(pc.grace_minutes(1800), 40)
        self.assertEqual(pc.grace_minutes(99999), 180)

    def test_error_tail_prefers_outage_line_then_traceback(self):
        out = "[2026-10-18 16:37:08] [journal] Claude Code call failed (haiku) attempt 3/3: [Errno 2] No such file or directory: 'claude'\n[x] LLM generation failed or too short"
        tail = pc.error_tail(_run("success", 0, "", out))
        self.assertIn("No such file or directory: 'claude'", tail)
        tb = pc.error_tail(_run("failure", 1, "Traceback\n  File x\nurllib.error.HTTPError: HTTP Error 402", "chatty stdout"))
        self.assertTrue(tb.endswith("HTTP Error 402"))
        self.assertEqual(pc.error_tail(None), "no scheduler run recorded")

    def test_classify_states(self):
        it = _item()
        with patch.object(pc, "origin_has", return_value=True):
            self.assertEqual(pc.classify(it, [("written", "content/a/x.md", "h", None)], [], True, None)[0], "OK")
        with patch.object(pc, "origin_has", return_value=False):
            self.assertEqual(pc.classify(it, [("written", "content/a/x.md", "h", None)], [], True, None)[0], "STRANDED")
        self.assertEqual(pc.classify(it, [("push:pushed", None, "h", None)], [], True, None)[0], "OK")
        self.assertEqual(pc.classify(it, [], [], False, (True, "f.md"))[0], "OK")
        self.assertEqual(pc.classify(it, [], [], False, (None, ""), (True, "g.md"))[0], "OK")   # manual re-run found
        self.assertEqual(pc.classify(it, [], [_run("running", None)], True, None)[0], "RUNNING")
        self.assertEqual(pc.classify(it, [], [], True, None, ever_ran=False)[0], "NEW")
        self.assertEqual(pc.classify(it, [], [], True, None)[0], "MISSING")
        self.assertEqual(pc.classify(it, [], [_run("failure", 1)], True, None)[0], "MISSING")
        silent = _run("success", 0, "", "Claude Code call failed: [Errno 2] No such file or directory: 'claude'")
        self.assertEqual(pc.classify(it, [], [silent], True, None)[0], "MISSING")    # the 2026-10-18 exit-0 lie
        skip = _run("success", 0, "", "no fresh material today")
        self.assertEqual(pc.classify(it, [], [skip], True, None)[0], "SKIPPED")      # intentional skip: no alert
        self.assertEqual(pc.classify(it, [], [skip], False, (None, ""))[0], "OK")    # unverifiable -> trust exit 0

    def test_learn_signatures_finds_the_jobs_name_and_drops_generic_words(self):
        index, runs = [], []
        for d in range(1, 8):
            t = 1_700_000_000 + d * 86400
            runs.append((t, t + 60))
            index.append((t + 30, f"content/watches/2026-10-{d:02d}-watches-and-friends-topic-{d}.md"))
            index.append((t + 40, f"content/burbank-ops/2026-10-{d:02d}-this-week-noise-{d}.md"))
            index.append((t + 9000, f"content/burbank-ops/2026-10-{d:02d}-this-week-other-{d}.md"))
        sigs = pc.learn_signatures(runs, index)
        self.assertTrue(any("watches-and-friends" in g for g in sigs))
        self.assertNotIn("this-week", sigs)                         # common elsewhere -> not distinctive
        self.assertNotIn("2026-10", " ".join(sigs))                 # dates are never a signature
        self.assertEqual(pc.learn_signatures(runs[:1], index), [])  # too little history


class TestIntegration(unittest.TestCase):
    def _yaml(self, name, body):
        p = TMP / name
        p.write_text(body)
        return p

    def test_load_tasks_reads_both_schedulers(self):
        core = self._yaml("core.yaml", """tasks:
  journal_security: {script: nova_journal_security.py, schedule: cron 0 9 * * *}
  journal_opinion: {script: nova_journal.py, args: [opinion], schedule: cron 0 12 * * 2,4,6}
  journal_art: {script: nova_journal.py, args: [art], schedule: cron 0 4 * * 0, enabled: false}
  journal_emergency_breaking: {script: nova_journal_emergency.py, args: [breaking], schedule: every 30m}
  wan_monitor: {script: nova_wan_monitor.py, schedule: cron 5 * * * *}
  local_burbank: {script: nova_local_burbank.py, schedule: cron 0 11 * * *}
""")
        six = self._yaml("six.yaml", """tasks:
  local_burbank: {script: nova_local_burbank.py, schedule: cron 0 10 * * *}
  journal_security: {script: nova_journal_security.py, schedule: cron 0 9 * * *, enabled: false}
""")
        tasks = {t["task"]: t for t in pc.load_tasks((("core", core), (".6", six)))}
        self.assertEqual(set(tasks), {"journal_security", "journal_opinion", "local_burbank"})
        self.assertEqual(tasks["journal_opinion"]["args"], ["opinion"])
        self.assertEqual(tasks["local_burbank"]["owner"], "core")       # first enabled definition wins
        self.assertEqual(tasks["journal_security"]["owner"], "core")

    def test_uses_watchdog_map_and_alert_channel(self):
        self.assertIn("import nova_article_watchdog as wd", SRC)          # one section map, not a copy
        self.assertIn("nova_config.SLACK_ALERTS", SRC)
        self.assertIn("journal_publish_log", SRC)
        nj_src = (SCRIPTS / "nova_journal.py").read_text()
        self.assertIn("INSERT INTO journal_publish_log", nj_src)           # the ledger this reads is written there
        self.assertIn("pg_try_advisory_lock(47110816)", (SCRIPTS / "nova_article_watchdog.py").read_text())
        self.assertEqual(pc.RERUN_LOCK_KEY, 47110816)

    def test_format_alert_is_brief_and_lists_each_missing(self):
        it = _item(detail="failure exit 1: [Errno 2] No such file or directory: 'claude'", action="rerun")
        msg = pc.format_alert([it, _item(task="opinion_fishbowl", detail="never ran", action="stale")],
                              dt.datetime(2026, 10, 19, 10, 0))
        self.assertIn("2 expected article(s) missing", msg)
        self.assertIn("`journal_security`", msg)
        self.assertIn("re-running once", msg)
        self.assertIn("too old to re-run", msg)
        self.assertLess(len(msg), 800)


class TestFunctional(unittest.TestCase):
    def _main(self, cur, missing_state="MISSING", rerun_rc=0, after_state="OK", argv=()):
        it = _item(due=dt.datetime.now() - dt.timedelta(hours=2), state=missing_state,
                   detail="failure exit 75: LLM UNAVAILABLE")
        states = iter([(missing_state, it["detail"]), (after_state, "content/burbank-ops/x.md")])
        with patch.object(pc, "_pg", return_value=_Conn(cur)), \
             patch.object(pc, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(pc, "check", return_value=[it]), \
             patch.object(pc, "evaluate", side_effect=lambda *a, **k: next(states)), \
             patch.object(pc, "added_index", return_value=[]), \
             patch.object(pc, "rerun", return_value=(rerun_rc, "done")) as rr, \
             patch.object(pc, "post_alert", return_value=True) as pa, _quiet():
            rc = pc.main(list(argv))
        return rc, rr, pa

    def test_missing_article_alerts_then_reruns_once(self):
        cur = _Cur()
        rc, rr, pa = self._main(cur)
        self.assertEqual(rc, 0)
        rr.assert_called_once()
        self.assertEqual(pa.call_count, 2)                                  # alert, then the outcome
        self.assertIn("journal_security", pa.call_args_list[0][0][0])
        self.assertIn("1/1 recovered", pa.call_args_list[1][0][0])
        self.assertTrue(any("INSERT INTO journal_publish_reruns" in s for s, _ in cur.sql))

    def test_idempotent_second_pass_neither_realerts_nor_reruns(self):
        rc, rr, pa = self._main(_Cur(alerted=True, claimed=False))
        rr.assert_not_called()
        pa.assert_not_called()

    def test_landed_meanwhile_is_not_rerun(self):
        cur = _Cur()
        it = _item(due=dt.datetime.now() - dt.timedelta(hours=2), state="MISSING", detail="x")
        with patch.object(pc, "_pg", return_value=_Conn(cur)), \
             patch.object(pc, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(pc, "check", return_value=[it]), patch.object(pc, "added_index", return_value=[]), \
             patch.object(pc, "evaluate", return_value=("OK", "content/a/landed.md")), \
             patch.object(pc, "rerun") as rr, patch.object(pc, "post_alert", return_value=True), _quiet():
            pc.main([])
        rr.assert_not_called()

    def test_error_path_failed_rerun_exits_nonzero_and_dry_run_touches_nothing(self):
        rc, rr, pa = self._main(_Cur(), rerun_rc=75, after_state="MISSING")
        self.assertEqual(rc, 1)
        self.assertIn("still missing", pa.call_args_list[1][0][0])
        cur = _Cur()
        rc, rr, pa = self._main(cur, argv=["--dry-run"])
        rr.assert_not_called()
        pa.assert_not_called()
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))

    def test_busy_lock_means_another_check_is_running(self):
        cur = _Cur(lock=False)
        with patch.object(pc, "_pg", return_value=_Conn(cur)), \
             patch.object(pc, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(pc, "check") as ck, _quiet():
            self.assertEqual(pc.main([]), 0)
        ck.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_is_silent(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)
        r = subprocess.run([sys.executable, "-c", "import nova_journal_publish_check"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
