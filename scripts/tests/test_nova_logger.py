#!/usr/bin/env python3
"""Tests for nova_logger.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_logger.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_logger_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lg = _load()


class _Tmp(unittest.TestCase):
    """Every test writes into its own tempdir — never ~/.openclaw/logs."""
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        ps = [mock.patch.object(lg, "LOG_DIR", d), mock.patch.object(lg, "LOG_FILE", d / "nova.jsonl"),
              mock.patch.object(lg, "MIN_LEVEL", lg.LOG_INFO),
              mock.patch("sys.stderr", new_callable=io.StringIO)]
        self.m = [p.start() for p in ps]
        self.err = self.m[3]
        self.dir = d
        self.addCleanup(lambda: ([p.stop() for p in ps], self.td.cleanup()))

    def lines(self):
        return [json.loads(l) for l in (self.dir / "nova.jsonl").read_text().splitlines()]


class TestSecurity(_Tmp):
    def test_no_hardcoded_credentials_or_network(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"urllib|requests|socket|psycopg2")

    def test_hostile_message_stays_one_json_line(self):
        lg.log('evil"\n{"level":"fatal"}\r\x00', source="t")
        recs = self.lines()
        self.assertEqual(len(recs), 1)                       # newline injection can't forge a second record
        self.assertEqual(recs[0]["level"], "info")
        self.assertEqual(recs[0]["msg"], 'evil"\n{"level":"fatal"}\r\x00')


class TestPerformance(_Tmp):
    def test_10k_writes_and_filtered_read(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            lg.log(f"m{i}", source="perf", level=lg.LOG_ERROR if i % 100 == 0 else lg.LOG_INFO)
        out = lg.read_logs(n=50, level=lg.LOG_ERROR)
        self.assertLess(time.perf_counter() - t0, 8.0)
        self.assertEqual(len(out), 50)
        self.assertEqual(out[0]["msg"], "m9900")


class TestRetry(_Tmp):
    def test_unwritable_log_fails_open(self):
        # RETRY GAP: log() file write — no retry by design; an OSError is swallowed so callers never crash
        with mock.patch("builtins.open", side_effect=OSError("disk full")) as op:
            lg.log("boom", level=lg.LOG_ERROR, source="t")
        self.assertEqual(op.call_count, 1)
        self.assertIn("[ERROR] t: boom", self.err.getvalue())    # still surfaced on stderr


class TestUnit(_Tmp):
    def test_level_filter_and_extra(self):
        lg.log("hidden", level=lg.LOG_DEBUG, source="t")
        lg.log("shown", level=lg.LOG_WARN, source="t", extra={"k": 1})
        recs = self.lines()
        self.assertEqual([r["msg"] for r in recs], ["shown"])
        self.assertEqual(recs[0]["extra"], {"k": 1})
        self.assertIn("[WARN] t: shown", self.err.getvalue())

    def test_guess_source_is_caller(self):
        lg.log("x")
        self.assertEqual(self.lines()[0]["source"], Path(__file__).stem)

    def test_read_logs_filters_and_skips_garbage(self):
        (self.dir / "nova.jsonl").write_text(
            '{"ts":"2020","level":"info","source":"a","msg":"old"}\nnot json\n\n'
            '{"ts":"2030","level":"error","source":"b","msg":"new"}\n')
        self.assertEqual([e["msg"] for e in lg.read_logs()], ["new", "old"])
        self.assertEqual([e["msg"] for e in lg.read_logs(source="a")], ["old"])
        self.assertEqual([e["msg"] for e in lg.read_logs(since="2025")], ["new"])
        self.assertEqual(lg.read_logs(n=1)[0]["msg"], "new")


class TestIntegration(_Tmp):
    def test_rotation_shifts_and_caps_files(self):
        with mock.patch.object(lg, "MAX_SIZE_BYTES", 10):
            for i in range(1, lg.MAX_FILES + 1):
                (self.dir / f"nova.jsonl.{i}").write_text(json.dumps({"msg": f"r{i}", "level": "info"}) + "\n")
            (self.dir / "nova.jsonl").write_text("x" * 50 + "\n")
            lg.log("fresh", source="t")
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertNotIn("nova.jsonl.7", names)
        self.assertEqual(len([n for n in names if n.startswith("nova.jsonl.")]), lg.MAX_FILES)
        self.assertEqual((self.dir / "nova.jsonl.1").read_text(), "x" * 50 + "\n")
        self.assertEqual(self.lines()[0]["msg"], "fresh")

    def test_read_spans_rotated_files(self):
        lg.log("current", source="t")
        (self.dir / "nova.jsonl.1").write_text(json.dumps({"msg": "older", "level": "info"}) + "\n")
        self.assertEqual([e["msg"] for e in lg.read_logs()], ["current", "older"])


class TestFunctional(_Tmp):
    def test_log_then_read_roundtrip(self):
        lg.log("started", source="svc")
        lg.log("failed", level=lg.LOG_ERROR, source="svc", extra={"host": "h"})
        out = lg.read_logs(level=lg.LOG_ERROR, source="svc")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["extra"], {"host": "h"})
        self.assertTrue(out[0]["ts"].endswith("+00:00"))

    def test_read_with_no_files(self):
        (self.dir / "nova.jsonl").unlink(missing_ok=True)
        self.assertEqual(lg.read_logs(), [])


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free_beyond_logdir(self):
        self.assertNotIn("__main__", SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_logger as l; print(l.LOG_FILE)"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.strip().startswith(home))
            self.assertEqual(os.listdir(Path(home) / ".openclaw" / "logs"), [])   # no file written on import


if __name__ == "__main__":
    unittest.main()
