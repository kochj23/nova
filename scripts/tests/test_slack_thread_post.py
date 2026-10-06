#!/usr/bin/env python3
"""Tests for slack_thread_post.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The `message` tool subprocess is mocked and Path.home() points at
a tempdir, so nothing is posted and no metadata lands in the real workspace.
Written by Jordan Koch (via Claude)."""
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
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "slack_thread_post.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("slack_thread_post_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


st = _load()


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["message"], rc, stdout=out, stderr=err)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ps = [patch.object(st.Path, "home", return_value=Path(self.tmp.name)),
                   patch.object(st.subprocess, "run", return_value=_cp(0, json.dumps({"ts": "171.5"})))]
        _, self.run = [p.start() for p in self.ps]
        self.poster = st.SlackThreadPoster("C123")

    def tearDown(self):
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def main(self, *argv, stdin=None):
        with patch.object(sys, "argv", ["x", *argv]), redirect_stdout(io.StringIO()) as out:
            if stdin is not None:
                with patch.object(sys, "stdin", io.StringIO(stdin)):
                    st.main()
            else:
                st.main()
        return out.getvalue()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"xox[bpa]-")
        self.assertNotRegex(SRC, r"(?i)(password|secret|token)\s*=\s*['\"][^'\"]{8,}")
        self.assertNotIn("shell=True", SRC)

    def test_message_passed_as_single_argv(self):
        evil = "hi $(whoami); `id` && echo pwned"
        self.poster.post_message("s", evil, thread_ts="1.2")
        cmd = self.run.call_args[0][0]
        self.assertEqual(cmd, ["message", "action=send", "target=C123", f"message={evil}", "threadId=1.2"])

    def test_metadata_written_under_temp_workspace(self):
        self.poster.post_message("s", "c", metadata={"kind": "digest"})
        files = list((Path(self.tmp.name) / ".openclaw/workspace/slack-threads").glob("*.json"))
        self.assertEqual([f.name for f in files], ["171-5.json"])


class TestPerformance(_Base):
    def test_parse_many_sections(self):
        md = "".join(f"## Section {i}\nline a\nline b\n" for i in range(10_000))
        t0 = time.perf_counter()
        secs = self.poster.parse_markdown_sections(md)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(secs), 10_000)


class TestRetry(_Base):
    def test_tool_failure_fails_open(self):
        # RETRY GAP: post_message/_run_slack_cmd — one subprocess attempt; error dict, never raises
        self.run.return_value = _cp(1, err="no such channel")
        self.assertEqual(self.poster.post_message("s", "c"), {"status": "error", "error": "no such channel"})
        self.run.side_effect = subprocess.TimeoutExpired("message", 15)
        self.assertEqual(self.poster.post_message("s", "c")["status"], "error")
        self.assertEqual(self.poster._run_slack_cmd(["message"])["status"], "error")
        self.assertEqual(self.run.call_count, 3)


class TestUnit(_Base):
    def test_markdown_sections(self):
        secs = self.poster.parse_markdown_sections("preamble\n## A\none\n\n## B\ntwo\n")
        self.assertEqual(secs, [{"title": "A", "content": "one"}, {"title": "B", "content": "two"}])
        self.assertEqual(self.poster.parse_markdown_sections("no headers"), [])

    def test_email_digest_grouping(self):
        d = {"emails": [{"from": "b@x", "subject": "S1", "body_preview": "p" * 300}, {"from": "a@x"}, {"from": "b@x"}]}
        secs = self.poster.parse_email_digest(d)
        self.assertEqual([s["title"] for s in secs], ["From: a@x", "From: b@x"])
        self.assertIn("p" * 200 + "...", secs[1]["content"])
        self.assertNotIn("p" * 201, secs[1]["content"])
        self.assertIn("(no subject)", secs[0]["content"])

    def test_run_slack_cmd_non_json(self):
        self.run.return_value = _cp(0, "plain text")
        self.assertEqual(self.poster._run_slack_cmd(["x"]), {"status": "ok", "stdout": "plain text"})


class TestIntegration(_Base):
    def test_sectioned_post_threads_replies_to_parent(self):
        calls = []

        def tool(msg, ts=None):
            calls.append((msg, ts))
            return {"status": "ok", "ts": f"{len(calls)}.0"}
        with patch.object(self.poster, "_post_via_tool", side_effect=tool):
            res = self.poster.post_sectioned("Digest", [{"title": "A", "content": "x"}, {"title": "B", "content": "y"}])
        self.assertEqual(calls[0], ("*Digest*\n\n• A\n• B", None))
        self.assertEqual([c[1] for c in calls[1:]], ["1.0", "1.0"])
        self.assertEqual([p["type"] for p in res["posts"]], ["parent", "reply", "reply"])


class TestFunctional(_Base):
    def test_cli_single_message(self):
        out = self.main("--channel", "C9", "--message", "hello", "--subject", "Hi", "--metadata", "a=1, b = 2")
        self.assertEqual(json.loads(out), {"status": "ok", "ts": "171.5", "thread_ts": "171.5"})
        self.assertEqual(self.run.call_args[0][0][3], "message=*Hi*\nhello")

    def test_cli_sections_from_stdin_get_ts(self):
        out = self.main("--channel", "C9", "--stdin", "--parse-sections", "--get-ts", stdin="## A\nx\n")
        self.assertRegex(out.strip(), r"^\d+\.000001$")
        self.run.assert_not_called()

    def test_cli_no_content_exits_1(self):
        with patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as cm:
            self.main("--channel", "C9")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("No content source", err.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--parse-sections", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(st.subprocess, "run") as run:
            _load()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
