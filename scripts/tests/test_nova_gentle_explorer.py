#!/usr/bin/env python3
"""Tests for nova_gentle_explorer.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gentle_explorer.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_gentle_explorer_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ge = _load()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "journal").mkdir()
        (t / "state").mkdir()
        self.t = t
        self.ps = [patch.object(ge, "GARDEN_FILE", t / "garden.json"), patch.object(ge, "JOURNAL_DIR", t / "journal"),
                   patch.object(ge, "STATE_FILE", t / "state" / "s.json"),
                   patch.object(ge.nova_config, "post_both"),
                   patch.object(ge.urllib.request, "urlopen", side_effect=OSError("offline"))]
        self.post = [p.start() for p in self.ps][3]
        self._r = redirect_stdout(io.StringIO())
        self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_posts_only_via_nova_config_to_jordan_dm(self):
        with patch.object(ge.nova_config, "post_both") as pb:
            ge.slack_post("hi")
        self.assertEqual(pb.call_args.kwargs["slack_channel"], ge.nova_config.JORDAN_DM)
        self.assertNotIn("chat.postMessage", SRC)


class TestPerformance(_Base):
    def test_select_from_10k_questions(self):
        today = date.today()
        garden = {"questions": [{"text": f"q{i}", "added": (today - timedelta(days=i % 90)).isoformat(),
                                 "times_reflected": i % 3} for i in range(10_000)]}
        t0 = time.perf_counter()
        idx, q = ge.select_question_for_reflection(garden)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(q["times_reflected"], 0)


class TestRetry(_Base):
    def test_memory_scan_fails_open(self):
        # RETRY GAP: scan_memory_for_questions — one recall per term, errors skipped
        with patch.object(ge.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(ge.scan_memory_for_questions(), [])
        self.assertEqual(u.call_count, 4)

    def test_corrupt_garden_and_state_fall_back(self):
        ge.GARDEN_FILE.write_text("{nope")
        ge.STATE_FILE.write_text("{nope")
        self.assertEqual(ge.load_garden(), {"questions": [], "resolved": []})
        self.assertEqual(ge.load_state()["last_question_index"], -1)


class TestUnit(_Base):
    def test_add_dedupes_case_insensitively(self):
        self.assertTrue(ge.add_question("Is it worth it?"))
        self.assertFalse(ge.add_question("is IT worth it?"))
        self.assertEqual(len(ge.load_garden()["questions"]), 1)

    def test_resolve_bounds(self):
        ge.add_question("What if we moved?")
        self.assertIsNone(ge.resolve_question(5))
        q = ge.resolve_question(0)
        self.assertEqual(q["text"], "What if we moved?")
        self.assertEqual(len(ge.load_garden()["resolved"]), 1)

    def test_select_empty(self):
        self.assertIsNone(ge.select_question_for_reflection({"questions": []}))


class TestIntegration(_Base):
    def test_journal_scan_feeds_garden(self):
        (self.t / "journal" / "2026-01.md").write_text(
            "# Heading I wonder\nI wonder whether the garden needs more shade this year.\n"
            "Buy milk.\nWhat if\n")
        found = ge.scan_journal_for_questions()
        self.assertEqual(found, ["I wonder whether the garden needs more shade this year."])

    def test_memory_scan_parses_results(self):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        body = json.dumps({"results": [{"text": "I wonder about tides?"}, {"text": "grocery list"}]}).encode()
        with patch.object(ge.urllib.request, "urlopen", side_effect=lambda *a, **k: R(body)) as u:
            out = ge.scan_memory_for_questions()
        self.assertEqual(out, ["I wonder about tides?"] * 4)
        self.assertIn("source=journal", u.call_args[0][0])


class TestFunctional(_Base):
    def test_main_reflects_and_updates_counts(self):
        ge.add_question("Should I even keep the old truck?")
        ge.main()
        q = ge.load_garden()["questions"][0]
        self.assertEqual(q["times_reflected"], 1)
        self.assertIn("old truck", self.post.call_args[0][0])
        self.assertTrue(json.loads(ge.STATE_FILE.read_text())["last_run"])

    def test_empty_garden_invites_once(self):
        ge.main()
        ge.main()
        self.assertEqual(self.post.call_count, 1)
        self.assertIn("questions garden is empty", self.post.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--garden", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(ge.nova_config, "post_both") as pb:
            _load()
        pb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
