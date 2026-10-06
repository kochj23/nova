#!/usr/bin/env python3
"""Tests for nova_memory_breakdown.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_breakdown.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_memory_breakdown_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mb = _load()


def _cp(out="", rc=0):
    return subprocess.CompletedProcess(["psql"], rc, stdout=out, stderr="")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Base(unittest.TestCase):
    def setUp(self):
        self.ps = [patch.object(mb, "notify"), patch.object(mb.urllib.request, "urlopen", side_effect=OSError("offline")),
                   patch("subprocess.run", return_value=_cp("wiki|300\nemail|100\n"))]
        self.notify, self.urlopen, self.run = [p.start() for p in self.ps]
        self._r = redirect_stdout(io.StringIO())
        self.out = self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_query_is_static_read_only(self):
        self.assertIn('"SELECT source, count(*) as cnt FROM memories GROUP BY source ORDER BY cnt DESC;"', SRC)
        self.assertNotRegex(SRC, r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b")


class TestPerformance(_Base):
    def test_parse_10k_sources(self):
        self.run.return_value = _cp("".join(f"src{i}|{i}\n" for i in range(10_000)))
        t0 = time.perf_counter()
        breakdown, total = mb.get_breakdown()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((len(breakdown), total), (10_000, sum(range(10_000))))


class TestRetry(_Base):
    def test_wait_polls_until_queue_drains(self):
        depths = iter([-1, 5000, 0])
        with patch.object(mb, "get_queue_depth", side_effect=lambda: next(depths)) as q, \
             patch.object(mb.time, "sleep") as sl, patch.object(mb, "post_breakdown") as pb:
            mb.wait_and_post()
        self.assertEqual(q.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [60, 60, 10])
        pb.assert_called_once()

    def test_queue_depth_fails_open(self):
        self.assertEqual(mb.get_queue_depth(), -1)


class TestUnit(_Base):
    def test_breakdown_parse_and_psql_failure(self):
        self.assertEqual(mb.get_breakdown(), ([("wiki", 300), ("email", 100)], 400))
        self.run.return_value = _cp("", rc=2)
        self.assertIsNone(mb.get_breakdown())

    def test_slack_post_title_body_split(self):
        mb.slack_post("*Title here*\nbody")
        args, kw = self.notify.call_args
        self.assertEqual(args[0], "Title here")
        self.assertEqual((kw["body"], kw["level"], kw["category"]), ("body", "info", "memory_ingest"))


class TestIntegration(_Base):
    def test_queue_depth_reads_memory_server_stats(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _Resp(json.dumps({"pending": 42}).encode())
        self.assertEqual(mb.get_queue_depth(), 42)
        self.assertEqual(self.urlopen.call_args[0][0], f"{mb.VECTOR_URL}/queue/stats")
        self.assertEqual(self.run.call_count, 0)


class TestFunctional(_Base):
    def test_post_breakdown_golden(self):
        mb.post_breakdown()
        body = self.notify.call_args.kwargs["body"]
        self.assertIn("Total: 400 memories", body)
        self.assertRegex(body, r"wiki\s+300\s+75\.0%")
        self.assertEqual(self.run.call_args[0][0][:2], ["psql", "-U"])

    def test_long_breakdown_split_in_two(self):
        self.run.return_value = _cp("".join(f"source_{i}|{i + 1}\n" for i in range(80)))
        mb.post_breakdown()
        self.assertEqual(self.notify.call_count, 2)

    def test_psql_failure_posts_nothing(self):
        self.run.return_value = _cp("", rc=1)
        mb.post_breakdown()
        self.notify.assert_not_called()
        self.assertIn("Failed to get breakdown", self.out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--wait", r.stdout)


if __name__ == "__main__":
    unittest.main()
