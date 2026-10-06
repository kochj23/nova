#!/usr/bin/env python3
"""Tests for nova_copenhagen.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_copenhagen.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_copenhagen_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load()
_guards = []


def setUpModule():
    for p in (patch.object(cp.nc, "post_both"), patch.object(cp.nc, "slack_bot_token", return_value="xoxb-test"),
              patch("urllib.request.urlopen", side_effect=OSError("offline"))):
        p.start()
        _guards.append(p)


def tearDownModule():
    for p in _guards:
        p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")
        self.assertIn("nc.slack_bot_token()", SRC)

    def test_sanitize_strips_ips_hosts_household(self):
        out = cp.sanitize("Amys-iPhone at 192.168.1.43 on nova-core5 via ble_presence; Dylan's laptop")
        for leak in ("192.168", "nova-core5", "Amy", "Dylan", "ble_presence"):
            self.assertNotIn(leak.lower(), out.lower())
        self.assertIn("a resident", out)

    def test_restart_stale_dry_run_never_executes(self):
        stale = [{"name": "nova-capacity", "auto": True, "stale_h": 5, "host": None, "label": "x"},
                 {"name": "nova-scheduler-core", "auto": False, "stale_h": 9, "host": "h", "unit": "u"}]
        with patch.object(cp, "_sh") as sh:
            acts = cp.restart_stale(stale, dry=True)
        sh.assert_not_called()
        self.assertEqual(acts, ["[dry-run] would reload nova-capacity (running 5h-old code)"])

    def test_safe_fix_dry_run_and_whitelist(self):
        real = [{"sig": "nas manifest-sync failed on host"}, {"sig": "gateway down"}]
        with patch.object(cp.subprocess, "run") as run:
            self.assertEqual(cp.apply_safe_fixes(real, dry=True), ["[dry-run] would re-run NAS manifest sync"])
            run.assert_not_called()
            self.assertEqual(cp.apply_safe_fixes(real, dry=False), ["auto-fix: re-run NAS manifest sync"])
        self.assertEqual(run.call_count, 1)  # only the whitelisted fix, never the unknown one


class TestPerformance(unittest.TestCase):
    def test_signature_and_classify_10k(self):
        texts = [f"Backup stale on 192.168.1.{i % 250} for {i}h (mem_headroom {i}%)" for i in range(10_000)]
        t0 = time.perf_counter()
        sigs = {cp.signature(t) for t in texts}
        for s in sigs:
            cp.classify(s)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(sigs), 1)  # numbers/IPs collapse to one incident


class TestRetry(unittest.TestCase):
    def test_fetch_channel_fails_open(self):
        # RETRY GAP: fetch_channel — one urlopen attempt per page; error breaks with what was collected
        with patch.object(cp.urllib.request, "urlopen", side_effect=OSError("slack down")) as u, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cp.fetch_channel("C1", 0), [])
        self.assertEqual(u.call_count, 1)

    def test_queue_error_swallowed(self):
        # RETRY GAP: queue_for_session/psycopg2.connect — single attempt, logged
        with patch("psycopg2.connect", side_effect=OSError("pg")), redirect_stdout(io.StringIO()) as out:
            cp.queue_for_session([{"sig": "x", "example": "x", "count": 1, "note": ""}], dry=False)
        self.assertIn("queue error", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_classify_labels(self):
        self.assertEqual(cp.classify("mem_headroom low on node")[0], "false")
        self.assertEqual(cp.classify("backup failed tonight")[0], "real")
        self.assertEqual(cp.classify("auto-resolved: thing")[0], "noise")
        self.assertEqual(cp.classify("zzz")[0], "unknown")

    def test_build_digest_unknown_recurring_is_real(self):
        b = cp.build_digest({"weird thing": ["a", "bb", "ccc"], "odd once": ["x"]})
        self.assertEqual(b["real"][0]["example"], "ccc")
        self.assertEqual(b["noise"][0]["sig"], "odd once")

    def test_strip_meta_preamble_and_msg_text(self):
        self.assertEqual(cp._strip_meta_preamble("Here's the review.\n\n---\n\nThe box opened."), "The box opened.")
        m = {"text": "hi", "blocks": [{"elements": [{"elements": [{"type": "text", "text": "there"}]}]}]}
        self.assertEqual(cp.msg_text(m), "hi there")

    def test_match_recent_fix(self):
        fixes = [{"hash": "abc", "date": "d", "subject": "s", "tokens": {"mem_headroom", "available"}}]
        self.assertEqual(cp.match_recent_fix({"sig": "mem_headroom critical", "note": ""}, fixes), ("abc", "d", "s"))
        self.assertIsNone(cp.match_recent_fix({"sig": "x", "note": ""}, fixes))


class TestIntegration(unittest.TestCase):
    def test_queue_uses_claude_queue_parameterized(self):
        cur = MagicMock()
        conn = MagicMock(cursor=MagicMock(return_value=cur))
        real = [{"sig": "gateway down", "example": "Gateway down!", "count": 4, "note": "n"},
                {"sig": "x", "example": "fixed", "count": 1, "note": "", "recent_fix": ("a", "b", "c")}]
        with patch("psycopg2.connect", return_value=conn):
            cp.queue_for_session(real, dry=False)
        inserts = [c for c in cur.execute.call_args_list if "claude_queue" in c[0][0]]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][0][1][0], "OVERNIGHT: Gateway down!")

    def test_write_article_sanitizes_and_publishes(self):
        calls = []

        def fake_or(system, user, **kw):
            calls.append(user)
            return "T" if kw.get("max_tokens") == 40 else ("word " * 3000 + " seen at 10.0.0.9")
        j = types.SimpleNamespace(call_openrouter=fake_or, publish_hugo=MagicMock(return_value=True), git_push=MagicMock())
        voice = types.SimpleNamespace(system_prompt=lambda c: "sys", CONTEXT_JOURNAL_OPS="ops")
        img = types.SimpleNamespace(generate_image=MagicMock(return_value=None))
        buckets = {"real": [{"sig": "s", "count": 2, "example": "fail on 192.168.1.9 for amy", "note": "n"}],
                   "false": [], "noise": []}
        with patch.dict(sys.modules, {"nova_journal": j, "nova_voice": voice, "nova_image_utils": img}), redirect_stdout(io.StringIO()):
            title = cp.write_article(buckets, [], 2, dry=False)
        self.assertEqual(title, "T")
        self.assertNotIn("192.168.1.9", calls[0])
        self.assertNotIn("amy", calls[0].lower())
        self.assertNotIn("10.0.0.9", j.publish_hugo.call_args.kwargs["body"])
        j.git_push.assert_called_once_with("operations", "T")


class TestFunctional(unittest.TestCase):
    def _main(self, argv, article="Title"):
        msgs = [{"text": "Backup stale on nova-core 1h"}, {"text": "Backup stale on nova-core 2h"},
                {"text": "mem_headroom 3% critical"}]
        with patch.object(sys, "argv", argv), patch.object(cp, "fetch_channel", side_effect=[msgs] + [[]] * 4), \
             patch.object(cp, "recent_fixes", return_value=[]), patch.object(cp, "stale_daemons", return_value=[]), \
             patch.object(cp, "queue_for_session") as q, patch.object(cp, "write_article", return_value=article) as wa, \
             patch.object(cp.nc, "post_both") as pb, redirect_stdout(io.StringIO()):
            rc = cp.main()
        return rc, q, wa, pb

    def test_main_golden_path_posts_digest(self):
        rc, q, wa, pb = self._main(["x"])
        self.assertEqual(rc, 0)
        buckets = wa.call_args[0][0]
        self.assertEqual(buckets["real"][0]["count"], 2)
        self.assertEqual(len(buckets["false"]), 1)
        self.assertIn("3 alerts", pb.call_args[0][0])
        self.assertEqual(pb.call_args.kwargs["slack_channel"], cp.nc.SLACK_DIGEST)

    def test_main_dry_run_posts_nothing(self):
        rc, q, wa, pb = self._main(["x", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue(q.call_args[0][1])
        pb.assert_not_called()

    def test_article_failure_skips_digest(self):
        _, _, _, pb = self._main(["x"], article=None)
        pb.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_copenhagen; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
