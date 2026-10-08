#!/usr/bin/env python3
"""7-category tests for generate_image.sh (the Studio's ComfyUI client). The functional tests run the
real script against a FAKE ComfyUI on 127.0.0.1 (random port) with HOME pointed at a temp dir, so no
image backend, no LAN host and no real output folder is touched. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_generate_image_sh_7cat.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SH = SCRIPTS / "generate_image.sh"
SRC = SH.read_text()
HAVE_TOOLS = bool(shutil.which("bash") and shutil.which("curl") and shutil.which("python3"))


class FakeComfy(BaseHTTPRequestHandler):
    """history_plan: list of 'flake' | 'pending' | 'success' | 'error' consumed per /history poll."""
    plan, prompts = [], []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if self.path == "/system_stats":
            return self._send(200, {"system": {}})
        if self.path.startswith("/history/"):
            step = FakeComfy.plan.pop(0) if FakeComfy.plan else "pending"
            if step == "flake":
                return self._send(500, {"error": "busy"})
            if step == "pending":
                return self._send(200, {})
            if step == "error":
                return self._send(200, {"pid": {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"exception_message": "OOM"}]]}}})
            return self._send(200, {"pid": {"status": {"status_str": "success", "completed": True},
                                            "outputs": {"9": {"images": [{"filename": "t_0001.png", "subfolder": ""}]}}}})
        if self.path.startswith("/view"):
            return self._send(200, b"\x89PNG fake", "image/png")
        self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        FakeComfy.prompts.append(json.loads(self.rfile.read(n)))
        self._send(200, {"prompt_id": "pid"})


class _Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_sh(self, plan, *args, url=None):
        FakeComfy.plan, FakeComfy.prompts = list(plan), []
        home = tempfile.mkdtemp()
        (Path(home) / ".openclaw/workspace").mkdir(parents=True)
        env = {**os.environ, "HOME": home, "COMFY_URL": url or self.url}
        t = time.perf_counter()
        r = subprocess.run(["bash", str(SH), *args], capture_output=True, text=True, env=env, timeout=120)
        return r, Path(home), time.perf_counter() - t


class TestSecurity(unittest.TestCase):
    def test_syntax_and_strict_mode(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SH)]).returncode, 0)
        self.assertIn("set -euo pipefail", SRC)

    def test_prompt_never_interpolated_into_code(self):
        # the prompt reaches Python as argv, never spliced into the heredoc (quoted 'PYEOF')
        self.assertIn("<<'PYEOF'", SRC)
        self.assertIn('python3 - "$PROMPT"', SRC)

    def test_no_secrets_or_user_paths(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]|api[_-]?key\s*=|Bearer ")

    def test_view_params_url_encoded(self):
        self.assertIn("urllib.parse.quote(fname)", SRC)


class TestPerformance(unittest.TestCase):
    def test_every_http_call_bounded(self):
        self.assertIn("--max-time 5", SRC)
        for t in ("timeout=15", "timeout=5", "timeout=30"):
            self.assertIn(t, SRC)
        self.assertIn("TIMEOUT=600", SRC)

    def test_poll_errors_capped(self):
        self.assertRegex(SRC, r"poll_errors >= 5")


@unittest.skipUnless(HAVE_TOOLS, "bash/curl/python3 needed")
class TestRetry(_Live):
    def test_history_flake_is_retried_not_fatal(self):
        r, home, _ = self.run_sh(["flake", "success"], "a lighthouse")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Workspace copy:", r.stdout)

    def test_health_check_failure_fails_fast_and_loud(self):
        r, _, dt = self.run_sh([], "p", url="http://127.0.0.1:9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ComfyUI not responding", r.stderr)
        self.assertEqual(FakeComfy.prompts, [])
        self.assertLess(dt, 15)


class TestUnit(unittest.TestCase):
    def test_defaults(self):
        for d in ('WIDTH="${2:-1024}"', 'HEIGHT="${3:-1024}"', 'STEPS="${4:-8}"', 'COMFY_URL="${COMFY_URL:-'):
            self.assertIn(d, SRC)

    def test_download_not_nested_under_missing_file_branch(self):
        lines = SRC.splitlines()
        i = next(n for n, l in enumerate(lines) if "dest = Path(WORKSPACE) / fname" in l)
        j = next(n for n, l in enumerate(lines) if "if not src.exists():" in l)
        indent = lambda s: len(s) - len(s.lstrip())
        self.assertEqual(indent(lines[i]), indent(lines[j]))


@unittest.skipUnless(HAVE_TOOLS, "bash/curl/python3 needed")
class TestIntegration(_Live):
    def test_workflow_from_shared_builder_is_submitted(self):
        r, _, _ = self.run_sh(["success"], "a red fox", "512", "512", "6", "flux1-dev.safetensors")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeComfy.prompts), 1)
        self.assertIn("a red fox", json.dumps(FakeComfy.prompts[0]["prompt"]))


@unittest.skipUnless(HAVE_TOOLS, "bash/curl/python3 needed")
class TestFunctional(_Live):
    def test_golden_writes_workspace_and_swarm_copy(self):
        r, home, _ = self.run_sh(["pending", "success"], "a lighthouse at dusk")
        self.assertEqual(r.returncode, 0, r.stderr)
        dest = re.search(r"^Workspace copy: (.+)$", r.stdout, re.M).group(1)
        self.assertTrue(dest.startswith(str(home)))
        self.assertEqual(Path(dest).read_bytes(), b"\x89PNG fake")
        self.assertTrue(list((home / "AI/SwarmUI/Output/local/raw").rglob("t_0001.png")))

    def test_execution_error_exits_nonzero_with_reason(self):
        r, _, _ = self.run_sh(["error"], "p")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("OOM", r.stderr)


class TestFrame(unittest.TestCase):
    def test_no_prompt_prints_usage(self):
        r = subprocess.run(["bash", str(SH)], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stderr)


if __name__ == "__main__":
    unittest.main()
