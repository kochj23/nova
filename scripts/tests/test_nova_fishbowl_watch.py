#!/usr/bin/env python3
"""Tests for nova_fishbowl_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fishbowl_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_fishbowl_watch_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fw = _load()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state" / "fishbowl_watch.json"
        self.ps = [patch.object(fw, "STATE", self.state),
                   patch.object(fw.nova_config, "post_both"), patch.object(fw.nova_config, "notify_local")]
        _, self.post, self.local = [p.start() for p in self.ps]

    def tearDown(self):
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, rows=None, newest=None):
        cur = MagicMock()
        cur.fetchone.return_value = (newest,)
        cur.fetchall.return_value = rows or []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(fw.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            fw.main()
        return cur, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_sql_parameterized(self):
        self.assertIn("created_at > %s ORDER BY created_at\", (last,)", SRC)
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')

    def test_identity_is_word_bounded(self):
        self.assertEqual(fw.hits("the kochjar brewery"), [])
        self.assertEqual(fw.hits("Jordan Koch was here"), ["identity:jordan koch"])


class TestPerformance(unittest.TestCase):
    def test_hits_10k(self):
        texts = [f"chat line {i} about the stream tonight and nothing else" for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(1 for t in texts if fw.hits(t))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(n, 0)


class TestRetry(_Base):
    def test_alert_failure_does_not_stop_scan(self):
        # RETRY GAP: main/post_both — one attempt per hit; failure logged, marker still advances
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"last_seen": "2026-01-01T00:00:00+00:00"}))
        self.post.side_effect = RuntimeError("slack down")
        ts = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.run_main(rows=[(1, ts, "koch is mentioned")])
        self.assertIn("alert post failed", err.getvalue())
        self.assertEqual(json.loads(self.state.read_text())["last_seen"], ts.isoformat())


class TestUnit(unittest.TestCase):
    def test_hits_cases(self):
        self.assertEqual(fw.hits(None), [])
        self.assertEqual(fw.hits("Watch Nicholas is great"), [])
        self.assertEqual(fw.hits("watch nicholas said he will dox people"), ["nicholas+threat"])
        self.assertEqual(fw.hits("Watch Nicholas will get kochj fired"), ["identity:kochj", "nicholas+threat"])

    def test_load_bad_state_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text("{not json")
            with patch.object(fw, "STATE", p):
                self.assertEqual(fw._load(), {})


class TestIntegration(_Base):
    def test_reads_fishbowl_source_and_alerts_critical_channel(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"last_seen": "2026-01-01T00:00:00+00:00"}))
        cur, _ = self.run_main(rows=[(1, datetime(2026, 1, 2, tzinfo=timezone.utc), "watch nick wants to expose you")])
        self.assertIn("source='fishbowl'", cur.execute.call_args[0][0])
        self.assertEqual(self.post.call_args.kwargs["slack_channel"], fw.nova_config.SLACK_BB)
        self.assertIn("youtube.com/@watchnicholaslive/live", self.post.call_args[0][0])
        self.assertTrue(self.local.call_args.kwargs["critical"])


class TestFunctional(_Base):
    def test_first_run_baselines_without_alerting(self):
        newest = datetime(2026, 3, 1, tzinfo=timezone.utc)
        _, out = self.run_main(newest=newest)
        self.assertEqual(json.loads(self.state.read_text()), {"last_seen": newest.isoformat()})
        self.assertIn("baselined", out)
        self.post.assert_not_called()

    def test_scan_alerts_once_per_hit(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"last_seen": "2026-01-01T00:00:00+00:00"}))
        rows = [(1, datetime(2026, 1, 2, tzinfo=timezone.utc), "benign chatter"),
                (2, datetime(2026, 1, 3, tzinfo=timezone.utc), "digitalnoise leaked")]
        _, out = self.run_main(rows=rows)
        self.assertEqual(self.post.call_count, 1)
        self.assertIn("scanned 2 new fishbowl memories, 1 alert(s)", out)
        self.assertEqual(json.loads(self.state.read_text())["last_seen"], rows[1][1].isoformat())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fishbowl_watch; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
