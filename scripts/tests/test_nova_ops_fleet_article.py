#!/usr/bin/env python3
"""Tests for nova_ops_fleet_article.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The LLM, image generation and publish are mocked; nothing is written
to the journal. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_ops_fleet_article.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("opsfleet", SCRIPTS / "nova_ops_fleet_article.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fa = _load()


def _run_main(llm="BODY " * 200, title="Tuning My Own Body", image="/tmp/x.webp", image_exc=None):
    with patch.object(fa, "system_prompt", return_value="SYS") as sp, \
         patch.object(fa, "call_llm", side_effect=llm if isinstance(llm, Exception) else None,
                      return_value=llm) as cl, \
         patch.object(fa, "generate_title", return_value=title) as gt, \
         patch.object(fa, "generate_image", side_effect=image_exc, return_value=image) as gi, \
         patch.object(fa, "publish") as pub, redirect_stdout(io.StringIO()) as out:
        err = None
        try:
            fa.main()
        except Exception as e:      # surfaced to the caller of _run_main
            err = e
    return {"sp": sp, "cl": cl, "gt": gt, "gi": gi, "pub": pub, "out": out.getvalue(), "err": err}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_public_article_carries_no_personal_paths_or_addresses(self):
        self.assertNotIn(str(Path.home()) + "/", fa.FLEET + fa.INSTR)
        self.assertNotIn("kochj23" + "@" + "gmail.com", SRC)
        self.assertIsNone(re.search(r"[\w.]+@[\w-]+\.\w+", fa.FLEET))


class TestPerformance(unittest.TestCase):
    def test_prompt_bounded_and_main_fast(self):
        self.assertLess(len(fa.FLEET) + len(fa.INSTR), 12_000)
        t0 = time.perf_counter()
        r = _run_main()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertLessEqual(r["cl"].call_args[1]["max_tokens"], 6000)


class TestRetry(unittest.TestCase):
    def test_image_failure_publishes_without_cover(self):
        r = _run_main(image_exc=RuntimeError("comfyui down"))
        self.assertIsNone(r["err"])
        self.assertIsNone(r["pub"].call_args[0][2])
        self.assertIn("publishing without cover", r["out"])

    def test_llm_failure_aborts_before_publish(self):
        # RETRY GAP: main()/call_llm — no retry here (one-off article); a failed generation raises before
        # anything is published, so a half-written article never ships.
        r = _run_main(llm=RuntimeError("openrouter 401"))
        self.assertIsInstance(r["err"], RuntimeError)
        r["pub"].assert_not_called()
        self.assertEqual(r["cl"].call_count, 1)


class TestUnit(unittest.TestCase):
    def test_fleet_facts_cover_all_seven_nodes(self):
        for host in ("192.168.1.6", "192.168.1.77", "192.168.1.7", "192.168.1.2", "192.168.1.5",
                     "192.168.1.86", "192.168.1.10"):
            self.assertIn(host, fa.FLEET)
        self.assertIn("700-1100 words", fa.INSTR)

    def test_empty_image_result_means_no_cover(self):
        r = _run_main(image=None)
        self.assertIsNone(r["pub"].call_args[0][2])


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_ops_machinery(self):
        import nova_rando_daily_ops as rdo
        import nova_voice
        self.assertIs(fa.publish, rdo.publish)
        self.assertIs(fa.call_llm, rdo.call_llm)
        self.assertEqual(fa.CONTEXT_JOURNAL_OPS, nova_voice.CONTEXT_JOURNAL_OPS)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_titled_article_with_image(self):
        r = _run_main()
        self.assertIsNone(r["err"])
        r["sp"].assert_called_once_with(fa.CONTEXT_JOURNAL_OPS)
        self.assertTrue(r["cl"].call_args[0][1].startswith(fa.FLEET))
        title, body, img = r["pub"].call_args[0]
        self.assertEqual(title, "Tuning My Own Body")
        self.assertEqual(img, Path("/tmp/x.webp"))
        self.assertEqual(r["gi"].call_args[0][1], "fleet_optimization")
        self.assertIn("TITLE: Tuning My Own Body", r["out"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running the script generates and publishes an article, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ops_fleet_article"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
