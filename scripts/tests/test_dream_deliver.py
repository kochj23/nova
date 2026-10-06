#!/usr/bin/env python3
"""Tests for dream_deliver.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "dream_deliver.py"
SRC = SCRIPT.read_text()

import nova_config  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("dream_deliver_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"):
        spec.loader.exec_module(mod)
    return mod


dd = _load()
_guard = None


def setUpModule():
    global _guard
    _guard = patch("urllib.request.urlopen", side_effect=OSError("offline"))
    _guard.start()


def tearDownModule():
    _guard.stop()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(obj):
    return _Resp(json.dumps(obj).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.pending = t / "pending_delivery.json"
        self.dead = t / "failed"
        self.p = [patch.object(dd, "PENDING_FILE", self.pending), patch.object(dd, "DEAD_LETTER", self.dead),
                  patch.object(dd, "HERD_RECIPIENTS", ["a@example.test", "b@example.test"])]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, slack_ok=True):
        posts = []

        def fake_post(endpoint, payload):
            posts.append((endpoint, payload))
            return {"ok": slack_ok}
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="cc@example.test\n", stderr="")
        out = io.StringIO()
        with patch.object(dd, "slack_post", side_effect=fake_post), \
             patch.object(dd, "generate_haiku", return_value="a\\nb\\nc"), \
             patch("subprocess.run", side_effect=fake_run), redirect_stdout(out):
            dd.main()
        return posts, runs, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bpa]-[0-9A-Za-z]")

    def test_token_and_cc_come_from_keychain(self):
        self.assertIn("nova_config.slack_bot_token()", SRC)
        self.assertIn('"security", "find-generic-password"', SRC)

    def test_haiku_stays_local(self):
        self.assertIn("http://127.0.0.1:11434/api/chat", SRC)
        self.assertNotRegex(SRC, r"openrouter|api\.openai|anthropic\.com")


class TestPerformance(unittest.TestCase):
    def test_chunking_large_narrative_fast(self):
        calls = []
        with patch.object(dd, "slack_post", side_effect=lambda e, p: calls.append(p) or {"ok": True}), \
             redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            ok = dd.post_dream("x" * 300_000, None, "2026-01-01")
            el = time.perf_counter() - t0
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1 + 100)  # header + 100 chunks of 3000
        self.assertLess(el, 1.0)


class TestRetry(_Base):
    def test_failed_slack_increments_retry_count(self):
        self.pending.write_text(json.dumps({"narrative": "dream", "date": "2026-01-01", "image": None}))
        self.run_main(slack_ok=False)
        self.assertEqual(json.loads(self.pending.read_text())["_retry_count"], 1)

    def test_dead_letter_after_max_retries(self):
        self.pending.write_text(json.dumps({"narrative": "dream", "date": "2026-01-02",
                                            "_retry_count": dd.MAX_RETRIES - 1}))
        self.run_main(slack_ok=False)
        self.assertFalse(self.pending.exists())
        dead = json.loads((self.dead / "2026-01-02.json").read_text())
        self.assertIn("failed after", dead["_failure_reason"])

    def test_slack_post_fails_open(self):
        # RETRY GAP: slack_post — single attempt per run; the cross-run retry is the pending-file counter
        with patch.object(dd.urllib.request, "urlopen", side_effect=OSError("down")), \
             redirect_stdout(io.StringIO()):
            r = dd.slack_post("chat.postMessage", {"text": "x"})
        self.assertEqual(r["ok"], False)
        self.assertIn("down", r["error"])


class TestUnit(unittest.TestCase):
    def test_haiku_strips_think_and_trims(self):
        body = {"message": {"content": "<think>hmm</think>line one\nline two\nline three\nline four"}}
        with patch.object(dd.urllib.request, "urlopen", return_value=_resp(body)), redirect_stdout(io.StringIO()):
            h = dd.generate_haiku("n")
        self.assertEqual(h, "line one\\nline two\\nline three")

    def test_haiku_fallback_on_error(self):
        with patch.object(dd.urllib.request, "urlopen", side_effect=OSError("x")), redirect_stdout(io.StringIO()):
            h = dd.generate_haiku("n")
        self.assertIn("Dreams loop", h)

    def test_upload_missing_image_returns_false(self):
        with redirect_stdout(io.StringIO()):
            self.assertFalse(dd.upload_image_to_channel("/nonexistent/x.png", "C1", "hdr"))

    def test_post_dream_meta_line(self):
        sent = []
        dd.post_dream._dream_meta = {"theme": "tides", "mood": "calm"}
        try:
            with patch.object(dd, "slack_post", side_effect=lambda e, p: sent.append(p) or {"ok": True}), \
                 redirect_stdout(io.StringIO()):
                dd.post_dream("n", None, "2026-01-01")
        finally:
            dd.post_dream._dream_meta = {}
        self.assertIn('Theme: "tides"', sent[0]["text"])
        self.assertTrue(sent[-1]["text"].endswith("2026-01-01_"))


class TestIntegration(_Base):
    def test_private_inspirations_dropped_via_nova_config(self):
        self.pending.write_text(json.dumps({"narrative": "dream", "date": "2026-01-03", "inspirations": [
            {"source": "imessage", "label": "x", "memory": "SECRET_TEXT"},
            {"source": "wikipedia", "label": "y", "memory": "public fact"}]}))
        posts, _, _ = self.run_main()
        text = " ".join(p["text"] for _, p in posts)
        self.assertIn("public fact", text)
        self.assertNotIn("SECRET_TEXT", text)

    def test_upload_three_step_flow(self):
        img = Path(self.tmp.name) / "d.png"
        img.write_bytes(b"\x89PNG")
        seq = [_resp({"ok": True, "upload_url": "https://up.example/x", "file_id": "F1"}), _resp({})]
        with patch.object(dd.urllib.request, "urlopen", side_effect=seq), \
             patch.object(dd, "slack_post", return_value={"ok": True}) as sp, redirect_stdout(io.StringIO()):
            self.assertTrue(dd.upload_image_to_channel(str(img), dd.SLACK_CHANNEL, "hdr"))
        self.assertEqual(sp.call_args[0][0], "files.completeUploadExternal")
        self.assertEqual(sp.call_args[0][1]["files"][0]["id"], "F1")


class TestFunctional(_Base):
    def test_golden_path_posts_emails_and_cleans_up(self):
        self.pending.write_text(json.dumps({"narrative": "A dream\n![Dream]([image path])", "date": "2026-01-04"}))
        posts, runs, _ = self.run_main()
        self.assertFalse(self.pending.exists())
        texts = [p["text"] for _, p in posts]
        self.assertTrue(any("Dream Journal" in t for t in texts))
        self.assertFalse(any("![Dream]([" in t for t in texts))
        herd = [r for r in runs if "send" in r]
        self.assertEqual(herd[0][herd[0].index("--to") + 1], "a@example.test")

    def test_no_pending_exits_zero(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            dd.main()
        self.assertEqual(cm.exception.code, 0)

    def test_json_auto_repair(self):
        raw = b'{"narrative": "said \\\xe2\x80\x9chi\xe2\x80\x9d", "date": "2026-01-05"}'
        self.pending.write_bytes(raw)
        posts, _, out = self.run_main()
        self.assertIn("Auto-repair OK", out)
        self.assertTrue(any("“hi" in p["text"] for _, p in posts))


class TestFrame(unittest.TestCase):
    def test_import_smoke_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys; sys.path.insert(0, '.'); import nova_config; "
                "nova_config._keychain = lambda *a, **k: 'x'; import dream_deliver; print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
