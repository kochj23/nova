#!/usr/bin/env python3
"""Tests for nova_ops_article_update3.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a one-off that runs top-to-bottom at import (LLM addendum, cover image, in-place Hugo edit,
git commit/push with rebase retry, Slack DM). Every test executes it under a fake $HOME and a temp Hugo
root with every collaborator module and subprocess.run stubbed, then inspects what the stubs received."""
import importlib.util
import io
import itertools
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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_article_update3.py"
SRC = SCRIPT.read_text()
SLUG = "2026-07-19-please-update-your-records-the-house-is-now-haunted-by-a-man"
_seq = itertools.count()
OLD = "---\ntitle: x\n---\n\n*Published Sunday old*\n\nORIGINAL BODY\n"


class _Stubs:
    def __init__(self, root, body, image, push_rcs):
        self.rd = types.ModuleType("nova_rando_daily_ops")
        self.rd.call_llm = MagicMock(return_value=body)
        self.rd.HUGO_ROOT = root; self.rd.CONTENT_DIR = root / "content/operations"
        self.iu = types.ModuleType("nova_image_utils")
        self.iu.generate_image = MagicMock(return_value=image)
        self.nv = types.ModuleType("nova_voice")
        self.nv.CONTEXT_JOURNAL_OPS = "[ops]"; self.nv.system_prompt = lambda c: "SYS:" + c
        self.cfg = types.ModuleType("nova_config")
        self.cfg.post_both = MagicMock(); self.cfg.JORDAN_DM = "D_JORDAN"
        self.push_rcs = list(push_rcs); self.cmds = []

    def run(self, argv, **kw):
        self.cmds.append(argv)
        rc = self.push_rcs.pop(0) if argv[:2] == ["git", "push"] and self.push_rcs else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="rejected" if rc else "")

    def modules(self):
        return {"nova_rando_daily_ops": self.rd, "nova_image_utils": self.iu, "nova_voice": self.nv,
                "nova_config": self.cfg}


def _run(td, body="ADDENDUM", image=None, push_rcs=(0,), old=OLD):
    home = Path(td) / "home"; (home / ".openclaw/logs").mkdir(parents=True)
    root = Path(td) / "hugo"; (root / "content/operations").mkdir(parents=True)
    post = root / "content/operations" / f"{SLUG}.md"
    if old is not None:
        post.write_text(old)
    st = _Stubs(root, body, image, push_rcs)
    spec = importlib.util.spec_from_file_location(f"opsu3_{next(_seq)}", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, st.modules()), patch("pathlib.Path.home", return_value=home), \
            patch("subprocess.run", side_effect=st.run), patch("time.sleep"), redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return st, post, (home / ".openclaw/logs/ops_article_update3.log").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC); self.assertNotIn("os.system", SRC)
        self.assertNotIn("--force", SRC)

    def test_dm_only_and_git_confined_to_hugo_root(self):
        with tempfile.TemporaryDirectory() as td:
            st, _, _ = _run(td)
        self.assertEqual(st.cfg.post_both.call_args.kwargs, {"slack_channel": "D_JORDAN"})
        self.assertTrue(all(c[0] in ("git", "cwebp") for c in st.cmds))
        self.assertEqual(SRC.count("cwd=HUGO_ROOT"), 5)


class TestPerformance(unittest.TestCase):
    def test_huge_addendum_and_post_rewrite_fast(self):
        with tempfile.TemporaryDirectory() as td:
            t0 = time.perf_counter()
            _, post, _ = _run(td, body="word " * 20_000, old=OLD.replace("ORIGINAL BODY", "old " * 20_000))
            self.assertLess(time.perf_counter() - t0, 3.0)
            self.assertEqual(post.read_text().count("word "), 20_000)


class TestRetry(unittest.TestCase):
    def test_push_rejected_twice_rebases_then_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            st, _, log = _run(td, push_rcs=(1, 1, 0))
        pushes = [c for c in st.cmds if c[:2] == ["git", "push"]]
        rebases = [c for c in st.cmds if c[:2] == ["git", "rebase"]]
        self.assertEqual((len(pushes), len(rebases)), (3, 2))
        self.assertIn("Pushed to GitHub", log)
        self.assertIn("Ops column updated again", st.cfg.post_both.call_args.args[0])

    def test_push_fails_five_times_then_dm_says_so(self):
        with tempfile.TemporaryDirectory() as td:
            st, _, log = _run(td, push_rcs=(1,) * 5)
        self.assertEqual(len([c for c in st.cmds if c[:2] == ["git", "push"]]), 5)
        self.assertIn("PUSH FAILED after 5 rebase attempts", st.cfg.post_both.call_args.args[0])
        self.assertIn("needs manual push", log)


class TestUnit(unittest.TestCase):
    def test_old_byline_replaced_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            _, post, _ = _run(td)
            txt = post.read_text()
        self.assertTrue(txt.startswith("---\ntitle: x\n---\n\n*Published "))
        self.assertEqual(txt.count("*Published "), 1)
        self.assertIn("updated again", txt)
        self.assertTrue(txt.endswith("## UPDATE: SO THEN I KEPT GOING\n\nADDENDUM"))

    def test_material_carries_named_beats(self):
        for beat in ("NOVA_GW_STANDBY", "posting list tuple with 3 items", "raw_classification", "Anonymous Diffie-Hellman"):
            self.assertIn(beat, SRC)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_llm_image_and_voice_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            st, _, _ = _run(td)
        sysmsg, user = st.rd.call_llm.call_args.args
        self.assertTrue(sysmsg.startswith("SYS:[ops]"))
        self.assertIn("THIRD PASS", sysmsg)
        self.assertIn("WAVE 3", user)
        self.assertEqual(st.rd.call_llm.call_args.kwargs, {"max_tokens": 20000})
        self.assertEqual(st.iu.generate_image.call_args.kwargs["section"], "operations")
        self.assertNotIn("def call_llm", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_with_cover_image(self):
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "c.png"; img.write_bytes(b"png")
            st, post, log = _run(td, image=str(img))
        cwebp = [c for c in st.cmds if c[0] == "cwebp"][0]
        self.assertTrue(cwebp[-1].endswith(f"static/images/operations/{SLUG}.webp"))
        self.assertEqual([c[:2] for c in st.cmds if c[0] == "git"][:2], [["git", "add"], ["git", "commit"]])
        self.assertIn(f"https://nova.digitalnoise.net/operations/{SLUG}/", st.cfg.post_both.call_args.args[0])
        self.assertIn("ARTICLE UPDATE3 DONE", log)

    def test_error_path_missing_post_aborts_before_git(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(FileNotFoundError):
            _run(td, old=None)


class TestFrame(unittest.TestCase):
    def test_parses_and_every_side_effect_is_stubbable(self):
        # a one-off with no main(): the frame is a clean parse plus proof the stub surface is complete
        r = subprocess.run([sys.executable, "-c", f"import ast;ast.parse(open({str(SCRIPT)!r}).read())"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        imported = set(re.findall(r"^(?:import|from)\s+([\w.]+)", SRC, re.M))
        self.assertEqual(imported - {"os", "sys", "pathlib"},
                         {"nova_rando_daily_ops", "nova_image_utils", "nova_voice", "nova_config"})
        self.assertIsNone(re.search(r"^\s*(?:import|from)\s+(?:urllib|requests|psycopg2)\b", SRC, re.M))


if __name__ == "__main__":
    unittest.main()
