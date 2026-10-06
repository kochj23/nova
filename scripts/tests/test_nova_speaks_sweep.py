#!/usr/bin/env python3
"""Tests for nova_speaks_sweep.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

JOURNAL is a tempdir, PG is a scripted fake cursor, and every side effect is mocked: sh()/ssh/scp/cp,
the YouTube uploader, post_approval (which reads the Slack token from the Keychain), is_live (HTTP HEAD)
and nova_journal.git_push."""
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
SCRIPT = SCRIPTS / "nova_speaks_sweep.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load("nova_speaks_sweep_t", SCRIPT)
_REAL_POST = ss.post_approval


class Cur:
    def __init__(self, rules=()):
        self.rules, self.sql, self._last = list(rules), [], None

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        self._last = next((ans for needle, ans in self.rules if needle in sql), None)

    def fetchall(self):
        return list(self._last or [])

    def fetchone(self):
        a = self._last
        return (a[0] if a else None) if isinstance(a, list) else a

    def writes(self, prefix):
        return [(s, p) for s, p in self.sql if s.startswith(prefix)]


def _host(name="studio", ssh=None, out_is_nas=True, enabled=True, speed=1.5):
    return {"host": name, "ssh": ssh, "python": "/usr/bin/python3", "scripts_dir": "/s", "tts_home": "/t",
            "journal_dir": "/j", "out_dir": "/out", "out_is_nas": out_is_nas, "env": "", "speed": speed,
            "enabled": enabled}


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.journal = Path(self.td.name) / "nova-journal"
        (self.journal / "content" / "essays").mkdir(parents=True)
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        self.post = MagicMock()
        for p in (patch.object(ss, "JOURNAL", self.journal), patch.object(ss.subprocess, "run", boom),
                  patch.object(ss, "post_approval", self.post), patch.object(ss.psycopg2, "connect", boom),
                  patch("urllib.request.urlopen", boom)):
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def article(self, slug="glass-tides", draft=False, date="2026-10-04T09:00:00-07:00"):
        p = self.journal / "content" / "essays" / f"{slug}.md"
        p.write_text(f'---\ntitle: "📝 Glass Tides"\ndate: {date}\ndraft: {str(draft).lower()}\n---\n\nBody.')
        return p


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-slack-bot-token"', SRC)          # Slack token from Keychain only

    def test_remote_paths_are_shell_quoted(self):
        h = _host(ssh="node1")
        calls = []
        with patch.object(ss, "sh", side_effect=lambda h, cmd, timeout=60: calls.append(cmd) or (1, "")):
            cur = Cur([("status='rendering'", [("x", "node1", 123, "/t/logs/a b;rm -rf ~.log", "T", None, None)])])
            ss.reap(cur, {"node1": h})
        self.assertIn("'/t/logs/a b;rm -rf ~.log'", calls[1])

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'cur\.execute\(\s*f["\']')


