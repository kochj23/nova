#!/usr/bin/env python3
"""Tests for nova_ops_article_week_from_hell_v2.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This is a ONE-SHOT script: all of its work runs at module level (no main(), no __main__ guard). So every test
executes the module with ALL of its collaborators stubbed — the LLM (nova_rando_daily_ops.call_llm), image
generation, nova_voice, nova_config.post_both and subprocess.run (cwebp / git add / commit / push / rebase) —
against a throwaway Hugo tree in a tempdir. Nothing is generated, committed, pushed or posted."""
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
SCRIPT = SCRIPTS / "nova_ops_article_week_from_hell_v2.py"
SRC = SCRIPT.read_text()
SLUG = re.search(r'EXISTING_SLUG = "([^"]+)"', SRC).group(1)
FRONT = '---\ntitle: "Old"\ndate: 2026-07-19\n---\n\n'


class _Run:
    """Fake subprocess.run: records argv; `git push` answers from push_rcs in order."""
    def __init__(self, push_rcs=(0,)):
        self.calls = []; self.push_rcs = list(push_rcs)

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        rc = self.push_rcs.pop(0) if argv[:2] == ["git", "push"] and self.push_rcs else 0
        return subprocess.CompletedProcess(argv, rc, "", "rejected" if rc else "")


def _execute(push_rcs=(0,), body="# The Rack\nA long column.", img=None):
    """Run the whole one-shot script in-process with every collaborator stubbed. Returns a result namespace."""
    tmp = Path(tempfile.mkdtemp(prefix="wfh-test-"))
    hugo = tmp / "hugo"; content = hugo / "content" / "operations"; content.mkdir(parents=True)
    (content / f"{SLUG}.md").write_text(FRONT + "old body")
    (tmp / ".openclaw" / "logs").mkdir(parents=True)
    rando = types.ModuleType("nova_rando_daily_ops")
    rando.call_llm = MagicMock(return_value=body); rando.HUGO_ROOT = hugo; rando.CONTENT_DIR = content
    imgmod = types.ModuleType("nova_image_utils"); imgmod.generate_image = MagicMock(return_value=img)
    voice = types.ModuleType("nova_voice")
    voice.system_prompt = MagicMock(side_effect=lambda s: "SYS:" + s); voice.CONTEXT_JOURNAL_OPS = "OPS-CONTEXT"
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.JORDAN_DM = "D_TEST"
    run = _Run(push_rcs)
    spec = importlib.util.spec_from_file_location("wfh_v2_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_rando_daily_ops": rando, "nova_image_utils": imgmod,
                                  "nova_voice": voice, "nova_config": cfg}), \
         patch.object(Path, "home", classmethod(lambda c: tmp)), \
         patch.object(subprocess, "run", run), \
         patch("builtins.print"):
        spec.loader.exec_module(mod)
    return types.SimpleNamespace(mod=mod, tmp=tmp, post=content / f"{SLUG}.md", hugo=hugo, run=run,
                                 llm=rando.call_llm, img=imgmod.generate_image, voice=voice, cfg=cfg)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_only_touches_its_own_post_and_repo(self):
        r = _execute()
        self.assertEqual(sorted(p.name for p in r.post.parent.iterdir()), [f"{SLUG}.md"])  # updated in place
        self.assertTrue(all(c[0] in ("git", "cwebp") for c in r.run.calls))
        self.assertNotIn(["git", "push", "--force"], r.run.calls)
        self.assertFalse(any("--force" in c or "-f" in c for c in r.run.calls))


class TestPerformance(unittest.TestCase):
    def test_runs_fast_with_large_body(self):
        t0 = time.perf_counter()
        r = _execute(body="word " * 200_000)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertGreater(len(r.post.read_text()), 1_000_000)


class TestRetry(unittest.TestCase):
    def test_push_rejected_twice_rebases_then_succeeds(self):
        r = _execute(push_rcs=(1, 1, 0))
        pushes = [c for c in r.run.calls if c[:2] == ["git", "push"]]
        self.assertEqual(len(pushes), 3)
        self.assertEqual(sum(1 for c in r.run.calls if c[:2] == ["git", "rebase"]), 2)
        self.assertTrue(r.mod.pushed)

    def test_push_gives_up_after_five_and_alerts(self):
        r = _execute(push_rcs=(1,) * 5)
        self.assertEqual(sum(1 for c in r.run.calls if c[:2] == ["git", "push"]), 5)
        self.assertFalse(r.mod.pushed)
        self.assertIn("PUSH FAILED", r.cfg.post_both.call_args[0][0])


