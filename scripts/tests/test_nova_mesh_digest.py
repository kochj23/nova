#!/usr/bin/env python3
"""Tests for nova_mesh_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_mesh_digest.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("mesh_digest_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


md = _load()
_PATCHES = []


def setUpModule():
    # every outbound side effect is stubbed for the whole file
    for p in (patch.object(md.nc, "post_both"), patch.object(md.urllib.request, "urlopen",
                                                             side_effect=OSError("offline"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _msgs(*texts, who="!abcd"):
    return [{"t": i, "who": who if i % 2 else "!ef01", "text": t} for i, t in enumerate(texts)]


def _resp(text):
    r = MagicMock()
    r.read.return_value = json.dumps({"response": text}).encode()
    return r


CHATTER = _msgs("Good morning ☀️", "test", "The humidity is crazy", "good morning ☀️",
                "Rain expected anywhere today?", "☕☕☕☕☕", "ping", "")
LONG = " ".join(["word"] * 40)


def _main(argv, msgs, llm=""):
    with patch.object(md, "fetch_messages", return_value=msgs), patch.object(md, "local_llm", return_value=llm), \
            patch.object(md.nc, "post_both") as post, patch.object(md.sys, "argv", ["x", *argv]), \
            redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        rc = md.main()
    return rc, post, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_parameterized_hours(self):
        cur = MagicMock(); cur.fetchall.return_value = []
        conn = MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value.__enter__.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            md.fetch_messages(24)
        sql, params = cur.execute.call_args.args
        self.assertEqual(params, (24,))
        self.assertIn("make_interval(hours => %s)", sql)

    def test_chatter_only_goes_to_local_llm_and_private_slack(self):
        self.assertTrue(md.OLLAMA_URL.startswith("http://192.168."))
        rc, post, _ = _main([], CHATTER, llm=LONG)
        self.assertEqual(post.call_args.kwargs["slack_channel"], md.nc.SLACK_CHAN)
        self.assertIsNone(post.call_args.kwargs["discord_channel"])


class TestPerformance(unittest.TestCase):
    def test_interesting_10k_fast(self):
        msgs = _msgs(*[f"msg {i % 2000}" for i in range(10_000)])
        t0 = time.perf_counter()
        out = md._interesting(msgs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), 2000)


class TestRetry(unittest.TestCase):
    def test_llm_error_fails_open_to_empty(self):
        # RETRY GAP: local_llm() — single Ollama attempt; any error returns "" and main falls back
        with patch.object(md.urllib.request, "urlopen", side_effect=OSError("down")) as u, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(md.local_llm("s", "u"), "")
        self.assertEqual(u.call_count, 1)

    def test_post_failure_does_not_raise(self):
        with patch.object(md, "fetch_messages", return_value=CHATTER), patch.object(md, "local_llm", return_value=LONG), \
                patch.object(md.nc, "post_both", side_effect=RuntimeError("slack 500")), \
                patch.object(md.sys, "argv", ["x"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(md.main(), 0)
        self.assertIn("post failed", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_interesting_drops_noise_and_dupes(self):
        got = [m["text"] for m in md._interesting(CHATTER)]
        self.assertEqual(got, ["Good morning ☀️", "The humidity is crazy", "Rain expected anywhere today?", "☕☕☕☕☕"])
        self.assertEqual(md._interesting([]), [])

    def test_local_llm_strips_think(self):
        with patch.object(md.urllib.request, "urlopen", return_value=_resp("<think>hmm</think> Hello there")):
            self.assertEqual(md.local_llm("s", "u"), "Hello there")
        with patch.object(md.urllib.request, "urlopen", return_value=_resp("  plain  ")):
            self.assertEqual(md.local_llm("s", "u"), "plain")


class TestIntegration(unittest.TestCase):
    def test_reads_meshtastic_bridge_observations(self):
        self.assertIn("observer='nova_meshtastic_bridge'", SRC)
        self.assertIn("FROM shared_observations", SRC)
        self.assertIs(md.nc, sys.modules["nova_config"])

    def test_fetch_then_filter_shape(self):
        cur = MagicMock(); cur.fetchall.return_value = [(1, None, " hi neighbors "), (2, "!a", "ping")]
        conn = MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value.__enter__.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            out = md._interesting(md.fetch_messages(6))
        self.assertEqual(out, [{"t": 1, "who": "?", "text": "hi neighbors"}])


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_llm_summary(self):
        rc, post, out = _main([], CHATTER, llm=LONG)
        self.assertEqual(rc, 0)
        body = post.call_args.args[0]
        self.assertIn("(4 msgs, ~2 people)", body)
        self.assertIn(LONG, body)

    def test_short_llm_falls_back_to_highlights(self):
        rc, post, _ = _main([], CHATTER, llm="too short")
        self.assertIn("A taste:", post.call_args.args[0])

    def test_quiet_day_and_dry_run_never_post(self):
        rc, post, out = _main(["--hours", "6"], _msgs("ping", "hi"))
        post.assert_not_called()
        self.assertIn("in 6h", out)
        rc, post, out = _main(["--dry-run"], CHATTER, llm=LONG)
        post.assert_not_called()
        self.assertIn("neighborhood mesh today", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_mesh_digest"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
