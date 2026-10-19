#!/usr/bin/env python3
"""Tests for nova_image_utils.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_image_utils.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_image_utils_t", SCRIPTS / "nova_image_utils.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iu = _load()
iu._comfyui_url_cache = "http://127.0.0.1:8188"   # Studio path, no PG lookup (LAN path: test_nova_image_utils_remote.py)


def _quiet():
    return mock.patch("sys.stdout", new_callable=io.StringIO)


def _ctx(payload):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return r


class _Home:
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        (self.home / ".openclaw/workspace").mkdir(parents=True)
        self.p = mock.patch.object(Path, "home", return_value=self.home)
        self.p.start()
        return self.home

    def __exit__(self, *a):
        self.p.stop()
        self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"sk-or-[A-Za-z0-9]")

    def test_no_cloud_image_path_remains(self):
        # 2026-10-19 (Jordan): images are LOCAL only — no OpenRouter/cloud endpoint, key or call.
        for needle in ("openrouter.ai", "_openrouter_generate", "openrouter_api_key", "Authorization"):
            self.assertNotIn(needle, SRC)

    def test_safety_policy_rewrites_youth_and_undress_cues(self):
        out = iu.apply_image_safety("a young girl in a bikini with little kids")
        for word in ("young", "girl", "bikini", "kids", "little"):
            self.assertNotRegex(out.lower(), rf"\b{word}\b")
        self.assertIn(iu._SAFETY_SENTINEL, out)
        self.assertEqual(iu.apply_image_safety(out), out)     # idempotent
        self.assertEqual(iu.apply_image_safety(""), "")

    def test_generate_image_submits_safe_prompt_with_quality_suffix(self):
        with _quiet(), mock.patch.object(iu, "_comfyui_generate", return_value="/tmp/x.png") as gen:
            iu.generate_image("a young girl on a beach")
        sent = gen.call_args[0][0]
        self.assertIn(iu._SAFETY_SENTINEL, sent)
        self.assertIn(iu.IMAGE_QUALITY_SUFFIX.strip(), sent)
        self.assertNotRegex(sent.lower(), r"\bgirl\b")

    def test_sdxl_graph_keeps_explicit_content_negative_prompt(self):
        wf = iu.comfy_workflow("p", "Juggernaut_X_RunDiffusion_Hyper.safetensors", 512, 512, 8, seed=1)
        for word in ("nudity", "nsfw", "explicit", "sexual content"):
            self.assertIn(word, wf["7"]["inputs"]["text"])


class TestPerformance(unittest.TestCase):
    def test_safety_pass_10k_prompts(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            iu.apply_image_safety(f"server rack number {i} glowing at dusk")
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_down_comfyui_fails_fast_without_real_waiting(self):
        with _quiet(), mock.patch.object(iu, "_comfyui_generate", return_value=None) as gen, \
                mock.patch.object(iu.time, "sleep") as sl:
            t0 = time.perf_counter()
            self.assertIsNone(iu.generate_image("p"))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(gen.call_count, iu.MAX_RETRIES)
        self.assertEqual(sl.call_count, iu.MAX_RETRIES - 1)


class TestRetry(unittest.TestCase):
    def test_unreachable_then_success_backs_off_once(self):
        with _quiet(), mock.patch.object(iu, "_comfyui_generate", side_effect=[None, "/tmp/ok.png"]) as gen, \
                mock.patch.object(iu.time, "sleep") as sl:
            self.assertEqual(iu.generate_image("p"), "/tmp/ok.png")
        self.assertEqual(gen.call_count, 2)
        sl.assert_called_once_with(iu.RETRY_DELAY)

    def test_exhausted_retries_back_off_exponentially_and_return_none(self):
        with _quiet() as out, mock.patch.object(iu, "_comfyui_generate", return_value=None), \
                mock.patch.object(iu.time, "sleep") as sl:
            self.assertIsNone(iu.generate_image("p", section="art"))
            log = out.getvalue()
        self.assertEqual([c.args[0] for c in sl.call_args_list],
                         [iu.RETRY_DELAY * 2 ** k for k in range(iu.MAX_RETRIES - 1)])
        self.assertIn("nova_fix_missing_images.py will backfill", log)

    def test_no_attempt_when_budget_is_too_small(self):
        with _quiet(), mock.patch.object(iu, "_wait_budget", return_value=iu.MIN_ATTEMPT_S - 1), \
                mock.patch.object(iu, "_comfyui_generate") as gen:
            self.assertIsNone(iu.generate_image("p"))
        gen.assert_not_called()

    def test_budget_respects_the_scheduler_task_time_left(self):
        fake_nj = mock.Mock(_task_time_left=mock.Mock(return_value=400))
        with mock.patch.dict(sys.modules, {"nova_journal": fake_nj}):
            self.assertEqual(iu._wait_budget(), 400 - iu.TASK_RESERVE_S)
        fake_nj._task_time_left.return_value = None                       # manual run
        with mock.patch.dict(sys.modules, {"nova_journal": fake_nj}):
            self.assertEqual(iu._wait_budget(), float(iu.TIMEOUT))
        fake_nj._task_time_left.return_value = 10                         # nearly out of time
        with mock.patch.dict(sys.modules, {"nova_journal": fake_nj}):
            self.assertEqual(iu._wait_budget(), 0.0)


class TestUnit(unittest.TestCase):
    def test_rotation_and_model_tables_consistent(self):
        self.assertTrue(set(iu.ART_MODEL_ROTATION.values()) <= set(iu.MODELS))
        self.assertIn(iu.get_model_for_today(), iu.MODELS)
        self.assertEqual(iu.DEFAULT_MODEL, "flux_dev")
        self.assertGreaterEqual(iu.MODELS["flux_dev"]["optimal_steps"], 20)
        self.assertLessEqual(iu.MODELS["flux_dev"]["optimal_steps"], 28)

    def test_random_model_falls_back_to_default(self):
        with mock.patch.object(iu, "_model_available_via_api", return_value=False):
            self.assertEqual(iu.get_random_model(), iu.DEFAULT_MODEL)

    def test_flux_size_scales_up_small_requests_only(self):
        self.assertEqual(iu._flux_size(1024, 768), (1280, 960))
        w, h = iu._flux_size(1024, 1024)
        self.assertEqual((w % 64, h % 64, w), (0, 0, h))
        self.assertGreaterEqual(w * h, iu.FLUX_MIN_PIXELS * 0.95)
        self.assertEqual(iu._flux_size(1600, 1200), (1600, 1200))

    def test_flux_guidance_wired_into_graph(self):
        wf = iu.comfy_workflow("p", "flux1-dev.safetensors", 1280, 960, 24, seed=3)
        self.assertEqual(wf["13"]["inputs"]["guidance"], iu.FLUX_GUIDANCE)
        self.assertEqual(wf["8"]["inputs"]["cfg"], 1.0)


class TestIntegration(unittest.TestCase):
    def setUp(self):
        iu._comfy_models_cache = None

    def tearDown(self):
        iu._comfy_models_cache = None

    def test_model_availability_comes_from_comfyui_and_is_cached(self):
        seen = []

        def uo(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            seen.append(url)
            return _ctx(["flux1-dev.safetensors"] if url.endswith("/diffusion_models") else ["juggernaut.safetensors"])
        with mock.patch.object(iu.urllib.request, "urlopen", side_effect=uo):
            self.assertTrue(iu._model_available_via_api("flux1-dev.safetensors"))
            self.assertTrue(iu._model_available_via_api("juggernaut.safetensors"))
            self.assertFalse(iu._model_available_via_api("ZImage/fp8.safetensors"))
        self.assertEqual(len(seen), 2)                                    # cached after first call
        self.assertTrue(all("7801" not in u for u in seen))               # SwarmUI no longer consulted

    def test_model_availability_unreachable_assumes_available(self):
        with mock.patch.object(iu.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertTrue(iu._model_available_via_api("x"))
        self.assertIsNone(iu._comfy_models_cache)

    def test_unreachable_local_comfyui_is_kickstarted(self):
        with _quiet(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=OSError("refused")), \
                mock.patch.object(iu.sys, "platform", "darwin"), \
                mock.patch.object(iu.subprocess, "run") as run:
            self.assertIsNone(iu._comfyui_generate("p", base="http://127.0.0.1:8188"))
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["launchctl", "kickstart"])
        self.assertTrue(argv[2].endswith("/" + iu.COMFYUI_LAUNCHD_LABEL))
        self.assertNotIn("-k", argv)                                       # never kills a running one

    def test_ensure_backend_restarts_idle_backends(self):
        calls = []
        def uo(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            calls.append(url)
            r = mock.Mock()
            r.read.return_value = json.dumps({"session_id": "s"} if "GetNewSession" in url else
                                             {"b1": {"status": "idle"}}).encode()
            return r
        with _quiet(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=uo), \
                mock.patch.object(iu.time, "sleep") as sl:
            self.assertTrue(iu.ensure_backend())
        self.assertTrue(calls[-1].endswith("/API/RestartBackends"))
        sl.assert_called_once_with(30)


class TestFunctional(unittest.TestCase):
    def test_comfyui_down_publishes_without_cover_and_never_calls_out(self):
        urls = []

        def uo(req, timeout=None):
            urls.append(req.full_url if hasattr(req, "full_url") else req)
            raise OSError("refused")
        with _quiet() as out, mock.patch.dict(os.environ, {"NOVA_COMFYUI_URL": "http://192.0.2.10:8188"}), \
                mock.patch.object(iu.urllib.request, "urlopen", side_effect=uo), \
                mock.patch.object(iu.time, "sleep"), mock.patch.object(iu.subprocess, "run") as run:
            self.assertIsNone(iu.generate_image("a quiet rack", section="burbank-ops"))
            log = out.getvalue()
        self.assertTrue(urls and all(u.startswith("http://192.0.2.10:8188/") for u in urls))
        run.assert_not_called()                                           # not local -> no kickstart
        self.assertIn("no cloud fallback", log)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertNotIn("__main__", SRC)   # library module
        r = subprocess.run([sys.executable, "-c", "import nova_image_utils"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