class TestUnit(unittest.TestCase):
    def test_front_matter_preserved_and_body_replaced(self):
        r = _execute(body="NEW BODY")
        text = r.post.read_text()
        self.assertTrue(text.startswith(FRONT))
        self.assertIn("(updated — now with 100% more disassembled rack)", text)
        self.assertTrue(text.endswith("NEW BODY"))
        self.assertNotIn("old body", text)

    def test_constants(self):
        r = _execute()
        self.assertEqual(r.mod.EXISTING_SLUG, SLUG)
        self.assertIn("nova-core5", r.mod.MATERIAL)
        self.assertTrue(str(r.mod.LOG).startswith(str(r.tmp)))      # log redirected under the fake home


class TestIntegration(unittest.TestCase):
    def test_uses_shared_voice_llm_and_image_helpers(self):
        r = _execute()
        system = r.llm.call_args[0][0]
        self.assertTrue(system.startswith("SYS:OPS-CONTEXT"))
        self.assertIn(r.mod.MATERIAL, r.llm.call_args[0][1])
        self.assertEqual(r.llm.call_args[1]["max_tokens"], 28000)
        self.assertEqual(r.img.call_args[1]["section"], "operations")

    def test_cover_image_converted_into_hugo_static(self):
        tmpimg = Path(tempfile.mkdtemp()) / "cover.png"; tmpimg.write_bytes(b"png")
        r = _execute(img=str(tmpimg))
        cw = [c for c in r.run.calls if c[0] == "cwebp"]
        self.assertEqual(cw[0][-1], str(r.hugo / "static/images/operations" / f"{SLUG}.webp"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_commits_pushes_and_dms(self):
        r = _execute()
        tools = [c[:2] for c in r.run.calls]
        self.assertEqual(tools, [["git", "add"], ["git", "commit"], ["git", "push"]])
        msg, kw = r.cfg.post_both.call_args
        self.assertIn(f"/operations/{SLUG}/", msg[0])
        self.assertEqual(kw["slack_channel"], "D_TEST")
        self.assertIn("ARTICLE V2 DONE", (r.tmp / ".openclaw/logs/ops_article_week_from_hell_v2.log").read_text())

    def test_missing_post_fails_before_any_git(self):
        # error path: if the published post is gone, the rewrite dies before git/Slack are touched
        tmp_home = Path(tempfile.mkdtemp()); (tmp_home / ".openclaw/logs").mkdir(parents=True)
        rando = types.ModuleType("nova_rando_daily_ops")
        rando.call_llm = MagicMock(return_value="b"); rando.HUGO_ROOT = tmp_home; rando.CONTENT_DIR = tmp_home / "nope"
        imgmod = types.ModuleType("nova_image_utils"); imgmod.generate_image = MagicMock(return_value=None)
        voice = types.ModuleType("nova_voice"); voice.system_prompt = lambda s: s; voice.CONTEXT_JOURNAL_OPS = ""
        cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.JORDAN_DM = "D"
        run = _Run()
        spec = importlib.util.spec_from_file_location("wfh_v2_err", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"nova_rando_daily_ops": rando, "nova_image_utils": imgmod,
                                      "nova_voice": voice, "nova_config": cfg}), \
             patch.object(Path, "home", classmethod(lambda c: tmp_home)), \
             patch.object(subprocess, "run", run), patch("builtins.print"):
            with self.assertRaises(FileNotFoundError):
                spec.loader.exec_module(mod)
        self.assertEqual(run.calls, [])
        cfg.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_one_shot_runs_clean_end_to_end_in_a_subprocess(self):
        # No --help/--selftest and no __main__ guard (a one-off rewrite whose import IS the run), so the frame
        # smoke runs it in a child process with every collaborator stubbed and HOME pointed at a tempdir.
        self.assertNotIn("__main__", SRC)
        tmp = Path(tempfile.mkdtemp(prefix="wfh-frame-"))
        content = tmp / "hugo" / "content"; content.mkdir(parents=True)
        (content / f"{SLUG}.md").write_text(FRONT + "x")
        code = f"""
import sys, types, subprocess
from pathlib import Path
r = types.ModuleType('nova_rando_daily_ops'); r.call_llm = lambda *a, **k: 'body'
r.HUGO_ROOT = Path({str(tmp / 'hugo')!r}); r.CONTENT_DIR = Path({str(content)!r})
i = types.ModuleType('nova_image_utils'); i.generate_image = lambda *a, **k: None
v = types.ModuleType('nova_voice'); v.system_prompt = lambda s: s; v.CONTEXT_JOURNAL_OPS = ''
c = types.ModuleType('nova_config'); c.post_both = lambda *a, **k: None; c.JORDAN_DM = 'D'
sys.modules.update({{'nova_rando_daily_ops': r, 'nova_image_utils': i, 'nova_voice': v, 'nova_config': c}})
subprocess.run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, '', '')
import runpy; runpy.run_path(sys.argv[1])
"""
        (tmp / ".openclaw/logs").mkdir(parents=True)
        res = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(tmp)})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ARTICLE V2 DONE", res.stdout)


if __name__ == "__main__":
    unittest.main()
