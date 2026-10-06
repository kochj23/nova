#!/usr/bin/env python3
"""Tests for nova_notify.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_notify.py"
SRC = PATH.read_text()
EVIL = "x'); SELECT pg_sleep(9);--"


def _load():
    spec = importlib.util.spec_from_file_location("nova_notify_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nn = _load()


def _pg():
    """A psycopg2.connect mock whose cursor records execute() calls."""
    cur = MagicMock()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    return MagicMock(return_value=conn), cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_insert_is_parameterized(self):
        connect, cur = _pg()
        with patch.object(psycopg2, "connect", connect):
            self.assertTrue(nn.notify(EVIL, body=EVIL, source="t"))
        sql, params = cur.execute.call_args.args
        self.assertNotIn(EVIL, sql)
        self.assertEqual(params["title"], EVIL)
        self.assertIn("%(title)s", sql)

    def test_psql_fallback_uses_variables_not_interpolation(self):
        with patch.object(psycopg2, "connect", side_effect=Exception("no pg")), \
                patch.object(nn.subprocess, "run") as run:
            nn.notify(EVIL, source="t")
        argv = run.call_args.args[0]
        sql = argv[argv.index("-c") + 1]
        self.assertNotIn(EVIL, sql)
        self.assertIn(f"t={EVIL}", argv)

    def test_no_slack_channel_hardcoded(self):
        self.assertNotIn("hooks.slack.com", SRC)
        self.assertIsNone(re.search(r"\bC0[A-Z0-9]{8,}\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_10k_notifies_fast_with_pg_mocked(self):
        connect, cur = _pg()
        with patch.object(psycopg2, "connect", connect):
            t0 = time.perf_counter()
            for i in range(10_000):
                nn.notify(f"disk {i}% full", source="s")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(cur.execute.call_count, 10_000)


class TestRetry(unittest.TestCase):
    def test_pg_failure_falls_back_to_psql_once(self):
        # RETRY GAP: notify() — no backoff; one psycopg2 attempt then one psql attempt, never raises
        with patch.object(psycopg2, "connect", side_effect=Exception("down")) as c, \
                patch.object(nn.subprocess, "run") as run:
            self.assertTrue(nn.notify("t", source="s"))
        self.assertEqual((c.call_count, run.call_count), (1, 1))

    def test_both_paths_failing_returns_false(self):
        with patch.object(psycopg2, "connect", side_effect=Exception("down")), \
                patch.object(nn.subprocess, "run", side_effect=OSError("no psql")):
            self.assertFalse(nn.notify("t", source="s"))


class TestUnit(unittest.TestCase):
    def _payload(self, *a, **k):
        connect, cur = _pg()
        with patch.object(psycopg2, "connect", connect):
            nn.notify(*a, **k)
        return cur.execute.call_args.args[1]

    def test_invalid_level_coerced_to_info(self):
        self.assertEqual(self._payload("t", level="EMERGENCY", source="s")["level"], "info")

    def test_auto_dedup_key_strips_volatile_numbers(self):
        a = self._payload("UNAS 91% full after 12:30", source="s", category="storage")["dedup_key"]
        b = self._payload("UNAS 97% full after 13:45", source="s", category="storage")["dedup_key"]
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("auto:s:storage:"))
        self.assertEqual(self._payload("t", source="s", dedup_key="mine")["dedup_key"], "mine")

    def test_title_truncated_meta_json_source_autodetect(self):
        with patch.object(nn.sys, "argv", ["/x/y/nova_thing.py"]):
            p = self._payload("T" * 900, meta={"k": 1})
        self.assertEqual(len(p["title"]), 500)
        self.assertEqual(json.loads(p["meta"]), {"k": 1})
        self.assertEqual(p["source"], "nova_thing.py")
        self.assertIsNone(p["body"])


class TestIntegration(unittest.TestCase):
    def test_writes_event_bus_table(self):
        connect, cur = _pg()
        with patch.object(psycopg2, "connect", connect):
            nn.notify("t", source="s")
        self.assertIn("INSERT INTO telemetry.events", cur.execute.call_args.args[0])
        self.assertIn("connect_timeout", str(connect.call_args))

    def test_importable_as_shared_module(self):
        import nova_notify
        self.assertTrue(callable(nova_notify.notify))


class TestFunctional(unittest.TestCase):
    def test_golden_path_persists_all_fields(self):
        connect, cur = _pg()
        with patch.object(psycopg2, "connect", connect):
            ok = nn.notify("UNAS low", body="1.4TB", level="warning", category="storage",
                           source="unas", dedup_key="k", correlation_id="c1")
        self.assertTrue(ok)
        p = cur.execute.call_args.args[1]
        self.assertEqual((p["level"], p["category"], p["body"], p["correlation_id"]),
                         ("warning", "storage", "1.4TB", "c1"))


class TestFrame(unittest.TestCase):
    def test_cli_without_args_prints_usage_exit_2(self):
        r = subprocess.run([sys.executable, str(PATH)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_notify"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
