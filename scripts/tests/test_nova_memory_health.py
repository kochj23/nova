#!/usr/bin/env python3
"""Tests for nova_memory_health.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_health.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mh = _load("mh", SCRIPT)
T0 = 1_800_000_000.0


class _Cur:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(health, prev=None, now=T0, pg_exc=None, health_exc=None):
    """Run main() with the health endpoint, PG, Slack and the state file all stubbed.
    Returns (stdout, post_both mock, cursor, state dict written)."""
    state = Path(tempfile.mkdtemp()) / "memory_health.json"
    if prev is not None:
        state.write_text(json.dumps(prev))
    cur = _Cur()
    connect = MagicMock(side_effect=pg_exc) if pg_exc else MagicMock(return_value=_Conn(cur))
    resp = MagicMock(); resp.read.return_value = json.dumps(health).encode()
    urlopen = MagicMock(side_effect=health_exc) if health_exc else MagicMock(return_value=resp)
    post = MagicMock()
    out = io.StringIO()
    with patch.object(mh, "STATE", str(state)), patch.object(mh.urllib.request, "urlopen", urlopen), \
         patch.object(psycopg2, "connect", connect), patch.object(mh.nova_config, "post_both", post), \
         patch.object(mh, "time", types.SimpleNamespace(time=lambda: now)), redirect_stdout(out):
        mh.main()
    written = json.loads(state.read_text()) if state.exists() else None
    return out.getvalue(), post, cur, written


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", mh.DSN_OPS)

    def test_sql_is_parameterized_and_writes_one_table(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry_memory_pipeline"})
        _, _, cur, _ = _run({"queue_length": 7, "count": 1})
        self.assertEqual(cur.sql[-1][1], (7, 1, 0, 0.0))

    def test_health_endpoint_is_loopback(self):
        self.assertTrue(mh.HEALTH_URL.startswith("http://127.0.0.1:18790/"))


class TestPerformance(unittest.TestCase):
    def test_main_hot_path_300_runs_under_bound(self):
        t0 = time.perf_counter()
        for _ in range(300):
            _run({"queue_length": 10, "count": 100})
        self.assertLess(time.perf_counter() - t0, 6.0)


class TestRetry(unittest.TestCase):
    def test_unreachable_health_posts_critical_once_and_returns(self):
        # RETRY GAP: main()/urlopen(HEALTH_URL) — one attempt; failure posts to #nova-critical and exits without state writes
        out, post, cur, written = _run({}, health_exc=ConnectionRefusedError("refused"))
        self.assertIn("health unreachable", out)
        post.assert_called_once()
        self.assertIn("Memory server unreachable", post.call_args[0][0])
        self.assertEqual(post.call_args[1]["slack_channel"], mh.nova_config.SLACK_BB)
        self.assertIsNone(written)
        self.assertEqual(cur.sql, [])

    def test_telemetry_failure_is_swallowed(self):
        # RETRY GAP: _log_telemetry()/psycopg2.connect — one attempt; a PG outage prints and the health verdict still runs
        out, post, _, written = _run({"queue_length": 1, "count": 5}, pg_exc=psycopg2.OperationalError("pg down"))
        self.assertIn("telemetry log failed", out)
        self.assertIn("OK — queue=1", out)
        self.assertEqual(written["count"], 5)
        post.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual((mh.STALL_QUEUE, mh.STALL_MINUTES, mh.BACKLOG_WARN), (5000, 10, 300_000))

    def test_first_run_writes_state_and_never_alerts(self):
        out, post, cur, written = _run({"queue_length": 999_999, "count": 1})
        post.assert_not_called()
        self.assertEqual(written, {"count": 1, "ts": T0, "queue": 999_999})
        self.assertEqual(cur.sql[-1][1], (999_999, 1, 0, 0.0))

    def test_corrupt_state_is_treated_as_first_run(self):
        state = Path(tempfile.mkdtemp()) / "s.json"; state.write_text("{not json")
        out = io.StringIO()
        with patch.object(mh, "STATE", str(state)), patch.object(mh.urllib.request, "urlopen", MagicMock(return_value=MagicMock(read=lambda: b'{"queue_length": 1, "count": 2}'))), \
             patch.object(psycopg2, "connect", MagicMock(return_value=_Conn(_Cur()))), patch.object(mh.nova_config, "post_both", MagicMock()) as post, \
             patch.object(mh, "time", types.SimpleNamespace(time=lambda: T0)), redirect_stdout(out):
            mh.main()
        post.assert_not_called()
        self.assertEqual(json.loads(state.read_text())["count"], 2)

    def test_missing_fields_default_to_zero(self):
        out, _, _, written = _run({})
        self.assertEqual((written["count"], written["queue"]), (0, 0))


class TestIntegration(unittest.TestCase):
    def test_delta_and_elapsed_flow_from_state_into_telemetry(self):
        _, _, cur, _ = _run({"queue_length": 50, "count": 1500}, prev={"count": 1000, "ts": T0 - 900, "queue": 40})
        sql, params = cur.sql[-1]
        self.assertTrue(sql.startswith("INSERT INTO telemetry_memory_pipeline"))
        self.assertEqual(params, (50, 1500, 500, 15.0))

    def test_stall_goes_to_critical_and_growth_to_warning_channel(self):
        _, post, _, _ = _run({"queue_length": 6000, "count": 100}, prev={"count": 100, "ts": T0 - 1200, "queue": 6000})
        self.assertEqual(post.call_args[1]["slack_channel"], mh.nova_config.SLACK_BB)
        _, post, _, _ = _run({"queue_length": 400_000, "count": 200}, prev={"count": 100, "ts": T0 - 1200, "queue": 300_000})
        self.assertEqual(post.call_args[1]["slack_channel"], mh.nova_config.SLACK_NOTIFY)


class TestFunctional(unittest.TestCase):
    def test_golden_path_healthy_drain_is_quiet(self):
        out, post, cur, written = _run({"queue_length": 500_000, "count": 2000}, prev={"count": 1000, "ts": T0 - 1200, "queue": 600_000})
        self.assertIn("OK — queue=500,000 total=2,000 written_since_last=1,000 elapsed=20m", out)
        post.assert_not_called()                                        # a big backlog draining is healthy
        self.assertEqual(written["queue"], 500_000)

    def test_stall_alert_fires_and_short_circuits(self):
        out, post, _, _ = _run({"queue_length": 1_190_000, "count": 100}, prev={"count": 100, "ts": T0 - 1200, "queue": 900_000})
        self.assertIn("ALERT: stalled", out)
        post.assert_called_once()
        self.assertIn("Memory ingest STALLED", post.call_args[0][0])
        self.assertIn("1,190,000 items queued", post.call_args[0][0])

    def test_stall_needs_elapsed_time_and_a_real_queue(self):
        _, post, _, _ = _run({"queue_length": 6000, "count": 100}, prev={"count": 100, "ts": T0 - 300, "queue": 6000})
        post.assert_not_called()                                        # 5 min: too soon to trust 0 written
        _, post, _, _ = _run({"queue_length": 4000, "count": 100}, prev={"count": 100, "ts": T0 - 1200, "queue": 4000})
        post.assert_not_called()                                        # small queue: idle, not stalled

    def test_growing_backlog_warns_only_when_growing(self):
        out, post, _, _ = _run({"queue_length": 400_000, "count": 200}, prev={"count": 100, "ts": T0 - 1200, "queue": 390_000})
        self.assertIn("Memory queue GROWING", post.call_args[0][0]); self.assertIn("up 10,000", post.call_args[0][0])
        _, post, _, _ = _run({"queue_length": 400_000, "count": 200}, prev={"count": 100, "ts": T0 - 1200, "queue": 398_000})
        post.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_memory_health"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
