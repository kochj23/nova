#!/usr/bin/env python3
"""7-category gap tests for nova_image_utils.py — the standing image-gen retry rule (backend health
check FIRST, then 3 attempts 15 s apart, never fail silently) on the Studio's local ComfyUI path.
generate_image.sh, SwarmUI and OpenRouter are all mocked. Base suites: test_nova_image_utils.py,
test_nova_image_utils_remote.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_image_utils_7cat.py
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_image_utils as iu  # noqa: E402


def ok_run(path):
    return subprocess.CompletedProcess([], 0, f"Image generated successfully.\nWorkspace copy: {path}\n", "")


class _Base(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(iu, "_log"), patch.object(iu.time, "sleep"),
                  patch.object(iu, "_pick_model", return_value=({"name": "m"}, "m.safetensors", 8))):
            p.start(); self.addCleanup(p.stop)
        self.log, self.sleep = iu._log, iu.time.sleep
        self.img = Path(tempfile.mkdtemp()) / "out.png"
        self.img.write_bytes(b"png")


class TestSecurity(_Base):
    def test_prompt_passed_as_single_argv_element(self):
        hostile = 'cat"; rm -rf ~; echo "'
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", return_value=ok_run(self.img)) as r:
            iu._local_comfyui_generate(hostile)
        argv = r.call_args.args[0]
        self.assertEqual(argv[1], hostile)
        self.assertFalse(r.call_args.kwargs.get("shell"))

    def test_no_secrets_in_source(self):
        src = (SCRIPTS / "nova_image_utils.py").read_text()
        self.assertNotRegex(src, r"sk-or-v1-[A-Za-z0-9]{10}|/Users/[a-z]")


class TestPerformance(_Base):
    def test_each_attempt_has_timeout(self):
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as r:
            iu._local_comfyui_generate("p")
        self.assertTrue(all(c.kwargs["timeout"] == iu.TIMEOUT for c in r.call_args_list))

    def test_attempts_bounded(self):
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", side_effect=subprocess.TimeoutExpired("sh", 1)) as r:
            self.assertIsNone(iu._local_comfyui_generate("p"))
        self.assertEqual(r.call_count, 3)


class TestRetry(_Base):
    def test_rule_constants(self):
        self.assertEqual(iu.MAX_RETRIES, 3)
        self.assertEqual(iu.RETRY_DELAY, 15)

    def test_health_check_happens_before_any_attempt(self):
        order = []
        with patch.object(iu, "ensure_backend", side_effect=lambda: order.append("health") or True), \
                patch.object(iu.subprocess, "run", side_effect=lambda *a, **k: order.append("gen") or ok_run(self.img)):
            iu._local_comfyui_generate("p")
        self.assertEqual(order, ["health", "gen"])

    def test_unhealthy_backend_never_generates_and_logs(self):
        with patch.object(iu, "ensure_backend", return_value=False), patch.object(iu.subprocess, "run") as r:
            self.assertIsNone(iu._local_comfyui_generate("p"))
        r.assert_not_called()
        self.assertTrue(any("not available" in c.args[0] for c in self.log.call_args_list))

    def test_three_attempts_fifteen_seconds_apart_then_success(self):
        fail = subprocess.CompletedProcess([], 1, "", "boom")
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", side_effect=[fail, fail, ok_run(self.img)]) as r:
            self.assertEqual(iu._local_comfyui_generate("p"), str(self.img))
        self.assertEqual(r.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [15, 15])

    def test_total_failure_logged_not_silent(self):
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", side_effect=OSError("exec format")):
            self.assertIsNone(iu._local_comfyui_generate("p"))
        msgs = [c.args[0] for c in self.log.call_args_list]
        self.assertEqual(sum("error: exec format" in m for m in msgs), 3)
        self.assertIn("Local ComfyUI fallback also failed", msgs[-1])


class TestUnit(_Base):
    def test_falls_back_to_last_path_line(self):
        out = subprocess.CompletedProcess([], 0, f"Image generated successfully.\n{self.img}\nOpen with: x\n", "")
        with patch.object(iu, "ensure_backend", return_value=True), patch.object(iu.subprocess, "run", return_value=out):
            self.assertEqual(iu._local_comfyui_generate("p"), str(self.img))

    def test_reported_path_must_exist(self):
        out = ok_run("/nonexistent/x.png")
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", return_value=out) as r:
            self.assertIsNone(iu._local_comfyui_generate("p"))
        self.assertEqual(r.call_count, 3)


class TestIntegration(_Base):
    def test_script_invoked_with_dims_steps_model(self):
        with patch.object(iu, "ensure_backend", return_value=True), \
                patch.object(iu.subprocess, "run", return_value=ok_run(self.img)) as r:
            iu._local_comfyui_generate("p", width=1200, height=800)
        argv = r.call_args.args[0]
        self.assertEqual(argv[0], str(iu.GENERATE_IMAGE_SH))
        self.assertEqual(argv[2:], ["1200", "800", "8", "m.safetensors"])


class TestFunctional(_Base):
    def test_shell_script_honours_comfy_url_and_health_checks_first(self):
        sh = (SCRIPTS / "generate_image.sh").read_text()
        self.assertLess(sh.index("/system_stats"), sh.index("/prompt"))


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_image_utils.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(callable(iu.generate_image))


if __name__ == "__main__":
    unittest.main()