class TestPerformance(_Base):
    def test_frontmatter_10k_fast(self):
        md = '---\ntitle: "x"\ndate: 2026-10-04\n---\n' + "body " * 200
        t0 = time.perf_counter()
        for _ in range(10_000):
            ss.frontmatter(md)
            ss._fm(md, "title")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_render_failure_counts_toward_auto_disable(self):
        # RETRY GAP: reap — a dead render is not retried; it is marked failed and the host's fail counter
        # advances (3 in a row disables it). The sweep itself never raises.
        cur = Cur([("status='rendering'", [("slug1", "studio", 99, "/t/logs/slug1.log", "T", None, None)])])
        with patch.object(ss, "sh", side_effect=[(1, ""), (0, "Traceback: OOM")]):
            ss.reap(cur, {"studio": _host()})
        self.assertTrue(any("status='failed'" in s for s, _ in cur.sql))
        self.assertTrue(any("enabled=(fails+1<3)" in s for s, _ in cur.sql))
        self.assertIn("render FAILED", self.post.call_args.args[0])

    def test_upload_failure_returns_none(self):
        with patch.object(ss.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "cookies stale")):
            self.assertIsNone(ss.upload("s"))
        with patch.object(ss.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIsNone(ss.upload("s"))
        self.assertIn("upload failed", self.out.getvalue())

    def test_post_approval_fails_open(self):
        # RETRY GAP: post_approval — one attempt at Keychain + Slack; any failure is logged, never raised
        with patch.object(ss.subprocess, "run", side_effect=OSError("no security binary")):
            _REAL_POST("hi")
        self.assertIn("slack post failed: no security binary", self.out.getvalue())


class TestUnit(_Base):
    def test_frontmatter_and_fm(self):
        self.assertEqual(ss.frontmatter("no fm"), "")
        self.assertEqual(ss._fm('title: "Hello"\n', "title"), "Hello")
        self.assertEqual(ss._fm("x: 1", "title"), "")

    def test_upload_parses_video_id(self):
        with patch.object(ss.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "log\nabcDEF12_-x\n", "")):
            self.assertEqual(ss.upload("s"), "abcDEF12_-x")

    def test_scan_skips_drafts_old_existing_and_not_live(self):
        self.article("a-draft", draft=True)
        self.article("before-rule", date="2026-10-01T09:00:00-07:00")
        self.article("already-queued")
        self.article("not-live")
        self.article("fresh")
        cur = Cur()
        orig = cur.execute

        def execute(sql, params=None):
            orig(sql, params)
            if "::timestamptz >=" in sql:
                cur._last = (params[0] >= params[1],)          # ISO strings compare like the timestamps here
            elif "WHERE slug=%s" in sql:
                cur._last = (1,) if params[0] == "already-queued" else None
        cur.execute = execute
        with patch.object(ss, "is_live", side_effect=lambda url: "not-live" not in url):
            n = ss.scan_new(cur)
        inserted = [p for _, p in cur.writes("INSERT INTO nova_speaks_renders")]
        self.assertEqual([p[0] for p in inserted], ["fresh"])
        self.assertEqual(inserted[0][3], "https://nova.digitalnoise.net/essays/fresh/")
        self.assertEqual(inserted[0][4], "Glass Tides")
        self.assertEqual(n, 1)


class TestIntegration(_Base):
    def test_reap_done_marks_uploads_and_posts(self):
        cur = Cur([("status='rendering'", [("glass-tides", "studio", 7, "/t/logs/g.log", "Glass Tides", None, None)])])
        log_txt = "...\nDONE /out/glass-tides.mp4 (3:12, 1080p)\n"
        with patch.object(ss, "sh", side_effect=[(1, ""), (0, log_txt)]), patch.object(ss, "upload", return_value="abcdefghijk"):
            ss.reap(cur, {"studio": _host()})
        (sql, params), = [w for w in cur.writes("UPDATE nova_speaks_renders")]
        self.assertEqual(params[0], ss.REVIEW + "/glass-tides.mp4")
        self.assertIn("https://youtu.be/abcdefghijk", self.post.call_args.args[0])


class TestFunctional(_Base):
    def test_dispatch_ships_article_and_starts_detached_render(self):
        art = self.article()
        cur = Cur([("status='queued'", [("glass-tides", "essays", str(art), "https://x/essays/glass-tides/")])])
        sh_calls = []

        def fake_sh(h, cmd, timeout=60):
            sh_calls.append(cmd)
            return (1, "") if cmd.startswith("pgrep") else (0, "4242\n")
        with patch.object(ss, "sh", fake_sh), patch.object(ss, "copy_to") as cp:
            ss.dispatch(cur, {"studio": _host(), "off": _host("off", enabled=False)})
        self.assertIn("nohup bash -c", sh_calls[-1])
        self.assertIn("--article /j/content/essays/glass-tides.md", sh_calls[-1])
        (sql, params), = [w for w in cur.writes("UPDATE nova_speaks_renders")]
        self.assertEqual(params[:2], ("studio", 4242))
        self.assertEqual(cp.call_args_list[0].args[2], "/j/content/essays/glass-tides.md")

    def test_podcast_index_writes_once_and_pushes(self):
        art = self.article()
        cur = Cur([("youtube_id ~", [("glass-tides", "essays", str(art), "u", "abcdefghijk")])])
        nj = types.ModuleType("nova_journal")
        nj.git_push = MagicMock()
        with patch.dict(sys.modules, {"nova_journal": nj}):
            self.assertEqual(ss.podcast_index(cur), 1)
            self.assertEqual(ss.podcast_index(cur), 0)        # unchanged -> no rewrite, no push
        data = json.loads((self.journal / "data" / "nova_speaks.json").read_text())
        self.assertEqual((data[0]["title"], data[0]["section"]), ("Glass Tides", "Essays"))
        self.assertEqual(nj.git_push.call_count, 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # any invocation connects to PG / ssh's the fleet, so the smoke is an import in a child process
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('s', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); print(m.SITE)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "https://nova.digitalnoise.net")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
