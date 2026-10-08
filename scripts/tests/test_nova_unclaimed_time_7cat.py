#!/usr/bin/env python3
"""7-category gap tests for nova_unclaimed_time.py — the memory-server and scheduler retries added
here (both were 'RETRY GAP' single attempts) and the 2026-10-06 hand-off to earned skills
(try_skill -> nova_pursue_skill). Memory server, scheduler and PG are mocked.
Base suite: test_nova_unclaimed_time.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_unclaimed_time_7cat.py
"""
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unclaimed_time.py"
spec = importlib.util.spec_from_file_location("unclaimed_7cat", SCRIPT)
ut = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ut)


def resp(obj):
    r = mock.MagicMock(); r.__enter__.return_value = io.BytesIO(json.dumps(obj).encode())
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch("time.sleep"); self.sleep = p.start(); self.addCleanup(p.stop)
        r = redirect_stderr(io.StringIO()); self.err = r.__enter__(); self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_remember_body_is_json_not_query(self):
        with mock.patch.object(ut.urllib.request, "urlopen", return_value=resp({"id": "m"})) as u:
            ut.remember("a&source=x", "unclaimed", {"k": "v"})
        req = u.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/remember"))
        self.assertEqual(json.loads(req.data)["text"], "a&source=x")

    def test_recall_query_encoded(self):
        with mock.patch.object(ut.urllib.request, "urlopen", return_value=resp({"memories": []})) as u:
            ut.recall("x&n=999")
        self.assertIn("x%26n%3D999", u.call_args.args[0])


class TestPerformance(_Base):
    def test_bounded_attempts_and_backoff(self):
        with mock.patch.object(ut.urllib.request, "urlopen", side_effect=OSError("x")) as u, self.assertRaises(OSError):
            ut.remember("t", "s", {})
        self.assertEqual(u.call_count, 3)
        self.assertLessEqual(sum(c.args[0] for c in self.sleep.call_args_list), 10)

    def test_scheduler_probe_fast(self):
        with mock.patch.object(ut.urllib.request, "urlopen", side_effect=OSError("refused")) as u:
            ut.yield_to_scheduled()
        self.assertTrue(all(c.kwargs["timeout"] == 5 for c in u.call_args_list))


class TestRetry(_Base):
    def test_remember_recovers(self):
        with mock.patch.object(ut.urllib.request, "urlopen", side_effect=[OSError("restart"), resp({"id": "m2"})]):
            self.assertEqual(ut.remember("t", "s", {}), "m2")
        self.assertIn("retry 1", self.err.getvalue())

    def test_remember_final_failure_raises(self):
        with mock.patch.object(ut.urllib.request, "urlopen", side_effect=OSError("down")), self.assertRaises(OSError):
            ut.remember("t", "s", {})

    def test_scheduler_blip_still_yields(self):
        now = 1_000_000.0
        tasks = {"unclaimed_time": {"group": "llm", "running": True},
                 "digest": {"group": "llm", "enabled": True, "next_run": now + 30}}
        with mock.patch.object(ut.urllib.request, "urlopen", side_effect=[OSError("restarting"), resp(tasks)]), \
                mock.patch("time.time", return_value=now):
            self.assertEqual(ut.yield_to_scheduled(), "digest")


class TestUnit(_Base):
    def test_scheduler_unreachable_is_none(self):
        with mock.patch.object(ut.urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertIsNone(ut.yield_to_scheduled())


class TestIntegration(_Base):
    def test_try_skill_routes_preoccupation_to_pursue_skill(self):
        import nova_pursue_skill as ps
        with mock.patch.object(ps, "run_skill", return_value={"handled": True, "slug": "x"}) as rs:
            out = ut.try_skill(mock.MagicMock(), mock.MagicMock(), {"mode": "preoccupation", "topic": "crime drama"})
        self.assertEqual(rs.call_args.args[0], "pursue-interest-crime-drama")
        self.assertTrue(out)

    def test_try_skill_unmapped_topic_falls_back(self):
        import nova_pursue_skill as ps
        with mock.patch.object(ps, "run_skill") as rs:
            out = ut.try_skill(mock.MagicMock(), mock.MagicMock(), {"mode": "preoccupation", "topic": "knitting"})
        rs.assert_not_called()
        self.assertFalse(out)


class TestFunctional(_Base):
    def test_two_blips_then_stored(self):
        with mock.patch.object(ut.urllib.request, "urlopen",
                               side_effect=[OSError("a"), OSError("b"), resp({"id": "ok"})]):
            self.assertEqual(ut.remember("t", "s", {}), "ok")
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 4])


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(callable(ut.main))


if __name__ == "__main__":
    unittest.main()
