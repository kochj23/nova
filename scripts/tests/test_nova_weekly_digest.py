#!/usr/bin/env python3
"""Tests for nova_weekly_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module reads the Keychain (JORDAN_CC) and the service registry (resolve_url) AT IMPORT, so it is
loaded with both patched. Slack, git, psql, the herd mail script and the LLMs are stubbed."""
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import nova_config as _real_cfg  # noqa: E402
import nova_resolve  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    no_keychain = MagicMock(return_value=types.SimpleNamespace(returncode=44, stdout=""))
    with patch("subprocess.run", no_keychain), \
            patch.object(nova_resolve, "resolve_url", return_value="http://plex.invalid:32400"):
        spec.loader.exec_module(mod)
    return mod


wd = _load("nova_weekly_digest_t", SCRIPTS / "nova_weekly_digest.py")
_TMP = Path(tempfile.mkdtemp())
for attr, rel in (("LOG_FILE", "digest.log"), ("STATE_FILE", "state.json"), ("DREAMS_DIR", "dreams"),
                  ("HUGO_ROOT", "hugo"), ("SCHEDULER_STATE", "sched.json")):
    setattr(wd, attr, _TMP / rel)
wd.nova_config = types.SimpleNamespace(post_both=MagicMock(), is_private_source=_real_cfg.is_private_source)
wd.nj = types.SimpleNamespace(git_push=MagicMock())
wd.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")),
                                      TimeoutExpired=subprocess.TimeoutExpired)
FAKE_VOICE = types.SimpleNamespace(system_prompt=lambda ctx: "VOICE\n" + ctx, CONTEXT_JOURNAL_DIGEST="CTX\n")
SRC = (SCRIPTS / "nova_weekly_digest.py").read_text()
PEER = "friend" + "@" + "example.org"
NOVA_ADDR = "nova" + "@" + "digitalnoise.net"     # the one SAFE_EMAILS entry, built at runtime


def _q():
    return redirect_stdout(io.StringIO())


