#!/usr/bin/env python3
"""Tests for nova_journal_stats_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal_stats_poller.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jp = _load("journal_stats_poller_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="journal-stats-test-"))
# every file the poller touches is redirected before any test runs; ~/.openclaw stays untouched
jp.LOG_FILE = TMP / "poller.log"
jp.STATS_FILE = TMP / "state" / "journal_stats.json"
jp.HISTORY_FILE = TMP / "state" / "journal_traffic_history.json"
jp.CONTENT_DIR = TMP / "content"
jp.STATS_FILE.parent.mkdir(parents=True)          # the real workspace/state dir always exists
# jp.subprocess IS the stdlib module: never assign onto it; every gh-reaching test patches `run` locally.


def _proc(stdout="", rc=0):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


def _gh_router(views=None, paths=None, refs=None, runs=None, rc=0):
    """A subprocess.run stand-in that answers each gh invocation from the given payloads."""
    def run(cmd, **kw):
        if cmd[:2] == ["gh", "run"]:
            return _proc(json.dumps(runs or []), rc)
        ep = cmd[2]
        if ep.endswith("traffic/views"):
            return _proc(json.dumps(views if views is not None else {}), rc)
        if ep.endswith("popular/paths"):
            return _proc(json.dumps(paths or []), rc)
        if ep.endswith("popular/referrers"):
            return _proc(json.dumps(refs or []), rc)
        raise AssertionError(f"unexpected gh endpoint {cmd}")
    return MagicMock(side_effect=run)


def _post(section, name, date, title="A post", words=50):
    d = jp.CONTENT_DIR / section
    d.mkdir(parents=True, exist_ok=True)
    body = " ".join(["word"] * words)
    (d / name).write_text(f'---\ntitle: "{title}"\ndate: {date}T08:00:00\n---\n{body}\n')


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _reset_content():
    import shutil
    shutil.rmtree(jp.CONTENT_DIR, ignore_errors=True)
    jp.CONTENT_DIR.mkdir(parents=True)
    (jp.CONTENT_DIR / "essays" / "_index.md").parent.mkdir(exist_ok=True)
    (jp.CONTENT_DIR / "essays" / "_index.md").write_text("---\ntitle: Essays\n---\n")


VIEWS = {"count": 40, "uniques": 9, "views": [
    {"timestamp": f"{_iso(1)}T00:00:00Z", "count": 30, "uniques": 7},
    {"timestamp": f"{_iso(2)}T00:00:00Z", "count": 0, "uniques": 0},
    {"timestamp": f"{_iso(3)}T00:00:00Z", "count": 10, "uniques": 2},
]}
PATHS = [{"path": "/essays/2026-05-09-x/", "title": "X", "count": 12, "uniques": 4},
         {"path": "/dreams", "title": "Dreams", "count": 5, "uniques": 5},
         {"path": "/", "title": "Home", "count": 7, "uniques": 7}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("ghp_", SRC)
        self.assertIn("uses its stored token", SRC)                 # auth lives in gh's keychain store, not here

    def test_gh_is_invoked_as_an_argv_list_never_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        run = _gh_router(views={})
        with patch.object(jp.subprocess, "run", run):
            jp._gh("traffic/views; rm -rf /")
        cmd = run.call_args.args[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[:2], ["gh", "api"])
        self.assertEqual(cmd[2], f"/repos/{jp.REPO}/traffic/views; rm -rf /")
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_path_and_title_inputs_are_passed_through_not_evaluated(self):
        evil = [{"path": "/essays/<script>alert(1)</script>", "title": "{{7*7}}", "count": 1, "uniques": 1}]
        with patch.object(jp.subprocess, "run", _gh_router(paths=evil)):
            out = jp.fetch_top_paths()
        self.assertEqual(out[0]["title"], "{{7*7}}")
        self.assertEqual(out[0]["section"], "essays")


class TestPerformance(unittest.TestCase):
    def test_section_inference_over_10k_paths_is_fast(self):
        raw = [{"path": f"/{jp.SECTIONS[i % 7]}/2026-01-01-p{i}/", "count": 1, "uniques": 1} for i in range(10_000)]
        with patch.object(jp.subprocess, "run", _gh_router(paths=raw)):
            t0 = time.perf_counter()
            out = jp.fetch_top_paths()
            dt = time.perf_counter() - t0
        self.assertLess(dt, 1.0)
        self.assertEqual(len(out), 10_000)
        self.assertEqual(out[3]["section"], "after-dark")

    def test_history_merge_of_10k_days_is_fast(self):
        days = [{"date": f"{2000 + i // 365:04d}-01-{1 + i % 28:02d}", "count": i, "uniques": 1} for i in range(10_000)]
        with patch.object(jp, "HISTORY_FILE", TMP / "perf_history.json"):
            t0 = time.perf_counter()
            hist = jp.update_history({"days": days})
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertGreater(len(hist), 700)


class TestRetry(unittest.TestCase):
    def test_gh_api_fails_open_on_exception_and_nonzero(self):
        # RETRY GAP: _gh — one gh invocation per endpoint; any failure returns None and the fetchers fall back to empty
        with patch.object(jp.subprocess, "run", MagicMock(side_effect=subprocess.TimeoutExpired("gh", 20))), \
             redirect_stdout(io.StringIO()):
            self.assertIsNone(jp._gh("traffic/views"))
            self.assertEqual(jp.fetch_traffic(), {"total_count": 0, "total_uniques": 0, "days": []})
            self.assertEqual(jp.fetch_top_paths(), [])
            self.assertEqual(jp.fetch_referrers(), [])
        with patch.object(jp.subprocess, "run", _gh_router(views={"count": 1}, rc=1)):
            self.assertIsNone(jp._gh("traffic/views"))
        self.assertIn("gh api traffic/views failed", jp.LOG_FILE.read_text())

    def test_deploy_fetch_fails_open(self):
        # RETRY GAP: fetch_recent_deploys — one attempt; failure logs and returns []
        with patch.object(jp.subprocess, "run", MagicMock(side_effect=OSError("gh missing"))), redirect_stdout(io.StringIO()):
            self.assertEqual(jp.fetch_recent_deploys(), [])
        with patch.object(jp.subprocess, "run", _gh_router(runs=[], rc=1)):
            self.assertEqual(jp.fetch_recent_deploys(), [])

    def test_bad_json_from_gh_is_swallowed(self):
        with patch.object(jp.subprocess, "run", MagicMock(return_value=_proc("not json"))), redirect_stdout(io.StringIO()):
            self.assertIsNone(jp._gh("traffic/views"))

    def test_state_reads_fail_open(self):
        with patch.object(jp.Path, "home", return_value=TMP / "nohome"), redirect_stdout(io.StringIO()):
            self.assertEqual(jp.get_scheduler_state(), {})
        bad = TMP / "bad_history.json"; bad.write_text("{broken")
        with patch.object(jp, "HISTORY_FILE", bad):
            hist = jp.update_history({"days": []})
        self.assertEqual(list(hist.values()), [{"count": 0, "uniques": 0}])


class TestUnit(unittest.TestCase):
    def test_fetch_traffic_drops_zero_days(self):
        with patch.object(jp.subprocess, "run", _gh_router(views=VIEWS)):
            t = jp.fetch_traffic()
        self.assertEqual((t["total_count"], t["total_uniques"]), (40, 9))
        self.assertEqual([d["count"] for d in t["days"]], [30, 10])
        self.assertEqual(t["days"][0]["date"], _iso(1))

    def test_section_inference_edges(self):
        with patch.object(jp.subprocess, "run", _gh_router(paths=PATHS)):
            out = jp.fetch_top_paths()
        self.assertEqual([p["section"] for p in out], ["essays", "dreams", "other"])
        self.assertEqual(out[2]["title"], "Home")
        with patch.object(jp.subprocess, "run", _gh_router(paths=[{}])):
            self.assertEqual(jp.fetch_top_paths()[0], {"path": "", "title": "", "count": 0, "uniques": 0, "section": "other"})

    def test_deploys_are_shaped_and_titles_truncated(self):
        runs = [{"databaseId": 1, "displayTitle": "t" * 100, "conclusion": "success", "createdAt": "2026-10-01T00:00:00Z", "url": "u"}]
        with patch.object(jp.subprocess, "run", _gh_router(runs=runs)) as run:
            out = jp.fetch_recent_deploys(limit=3)
        self.assertEqual(out, [{"id": 1, "title": "t" * 70, "conclusion": "success", "created_at": "2026-10-01T00:00:00Z", "url": "u"}])
        self.assertIn("--limit=3", run.call_args.args[0])

    def test_next_run_time_is_always_in_the_future(self):
        now = datetime.now()
        nxt = datetime.fromisoformat(jp.next_run_time(now.hour, now.minute))
        self.assertGreater(nxt, now)
        self.assertLessEqual(nxt - now, timedelta(days=1))
        self.assertEqual((nxt.second, nxt.microsecond), (0, 0))

    def test_scan_content_empty_and_populated(self):
        _reset_content()
        empty = jp.scan_content()
        self.assertEqual(set(empty), set(jp.SECTIONS))
        # a missing section dir must carry the same keys main() sums over (regression: KeyError 'posts_this_week')
        self.assertEqual(empty["dreams"], {"post_count": 0, "coverage_7d": [False] * 7, "coverage_titles": [""] * 7,
                                           "latest_ts": None, "latest_title": "", "age_hours": 9999,
                                           "posts_this_week": 0, "posts_last_week": 0, "words_this_week": 0})
        self.assertEqual(empty["essays"]["post_count"], 0)                       # _index.md is not a post
        _post("essays", "a.md", _iso(0), "Today", 30)
        _post("essays", "b.md", _iso(3), "Three days", 20)
        _post("essays", "c.md", _iso(10), "Old", 100)
        (jp.CONTENT_DIR / "essays" / "nodate.md").write_text("no front matter at all")
        s = jp.scan_content()["essays"]
        self.assertEqual(s["post_count"], 4)
        self.assertEqual(s["coverage_7d"][:4], [True, False, False, True])
        self.assertEqual(s["coverage_titles"][0], "Today")
        self.assertEqual(s["latest_title"], "Today")
        self.assertEqual((s["posts_this_week"], s["posts_last_week"], s["words_this_week"]), (2, 1, 50))
        self.assertLess(s["age_hours"], 48)

    def test_scheduler_state_shapes_every_section(self):
        home = TMP / "home"; (home / ".openclaw/config").mkdir(parents=True, exist_ok=True)
        (home / ".openclaw/config/scheduler_state.json").write_text(json.dumps(
            {"tasks": {"daily_essay": {"last_run": 123, "last_exit_code": 0, "consecutive_failures": 2, "run_count": 9}}}))
        with patch.object(jp.Path, "home", return_value=home):
            st = jp.get_scheduler_state()
        self.assertEqual(set(st), set(jp.SECTIONS))
        self.assertEqual(st["essays"], {"task_id": "daily_essay", "last_run_ts": 123, "last_exit_code": 0, "consecutive_failures": 2,
                                        "run_count": 9, "scheduled_hour": 9, "scheduled_minute": 0})
        self.assertEqual(st["dreams"]["last_run_ts"], None)

    def test_schedule_panel_sorted_by_fire_time(self):
        panel = jp.build_schedule_panel()
        self.assertEqual([p["fires_at"] for p in panel], sorted(p["fires_at"] for p in panel))
        self.assertEqual(panel[0]["section"], "essays")
        self.assertEqual(len(panel), len(jp.SECTION_SCHEDULES))


class TestIntegration(unittest.TestCase):
    def test_schedule_tables_agree_with_sections(self):
        self.assertEqual(set(jp.SECTION_SCHEDULES), set(jp.SECTIONS))
        for task_id, h, m in jp.SECTION_SCHEDULES.values():
            self.assertTrue(0 <= h < 24 and 0 <= m < 60, task_id)

    def test_every_gh_call_targets_the_journal_repo(self):
        run = _gh_router(views={}, paths=[], refs=[], runs=[])
        with patch.object(jp.subprocess, "run", run):
            jp.fetch_traffic(); jp.fetch_top_paths(); jp.fetch_referrers(); jp.fetch_recent_deploys()
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len(cmds), 4)
        for c in cmds:
            self.assertTrue(any(jp.REPO in part for part in c), c)
        self.assertEqual(jp.REPO, "kochj23/nova-journal")

    def test_traffic_feeds_history_upsert(self):
        hf = TMP / "chain_history.json"
        hf.write_text(json.dumps({_iso(3): {"count": 1, "uniques": 1}, "2020-01-01": {"count": 2, "uniques": 2}}))
        with patch.object(jp.subprocess, "run", _gh_router(views=VIEWS)), patch.object(jp, "HISTORY_FILE", hf):
            hist = jp.update_history(jp.fetch_traffic())
        self.assertEqual(hist[_iso(3)], {"count": 10, "uniques": 2})      # upserted from the API
        self.assertEqual(hist["2020-01-01"], {"count": 2, "uniques": 2})  # survives the GitHub 14-day cliff
        self.assertIn(_iso(0), hist)
        self.assertEqual(json.loads(hf.read_text()), hist)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_stats_file(self):
        _reset_content()
        _post("essays", "a.md", _iso(0), "Today", 30)
        _post("dreams", "d.md", _iso(1), "Dream", 10)
        runs = [{"databaseId": 7, "displayTitle": "deploy", "conclusion": "success", "createdAt": "x", "url": "u"}]
        home = TMP / "home"; (home / ".openclaw/config").mkdir(parents=True, exist_ok=True)
        (home / ".openclaw/config/scheduler_state.json").write_text(json.dumps({"tasks": {}}))
        for f in (jp.STATS_FILE, jp.HISTORY_FILE):
            if f.exists():
                f.unlink()
        with patch.object(jp.subprocess, "run", _gh_router(views=VIEWS, paths=PATHS, refs=[{"referrer": "google.com", "count": 3}], runs=runs)), \
             patch.object(jp.Path, "home", return_value=home), redirect_stdout(io.StringIO()) as out:
            jp.main()
        stats = json.loads(jp.STATS_FILE.read_text())
        self.assertEqual(stats["traffic"]["total_count"], 40)
        self.assertEqual(stats["section_views"], {"dreams": 5, "essays": 12, "opinions": 0, "after-dark": 0, "tech-today": 0, "research": 0, "digests": 0})
        self.assertEqual(stats["totals"], {"posts": 2, "posts_this_week": 2, "words_this_week": 40})
        self.assertEqual(stats["recent_deploys"][0]["id"], 7)
        self.assertEqual(stats["referrers"][0]["referrer"], "google.com")
        self.assertEqual(len(stats["schedule"]), 7)
        self.assertIn(_iso(1), stats["traffic_history"])
        self.assertTrue(jp.HISTORY_FILE.exists())
        self.assertIn("Stats written: 2 posts, 40 total views, 1 deploys", out.getvalue())
        self.assertIn("Stats written", jp.LOG_FILE.read_text())

    def test_gh_outage_still_writes_a_complete_stats_file(self):
        _reset_content()
        with patch.object(jp.subprocess, "run", MagicMock(side_effect=OSError("no gh"))), \
             patch.object(jp.Path, "home", return_value=TMP / "nohome"), redirect_stdout(io.StringIO()):
            jp.main()
        stats = json.loads(jp.STATS_FILE.read_text())
        self.assertEqual((stats["traffic"]["total_count"], stats["top_paths"], stats["recent_deploys"], stats["scheduler"]), (0, [], [], {}))
        self.assertEqual(stats["totals"]["posts"], 0)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_or_calls_gh(self):
        # no argparse here: --help would run main() and shell out to gh, so the smoke test is a bare import
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_journal_stats_poller as m; print(m.REPO)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "kochj23/nova-journal")

    def test_no_output_was_written_outside_the_tempdir(self):
        for p in (jp.LOG_FILE, jp.STATS_FILE, jp.HISTORY_FILE, jp.CONTENT_DIR):
            self.assertTrue(str(p).startswith(str(TMP)), p)


if __name__ == "__main__":
    unittest.main()
