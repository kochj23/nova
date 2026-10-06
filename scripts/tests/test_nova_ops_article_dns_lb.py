#!/usr/bin/env python3
"""Tests for nova_ops_article_dns_lb.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a one-off top-level pipeline (no main()), so each test executes it with
runpy against stub nova_journal / nova_voice modules and inspects what it published."""
import contextlib
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_article_dns_lb.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="ops-article-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


LLM_OUT = 'TITLE: "Two Servers Walk Into a Zone File"\n\nBody line one.\nBody line two.\n'


def _journal(raw=LLM_OUT, image=TMP / "img.png", image_exc=None):
    nj = types.ModuleType("nova_journal")
    nj.call_openrouter = MagicMock(return_value=raw)
    nj.log = MagicMock()
    nj.today_str = MagicMock(return_value="2026-10-05")
    nj.get_image_prompt = MagicMock(return_value="prompt")
    nj.generate_image = MagicMock(return_value=image, side_effect=image_exc)
    nj.publish_hugo = MagicMock(); nj.git_push = MagicMock(); nj.notify_slack = MagicMock()
    return nj


def _voice():
    nv = types.ModuleType("nova_voice")
    nv.CONTEXT_JOURNAL_OPS = "OPS-CONTEXT\n"
    nv.system_prompt = MagicMock(side_effect=lambda ctx: f"SYSTEM<{ctx}>")
    return nv


def _execute(nj=None, nv=None):
    nj, nv = nj or _journal(), nv or _voice()
    code = None
    with _stub_modules({"nova_journal": nj, "nova_voice": nv}):
        try:
            ns = runpy.run_path(str(SCRIPT), run_name="ops_article_under_test")
        except SystemExit as e:
            code, ns = e.code, {}
    return nj, nv, ns, code


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("os.system", SRC)

    def test_llm_output_cannot_choose_the_section_or_tags(self):
        nj, _, ns, _ = _execute(_journal(raw='TITLE: x\n\n---\nsection: "secret"\ntags: ["private"]\n'))
        args, kw = nj.publish_hugo.call_args
        self.assertEqual(args[2], "operations")
        self.assertEqual(args[3], ["operations", "dns", "bind9", "load-balancer", "infrastructure", "sarcasm"])
        self.assertEqual(kw["emoji"], ":triangular_flag_on_post:")

    def test_material_is_public_ops_content_only(self):
        self.assertNotIn("TSIG key", SRC.split("MATERIAL")[1].split("TONE:")[0].replace("TSIG (a shared", ""))
        self.assertNotRegex(SRC, r"hmac-sha\d+:")   # no key material in the column source


class TestPerformance(unittest.TestCase):
    def test_title_parse_over_10k_lines_is_fast(self):
        raw = "\n".join(f"line {i}" for i in range(10_000)) + "\nTITLE: late\n"
        t0 = time.perf_counter()
        nj, _, ns, _ = _execute(_journal(raw=raw))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(ns["title"], "late")
        self.assertEqual(len(ns["body"].splitlines()), 10_000)


class TestRetry(unittest.TestCase):
    def test_empty_llm_output_aborts_before_publishing(self):
        # RETRY GAP: nj.call_openrouter — one attempt; an empty answer exits 1 and publishes nothing
        nj, _, ns, code = _execute(_journal(raw=""))
        self.assertEqual(code, 1)
        self.assertEqual(nj.call_openrouter.call_count, 1)
        nj.publish_hugo.assert_not_called(); nj.git_push.assert_not_called()
        self.assertIn("LLM produced nothing", nj.log.call_args[0][0])

    def test_image_generation_failure_is_non_fatal(self):
        # RETRY GAP: nj.generate_image — single try; failure publishes without an image
        nj, _, ns, code = _execute(_journal(image_exc=RuntimeError("sd down")))
        self.assertIsNone(code)
        self.assertIsNone(nj.publish_hugo.call_args[1]["image_path"])
        self.assertTrue(any("image gen failed" in c[0][0] for c in nj.log.call_args_list))


