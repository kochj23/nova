#!/usr/bin/env python3
"""7-category gap tests for nova_image_utils.py — the standing image-gen retry rule (backend health
check FIRST, then up to 3 attempts with backoff, never fail silently) on the LOCAL-ONLY ComfyUI path
(2026-10-19: no cloud image fallback; a down ComfyUI means "publish without a cover, backfill later").
ComfyUI HTTP is mocked at _comfy_json. Base suites: test_nova_image_utils.py,
test_nova_image_utils_remote.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_image_utils_7cat.py
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_image_utils as iu  # noqa: E402

BASE = "http://192.0.2.10:8188"   # TEST-NET-1: never local, never reachable


class _Comfy:
    """Scripted ComfyUI: records every endpoint hit; `down` makes /system_stats fail."""

    def __init__(self, down=False, fail_job=False):
        self.down, self.fail_job, self.calls = down, fail_job, []

    def __call__(self, url, data=None, timeout=10):
        path = url.split("8188", 1)[1]
        self.calls.append((path, data))
        if path == "/system_stats":
            if self.down:
                raise OSError("refused")
            return {"system": {}}
        if path == "/queue":
            return {"queue_running": [], "queue_pending": []}
        if path == "/prompt":
            return {"prompt_id": "pid"}
        if path == "/history/pid":
            if self.fail_job:
                return {"pid": {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"exception_message": "MPS OOM"}]]}}}
            return {"pid": {"status": {"status_str": "success", "completed": True},
                            "outputs": {"10": {"images": [{"filename": "a.png"}]}}}}
        raise AssertionError(path)

    def paths(self):
        return [p for p, _ in self.calls]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for p in (patch.object(iu, "_log"), patch.object(iu.time, "sleep"),
                  patch.object(iu, "comfyui_url", return_value=BASE),
                  patch.object(iu, "_pick_model", return_value=({"name": "m"}, "flux1-dev.safetensors", 24)),
                  patch.object(iu, "_comfy_download", side_effect=lambda *a: str(Path(self.tmp) / "x.png"))):
            p.start()
            self.addCleanup(p.stop)
        self.log, self.sleep = iu._log, iu.time.sleep

    def msgs(self):
        return [c.args[0] for c in self.log.call_args_list]


class TestSecurity(_Base):
    def test_prompt_travels_as_json_data_never_a_shell(self):
        hostile = 'cat"; rm -rf ~; echo "'
        fc = _Comfy()
        with patch.object(iu, "_comfy_json", side_effect=fc), patch.object(iu.subprocess, "run") as run:
            iu.generate_image(hostile)
        sub = [d for p, d in fc.calls if p == "/prompt"][0]
        self.assertTrue(sub["prompt"]["6"]["inputs"]["text"].startswith(hostile))
        run.assert_not_called()

    def test_no_secrets_or_home_paths_in_source(self):
        src = (SCRIPTS / "nova_image_utils.py").read_text()
        self.assertNotRegex(src, r"sk-or-v1-[A-Za-z0-9]{10}|/Users/[a-z]")


class TestPerformance(_Base):
    def test_attempts_bounded_when_comfyui_down(self):
        fc = _Comfy(down=True)
        with patch.object(iu, "_comfy_json", side_effect=fc):
            self.assertIsNone(iu.generate_image("p"))
        self.assertEqual(fc.paths().count("/system_stats"), iu.MAX_RETRIES)
        self.assertNotIn("/prompt", fc.paths())

    def test_gen_cap_allows_a_cold_flux_load(self):
        self.assertGreaterEqual(iu.REMOTE_GEN_TIMEOUT, 840)      # cold load measured ~14 min
        self.assertGreaterEqual(iu.TIMEOUT, iu.REMOTE_GEN_TIMEOUT)


class TestRetry(_Base):
    def test_rule_constants(self):
        self.assertEqual(iu.MAX_RETRIES, 3)
        self.assertEqual(iu.RETRY_DELAY, 15)

    def test_health_check_happens_before_any_submit(self):
        fc = _Comfy()
        with patch.object(iu, "_comfy_json", side_effect=fc):
            iu.generate_image("p")
        self.assertLess(fc.paths().index("/system_stats"), fc.paths().index("/prompt"))

    def test_job_error_retried_with_backoff_then_logged_not_silent(self):
        fc = _Comfy(fail_job=True)
        with patch.object(iu, "_comfy_json", side_effect=fc):
            self.assertIsNone(iu.generate_image("p"))
        self.assertEqual(fc.paths().count("/prompt"), 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list if c.args[0] >= 15], [15, 30])
        self.assertTrue(any("MPS OOM" in m for m in self.msgs()))
        self.assertIn("no cloud fallback", self.msgs()[-1])


class TestUnit(_Base):
    def test_success_returns_downloaded_path_first_try(self):
        with patch.object(iu, "_comfy_json", side_effect=_Comfy()):
            self.assertEqual(iu.generate_image("p"), str(Path(self.tmp) / "x.png"))
        self.assertTrue(any("attempt 1" in m for m in self.msgs()))


class TestIntegration(_Base):
    def test_flux_request_scaled_to_quality_floor(self):
        fc = _Comfy()
        with patch.object(iu, "_comfy_json", side_effect=fc):
            iu.generate_image("p", width=1024, height=768)
        latent = [d for p, d in fc.calls if p == "/prompt"][0]["prompt"]["5"]["inputs"]
        self.assertEqual((latent["width"], latent["height"]), (1280, 960))


class TestFunctional(_Base):
    def test_shell_script_still_health_checks_first(self):
        sh = (SCRIPTS / "generate_image.sh").read_text()
        self.assertLess(sh.index("/system_stats"), sh.index("/prompt"))


class TestFrame(unittest.TestCase):
    def test_compiles_and_exports(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_image_utils.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("generate_image", "apply_image_safety", "ensure_backend", "comfy_workflow",
                     "get_random_model", "MODELS"):
            self.assertTrue(hasattr(iu, name), name)


if __name__ == "__main__":
    unittest.main()
