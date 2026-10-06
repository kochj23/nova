#!/usr/bin/env python3
"""Tests for nova_bujo.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_bujo.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_bujo_test_"))


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("nbujo", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


bj = _load()
bj.BUJO_DIR = TMP
bj.DAILY_FILE = TMP / "daily.json"
bj.MONTHLY_FILE = TMP / "monthly.json"
bj.FUTURE_FILE = TMP / "future.json"
bj.COLLECTIONS = TMP / "collections"
bj.notify = MagicMock(return_value=True)
bj.git_commit = MagicMock()
bj.remember = MagicMock()


def _task(title="t", status="open", age=0, migs=0, prio="medium"):
    return {"id": bj.short_id(), "title": title, "priority": prio, "tags": [], "status": status,
            "created_at": (datetime.now() - timedelta(days=age)).isoformat(), "completed_at": None,
            "migration_history": [{"from": "a", "to": "b"}] * migs}


def _run(argv):
    out = io.StringIO()
    with patch.object(sys, "argv", ["nova_bujo.py"] + argv), patch.object(bj, "ensure_bujo_dir"), redirect_stdout(out):
        bj.main()
    return out.getvalue()


def _clean():
    for f in (bj.DAILY_FILE, bj.MONTHLY_FILE, bj.FUTURE_FILE):
        f.unlink(missing_ok=True)
    bj.notify.reset_mock(); bj.remember.reset_mock(); bj.git_commit.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_git_is_argv_never_shell(self):
        self.assertNotIn("shell=True", SRC)
        fresh = _load(); fresh.BUJO_DIR = TMP
        with patch.object(fresh.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=1)
            fresh.git_commit("msg; touch pwned")
        self.assertEqual(run.call_args_list[-1][0][0], ["git", "commit", "-m", "msg; touch pwned"])

    def test_memories_are_marked_local_only(self):
        fresh = _load()
        with patch.object(fresh.urllib.request, "urlopen") as uo:
            fresh.remember("private task", ["x"])
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["metadata"]["privacy"], "local-only")


class TestPerformance(unittest.TestCase):
    def test_stale_scan_on_10k_tasks(self):
        data = {f"d{i}": {"tasks": []} for i in range(100)}
        for i in range(10_000):
            data[f"d{i % 100}"]["tasks"].append(_task(age=i % 10, migs=i % 4))
        t0 = time.perf_counter()
        stale, stuck = bj.get_stale_tasks(data), bj.get_stuck_tasks(data)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertGreater(len(stale), 0)
        self.assertEqual(len(stuck), 2500)


class TestRetry(unittest.TestCase):
    def test_remember_fails_open_single_attempt(self):
        # RETRY GAP: remember — one POST to the memory server; failure is logged and swallowed
        fresh = _load()
        with patch.object(fresh.urllib.request, "urlopen", side_effect=OSError("down")) as uo, \
             redirect_stdout(io.StringIO()):
            self.assertIsNone(fresh.remember("x"))
        self.assertEqual(uo.call_count, 1)

    def test_git_commit_failure_is_swallowed(self):
        # RETRY GAP: git_commit — a failing git call is logged, never raised into the CLI
        fresh = _load(); fresh.BUJO_DIR = TMP
        with patch.object(fresh.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git")), \
             redirect_stdout(io.StringIO()):
            self.assertIsNone(fresh.git_commit("m"))


class TestUnit(unittest.TestCase):
    def test_load_json_missing_empty_corrupt(self):
        p = TMP / "x.json"; p.unlink(missing_ok=True)
        self.assertEqual(bj.load_json(p), {})
        p.write_text("  ")
        self.assertEqual(bj.load_json(p), {})
        p.write_text("{bad")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bj.load_json(p), {})

    def test_stale_and_stuck_rules(self):
        self.assertTrue(bj.is_stale(_task(age=bj.STALE_DAYS + 1)))
        self.assertFalse(bj.is_stale(_task(age=bj.STALE_DAYS)))
        self.assertFalse(bj.is_stale(_task(status="completed", age=99)))
        self.assertTrue(bj.is_stuck(_task(migs=bj.STUCK_MIGRATES)))
        self.assertFalse(bj.is_stuck(_task(migs=bj.STUCK_MIGRATES - 1)))

    def test_find_task_and_get_day(self):
        t = _task()
        data = {"2026-01-01": {"tasks": [t]}}
        self.assertEqual(bj.find_task(data, t["id"][:4])[2], t)
        self.assertEqual(bj.find_task(data, "zzzz"), (None, None, None))
        self.assertEqual(bj.get_day({}, "d"), {"tasks": [], "events": [], "notes": []})

    def test_fmt_task_flags(self):
        s = bj.fmt_task({**_task("Ship it", age=30, migs=3), "tags": ["work"]})
        self.assertIn("[STALE]", s)
        self.assertIn("[STUCK]", s)
        self.assertIn("#work", s)


class TestIntegration(unittest.TestCase):
    def test_slack_post_routes_through_notify_bus(self):
        bj.notify.reset_mock()
        bj.slack_post("*Title*\nbody text", dedup_key="k")
        args, kw = bj.notify.call_args
        self.assertEqual(args[0], "Title")
        self.assertEqual((kw["body"], kw["category"], kw["dedup_key"]), ("body text", "journal", "k"))
        self.assertIn("from nova_notify import notify", SRC)

    def test_add_then_migrate_chain(self):
        _clean()
        _run(["add", "task", "Write tests", "--priority", "high"])
        tid = next(iter(bj.load_json(bj.DAILY_FILE).values()))["tasks"][0]["id"]
        out = _run(["migrate", tid, "--to", "2099-01-01"])
        self.assertIn("migration #1", out)
        data = bj.load_json(bj.DAILY_FILE)
        self.assertEqual(data["2099-01-01"]["tasks"][0]["status"], "open")
        self.assertEqual(bj.find_task(data, tid)[2]["status"], "migrated")


class TestFunctional(unittest.TestCase):
    def test_digest_posts_once_with_open_tasks(self):
        _clean()
        bj.save_json(bj.DAILY_FILE, {bj.TODAY: {"tasks": [_task("Pay bills", prio="high")], "events": [], "notes": []}})
        out = _run(["digest"])
        self.assertIn("Pay bills", out)
        bj.notify.assert_called_once()
        self.assertEqual(bj.notify.call_args.kwargs["dedup_key"], f"bujo-digest-{bj.TODAY}")

    def test_digest_quiet_never_posts(self):
        _clean()
        _run(["digest", "--quiet"])
        bj.notify.assert_not_called()

    def test_complete_unknown_task_exits_1(self):
        _clean()
        with self.assertRaises(SystemExit) as cm:
            _run(["complete", "deadbeef"])
        self.assertEqual(cm.exception.code, 1)
        bj.git_commit.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_in_a_sandboxed_home(self):
        # main() runs ensure_bujo_dir() before argparse, so --help gets a throwaway HOME
        home = Path(tempfile.mkdtemp(prefix="bujo_home_"))
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("digest", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(sys, "argv", ["x", "digest"]):
            m = _load()
        m.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