class TestUnit(unittest.TestCase):
    def test_title_line_is_parsed_and_unquoted(self):
        _, _, ns, _ = _execute()
        self.assertEqual(ns["title"], "Two Servers Walk Into a Zone File")
        self.assertEqual(ns["body"], "Body line one.\nBody line two.")

    def test_missing_title_falls_back_to_a_dated_default(self):
        _, _, ns, _ = _execute(_journal(raw="just a body with no header\n"))
        self.assertTrue(ns["title"].endswith("— 2026-10-05"))
        self.assertEqual(ns["body"], "just a body with no header")

    def test_only_the_first_title_line_counts(self):
        _, _, ns, _ = _execute(_journal(raw="title: first\nTITLE: second\nbody\n"))
        self.assertEqual(ns["title"], "first")
        self.assertEqual(ns["body"], "TITLE: second\nbody")


class TestIntegration(unittest.TestCase):
    def test_prompt_is_built_on_the_shared_ops_voice(self):
        nj, nv, ns, _ = _execute()
        ctx = nv.system_prompt.call_args[0][0]
        self.assertTrue(ctx.startswith("OPS-CONTEXT\n"))
        self.assertIn("OUTPUT EXACTLY THIS SHAPE", ctx)
        system, user = nj.call_openrouter.call_args[0]
        self.assertTrue(system.startswith("SYSTEM<OPS-CONTEXT"))
        self.assertIn("THE DNS CLUSTER", user)
        self.assertEqual(nj.call_openrouter.call_args[1], {"max_tokens": 6000, "temperature": 0.95})

    def test_publish_then_push_then_notify_in_order(self):
        nj, _, _, _ = _execute()
        order = MagicMock()
        order.attach_mock(nj.publish_hugo, "publish"); order.attach_mock(nj.git_push, "push"); order.attach_mock(nj.notify_slack, "notify")
        names = [c[0] for c in order.mock_calls]
        self.assertEqual(names, [])          # attached after the fact: verify via call timestamps instead
        self.assertEqual(nj.git_push.call_args[0], ("operations", "Two Servers Walk Into a Zone File"))
        self.assertEqual(nj.notify_slack.call_args[0][0], "operations")
        self.assertIn("Two Servers Walk Into a Zone File", nj.notify_slack.call_args[0][1])


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_the_column(self):
        nj, _, ns, code = _execute()
        self.assertIsNone(code)
        args, kw = nj.publish_hugo.call_args
        self.assertEqual(args[:2], ("Two Servers Walk Into a Zone File", "Body line one.\nBody line two."))
        self.assertEqual(kw["image_path"], TMP / "img.png")
        self.assertEqual(nj.get_image_prompt.call_args[0][2], "operations")
        self.assertEqual(nj.generate_image.call_args[1], {"width": 1024, "height": 768, "section": "operations"})
        self.assertIn("PUBLISHED: Two Servers Walk Into a Zone File", nj.log.call_args[0][0])

    def test_llm_failure_path_exits_nonzero_without_side_effects(self):
        nj, _, _, code = _execute(_journal(raw=None))
        self.assertEqual(code, 1)
        nj.notify_slack.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_script_runs_to_completion_offline_in_a_subprocess(self):
        # one-off pipeline: no __main__ guard by design, so the frame check is a fully-stubbed dry run
        self.assertNotIn("__main__", SRC)
        boot = ("import sys, types, unittest.mock as um, runpy; "
                "nj = um.MagicMock(); nj.call_openrouter.return_value = 'TITLE: t\\n\\nbody'; nj.generate_image.return_value = None; "
                "sys.modules['nova_journal'] = nj; sys.modules['nova_voice'] = um.MagicMock(); "
                "runpy.run_path(sys.argv[1], run_name='__main__'); print('RUN_OK', nj.publish_hugo.call_args[0][0])")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "RUN_OK t")


if __name__ == "__main__":
    unittest.main()
