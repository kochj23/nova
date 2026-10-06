#!/usr/bin/env python3
"""Tests for nova_security_weekly.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import atexit
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
import urllib.error
import urllib.request          # imported BEFORE the patch.dict load so the module and the tests share one urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_security_weekly.py"
SRC = SCRIPT.read_text()

_TMP = tempfile.TemporaryDirectory()            # a fake $HOME so hugo/log paths never touch ~/nova-journal or ~/.openclaw/logs
atexit.register(_TMP.cleanup)
HOME = Path(_TMP.name)


def _stub_modules():
    cfg = types.ModuleType("nova_config"); cfg.openrouter_api_key = MagicMock(return_value="sk-or-test")
    img = types.ModuleType("nova_image_utils"); img.generate_image = MagicMock(return_value=None)
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    return {"nova_config": cfg, "nova_image_utils": img, "nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch("pathlib.Path.home", return_value=HOME):
        spec.loader.exec_module(mod)
    return mod


sw = _load("sw", SCRIPT)
LONG = "# WEEK IN INTELLIGENCE — 29 Sep-05 Oct 2026\n\n## BLUF\n" + ("Threat actors kept busy. " * 40)


class _Runner:
    """subprocess.run stand-in: psql -> memory rows, git push -> scripted results, everything else rc 0."""
    def __init__(self, memories="", psql_rc=0, push=(0,)):
        self.memories = memories; self.psql_rc = psql_rc; self.push = list(push); self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "psql":
            return subprocess.CompletedProcess(argv, self.psql_rc, stdout=self.memories, stderr="")
        if argv[:2] == ["git", "push"]:
            rc = self.push.pop(0) if self.push else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="rejected" if rc else "")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def named(self, *prefix):
        return [a for a in self.calls if a[:len(prefix)] == list(prefix)]


def _llm(*answers):
    answers = list(answers)

    def urlopen(req, timeout=None):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        urlopen.requests.append(req)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": a}}]}).encode())
    urlopen.requests = []
    return urlopen


def _reset():
    for f in sw.CONTENT_DIR.glob("*.md"):
        f.unlink()
    sw.generate_image.reset_mock(); sw.generate_image.side_effect = None; sw.generate_image.return_value = None
    sw.notify.reset_mock()


def _run(memories="- intel line", llm=None, runner=None):
    _reset()
    runner = runner or _Runner(memories=memories)
    llm = llm or _llm(LONG)
    out = io.StringIO()
    with patch("subprocess.run", runner), patch("urllib.request.urlopen", llm), redirect_stdout(out):
        sw.run()
    return runner, llm, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_comes_from_nova_config(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("nova_config.openrouter_api_key()", SRC)
        self.assertNotIn("shell=True", SRC)
        runner, llm, _ = _run()
        self.assertEqual(llm.requests[0].get_header("Authorization"), "Bearer sk-or-test")

    def test_memory_query_is_a_fixed_literal(self):
        runner, _, _ = _run()
        psql = runner.named("psql")[0]
        self.assertIn("WHERE source IN ('intelligence', 'military_history', 'law')", psql[-1])
        self.assertNotIn("%s", psql[-1]); self.assertNotIn("{", psql[-1])         # nothing interpolated

    def test_hostile_title_cannot_escape_the_content_dir(self):
        title = '../../etc/passwd "; rm -rf / #' + "x" * 100
        _run(llm=_llm("# " + title + "\n\n" + "body " * 200))
        files = list(sw.CONTENT_DIR.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{4}-\d\d-\d\d-[a-z0-9-]{1,60}\.md$")
        self.assertNotIn('"', files[0].read_text().split("\n")[1].replace('title: "', "").split("📊 ")[1].rstrip('"'))


class TestPerformance(unittest.TestCase):
    def test_memory_formatting_on_10k_rows(self):
        rows = "\n".join(f"row {i} " + "x" * 400 for i in range(10_000))
        t0 = time.perf_counter()
        with patch("subprocess.run", _Runner(memories=rows)):
            text = sw.get_week_memories()
        self.assertLess(time.perf_counter() - t0, 1.0)
        lines = text.split("\n")
        self.assertEqual(len(lines), 10_000)
        self.assertTrue(all(l.startswith("- ") and len(l) <= 252 for l in lines))


class TestRetry(unittest.TestCase):
    def test_git_push_rebases_and_retries_once(self):
        runner, _, out = _run(runner=_Runner(memories="- m", push=(1, 0)))
        self.assertEqual(len(runner.named("git", "push")), 2)
        self.assertEqual(len(runner.named("git", "pull", "--rebase")), 1)
        self.assertNotIn("ERROR: git push still failing", out)
        self.assertIn("rebasing on origin and retrying", out)

    def test_git_push_failing_twice_is_logged_as_an_error(self):
        runner, _, out = _run(runner=_Runner(memories="- m", push=(1, 1)))
        self.assertEqual(len(runner.named("git", "push")), 2)
        self.assertIn("ERROR: git push still failing after rebase", out)
        sw.notify.assert_called_once()           # the notify still fires: the error is logged, not raised

    def test_llm_failure_is_one_shot_and_escapes(self):
        # RETRY GAP: call_llm — a single urlopen; a URLError propagates out of run() and nothing is published
        _reset()
        with patch("subprocess.run", _Runner(memories="- m")), patch("urllib.request.urlopen", _llm(urllib.error.URLError("down"), LONG)), \
             redirect_stdout(io.StringIO()), self.assertRaises(urllib.error.URLError):
            sw.run()
        self.assertEqual(list(sw.CONTENT_DIR.glob("*.md")), [])
        sw.notify.assert_not_called()

    def test_memory_read_fails_open_to_empty(self):
        # RETRY GAP: get_week_memories — one psql attempt; non-zero exit yields ""
        with patch("subprocess.run", _Runner(memories="- m", psql_rc=1)):
            self.assertEqual(sw.get_week_memories(), "")

    def test_image_failure_still_publishes_without_a_cover(self):
        # RETRY GAP: generate_image — wrapped in try/except, img_path falls to None
        _reset(); sw.generate_image.side_effect = RuntimeError("swarm down")
        runner = _Runner(memories="- m")
        with patch("subprocess.run", runner), patch("urllib.request.urlopen", _llm(LONG)), redirect_stdout(io.StringIO()):
            sw.run()
        text = next(sw.CONTENT_DIR.glob("*.md")).read_text()
        self.assertNotIn("cover:", text)
        self.assertEqual(runner.named("cwebp"), [])


class TestUnit(unittest.TestCase):
    def test_log_writes_to_the_redirected_file(self):
        self.assertTrue(str(sw.LOG_FILE).startswith(str(HOME)))
        with redirect_stdout(io.StringIO()) as out:
            sw.log("hello")
        self.assertIn("[sec-weekly ", out.getvalue()); self.assertIn("] hello", out.getvalue())
        self.assertIn("hello", sw.LOG_FILE.read_text())

    def test_week_articles_strip_front_matter_and_cap(self):
        _reset()
        today = datetime.now().strftime("%Y-%m-%d")
        for i in range(9):
            (sw.CONTENT_DIR / f"{today}-a{i}.md").write_text(f"---\ntitle: t{i}\n---\n" + f"BODY{i} " * 600)
        (sw.CONTENT_DIR / "2001-01-01-old.md").write_text("---\nx\n---\nancient")
        text = sw.get_week_articles()
        parts = text.split("\n\n---\n\n")
        self.assertEqual(len(parts), 7)
        self.assertTrue(all(len(p) <= 2000 and p.startswith("BODY") for p in parts))
        self.assertNotIn("ancient", text)
        _reset()

    def test_memory_rows_are_prefixed_and_truncated(self):
        with patch("subprocess.run", _Runner(memories="a\n\n  " + "b" * 300 + "  \n")):
            lines = sw.get_week_memories().split("\n")
        self.assertEqual(lines[0], "- a")
        self.assertEqual(len(lines), 2); self.assertEqual(len(lines[1]), 252)

    def test_run_with_no_content_skips_the_llm(self):
        llm = _llm(LONG)
        _, _, out = _run(memories="", llm=llm)
        self.assertEqual(llm.requests, [])
        self.assertIn("No content for weekly rollup", out)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported_not_reimplemented(self):
        self.assertIn("from nova_image_utils import generate_image", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("def generate_image", SRC); self.assertNotIn("def notify(", SRC)
        self.assertEqual(sw.MODEL, "anthropic/claude-haiku-4.5")

    def test_memory_source_and_window(self):
        runner, _, _ = _run()
        psql = runner.named("psql")[0]
        self.assertEqual(psql[1:7], ["-h", "192.168.1.6", "-U", "kochj", "-d", "nova_memories"])
        self.assertIn("interval '7 days'", psql[-1])

    def test_cover_url_matches_the_images_dir(self):
        self.assertTrue(str(sw.IMAGES_DIR).endswith("static/images/security"))
        self.assertIn('hugo_image = f"/images/security/', SRC)

    def test_notify_carries_the_journal_dedup_key(self):
        _run()
        kw = sw.notify.call_args.kwargs
        self.assertEqual((kw["category"], kw["dedup_key"], kw["level"]), ("journal", "security-weekly-rollup", "info"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_with_cover_commits_and_notifies(self):
        _reset()
        png = HOME / "cover.png"; png.write_bytes(b"\x89PNG")
        sw.generate_image.return_value = str(png)
        runner = _Runner(memories="- intel")
        with patch("subprocess.run", runner), patch("urllib.request.urlopen", _llm(LONG)), redirect_stdout(io.StringIO()) as out:
            sw.run()
        f = next(sw.CONTENT_DIR.glob("*.md")); text = f.read_text()
        self.assertTrue(f.name.endswith("-week-in-intelligence-29-sep-05-oct-2026.md"))
        self.assertIn('title: "📊 WEEK IN INTELLIGENCE — 29 Sep-05 Oct 2026"', text)
        self.assertIn('categories: ["operations"]', text)
        self.assertIn("cover:\n  image: \"/images/security/", text)
        self.assertIn("## BLUF", text)
        cwebp = runner.named("cwebp")[0]
        self.assertEqual(cwebp[-3], str(png)); self.assertTrue(cwebp[-1].endswith(".webp"))
        self.assertEqual(sw.generate_image.call_args.kwargs["section"], "security")
        self.assertEqual([a[:2] for a in runner.named("git")], [["git", "add"], ["git", "commit"], ["git", "push"]])
        sw.notify.assert_called_once()
        self.assertEqual(sw.notify.call_args.args[0], "Nova Security — Week in Intelligence")
        self.assertIn("Published: security/", out.getvalue())

    def test_short_llm_output_publishes_nothing(self):
        runner, _, out = _run(llm=_llm("# too short"))
        self.assertIn("Weekly generation failed", out)
        self.assertEqual(list(sw.CONTENT_DIR.glob("*.md")), [])
        self.assertEqual(runner.named("git"), [])
        sw.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_run_is_guarded(self):
        # no --help and running the script reaches psql + OpenRouter, so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":\n    run()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_security_weekly"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
