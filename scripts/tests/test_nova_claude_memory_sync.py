#!/usr/bin/env python3
"""Tests for nova_claude_memory_sync.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.parse    # noqa: F401  (locked in before the module load)
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_claude_memory_sync.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="claude_mem_sync_test_"))

import nova_config  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("cms", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


cms = _load()
cms.LOG_FILE = TMP / "claude_memory_sync.log"
cms.notify = MagicMock(return_value=True)          # never reach Slack, in any test


def _resp(payload):
    r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _conn(rows):
    cur = MagicMock(); cur.fetchall.return_value = rows
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(cms.OPS_DSN, r":[^@/]+@")      # no password in the DSN

    def test_sql_is_read_only_and_static(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|UPDATE|DELETE FROM)\b")
        self.assertNotRegex(SRC, r'execute\(\s*f"')

    def test_recall_query_is_url_encoded(self):
        conn, _ = _conn([("a b&source=evil", "d", "user", "content", None)])
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url if hasattr(req, "full_url") else req)
            return _resp({"memories": []})
        with patch.object(cms.psycopg2, "connect", return_value=conn), \
             patch.object(cms.urllib.request, "urlopen", side_effect=fake), _quiet():
            cms.main()
        self.assertIn("a%20b%26source%3Devil", seen[0])
        self.assertNotIn("&source=evil", seen[0])


class TestPerformance(unittest.TestCase):
    def test_main_over_10k_rows_is_bounded(self):
        rows = [(f"m{i}", "d", "user", f"content {i}", None) for i in range(10_000)]
        conn, _ = _conn(rows)
        t0 = time.perf_counter()
        with patch.object(cms.psycopg2, "connect", return_value=conn), \
             patch.object(cms.urllib.request, "urlopen", return_value=_resp({"memories": []})), \
             patch.object(cms, "log"):
            cms.main()
        self.assertLess(time.perf_counter() - t0, 8.0)


class TestRetry(unittest.TestCase):
    def test_remember_is_one_shot_and_fails_open(self):
        # RETRY GAP: remember() — one POST, no retry; a failure returns False, never raises
        m = MagicMock(side_effect=OSError("memory server down"))
        with patch.object(cms.urllib.request, "urlopen", m), _quiet():
            self.assertFalse(cms.remember("text", {"name": "x"}))
        self.assertEqual(m.call_count, 1)

    def test_synced_hashes_fails_open_to_zero(self):
        # RETRY GAP: get_synced_hashes() — one GET, returns 0 when the server is down
        with patch.object(cms.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(cms.get_synced_hashes(None), 0)


class TestUnit(unittest.TestCase):
    def test_remember_payload_shape_and_truncation(self):
        captured = {}

        def fake(req, timeout=None):
            captured["body"] = json.loads(req.data.decode()); captured["timeout"] = timeout
            return _resp({})
        with patch.object(cms.urllib.request, "urlopen", side_effect=fake):
            self.assertTrue(cms.remember("word " * 2000, {"k": 1}))
        body = captured["body"]
        self.assertEqual(body["source"], "claude_memory")
        self.assertEqual(body["tier"], "long_term")
        self.assertLessEqual(len(body["text"]), 2000)
        self.assertEqual(body["metadata"], {"k": 1})

    def test_synced_hashes_reads_total(self):
        with patch.object(cms.urllib.request, "urlopen", return_value=_resp({"total": 42})):
            self.assertEqual(cms.get_synced_hashes(None), 42)
        with patch.object(cms.urllib.request, "urlopen", return_value=_resp({})):
            self.assertEqual(cms.get_synced_hashes(None), 0)

    def test_log_writes_to_redirected_file(self):
        with _quiet():
            cms.log("hello unit")
        self.assertIn("hello unit", cms.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_uses_shared_helpers_and_right_table(self):
        self.assertIn("nova_config.truncate_at_boundary", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("FROM claude_memories", SRC)
        self.assertEqual(cms.SOURCE, "claude_memory")

    def test_unchanged_hash_is_skipped(self):
        import hashlib
        h = hashlib.md5(b"same").hexdigest()
        conn, _ = _conn([("n", "d", "user", "same", None)])
        rem = MagicMock(return_value=True)
        with patch.object(cms.psycopg2, "connect", return_value=conn), \
             patch.object(cms.urllib.request, "urlopen", return_value=_resp({"memories": [{"metadata": {"hash": h}}]})), \
             patch.object(cms, "remember", rem), _quiet():
            cms.main()
        rem.assert_not_called()


class TestFunctional(unittest.TestCase):
    def setUp(self):
        cms.notify.reset_mock()

    def test_golden_path_syncs_and_notifies(self):
        conn, cur = _conn([("alpha", "desc", "feedback", "body text", "2026-01-01")])
        rem = MagicMock(return_value=True)
        with patch.object(cms.psycopg2, "connect", return_value=conn), \
             patch.object(cms.urllib.request, "urlopen", return_value=_resp({"memories": []})), \
             patch.object(cms, "remember", rem), _quiet():
            cms.main()
        text, meta = rem.call_args[0]
        self.assertTrue(text.startswith("[Claude Memory: alpha] (feedback) desc"))
        self.assertEqual(meta["name"], "alpha")
        self.assertEqual(len(meta["hash"]), 32)
        cms.notify.assert_called_once()
        self.assertIn("1 memories synced", cms.notify.call_args.kwargs["body"])
        conn.close.assert_called_once()

    def test_failed_ingest_does_not_notify(self):
        conn, _ = _conn([("alpha", None, "user", "body", None)])
        with patch.object(cms.psycopg2, "connect", return_value=conn), \
             patch.object(cms.urllib.request, "urlopen", side_effect=OSError("down")), _quiet():
            cms.main()
        cms.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_claude_memory_sync"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
