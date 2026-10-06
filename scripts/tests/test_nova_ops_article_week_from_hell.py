#!/usr/bin/env python3
"""Tests for nova_ops_article_week_from_hell.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This is a one-off column generator: the whole pipeline (call_llm -> generate_title -> generate_image -> publish)
runs at module load. Every load in this file goes through _load(), which stubs the three pipeline modules and
points Path.home() at a tempdir so the real LLM, image generator, publisher and ~/.openclaw/logs are never touched."""
import importlib.util
import io
import os
import re
import shutil
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_article_week_from_hell.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="week-from-hell-test-"))


def _stubs(body="BODY " * 50, title="A Title", img="/tmp/x.png", llm_exc=None, publish_exc=None):
    ops = types.ModuleType("nova_rando_daily_ops")
    ops.call_llm = MagicMock(return_value=body, side_effect=llm_exc)
    ops.generate_title = MagicMock(return_value=title)
    ops.publish = MagicMock(side_effect=publish_exc)
    img_mod = types.ModuleType("nova_image_utils")
    img_mod.generate_image = MagicMock(return_value=img)
    voice = types.ModuleType("nova_voice")
    voice.CONTEXT_JOURNAL_OPS = "OPS-CONTEXT\n"
    voice.system_prompt = MagicMock(side_effect=lambda ctx: "SYSTEM:" + ctx)
    return {"nova_rando_daily_ops": ops, "nova_image_utils": img_mod, "nova_voice": voice}


def _load(name="week_from_hell_under_test", **kw):
    """Exec the one-off with its pipeline stubbed; returns (module, stubs, home, stdout)."""
    home = Path(tempfile.mkdtemp(prefix="wfh-home-", dir=TMP))
    (home / ".openclaw/logs").mkdir(parents=True)
    stubs = _stubs(**kw)
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    with patch.dict(sys.modules, stubs), patch.object(Path, "home", classmethod(lambda cls: home)), redirect_stdout(buf):
        spec.loader.exec_module(mod)
    return mod, stubs, home, buf.getvalue()


M, STUBS, HOME, OUT = _load()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_direct_network_db_or_shell_in_the_one_off(self):
        # Everything outbound goes through the shared pipeline modules — nothing in here talks to the world itself.
        for banned in ("urllib", "requests", "psycopg2", "subprocess", "socket", "os.system"):
            self.assertNotIn(banned, SRC.replace("os.path.expanduser", ""), banned)

    def test_material_contains_no_credentials_only_private_lan_addresses(self):
        self.assertIsNone(re.search(r"xoxb-|sk-[A-Za-z0-9]{20,}|BEGIN (RSA|OPENSSH) PRIVATE KEY", M.MATERIAL))
        for ip in re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", M.MATERIAL):
            self.assertTrue(ip.startswith("192.168."), ip)

    def test_log_write_stayed_in_the_tempdir(self):
        self.assertEqual(M.LOG, HOME / ".openclaw/logs/ops_article_week_from_hell.log")
        self.assertTrue(M.LOG.exists())
        self.assertTrue(str(M.LOG).startswith(str(TMP)))


class TestPerformance(unittest.TestCase):
    def test_log_helper_fast_on_10k_lines(self):
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            for i in range(10_000):
                M.log(f"line {i}")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertGreaterEqual(M.LOG.read_text().count("\n"), 10_000)

    def test_material_is_bounded(self):
        self.assertLess(len(M.MATERIAL), 20_000)        # a single prompt, not a corpus


class TestRetry(unittest.TestCase):
    def test_llm_failure_aborts_before_image_or_publish(self):
        # RETRY GAP: call_llm (via nova_rando_daily_ops) — the one-off makes one call; an exception escapes
        # and nothing downstream (image, publish) runs, so a half-article is never published.
        with self.assertRaises(RuntimeError):
            _load("wfh_llm_fail", llm_exc=RuntimeError("ollama down"))
        # inspect the stubs that _load built: generate_image / publish never ran
        stubs = _stubs(llm_exc=RuntimeError("x"))
        spec = importlib.util.spec_from_file_location("wfh_llm_fail2", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs), patch.object(Path, "home", classmethod(lambda cls: HOME)), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                spec.loader.exec_module(mod)
        stubs["nova_image_utils"].generate_image.assert_not_called()
        stubs["nova_rando_daily_ops"].publish.assert_not_called()

    def test_publish_failure_is_not_retried_and_leaves_no_done_marker(self):
        # RETRY GAP: publish — one attempt; the "ARTICLE DONE" line is only logged on success
        stubs = _stubs(publish_exc=OSError("wp down"))
        home = Path(tempfile.mkdtemp(prefix="wfh-home-", dir=TMP)); (home / ".openclaw/logs").mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("wfh_pub_fail", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs), patch.object(Path, "home", classmethod(lambda cls: home)), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                spec.loader.exec_module(mod)
        self.assertEqual(stubs["nova_rando_daily_ops"].publish.call_count, 1)
        self.assertNotIn("ARTICLE DONE", (home / ".openclaw/logs/ops_article_week_from_hell.log").read_text())