def _cp(rc=0, out=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")


class _Resp:
    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return json.dumps(self.obj).encode()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-openrouter-api-key"', SRC)

    def test_herd_email_and_plex_are_hard_disabled(self):
        wd.subprocess.run.reset_mock()
        with _q() as out:
            wd.send_to_herd("digest", "2026-01-01")
            self.assertEqual(wd.gather_plex_history(), [])
        wd.subprocess.run.assert_not_called()
        self.assertIn("DISABLED", out.getvalue())

    def test_published_text_scrubbed(self):
        txt = f"mail {PEER} or {NOVA_ADDR}; - **work_internal** 40 new\nsee cloud governance notes"
        out = wd.scrub_private_sources(wd.scrub_emails(txt))
        self.assertNotIn(PEER, out)
        self.assertIn(NOVA_ADDR, out)
        self.assertNotIn("work_internal", out)
        self.assertIn("[memory source]", out)

    def test_psql_date_is_always_a_formatted_date(self):
        start, end = wd.get_week_range()
        self.assertRegex(start, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(end, r"^\d{4}-\d{2}-\d{2}$")


class TestPerformance(unittest.TestCase):
    def test_scrub_10k_lines(self):
        text = "\n".join(f"line {i} {PEER} work memo stuff" for i in range(10_000))
        t0 = time.perf_counter()
        out = wd.scrub_private_sources(wd.scrub_emails(text))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertNotIn(PEER, out)


class TestRetry(unittest.TestCase):
    def test_editorial_falls_back_through_ollama_models(self):
        calls = []

        def uo(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"response": "E" * 200})
        with patch.dict(sys.modules, {"nova_voice": FAKE_VOICE}), \
                patch.object(wd, "get_openrouter_key", return_value="k"), \
                patch.object(wd.urllib.request, "urlopen", side_effect=uo), _q():
            self.assertEqual(wd.generate_editorial("body"), "E" * 200)
        self.assertEqual(calls, [wd.OPENROUTER_URL, wd.OLLAMA_URL, wd.OLLAMA_URL])

    def test_image_generation_retries_with_backoff(self):
        img = _TMP / "img.png"
        img.write_bytes(b"png")
        seq = [_cp(1), _cp(1), _cp(0, f"Workspace copy: {img}\n")]

        def run(cmd, **k):
            if cmd[0] == "cwebp":
                raise FileNotFoundError
            return seq.pop(0)
        with patch.object(wd, "ensure_backend", return_value=True), \
                patch.object(wd.subprocess, "run", side_effect=run), patch.object(wd.time, "sleep") as sl, _q():
            self.assertEqual(wd._generate_digest_image("ed", "2026-01-02"), "/images/digests/2026-01-02.webp")
        self.assertEqual(sl.call_count, 2)
        sl.assert_called_with(15)

    def test_psql_failures_fail_open(self):
        with patch.object(wd.subprocess, "run", side_effect=OSError("no psql")), _q():
            h = wd.gather_system_health()
            self.assertEqual(wd.gather_memory_sources(), [])
            self.assertEqual(wd.gather_herd_activity(), "Could not check herd mail")
        self.assertEqual((h["total_memories"], h["memory_growth"]), (0, 0))


class TestUnit(unittest.TestCase):
    def test_format_plex_summary(self):
        self.assertEqual(wd.format_plex_summary([]), "No viewing activity recorded this week.")
        items = [{"type": "movie", "title": f"M{i}", "year": 2000} for i in range(7)] + \
                [{"type": "episode", "grandparentTitle": "Show"}] * 2
        s = wd.format_plex_summary(items)
        self.assertIn("...and 2 more", s)
        self.assertIn("Show: 2 episodes", s)

    def test_gather_dreams_in_window(self):
        wd.DREAMS_DIR.mkdir(exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        (wd.DREAMS_DIR / f"{today}.md").write_text('Theme: "Glass Ocean"\nMood: wistful')
        (wd.DREAMS_DIR / f"{old}.md").write_text('Theme: "Old"')
        (wd.DREAMS_DIR / "notes.md").write_text("x")
        self.assertEqual(wd.gather_dreams(), [{"date": today, "theme": "Glass Ocean", "mood": "wistful"}])

    def test_scheduler_failures_parsed(self):
        wd.SCHEDULER_STATE.write_text(json.dumps({"tasks": {"a": {"consecutive_failures": 2, "last_exit_code": 1},
                                                             "b": {"consecutive_failures": 0}}}))
        with patch.object(wd.subprocess, "run", return_value=_cp(0, "123\n")), _q():
            h = wd.gather_system_health()
        self.assertEqual(h["failures"], [{"task": "a", "consecutive": 2, "last_exit": 1}])
        self.assertEqual(h["total_memories"], 123)


class TestIntegration(unittest.TestCase):
    def test_memory_sources_drop_private_shelves(self):
        out = "wikipedia|40\nimessage|9\nemail_archive|3\n"
        with patch.object(wd.subprocess, "run", return_value=_cp(0, out)):
            src = wd.gather_memory_sources()
        self.assertIn({"source": "wikipedia", "count": 40}, src)
        for s in src:
            self.assertFalse(_real_cfg.is_private_source(s["source"]))

    def test_body_sections_compose(self):
        data = {"dreams": [], "essays": [{"title": "T", "subject": "s", "date": "d"}], "opinions": [],
                "plex_items": [], "health": {"total_memories": 1000, "memory_growth": 5, "failures": []},
                "herd_activity": "quiet", "memory_sources": []}
        body = wd.format_digest_body(data)
        for h in ("## Dreams This Week", "## Essays This Week", "- **T** — subject: s (d)",
                  "**Total memories:** 1,000", "All scheduled tasks healthy"):
            self.assertIn(h, body)


class TestFunctional(unittest.TestCase):
    def test_main_publishes_scrubbed_digest_and_updates_state(self):
        wd.nova_config.post_both.reset_mock()
        wd.nj.git_push.reset_mock()
        data = {"dreams": [], "essays": [], "opinions": [], "plex_items": [],
                "health": {"total_memories": 1, "memory_growth": 2, "failures": []},
                "herd_activity": "x", "memory_sources": []}
        with patch.object(wd, "compile_digest_data", return_value=data), \
                patch.object(wd, "generate_editorial", return_value=f"Dear herd, write {PEER}. " * 10), \
                patch.object(wd, "_generate_digest_image", return_value=None), _q():
            wd.main()
        post = next((wd.HUGO_ROOT / "content/operations").glob("*-daily-digest.md")).read_text()
        self.assertNotIn(PEER, post)
        self.assertEqual(wd.nova_config.post_both.call_count, 2)        # digest + image-failure warning
        wd.nj.git_push.assert_called_once_with("digest", "daily digest")
        self.assertEqual(json.loads(wd.STATE_FILE.read_text())["last_digest"]["memory_growth"], 2)

    def test_editorial_failure_uses_placeholder(self):
        data = {"dreams": [], "essays": [], "opinions": [], "plex_items": [],
                "health": {"total_memories": 0, "memory_growth": 0, "failures": []},
                "herd_activity": "x", "memory_sources": []}
        with patch.object(wd, "compile_digest_data", return_value=data), \
                patch.object(wd, "generate_editorial", return_value=None), \
                patch.object(wd, "_generate_digest_image", return_value="/images/digests/x.webp"), _q():
            wd.main()
        self.assertIn("could not be generated", wd.nova_config.post_both.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import subprocess, types\n"
                "subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout='')\n"
                "import nova_resolve\n"
                "nova_resolve.resolve_url = lambda *a, **k: 'http://plex.invalid'\n"
                "import nova_weekly_digest\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
