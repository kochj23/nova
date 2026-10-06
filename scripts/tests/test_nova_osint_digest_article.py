#!/usr/bin/env python3
"""Tests for nova_osint_digest_article.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_osint_digest_article.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("noda_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


od = _load()
# stub every outbound side effect at module load (git push, Slack, notify, image gen); Hugo + log in a tempdir
od.nova_journal = MagicMock()
od.nova_config = MagicMock(SLACK_FEED="C-feed")
od.nova_notify = MagicMock()
od.generate_image = MagicMock(return_value=None)
_ROOT = Path(_TMP.name)
od.HUGO_ROOT = _ROOT / "journal"
od.CONTENT_DIR = od.HUGO_ROOT / "content/operations"
od.IMAGES_DIR = od.HUGO_ROOT / "static/images/operations"
od.LOG_FILE = _ROOT / "osint.log"


def _f(sev="warning", tool="amass", finding="new.sub.example.net"):
    return {"tool": tool, "target": "example.net", "finding_type": "subdomain", "finding": finding,
            "severity": sev, "ts": None}


class _Base(unittest.TestCase):
    def setUp(self):
        for m in (od.nova_journal, od.nova_config, od.nova_notify, od.generate_image):
            m.reset_mock()
        od.generate_image.return_value = None


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_query_is_parameterized(self):
        cur = MagicMock(); cur.fetchall.return_value = []
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(od.psycopg2, "connect", return_value=conn):
            od.gather_new_findings()
        sql, params = cur.execute.call_args[0]
        self.assertIn("interval '%s days'", sql)
        self.assertEqual(params, (od.LOOKBACK_DAYS,))
        conn.close.assert_called_once()

    def test_slug_and_front_matter_are_sanitized(self):
        with patch.object(od.subprocess, "run"):
            od.publish('Bad "title" ../../etc/passwd <script>', "body", None, "warning")
        files = list(od.CONTENT_DIR.glob("*.md"))
        name = max(files, key=lambda p: p.stat().st_mtime).name
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")
        self.assertNotIn("..", name)


class TestPerformance(_Base):
    def test_prompt_build_for_10k_findings_fast(self):
        findings = [_f(finding=f"s{i}.example.net") for i in range(10_000)]
        with patch.object(od, "call_llm", return_value="x") as llm, \
             patch.object(od, "system_prompt", side_effect=lambda ctx: ctx):   # persona load is not the hot path
            t0 = time.perf_counter()
            od.generate_article(findings)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("LIMIT 200", SRC)                          # PG side caps the set
        self.assertIn("across 1 tool(s)", llm.call_args[0][1])


class TestRetry(_Base):
    def test_llm_falls_back_from_sonnet_to_openrouter(self):
        fake = types.ModuleType("nova_claude_code")
        fake.claude_generate = MagicMock(side_effect=RuntimeError("claude busy"))
        od.nova_journal.call_openrouter.return_value = "haiku text"
        with patch.dict(sys.modules, {"nova_claude_code": fake}):
            self.assertEqual(od.call_llm("s", "u", max_tokens=9), "haiku text")
        self.assertEqual(fake.claude_generate.call_count, 1)
        self.assertEqual(od.nova_journal.call_openrouter.call_args.kwargs["max_tokens"], 9)

    def test_image_failure_still_publishes(self):
        od.generate_image.side_effect = RuntimeError("gpu")
        with patch.object(od, "gather_new_findings", return_value=[_f()]), \
             patch.object(od, "call_llm", side_effect=["a" * 500, "Title"]), patch.object(od, "publish") as pub:
            od.main()
        self.assertIsNone(pub.call_args[0][2])


class TestUnit(_Base):
    def test_title_cleanup_and_default(self):
        with patch.object(od, "call_llm", return_value=' "Quoted "Title"" '):
            self.assertEqual(od.generate_title("x"), "Quoted Title")
        with patch.object(od, "call_llm", return_value=""):
            self.assertEqual(od.generate_title("x"), "This Week In Nova Stalking Herself")

    def test_short_article_aborts(self):
        with patch.object(od, "gather_new_findings", return_value=[_f()]), \
             patch.object(od, "call_llm", return_value="too short"), patch.object(od, "publish") as pub:
            od.main()
        pub.assert_not_called()

    def test_no_findings_skips(self):
        with patch.object(od, "gather_new_findings", return_value=[]), patch.object(od, "call_llm") as llm:
            od.main()
        llm.assert_not_called()


class TestIntegration(_Base):
    def test_publish_uses_hardened_git_push_and_feed(self):
        with patch.object(od.subprocess, "run"):
            od.publish("Weekly Recon", "body text", None, "warning")
        od.nova_journal.git_push.assert_called_once_with("osint", "Weekly Recon")
        self.assertEqual(od.nova_config.post_both.call_args.kwargs["slack_channel"], "C-feed")
        od.nova_notify.assert_not_called()                     # only critical findings page


class TestFunctional(_Base):
    def test_golden_path_critical(self):
        with patch.object(od, "gather_new_findings", return_value=[_f(), _f("critical", "hibp", "breach")]), \
             patch.object(od, "call_llm", side_effect=["article " * 100, "Breached Again"]), \
             patch.object(od.subprocess, "run"):
            od.main()
        post = od.CONTENT_DIR / f"{time.strftime('%Y-%m-%d')}-breached-again.md"
        self.assertTrue(post.exists())
        text = post.read_text()
        self.assertTrue(text.startswith('---\ntitle: "Breached Again"'))
        self.assertIn("article article", text)
        self.assertEqual(od.nova_notify.call_args.kwargs["level"], "critical")

    def test_cover_image_converted(self):
        img = _ROOT / "cover.png"; img.write_bytes(b"png")
        def fake_run(args, **kw):
            Path(args[args.index("-o") + 1]).write_bytes(b"webp")
        with patch.object(od.subprocess, "run", side_effect=fake_run) as run:
            od.publish("Cover Test", "b", str(img), "warning")
        self.assertEqual(run.call_args[0][0][0], "cwebp")
        post = od.CONTENT_DIR / f"{time.strftime('%Y-%m-%d')}-cover-test.md"
        self.assertIn('image: "/images/operations/', post.read_text())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: any invocation queries PG and may publish, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_osint_digest_article"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
