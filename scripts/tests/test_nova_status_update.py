#!/usr/bin/env python3
"""Tests for nova_status_update.py — the 7 house categories (Security, Performance, Retry, Unit,
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_status_update.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


su = _load("su_mod", SCRIPT)
_TMP = tempfile.TemporaryDirectory()
su.STATUS_FILE = Path(_TMP.name) / "STATUS.md"        # never touch ~/.openclaw/workspace


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _urlopen_for(table):
    def fake(url, timeout=3):
        for k, v in table.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return _Resp(v)
        raise OSError("connection refused")
    return fake


def _main(table, cron=None):
    cron = cron or types.SimpleNamespace(returncode=0, stdout=json.dumps({"enabled": True, "jobs": 7}))
    with patch.object(su.urllib.request, "urlopen", side_effect=_urlopen_for(table)), \
         patch.object(su.subprocess, "run", return_value=cron) as sp, redirect_stdout(io.StringIO()) as out:
        su.main()
    return su.STATUS_FILE.read_text(), sp, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_subprocess_is_argv_list_no_shell(self):
        self.assertIn('["openclaw", "cron", "status"]', SRC)
        self.assertNotIn("shell=True", SRC)

    def test_remote_json_is_rendered_not_executed(self):
        evil = {"status": "ok", "count": 1, "model": "<script>alert(1)</script>"}
        text, _, _ = _main({"/health": evil, "/stats": {"by_source": {}}})
        self.assertIn("<script>", text)       # status file is a plain markdown dump; nothing is eval'd
        self.assertNotIn("eval(", SRC); self.assertNotIn("exec(", SRC)


class TestPerformance(unittest.TestCase):
    def test_by_source_rendering_is_capped_at_eight(self):
        big = {f"src{i}": i for i in range(10_000)}
        t0 = time.perf_counter()
        text, _, _ = _main({"/health": {"status": "ok", "count": 5, "model": "m"}, "/stats": {"by_source": big}})
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(text.count("  - src"), 8)
        self.assertIn("  - src9999: 9999", text)


class TestRetry(unittest.TestCase):
    def test_check_fails_open_to_empty_dict(self):
        # RETRY GAP: check()/urllib.request.urlopen — single attempt, any error returns {} (next run retries)
        attempts = []

        def boom(url, timeout=3):
            attempts.append(url); raise OSError("down")
        with patch.object(su.urllib.request, "urlopen", side_effect=boom):
            self.assertEqual(su.check("http://127.0.0.1:1/x"), {})
        self.assertEqual(len(attempts), 1)

    def test_openclaw_cli_failure_fails_open(self):
        # RETRY GAP: main()/subprocess.run(openclaw cron status) — one shot; any exception → "not responding"
        with patch.object(su.urllib.request, "urlopen", side_effect=OSError("down")), \
             patch.object(su.subprocess, "run", side_effect=subprocess.TimeoutExpired("openclaw", 5)), \
             redirect_stdout(io.StringIO()):
            su.main()
        self.assertIn("❌ Gateway not responding", su.STATUS_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_check_parses_json_and_passes_timeout(self):
        with patch.object(su.urllib.request, "urlopen", return_value=_Resp({"a": 1})) as u:
            self.assertEqual(su.check("http://x", timeout=9), {"a": 1})
        self.assertEqual(u.call_args[1]["timeout"], 9)

    def test_check_bad_json_is_empty(self):
        class Bad(_Resp):
            def read(self):
                return b"not json"
        with patch.object(su.urllib.request, "urlopen", return_value=Bad({})):
            self.assertEqual(su.check("http://x"), {})

    def test_app_status_running_vs_not(self):
        with patch.object(su.urllib.request, "urlopen", return_value=_Resp({"status": "running"})):
            self.assertEqual(su.app_status(37422, "MLXCode"), "✅ MLXCode running (port 37422)")
        with patch.object(su.urllib.request, "urlopen", return_value=_Resp({"app": "x"})):
            self.assertTrue(su.app_status(1, "A").startswith("✅"))
        with patch.object(su.urllib.request, "urlopen", return_value=_Resp({})):
            self.assertEqual(su.app_status(1, "A"), "❌ A not running (port 1)")


class TestIntegration(unittest.TestCase):
    def test_memory_and_ollama_endpoints_and_status_path(self):
        self.assertIn("memory-server.digitalnoise.net:18790/health", SRC)
        self.assertIn("127.0.0.1:11434/api/tags", SRC)
        self.assertEqual(SRC.count("STATUS_FILE = WORKSPACE"), 1)
        self.assertIn('WORKSPACE = Path.home() / ".openclaw/workspace"', SRC)

    def test_stats_only_fetched_when_memory_is_ok(self):
        calls = []

        def fake(url, timeout=3):
            calls.append(url); raise OSError("down")
        with patch.object(su.urllib.request, "urlopen", side_effect=fake), \
             patch.object(su.subprocess, "run", side_effect=OSError("no cli")), redirect_stdout(io.StringIO()):
            su.main()
        self.assertFalse(any("/stats" in u for u in calls))
        self.assertEqual(sum("/api/status" in u for u in calls), 5)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_status_md(self):
        table = {"/health": {"status": "ok", "count": 1400000, "model": "nomic"},
                 "/stats": {"by_source": {"email": 9, "scanner": 20}},
                 "37422/api/status": {"status": "running"},
                 "11434/api/tags": {"models": [{"name": "qwen"}, {"name": "llama"}]}}
        text, sp, out = _main(table)
        self.assertIn("✅ ONLINE — 1400000 memories, model: nomic", text)
        self.assertEqual(text.index("  - scanner: 20") < text.index("  - email: 9"), True)
        self.assertIn("✅ MLXCode running (port 37422)", text)
        self.assertIn("❌ RsyncGUI not running (port 37424)", text)
        self.assertIn("✅ Ollama running — 2 models loaded", text)
        self.assertIn("✅ Gateway up — 7 cron jobs active", text)
        self.assertIn("**1,400,000 memories**", text)
        self.assertEqual(sp.call_args[0][0], ["openclaw", "cron", "status"])
        self.assertIn("Status written", out)

    def test_everything_down_still_writes_a_file(self):
        text, _, _ = _main({}, cron=types.SimpleNamespace(returncode=1, stdout=""))
        for needle in ("❌ DOWN", "❌ Ollama not responding", "❌ Gateway not responding", "**0 memories**"):
            self.assertIn(needle, text)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_status_update"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
