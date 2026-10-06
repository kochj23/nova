#!/usr/bin/env python3
"""Tests for slack_post_image.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module reads the Slack token at import, so nova_config.slack_bot_token is patched for the load (the
Keychain is never read) and every Slack call goes through a mocked urlopen."""
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "slack_post_image.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
FAKE_TOKEN = "tok-" + "test-only"


def _load():
    import nova_config
    with patch.object(nova_config, "slack_bot_token", return_value=FAKE_TOKEN) as tok:
        spec = importlib.util.spec_from_file_location("slack_post_image_t", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod._TOKEN_CALLS = tok.call_count
    return mod


sp = _load()


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    r.__enter__.return_value = r
    return r


class FakeSlack:
    def __init__(self, get_ok=True, complete_ok=True, put_exc=None):
        self.get_ok, self.complete_ok, self.put_exc, self.reqs = get_ok, complete_ok, put_exc, []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if req.full_url.endswith("files.getUploadURLExternal"):
            return _resp({"ok": True, "upload_url": "https://files.slack.example/up/1", "file_id": "F1"}
                         if self.get_ok else {"ok": False, "error": "invalid_auth"})
        if req.full_url.startswith("https://files.slack.example"):
            if self.put_exc:
                raise self.put_exc
            return _resp({})
        return _resp({"ok": self.complete_ok, "error": None if self.complete_ok else "channel_not_found"})


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.img = Path(self.td.name) / "cat pic.png"
        self.img.write_bytes(b"\x89PNG" + b"0" * 100)
        p = patch.object(sp.urllib.request, "urlopen", MagicMock(side_effect=AssertionError("unmocked Slack")))
        p.start()
        self.addCleanup(p.stop)

    def upload(self, slack, *args):
        out, err, code = io.StringIO(), io.StringIO(), None
        with patch.object(sp.urllib.request, "urlopen", slack), redirect_stdout(out), redirect_stderr(err):
            try:
                sp.upload_image(*args)
            except SystemExit as e:
                code = e.code
        return out.getvalue(), err.getvalue(), code


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")
        self.assertIn("nova_config.slack_bot_token()", SRC)

    def test_token_sent_only_to_slack_api_not_upload_host(self):
        slack = FakeSlack()
        self.upload(slack, str(self.img), "C1")
        put = [r for r in slack.reqs if r.full_url.startswith("https://files.slack.example")][0]
        self.assertIsNone(put.get_header("Authorization"))
        api = [r for r in slack.reqs if r.full_url.startswith(sp.SLACK_API)]
        self.assertTrue(all(r.get_header("Authorization") == "Bearer " + FAKE_TOKEN for r in api))


class TestPerformance(_Base):
    def test_large_image_single_put(self):
        self.img.write_bytes(b"0" * 5_000_000)
        slack = FakeSlack()
        t0 = time.perf_counter()
        self.upload(slack, str(self.img), "C1")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(slack.reqs), 3)


class TestRetry(_Base):
    def test_put_failure_is_one_shot_and_raises(self):
        # RETRY GAP: upload_image — each of the 3 Slack calls is tried once; a failing byte upload propagates
        slack = FakeSlack(put_exc=OSError("reset"))
        with self.assertRaises(OSError):
            self.upload(slack, str(self.img), "C1")
        self.assertEqual(len(slack.reqs), 2)

    def test_api_errors_exit_1(self):
        _, err, code = self.upload(FakeSlack(get_ok=False), str(self.img), "C1")
        self.assertEqual(code, 1)
        self.assertIn("invalid_auth", err)
        _, err, code = self.upload(FakeSlack(complete_ok=False), str(self.img), "C1")
        self.assertEqual(code, 1)
        self.assertIn("channel_not_found", err)


class TestUnit(_Base):
    def test_missing_file_exits_before_any_call(self):
        slack = FakeSlack()
        _, err, code = self.upload(slack, str(Path(self.td.name) / "nope.png"), "C1")
        self.assertEqual(code, 1)
        self.assertEqual(slack.reqs, [])
        self.assertIn("File not found", err)

    def test_slack_post_helper(self):
        with patch.object(sp.urllib.request, "urlopen", return_value=_resp({"ok": True})) as uo:
            self.assertEqual(sp.slack_post("chat.postMessage", {"a": 1}), {"ok": True})
        self.assertEqual(uo.call_args.args[0].full_url, sp.SLACK_API + "/chat.postMessage")


class TestIntegration(_Base):
    def test_token_resolved_once_from_nova_config(self):
        self.assertEqual(sp._TOKEN_CALLS, 1)
        self.assertEqual(sp.SLACK_TOKEN, FAKE_TOKEN)

    def test_three_step_external_upload_flow(self):
        slack = FakeSlack()
        self.upload(slack, str(self.img), "C9", "look")
        urls = [r.full_url for r in slack.reqs]
        self.assertTrue(urls[0].endswith("/files.getUploadURLExternal"))
        self.assertIn(b"filename=cat+pic.png", slack.reqs[0].data)
        self.assertTrue(urls[2].endswith("/files.completeUploadExternal"))


class TestFunctional(_Base):
    def test_golden_path_shares_with_caption(self):
        slack = FakeSlack()
        out, _, code = self.upload(slack, str(self.img), "C9", "look at this")
        self.assertIsNone(code)
        body = json.loads(slack.reqs[2].data)
        self.assertEqual(body, {"files": [{"id": "F1", "title": "cat pic"}], "channel_id": "C9",
                                "initial_comment": "look at this"})
        self.assertIn("Image posted to C9: cat pic.png", out)
        self.assertEqual(slack.reqs[1].data, self.img.read_bytes())


class TestFrame(unittest.TestCase):
    def test_cli_usage_with_stubbed_token(self):
        # the token is read at import (Keychain), so the CLI smoke stubs nova_config in a child process
        code = ("import sys, types, runpy\n"
                "c = types.ModuleType('nova_config'); c.slack_bot_token = lambda: 'x'\n"
                "sys.modules.update({'nova_config': c})\n"
                f"sys.argv = [{str(SCRIPT)!r}]\n"
                f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: slack_post_image.py", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
