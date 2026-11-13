"""Tests for nova_image_safety.py (+ its hooks in nova_image_utils.generate_image and nova_satire) — the 7 house
categories (Security, Performance, Retry, Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The gate exists because antisemitic big-nosed caricatures and nude statues reached the journal and Nova Speaks
videos (2026-11-12). Offline: the vision model is always injected or urlopen is mocked."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("NOVA_TEST_QUIET", "1")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_gate_t", SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


nis = _load("nova_image_safety")
CLEAN = {c: False for c in nis.CATS}


def _png(d, name="x.png"):
    from PIL import Image
    p = Path(d) / name
    Image.new("RGB", (40, 30), (20, 30, 60)).save(p)
    return str(p)


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_local_only(self):
        src = (SCRIPTS / "nova_image_safety.py").read_text()
        self.assertIsNone(re.search(r"(api[_-]?key|token|password|secret)\s*=\s*['\"][^'\"]{6,}", src, re.I))
        self.assertNotIn("openrouter", src.lower())
        self.assertNotIn("anthropic.com", src.lower())
        self.assertTrue(nis.OLLAMA.startswith("http://192.168.") or "NOVA_VISION_URL" in os.environ)

    def test_unsafe_and_unanswered_verdicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = _png(d)
            ok, info = nis.screen_image(p, models=["m"], ask=lambda *_: dict(CLEAN, ethnic_religious_caricature=True))
            self.assertFalse(ok)
            self.assertEqual(info["flags"], ["ethnic_religious_caricature"])
            ok, _ = nis.screen_image(p, models=["m"], ask=lambda *_: {"description": "half an answer"}, sleep=lambda s: None)
            self.assertFalse(ok)
            self.assertFalse(nis.screen_image(str(Path(d) / "missing.png"), ask=lambda *_: CLEAN)[0])

    def test_prompt_hardening_strips_caricature_and_nudity_cues(self):
        out = nis.harden_prompt("an editorial cartoon caricature of a greedy banker with a huge hooked nose beside a nude statue")
        low = out.lower()
        for bad in ("caricature of", "greedy banker", "hooked nose", "nude statue"):
            self.assertNotIn(bad, low)
        self.assertIn(nis.SENTINEL, out)


class TestPerformance(unittest.TestCase):
    def test_hardening_and_keyword_scan_are_fast(self):
        prompts = [f"a robot number {i} with a big nose folding laundry near a nude statue" for i in range(10000)]
        t = time.perf_counter()
        for p in prompts:
            nis.harden_prompt(p)
            nis.risky_terms(p)
        self.assertLess(time.perf_counter() - t, 5.0)


class TestRetry(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = []

        def ask(path, model):
            calls.append(model)
            if len(calls) < 3:
                raise OSError("ollama loading")
            return CLEAN
        with tempfile.TemporaryDirectory() as d:
            ok, info = nis.screen_image(_png(d), models=["m1"], ask=ask, retries=2, sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)

    def test_falls_back_to_second_model_then_closed(self):
        seen = []

        def ask(path, model):
            seen.append(model)
            if model == "big":
                raise TimeoutError("busy")
            return CLEAN
        with tempfile.TemporaryDirectory() as d:
            ok, info = nis.screen_image(_png(d), models=["big", "small"], ask=ask, retries=1, sleep=lambda s: None)
            self.assertTrue(ok)
            self.assertEqual(info["model"], "small")
            ok, info = nis.screen_image(_png(d), models=["big"], ask=ask, retries=1, sleep=lambda s: None)
            self.assertFalse(ok)
            self.assertEqual(info["flags"], ["no_verdict"])


class TestUnit(unittest.TestCase):
    def test_verdict_flags(self):
        self.assertEqual(nis.verdict_flags(CLEAN), [])
        self.assertEqual(nis.verdict_flags(dict(CLEAN, nudity_or_sexual="true")), ["nudity_or_sexual"])
        self.assertEqual(nis.verdict_flags(None), ["unparsed"])
        self.assertEqual(nis.verdict_flags({}), ["unparsed"])

    def test_harden_idempotent_and_empty(self):
        self.assertEqual(nis.harden_prompt(""), "")
        once = nis.harden_prompt("a cat DJ")
        self.assertEqual(nis.harden_prompt(once), once)

    def test_risky_terms(self):
        self.assertIn("rabbi", nis.risky_terms("a rabbi counting coins"))
        self.assertEqual(nis.risky_terms("a raccoon saluting a recycling bin"), [])

    def test_rubric_covers_every_category(self):
        for c in nis.CATS:
            self.assertIn(c, nis.RUBRIC)
        self.assertIn("nipples", nis.RUBRIC)
        self.assertIn("hooked", nis.RUBRIC)

    def test_selftest(self):
        self.assertEqual(nis.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_ask_builds_local_ollama_request(self):
        captured = {}

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return R(json.dumps({"message": {"content": json.dumps(CLEAN)}}).encode())
        with tempfile.TemporaryDirectory() as d:
            v = nis._ask(_png(d), "qwen3-vl:30b", urlopen=fake_urlopen)
        self.assertEqual(v, CLEAN)
        self.assertTrue(captured["url"].endswith("/api/chat"))
        self.assertEqual(captured["body"]["messages"][0]["content"], nis.RUBRIC)
        self.assertEqual(len(captured["body"]["messages"][0]["images"]), 1)
        self.assertFalse(captured["body"]["think"])

    def test_generators_import_the_shared_gate(self):
        self.assertIn("nova_image_safety", (SCRIPTS / "nova_image_utils.py").read_text())
        self.assertIn("nova_image_safety", (SCRIPTS / "generate_image.sh").read_text())
        sat = (SCRIPTS / "nova_satire.py").read_text()
        self.assertIn("nova_image_safety", sat)
        for gone in ('"editorial-cartoon"', '"claymation"', '"pulp-cover"', '"renaissance-oil"', '"propaganda-parody"'):
            self.assertNotIn(f"({gone},", sat)

    def test_sdxl_negative_carries_safety_terms(self):
        iu = _load("nova_image_utils")
        self.assertIn("hooked nose", iu._SDXL_NEGATIVE)
        self.assertIn("nude statue", iu._SDXL_NEGATIVE)


class TestFunctional(unittest.TestCase):
    def test_generate_image_rejects_unsafe_render_and_rerenders(self):
        iu = _load("nova_image_utils")
        with tempfile.TemporaryDirectory() as d:
            renders = [_png(d, "bad.png"), _png(d, "good.png")]
            verdicts = iter([(False, {"flags": ["ethnic_religious_caricature"]}), (True, {"flags": []})])
            seen_prompts = []

            def fake_gen(prompt, *a, **k):
                seen_prompts.append(prompt)
                return renders.pop(0)
            with mock.patch.dict(os.environ, {"NOVA_IMAGE_SCREEN": "1"}), \
                    mock.patch.object(iu, "_comfyui_generate", side_effect=fake_gen), \
                    mock.patch.object(iu, "comfyui_url", return_value="http://comfy.test"), \
                    mock.patch.object(iu, "_wait_budget", return_value=10_000), \
                    mock.patch.object(iu.time, "sleep"), \
                    mock.patch("nova_image_safety.screen_image", side_effect=lambda p: next(verdicts)):
                out = iu.generate_image("a banker with a big nose")
            self.assertTrue(out.endswith("good.png"))
            self.assertFalse(Path(d, "bad.png").exists())        # the rejected render is deleted
            self.assertIn(nis.SENTINEL, seen_prompts[0])
            self.assertNotIn("big nose", seen_prompts[0])

    def test_satire_article_not_illustrated_when_screen_rejects(self):
        sat = _load("nova_satire")
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / "2026-11-12-x.md"
            md.write_text('---\ntitle: "X"\n---\n\npara one.\n\npara two.\n\npara three.\n')
            png = _png(d)
            with mock.patch.object(sat, "safe_to_use", return_value=False):
                r = sat.satirize_article(md, gen=lambda *a, **k: png, llm=lambda s, u: json.dumps(
                    {"prompt": "a robot", "caption": "c", "tags": ["t"]}))
            self.assertIsNone(r)
            self.assertNotIn(sat.MARKER, md.read_text())

    def test_safe_to_use_uses_injected_screen(self):
        sat = _load("nova_satire")
        self.assertFalse(sat.safe_to_use("/x.png", screen=lambda p: (False, {"flags": ["gore"]})))
        self.assertTrue(sat.safe_to_use("/x.png", screen=lambda p: (True, {})))


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_image_safety.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import nova_image_safety"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
