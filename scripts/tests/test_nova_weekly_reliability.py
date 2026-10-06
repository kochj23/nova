#!/usr/bin/env python3
"""Tests for nova_weekly_reliability.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
notify, the structured logger, urlopen (scheduler / memory / health probes) and socket are mocked;
Path.home() is pointed at a tempdir so the daily memory file never lands in the real workspace."""
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_weekly_reliability.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_weekly_reliability_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wr = _load()
wr.notify = MagicMock()          # stub the outbound at module level
wr.log = MagicMock()


def _resp(obj):
    m = MagicMock()
    m.read.return_value = json.dumps(obj).encode()
    return m


STATUS = {"uptime_s": 7200, "total_runs": 1000, "total_failures": 5}
TASKS = {"good": {"run_count": 50, "last_duration": 1.5}, "bad": {"run_count": 9, "consecutive_failures": 4,
         "last_exit_code": 2}, "idle": {"run_count": 0}, "off": {"enabled": False, "run_count": 99}}


class _Main:
    def __init__(self, status=STATUS, tasks=TASKS, health_ok=True, errors=()):
        self.status, self.tasks, self.health_ok, self.errors = status, tasks, health_ok, list(errors)

    def _urlopen(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.urls.append(url)
        if url.endswith("/status"):
            return _resp(self.status)
        if url.endswith("/tasks"):
            return _resp(self.tasks)
        if url.endswith("/stats"):
            return _resp({"count": 12345, "by_source": {"a": 1, "b": 2}})
        if url.endswith("/health") and not self.health_ok:
            raise OSError("down")
        return _resp({})

    def __enter__(self):
        self.urls = []
        self.home = Path(tempfile.mkdtemp())
        self.st = ExitStack()
        self.st.enter_context(patch.object(wr.urllib.request, "urlopen", side_effect=self._urlopen))
        self.notify = self.st.enter_context(patch.object(wr, "notify"))
        self.st.enter_context(patch.object(wr, "log"))
        self.st.enter_context(patch.object(wr, "read_logs", side_effect=lambda **kw: self.errors if kw["level"] == "error" else []))
        self.st.enter_context(patch.object(pathlib.Path, "home", return_value=self.home))
        sock = MagicMock(); sock.connect.side_effect = OSError("refused")
        self.st.enter_context(patch("socket.socket", return_value=sock))
        return self

    def __exit__(self, *a):
        self.st.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def posted(self):
        title, = self.notify.call_args[0]
        return title + "\n" + self.notify.call_args.kwargs["body"]


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token|secret)\s*=\s*['\"]", SRC, re.I))

    def test_health_probes_are_loopback_only(self):
        with _Main() as m:
            wr.main()
        health = [u for u in m.urls if u.endswith("/health")]
        self.assertEqual(len(health), 4)
        self.assertTrue(all(u.startswith("http://127.0.0.1:") for u in health))


class TestPerformance(unittest.TestCase):
    def test_10k_tasks_and_errors_fast(self):
        tasks = {f"t{i}": {"run_count": i % 7, "consecutive_failures": i % 5} for i in range(10_000)}
        errs = [{"source": f"s{i % 50}"} for i in range(10_000)]
        t0 = time.perf_counter()
        with _Main(tasks=tasks, errors=errs) as m:
            wr.main()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("10000 errors", m.posted())


class TestRetry(unittest.TestCase):
    def test_scheduler_and_memory_down_fail_open(self):
        # RETRY GAP: get_scheduler_tasks/get_scheduler_status/get_memory_count — one GET each, {} / (0, 0)
        with patch.object(wr.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(wr.get_scheduler_tasks(), {})
            self.assertEqual(wr.get_scheduler_status(), {})
            self.assertEqual(wr.get_memory_count(), (0, 0))
        self.assertEqual(u.call_count, 3)

    def test_everything_down_still_posts_report(self):
        with _Main(status={}, tasks={}, health_ok=False) as m:
            with patch.object(wr, "get_memory_count", return_value=(0, 0)):
                wr.main()
        text = m.posted()
        self.assertIn("Success rate: 0.0%", text)
        self.assertIn("0/4 healthy", text)
        self.assertIn("Needs work", text)


class TestUnit(unittest.TestCase):
    def test_slack_post_splits_title_and_body(self):
        with patch.object(wr, "notify") as n:
            wr.slack_post("Title only")
            self.assertIsNone(n.call_args.kwargs["body"])
            wr.slack_post("T\nline1\nline2")
            self.assertEqual((n.call_args[0][0], n.call_args.kwargs["body"]), ("T", "line1\nline2"))
        self.assertEqual(n.call_args.kwargs["dedup_key"], "weekly-reliability")

    def test_analyze_logs_counts_by_source(self):
        errs = [{"source": "a"}, {"source": "a"}, {}]
        with patch.object(wr, "read_logs", side_effect=[errs, [{}]]):
            self.assertEqual(wr.analyze_logs(), (3, 1, {"a": 2, "unknown": 1}))

    def test_verdict_tiers(self):
        with _Main(tasks={"good": {"run_count": 3}}, status={"total_runs": 1000, "total_failures": 1}) as m:
            wr.main()
        self.assertIn("Rock solid", m.posted())
        with _Main() as m:
            wr.main()
        self.assertIn("Mostly stable", m.posted())


class TestIntegration(unittest.TestCase):
    def test_uses_shared_notify_logger_and_config(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("from nova_logger import", SRC)
        self.assertEqual(wr.VECTOR_URL, wr.nova_config.VECTOR_URL)

    def test_summary_posted_to_vector_memory(self):
        with _Main() as m:
            wr.main()
        self.assertIn(wr.VECTOR_URL, m.urls)


class TestFunctional(unittest.TestCase):
    def test_golden_path_report_and_memory_file(self):
        with _Main() as m:
            wr.main()
            text = m.posted()
            files = list((m.home / ".openclaw/workspace/memory").glob("*.md"))
            body = files[0].read_text()
        self.assertIn("1 healthy, 1 failing, 1 idle (of 4)", text)
        self.assertIn(":red_circle: bad — 4 consecutive failures (exit 2)", text)
        self.assertIn("12,345 vectors across 2 sources", text)
        self.assertIn("4/4 healthy", text)
        self.assertIn("Struggling: bad", body)

    def test_memory_block_not_duplicated(self):
        with _Main() as m:
            wr.main(); wr.main()
            (f,) = list((m.home / ".openclaw/workspace/memory").glob("*.md"))
            self.assertEqual(f.read_text().count("Nova's body this week"), 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_weekly_reliability"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
