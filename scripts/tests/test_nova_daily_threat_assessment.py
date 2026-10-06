#!/usr/bin/env python3
"""Tests for nova_daily_threat_assessment.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Never reads the real mailbox: osascript (subprocess.run), PG, the LLM, Slack and the Hugo
publish/git push are all mocked for the whole file; state/evidence files go to a tempdir."""
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
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_daily_threat_assessment.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("daily_threat_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ta = _load()
import nova_journal as nj  # noqa: E402 — already imported by nova_rando_daily_ops chain; patched below

_TD = tempfile.TemporaryDirectory()
_REAL_RUN = subprocess.run
_PATCHES = []


def setUpModule():
    d = Path(_TD.name)
    ps = [patch.object(ta, "STATE_FILE", d / "seen.json"), patch.object(ta, "MEM_STATE_FILE", d / "mem.json"),
          patch.object(ta, "EVIDENCE_DIR", d / "evidence"),
          patch.object(ta.subprocess, "run", side_effect=AssertionError("unmocked osascript")),
          patch.object(ta.psycopg2, "connect", side_effect=AssertionError("unmocked PG")),
          patch.object(ta, "call_llm", side_effect=AssertionError("unmocked LLM")),
          patch.object(ta.nova_config, "post_both"),
          patch.object(ta.time, "sleep")]
    for name in ("log", "call_openrouter", "get_image_prompt", "generate_image", "publish_hugo", "git_push"):
        ps.append(patch.object(nj, name))
    for p in ps:
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _mail(*msgs):
    return "".join(f"###MSGID###{i}###SENDER###{s}###SUBJECT###{subj}###BODY###{b}###END###\n" for i, s, subj, b in msgs)


PHISH = {"category": "phishing", "confidence": 0.9, "reasoning": "fake bank", "suggested_action": "block sender"}
SALES = {"category": "cold_outreach", "confidence": 0.95, "reasoning": "sales"}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_personal_address(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("nova_config.JORDAN_DOMAIN_EMAIL", SRC)        # account from config, not a literal
        self.assertIsNone(re.search(r"[\w.]+@digitalnoise\.net", SRC))

    def test_evidence_filename_sanitized(self):
        p = Path(ta.write_evidence({"msgid": "<../../etc/passwd@x>", "sender": "a", "subject": "s", "body": "full"}, PHISH))
        self.assertEqual(p.parent, ta.EVIDENCE_DIR)
        self.assertNotIn("/", p.name.split("-", 2)[-1])
        self.assertIn("FULL ORIGINAL MESSAGE BODY:\n\nfull", p.read_text())

    def test_public_article_is_vague(self):
        with patch.object(nj, "call_openrouter", return_value="TITLE: Quiet Night\n\nAll calm.") as co:
            ta.publish_vague_local_article(5, 1, 0, 2)
        material = co.call_args.args[1]
        self.assertNotIn("@", material)
        self.assertIn("nothing dramatic enough to spell out", material)
        self.assertIn("DELIBERATELY VAGUE", co.call_args.args[0])

    def test_inserts_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))


class TestPerformance(unittest.TestCase):
    def test_parse_2k_messages_fast(self):
        out = _mail(*[(f"id{i}", "a@b", "s", "body " * 20) for i in range(2000)])
        with patch.object(ta.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=out, stderr="")):
            t0 = time.perf_counter()
            msgs = ta.fetch_recent_inbox()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(msgs), 2000)


class TestRetry(unittest.TestCase):
    def test_applescript_failure_fails_open(self):
        # RETRY GAP: fetch_recent_inbox() — one osascript call (300s cap); failure returns [] and the day's digest still posts
        with patch.object(ta.subprocess, "run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="Mail not running")) as r, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ta.fetch_recent_inbox(), [])
        self.assertEqual(r.call_count, 1)
        self.assertIn("AppleScript fetch failed", out.getvalue())

    def test_llm_garbage_defaults_safe(self):
        with patch.object(ta, "call_llm", return_value="I think it is fine"):
            v = ta.score_message({"sender": "a", "subject": "s", "body": "b"})
        self.assertEqual((v["category"], v["confidence"]), ("benign", 0.0))

    def test_article_generation_failure_non_fatal(self):
        with patch.object(nj, "call_openrouter", return_value=""), patch.object(nj, "publish_hugo") as ph:
            ta.publish_vague_local_article(3, 0, 0, 0)
        ph.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_identity_threat_regex(self):
        self.assertTrue(ta.IDENTITY_RE.search("someone said jordan koch should be fired"))
        self.assertIsNone(ta.IDENTITY_RE.search("kochjxyz"))
        self.assertIn("swat", ta.THREAT_TERMS)

    def test_parse_skips_malformed_blocks_and_seen_cap(self):
        out = _mail(("1", "a@b", "Hi", "body")) + "###MSGID###broken###END###"
        with patch.object(ta.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=out, stderr="")):
            self.assertEqual([m["msgid"] for m in ta.fetch_recent_inbox()], ["1"])
        ta._save_seen({str(i) for i in range(900)})
        self.assertEqual(len(ta._load_seen()), 500)

    def test_score_extracts_json(self):
        with patch.object(ta, "call_llm", return_value='sure: {"category":"phishing","confidence":0.8} done'):
            self.assertEqual(ta.score_message({"sender": "a", "subject": "s", "body": "b" * 9000})["category"], "phishing")


