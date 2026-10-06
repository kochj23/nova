#!/usr/bin/env python3
"""Tests for nova_log_rotate.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_log_rotate.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="log-rotate-test-"))


@contextlib.contextmanager
def _modules(**mods):
    """Set sys.modules keys for the block and restore ONLY those keys afterwards."""
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is missing:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load(name, path):
    notify_mod = types.ModuleType("nova_notify"); notify_mod.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _modules(nova_notify=notify_mod):          # the daemon binds `notify` at import; never the PG-backed real one
        spec.loader.exec_module(mod)
    return mod


lr = _load("log_rotate_under_test", SCRIPT)
lr.CRON_RUNS_DIR = TMP / "cron" / "runs"
lr.LOGS_DIR = TMP / "logs"


def _ts(days_ago):
    return int((datetime.now() - timedelta(days=days_ago)).timestamp() * 1000)


def _jsonl(path, ages):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"ts": _ts(a), "i": i}) + "\n" for i, a in enumerate(ages)))


class _Base(unittest.TestCase):
    def setUp(self):
        for d in (lr.CRON_RUNS_DIR, lr.LOGS_DIR):
            shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
        lr.nova_notify = MagicMock(return_value=True)
        self._max = lr.MAX_LOG_BYTES
        lr.MAX_LOG_BYTES = 1024

    def tearDown(self):
        lr.MAX_LOG_BYTES = self._max

    def _main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            lr.main()
        return out.getvalue()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials_and_no_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for forbidden in ("subprocess", "os.system", "rm -rf", "shutil.rmtree", "unlink("):
            self.assertNotIn(forbidden, SRC)

    def test_only_openclaw_cron_runs_and_logs_are_in_scope(self):
        self.assertIn('Path.home() / ".openclaw/cron/runs"', SRC)
        self.assertIn('Path.home() / ".openclaw/logs"', SRC)
        self.assertEqual(re.findall(r'\.glob\("([^"]+)"\)', SRC), ["*.jsonl", "*.log"])

    def test_untouched_files_are_never_rewritten(self):
        p = lr.LOGS_DIR / "small.log"; p.write_bytes(b"x" * 100)
        before = p.stat().st_mtime_ns
        self.assertEqual(lr.trim_log_file(p), 0)
        self.assertEqual(p.stat().st_mtime_ns, before)


class TestPerformance(_Base):
    def test_trimming_a_10k_line_history_is_fast(self):
        p = lr.CRON_RUNS_DIR / "big.jsonl"
        _jsonl(p, [45 if i % 2 else 1 for i in range(10_000)])
        t0 = time.perf_counter()
        before, after = lr.trim_jsonl(p)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((before, after), (10_000, 5_000))


class TestRetry(_Base):
    def test_unreadable_inputs_fail_open(self):
        # RETRY GAP: trim_jsonl/trim_log_file — one pass per file; an I/O error is logged and the file left alone
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(lr.trim_jsonl(lr.CRON_RUNS_DIR), (0, 0))          # a directory, not a file
            self.assertEqual(lr.trim_log_file(lr.LOGS_DIR / "missing.log"), 0)
        self.assertIn("Error trimming runs", out.getvalue()); self.assertIn("Error rotating missing.log", out.getvalue())

    def test_notification_failure_does_not_abort_the_run(self):
        # RETRY GAP: slack_post/nova_notify — one enqueue attempt; a False return is ignored
        lr.nova_notify = MagicMock(return_value=False)
        _jsonl(lr.CRON_RUNS_DIR / "a.jsonl", [40, 1])
        out = self._main()
        self.assertIn("Done — 1 jsonl files trimmed", out)
        lr.nova_notify.assert_called_once()

    def test_missing_directories_are_skipped_cleanly(self):
        shutil.rmtree(lr.CRON_RUNS_DIR); shutil.rmtree(lr.LOGS_DIR)
        out = self._main()
        self.assertIn("0 jsonl files trimmed (0 entries removed), 0 log files rotated (0.0MB freed)", out)
        lr.nova_notify.assert_not_called()


class TestUnit(_Base):
    def test_trim_jsonl_keeps_fresh_drops_old_and_preserves_malformed_lines(self):
        p = lr.CRON_RUNS_DIR / "x.jsonl"
        _jsonl(p, [1, 31, 29])
        p.write_text(p.read_text() + "this is not json\n" + json.dumps({"no_ts": True}) + "\n")
        self.assertEqual(lr.trim_jsonl(p), (5, 4))
        kept = p.read_text().splitlines()
        self.assertEqual(len(kept), 4); self.assertIn("this is not json", kept)
        self.assertNotIn('"i": 1', p.read_text())                       # the 31-day entry is gone
        self.assertTrue(p.read_text().endswith("\n"))

    def test_trim_jsonl_edge_cases(self):
        p = lr.CRON_RUNS_DIR / "empty.jsonl"; p.write_text("")
        self.assertEqual(lr.trim_jsonl(p), (0, 0))
        _jsonl(p, [40, 41])
        self.assertEqual(lr.trim_jsonl(p), (2, 0)); self.assertEqual(p.read_text(), "")

    def test_trim_log_file_keeps_the_tail_on_a_line_boundary(self):
        p = lr.LOGS_DIR / "big.log"
        p.write_bytes(b"".join(b"line %05d\n" % i for i in range(500)))    # 5500 bytes, 11 per line
        size = p.stat().st_size
        freed = lr.trim_log_file(p)
        kept = p.read_bytes()
        self.assertTrue(kept.startswith(b"line ")); self.assertTrue(kept.endswith(b"line 00499\n"))
        self.assertLess(len(kept), lr.MAX_LOG_BYTES); self.assertEqual(freed, size - len(kept))
        self.assertEqual(lr.trim_log_file(p), 0)                         # second pass is a no-op

    def test_log_line_format(self):
        out = io.StringIO()
        with redirect_stdout(out):
            lr.log("hi")
        self.assertRegex(out.getvalue(), r"^\[nova_log_rotate \d\d:\d\d:\d\d\] hi\n$")


class TestIntegration(_Base):
    def test_slack_post_splits_title_and_body_onto_the_notification_bus(self):
        lr.slack_post("*Nova Log Rotation* 🗂️\n• line one\n• line two")
        lr.nova_notify.assert_called_once_with("Nova Log Rotation 🗂️", body="• line one\n• line two", level="info",
                                               category="scheduler", dedup_key="log-rotation-weekly")
        lr.nova_notify.reset_mock()
        lr.slack_post("just a title")
        self.assertIsNone(lr.nova_notify.call_args[1]["body"])

    def test_cutoff_is_thirty_days(self):
        self.assertAlmostEqual((datetime.now() - lr.CUTOFF).days, 30, delta=1)
        self.assertEqual(lr.MAX_LOG_BYTES, 1024)                         # patched for the test; real default below
        self.assertIn("MAX_LOG_BYTES = 5 * 1024 * 1024", SRC)


class TestFunctional(_Base):
    def test_golden_path_trims_rotates_and_reports(self):
        _jsonl(lr.CRON_RUNS_DIR / "morning_brief.jsonl", [45, 44, 2])
        _jsonl(lr.CRON_RUNS_DIR / "fresh.jsonl", [1, 2])
        (lr.LOGS_DIR / "nova_journal.log").write_bytes(b"".join(b"entry %05d\n" % i for i in range(400)))
        (lr.LOGS_DIR / "tiny.log").write_bytes(b"ok\n")
        out = self._main()
        self.assertIn("morning_brief.jsonl: removed 2 old entries (3 → 1)", out)
        self.assertNotIn("fresh.jsonl", out)
        self.assertIn("nova_journal.log: freed", out)
        self.assertIn("1 jsonl files trimmed (2 entries removed), 1 log files rotated", out)
        title, kw = lr.nova_notify.call_args[0][0], lr.nova_notify.call_args[1]
        self.assertEqual(title, "Nova Log Rotation 🗂️")
        self.assertIn("1 cron history files trimmed to 30 days (2 old entries removed)", kw["body"])
        self.assertIn("1 log files truncated to 5MB", kw["body"])
        self.assertEqual((lr.LOGS_DIR / "tiny.log").read_bytes(), b"ok\n")

    def test_nothing_to_do_posts_nothing(self):
        _jsonl(lr.CRON_RUNS_DIR / "fresh.jsonl", [1]); (lr.LOGS_DIR / "s.log").write_bytes(b"x")
        out = self._main()
        self.assertIn("0 jsonl files trimmed", out)
        lr.nova_notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_log_rotate"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
