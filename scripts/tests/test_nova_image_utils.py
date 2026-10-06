#!/usr/bin/env python3
"""Tests for nova_image_utils.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import base64
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
PNG = base64.b64encode(b"\x89PNG fake").decode()


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

    def test_openrouter_key_comes_from_config_and_rides_in_header(self):
        with _Home(), _quiet(), mock.patch("nova_config.openrouter_api_key", return_value="sk-test"), \
                mock.patch.object(iu.urllib.request, "urlopen",
                                  return_value=_ctx({"choices": [{"message": {"images": []}}]})) as uo:
            iu._openrouter_generate("a lighthouse")
        req = uo.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer sk-test")
        self.assertNotIn("sk-test", req.data.decode())

    def test_safety_policy_rewrites_youth_and_undress_cues(self):
        out = iu.apply_image_safety("a young girl in a bikini with little kids")
        for word in ("young", "girl", "bikini", "kids", "little"):
            self.assertNotRegex(out.lower(), rf"\b{word}\b")
        self.assertIn(iu._SAFETY_SENTINEL, out)
        self.assertEqual(iu.apply_image_safety(out), out)     # idempotent
        self.assertEqual(iu.apply_image_safety(""), "")


class TestPerformance(unittest.TestCase):
    def test_safety_pass_10k_prompts(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            iu.apply_image_safety(f"server rack number {i} glowing at dusk")
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_local_retries_with_delay_then_succeeds(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as img, _quiet(), \
                mock.patch.object(iu, "ensure_backend", return_value=True), \
                mock.patch.object(iu, "_model_available_via_api", return_value=True), \
                mock.patch.object(iu.subprocess, "run", side_effect=[
                    mock.Mock(returncode=1, stdout=""),
                    mock.Mock(returncode=0, stdout=f"Workspace copy: {img.name}\nOpen with: x")]) as run, \
                mock.patch.object(iu.time, "sleep") as sl:
            self.assertEqual(iu._local_comfyui_generate("p"), img.name)
        self.assertEqual(run.call_count, 2)
        sl.assert_called_once_with(iu.RETRY_DELAY)

    def test_local_exhausts_retries_then_openrouter_fallback(self):
        with _quiet(), mock.patch.object(iu, "ensure_backend", return_value=True), \
                mock.patch.object(iu, "_model_available_via_api", return_value=True), \
                mock.patch.object(iu.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("x", 1)) as run, \
                mock.patch.object(iu.time, "sleep"), \
                mock.patch.object(iu, "_openrouter_generate", return_value="/tmp/or.png") as orr:
            self.assertEqual(iu.generate_image("p", section="art"), "/tmp/or.png")
        self.assertEqual(run.call_count, iu.MAX_RETRIES)
        self.assertIn(iu._SAFETY_SENTINEL, orr.call_args[0][0])   # fallback also gets the safe prompt

    def test_backend_down_and_openrouter_error_fail_open(self):
        with _quiet(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=OSError("down")), \
                mock.patch("nova_config.openrouter_api_key", return_value="k"), \
                mock.patch.object(iu.subprocess, "run") as run:
            self.assertFalse(iu.ensure_backend())
            self.assertIsNone(iu.generate_image("p"))
            self.assertTrue(iu._model_available_via_api("x"))     # unknown -> assume available
        run.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_rotation_and_model_tables_consistent(self):
        self.assertTrue(set(iu.ART_MODEL_ROTATION.values()) <= set(iu.MODELS))
        self.assertIn(iu.get_model_for_today(), iu.MODELS)
        self.assertTrue(set(iu.SECTION_MODEL_MAP.values()) <= set(iu.OPENROUTER_MODELS))
        self.assertIn(iu.DEFAULT_MODEL, iu.MODELS)

    def test_random_model_falls_back_to_default(self):
        with mock.patch.object(iu, "_model_available_via_api", return_value=False):
            self.assertEqual(iu.get_random_model(), iu.DEFAULT_MODEL)

    def test_openrouter_no_key_returns_none(self):
        with _quiet(), mock.patch("nova_config.openrouter_api_key", return_value=""), \
                mock.patch.object(iu.urllib.request, "urlopen") as uo:
            self.assertIsNone(iu._openrouter_generate("p"))
        uo.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_local_passes_model_file_and_optimal_steps_to_shell_script(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as img, _quiet(), \
                mock.patch.object(iu, "ensure_backend", return_value=True), \
                mock.patch.object(iu, "_model_available_via_api", return_value=False), \
                mock.patch.object(iu.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout=f"junk\n{img.name}\n")) as run:
            self.assertEqual(iu._local_comfyui_generate("p", model="longcat"), img.name)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], str(iu.GENERATE_IMAGE_SH))
        d = iu.MODELS[iu.DEFAULT_MODEL]                      # unavailable model -> default
        self.assertEqual(argv[-2:], [str(d["optimal_steps"]), d["file"]])

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
    def test_openrouter_saves_base64_image_under_workspace(self):
        payload = {"choices": [{"message": {"images": [{"image_url": {"url": f"data:image/png;base64,{PNG}"}}]}}]}
        with _Home() as home, _quiet(), mock.patch("nova_config.openrouter_api_key", return_value="k"), \
                mock.patch.object(iu.urllib.request, "urlopen", return_value=_ctx(payload)) as uo:
            out = iu._openrouter_generate("a quiet rack", section="dreams")
            self.assertTrue(out.startswith(str(home / ".openclaw/workspace")))
            self.assertEqual(Path(out).read_bytes(), b"\x89PNG fake")
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["model"], iu.OPENROUTER_MODELS["cinematic"]["id"])
        self.assertEqual(body["max_tokens"], 8000)

    def test_openrouter_content_array_and_empty_choices(self):
        payload = {"choices": [{"message": {"content": [{"type": "image_url",
                                                          "image_url": {"url": f"data:image/png;base64,{PNG}"}}]}}]}
        with _Home(), _quiet(), mock.patch("nova_config.openrouter_api_key", return_value="k"):
            with mock.patch.object(iu.urllib.request, "urlopen", return_value=_ctx(payload)):
                self.assertTrue(Path(iu._openrouter_generate("p")).exists())
            with mock.patch.object(iu.urllib.request, "urlopen", return_value=_ctx({"choices": []})):
                self.assertIsNone(iu._openrouter_generate("p"))


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertNotIn("__main__", SRC)   # library module
        r = subprocess.run([sys.executable, "-c", "import nova_image_utils"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
