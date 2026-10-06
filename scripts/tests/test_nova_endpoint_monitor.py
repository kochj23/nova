#!/usr/bin/env python3
"""Tests for nova_endpoint_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_endpoint_monitor.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nem_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


em = _load()
em.LOG_FILE = Path(_TMP.name) / "endpoint.log"      # never write ~/.openclaw/logs


def _lsof(*rows):
    head = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME"
    return SimpleNamespace(stdout="\n".join([head, *rows]), returncode=0)


class _Base(unittest.TestCase):
    def setUp(self):
        em._fim_baseline = {}
        em._alerts.clear()
        for k in em._stats:
            em._stats[k] = 0
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(em, "record_event", AsyncMock()); self.rec = p.start(); self.addCleanup(p.stop)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn(":", em.DB_DSN.split("@")[0].split("//")[1])     # no password in the DSN

    def test_insert_is_parameterized(self):
        conn = MagicMock(); conn.execute = AsyncMock()
        pool = MagicMock(); pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(em, "get_pool", AsyncMock(return_value=pool)):
            asyncio.run(em.record_event("fim_new_file", "warning", "/etc/x'; --", {"a": 1}))
        sql, *args = conn.execute.call_args[0]
        self.assertIn("VALUES ($1, $2, $3, $4, $5)", sql)
        self.assertNotIn("/etc/x", sql)
        self.assertEqual(args[3], "/etc/x'; --")

    def test_watches_sensitive_paths(self):
        self.assertIn("/etc/ssh/sshd_config", em.FIM_PATHS)
        self.assertTrue(any(p.endswith(".ssh/authorized_keys") for p in em.FIM_PATHS))


class TestPerformance(_Base):
    def test_lsof_parse_10k_lines_fast(self):
        rows = [f"python3 {i} u 3u IPv4 0x1 0t0 TCP *:{1000 + i % 5000} (LISTEN)" for i in range(10_000)]
        with patch.object(em.subprocess, "run", return_value=_lsof(*rows)):
            t0 = time.perf_counter()
            asyncio.run(em.process_check())
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.rec.assert_not_called()

    def test_hash_streams_in_chunks(self):
        f = self.root / "big"; f.write_bytes(b"a" * (1 << 20))
        t0 = time.perf_counter()
        self.assertEqual(len(em.hash_file(str(f))), 64)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_subprocess_failures_fail_open(self):
        # RETRY GAP: process_check()/auth_check() — lsof/log are one-shot; failure returns silently
        with patch.object(em.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 10)) as r:
            asyncio.run(em.process_check())
            asyncio.run(em.auth_check())
        self.assertEqual(r.call_count, 2)
        self.rec.assert_not_called()

    def test_loop_survives_check_error(self):
        # RETRY GAP: fim_loop — no backoff; an error is logged and the next interval retries
        calls = []
        async def fake_sleep(_):
            calls.append(1)
            if len(calls) > 1:
                em._shutdown = True
        try:
            with patch.object(em, "fim_check", AsyncMock(side_effect=RuntimeError("db"))) as fc, \
                 patch.object(em.asyncio, "sleep", fake_sleep):
                asyncio.run(em.fim_loop())
        finally:
            em._shutdown = False
        self.assertEqual(fc.call_count, 1)


class TestUnit(_Base):
    def test_hash_and_scan(self):
        (self.root / "a").write_text("x"); (self.root / ".hidden").write_text("y")
        self.assertIsNone(em.hash_file(str(self.root / "missing")))
        self.assertIsNone(em.hash_file(str(self.root)))
        self.assertEqual(list(em.scan_path(str(self.root))), [str(self.root / "a")])
        self.assertEqual(len(em.scan_path(str(self.root / "a"))), 1)
        self.assertEqual(em.scan_path(str(self.root / "nope")), {})

    def test_suspicious_listener_detected(self):
        with patch.object(em.subprocess, "run", return_value=_lsof("ncat 666 u 3u IPv4 0x1 0t0 TCP *:4444 (LISTEN)")):
            asyncio.run(em.process_check())
        self.assertEqual(self.rec.call_args[0][:2], ("suspicious_listener", "critical"))
        self.assertEqual(self.rec.call_args[0][3]["pid"], "666")

    def test_auth_failures_parsed(self):
        lines = "\n".join([json.dumps({"eventMessage": "Authentication failed for user x", "timestamp": "t"}),
                           "not json", "", json.dumps({"eventMessage": "fine"})])
        with patch.object(em.subprocess, "run", return_value=SimpleNamespace(stdout=lines)):
            asyncio.run(em.auth_check())
        self.assertEqual(self.rec.call_count, 1)
        self.assertEqual(em._stats["auth_events"], 1)


class TestIntegration(_Base):
    def test_fim_baseline_then_new_modified_deleted(self):
        a, b = self.root / "a", self.root / "b"
        a.write_text("1"); b.write_text("2")
        with patch.object(em, "FIM_PATHS", [str(self.root)]):
            asyncio.run(em.fim_check())
            self.rec.assert_not_called()                   # first pass = baseline
            a.write_text("changed"); b.unlink(); (self.root / "c").write_text("3")
            asyncio.run(em.fim_check())
        kinds = sorted(c[0][0] for c in self.rec.call_args_list)
        self.assertEqual(kinds, ["fim_deleted", "fim_modified", "fim_new_file"])
        self.assertEqual(em._stats["fim_changes"], 3)
        self.assertIn("telemetry.endpoint_events", SRC)


class TestFunctional(_Base):
    def test_main_golden_path_with_everything_mocked(self):
        from aiohttp import web
        site = MagicMock(); site.start = AsyncMock()
        runner = MagicMock(); runner.setup = AsyncMock(); runner.cleanup = AsyncMock()

        async def fake_sleep(_):
            em._shutdown = True
        try:
            with patch.object(em, "ensure_table", AsyncMock()) as et, \
                 patch.object(web, "AppRunner", return_value=runner), \
                 patch.object(web, "TCPSite", return_value=site) as tcp, \
                 patch.object(em.signal, "signal"), patch.object(em, "_pool", None), \
                 patch.object(em.asyncio, "sleep", fake_sleep), \
                 patch.object(em, "fim_check", AsyncMock()), patch.object(em, "process_check", AsyncMock()), \
                 patch.object(em, "auth_check", AsyncMock()):
                asyncio.run(em.main())
        finally:
            em._shutdown = False
        et.assert_awaited_once()
        self.assertEqual(tcp.call_args[0][2], em.HTTP_PORT)
        runner.cleanup.assert_awaited_once()

    def test_status_endpoint(self):
        em._alerts.extend(f"NEW: {i}" for i in range(30))
        body = json.loads(asyncio.run(em.handle_status(None)).body)
        self.assertEqual(len(body["recent_alerts"]), 20)
        self.assertTrue(json.loads(asyncio.run(em.handle_health(None)).body)["ok"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_endpoint_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