class TestUnit(unittest.TestCase):
    def test_log_appends_timestamped_line_and_echoes(self):
        before = M.LOG.read_text()
        with redirect_stdout(io.StringIO()) as out:
            M.log("hello unit")
        self.assertEqual(out.getvalue(), "hello unit\n")
        tail = M.LOG.read_text()[len(before):]
        self.assertRegex(tail, r"^\[\d\d:\d\d:\d\d\] hello unit\n$")

    def test_material_covers_the_weeks_incidents(self):
        for needle in ("192.168.1.149", "nova-core5", "Hue Bridge", "nova-core4", "verbose=5", "USW Pro 48"):
            self.assertIn(needle, M.MATERIAL)

    def test_image_prompt_is_a_single_descriptive_string(self):
        self.assertIsInstance(M.img_prompt, str)
        self.assertIn("server rack", M.img_prompt)


class TestIntegration(unittest.TestCase):
    def test_reuses_the_shared_pipeline_not_a_private_copy(self):
        self.assertIn("from nova_rando_daily_ops import call_llm, generate_title, publish", SRC)
        self.assertIn("from nova_image_utils import generate_image", SRC)
        self.assertIn("from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS", SRC)
        self.assertNotIn("def call_llm", SRC); self.assertNotIn("def publish", SRC)

    def test_system_prompt_is_built_on_the_ops_journal_context(self):
        STUBS["nova_voice"].system_prompt.assert_called_once()
        ctx = STUBS["nova_voice"].system_prompt.call_args[0][0]
        self.assertTrue(ctx.startswith("OPS-CONTEXT\n"))
        self.assertIn("SPECIAL ONE-OFF RETROSPECTIVE", ctx)
        self.assertEqual(M.system, "SYSTEM:" + ctx)

    def test_title_is_derived_from_the_generated_body(self):
        STUBS["nova_rando_daily_ops"].generate_title.assert_called_once_with(M.body)
        self.assertEqual(M.title, "A Title")


class TestFunctional(unittest.TestCase):
    def test_golden_path_calls_llm_then_image_then_publish(self):
        ops, img = STUBS["nova_rando_daily_ops"], STUBS["nova_image_utils"]
        ops.call_llm.assert_called_once()
        system, user = ops.call_llm.call_args[0]
        self.assertEqual(ops.call_llm.call_args[1], {"max_tokens": 16000})
        self.assertTrue(user.endswith(M.MATERIAL))
        img.generate_image.assert_called_once_with(M.img_prompt, width=1024, height=768, section="operations")
        ops.publish.assert_called_once_with("A Title", M.body, Path("/tmp/x.png"))
        log = M.LOG.read_text()
        self.assertIn("PUBLISHED: A Title | image=yes", log)
        self.assertIn("ARTICLE DONE", log)
        self.assertTrue(OUT.startswith("generating article body (Nova's voice)..."))
        self.assertTrue(OUT.rstrip().endswith("ARTICLE DONE"))

    def test_missing_image_publishes_without_one(self):
        mod, stubs, home, out = _load("wfh_noimg", img=None)
        stubs["nova_rando_daily_ops"].publish.assert_called_once_with("A Title", mod.body, None)
        self.assertIn("image=NO", (home / ".openclaw/logs/ops_article_week_from_hell.log").read_text())


class TestFrame(unittest.TestCase):
    def test_one_off_runs_to_completion_against_stubbed_pipeline(self):
        # No __main__ guard by design (one-off); run a copy from a sandbox dir with stub modules on PYTHONPATH and
        # HOME in a tempdir so the real ~/.openclaw/scripts pipeline is never importable.
        self.assertNotIn('if __name__ == "__main__"', SRC)
        box = Path(tempfile.mkdtemp(prefix="wfh-frame-", dir=TMP))
        (box / "home/.openclaw/logs").mkdir(parents=True)
        shutil.copy(SCRIPT, box / "one_off.py")
        stubs = box / "stubs"; stubs.mkdir()
        (stubs / "nova_rando_daily_ops.py").write_text(
            "def call_llm(s, u, max_tokens=0): return 'body'\n"
            "def generate_title(b): return 'T'\n"
            "def publish(t, b, img, pub_date=None): print('PUBLISH-STUB', t, img)\n")
        (stubs / "nova_image_utils.py").write_text("def generate_image(p, **k): return None\n")
        (stubs / "nova_voice.py").write_text("CONTEXT_JOURNAL_OPS='ctx'\ndef system_prompt(c): return c\n")
        env = {**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(box / "home"), "PYTHONPATH": str(stubs)}
        r = subprocess.run([sys.executable, str(box / "one_off.py")], cwd=str(box), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PUBLISH-STUB T None", r.stdout)
        self.assertIn("ARTICLE DONE", r.stdout)
        self.assertTrue((box / "home/.openclaw/logs/ops_article_week_from_hell.log").exists())


if __name__ == "__main__":
    unittest.main()