class TestIntegration(unittest.TestCase):
    def test_memory_scan_baseline_then_hits(self):
        ta.MEM_STATE_FILE.unlink(missing_ok=True)
        cur = MagicMock(); cur.fetchone.return_value = (datetime(2026, 1, 1),)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(ta.psycopg2, "connect", return_value=conn) as c:
            self.assertEqual(ta.scan_memory_identity_threats(), [])          # baseline only
            self.assertEqual(c.call_args.args[0], ta.MEM_DSN)
            cur.fetchall.return_value = [(1, "reddit", datetime(2026, 1, 2), "we will get Jordan Koch fired"),
                                         (2, "news", datetime(2026, 1, 3), "jordan koch released an app")]
            with redirect_stdout(io.StringIO()):
                hits = ta.scan_memory_identity_threats()
        self.assertEqual([h["id"] for h in hits], [1])
        self.assertEqual(json.loads(ta.MEM_STATE_FILE.read_text())["last_seen"], "2026-01-03T00:00:00")

    def test_infra_summary_enriches_components(self):
        cur = MagicMock(); cur.fetchall.return_value = [("core4", 9000.0, 3000.0)]
        cur.fetchone.return_value = ({"auth_failures": 2},)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(ta.psycopg2, "connect", return_value=conn):
            self.assertEqual(ta.pull_infra_threat_summary(), [("core4", 9000.0, 3000.0, {"auth_failures": 2})])
        self.assertEqual(cur.execute.call_args_list[0].args[1], (24, 7, 2.0))


class TestFunctional(unittest.TestCase):
    def test_golden_path_scores_records_and_posts(self):
        ta.STATE_FILE.write_text(json.dumps(["old"]))
        out = _mail(("old", "x@y", "seen", "b"), ("p1", "bank@evil.example", "Verify now", "click"), ("s1", "rep@co", "Demo?", "hi"))
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(ta.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=out, stderr="")), \
                patch.object(ta.psycopg2, "connect", return_value=conn), patch.object(ta, "call_llm", side_effect=[json.dumps(PHISH), json.dumps(SALES)]), \
                patch.object(ta, "scan_memory_identity_threats", return_value=[]), \
                patch.object(ta, "pull_infra_threat_summary", return_value=[("h", 50.0, 10.0, {"critical_events": 1})]), \
                patch.object(ta, "rollup_link", return_value=None), patch.object(ta, "publish_vague_local_article") as art, \
                patch.object(ta.nova_config, "post_both") as post, redirect_stdout(io.StringIO()):
            ta.main()
        inserts = [c.args[1] for c in cur.execute.call_args_list if "INSERT INTO email_threat_scan" in c.args[0]]
        self.assertEqual([i[0] for i in inserts], ["p1", "s1"])
        self.assertIsNotNone(inserts[0][7]); self.assertIsNone(inserts[1][7])     # evidence only for notable
        digest = post.call_args.args[0]
        self.assertIn("*phishing* (90%) from bank@evil.example", digest)
        self.assertIn(":rotating_light: h: 50 vs. 10 baseline (5.0x)", digest)
        self.assertEqual(post.call_args.kwargs["slack_channel"], ta.nova_config.SLACK_DIGEST)
        art.assert_called_once_with(2, 1, 0, 1)
        self.assertEqual(set(json.loads(ta.STATE_FILE.read_text())), {"old", "p1", "s1"})

    def test_quiet_day_skips_article(self):
        with patch.object(nj, "publish_hugo") as ph, patch.object(nj, "call_openrouter") as co:
            ta.publish_vague_local_article(0, 0, 0, 0)
        ph.assert_not_called(); co.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = _REAL_RUN([sys.executable, "-c", "import nova_daily_threat_assessment"], cwd=str(SCRIPTS),
                      capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
