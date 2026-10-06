#!/usr/bin/env python3
"""Tests for nova_image_utils.py (LAN ComfyUI backend selection, 2026-10-06) — the 7 house
categories (Security, Performance, Retry, Unit, Integration, Functional, Frame).
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import ipaddress
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
SH = (SCRIPTS / "generate_image.sh").read_text()
BASE = "http://192.0.2.10:8188"          # TEST-NET-1: never a local address
PNG = b"\x89PNG\r\n\x1a\n fake"


def _load():
    spec = importlib.util.spec_from_file_location("nova_image_utils_rt", SCRIPTS / "nova_image_utils.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iu = _load()


def _quiet():
    return mock.patch("sys.stdout", new_callable=io.StringIO)


class _Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeComfy:
    """Routes urlopen() by path. `history` is a list consumed one item per poll (last repeats)."""

    def __init__(self, history, queue_running=None, queue_pending=None, fail_paths=None):
        self.history = list(history)
        self.queue_running = queue_running or []
        self.queue_pending = queue_pending or []
        self.fail_paths = dict(fail_paths or {})   # path prefix -> remaining failures
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        data = getattr(req, "data", None)
        path = url.split("8188", 1)[1]
        self.calls.append((path, json.loads(data) if data else None))
        for pfx, n in list(self.fail_paths.items()):
            if path.startswith(pfx) and n > 0:
                self.fail_paths[pfx] = n - 1
                raise OSError("connection reset")
        if path == "/system_stats":
            return _Resp({"system": {"os": "darwin"}})
        if path == "/queue" and data is None:
            return _Resp({"queue_running": self.queue_running, "queue_pending": self.queue_pending})
        if path == "/queue":
            return _Resp({})
        if path == "/prompt":
            return _Resp({"prompt_id": "pid-1", "number": 1})
        if path.startswith("/history/"):
            h = self.history.pop(0) if len(self.history) > 1 else self.history[0]
            return _Resp(h)
        if path.startswith("/view?"):
            return _Resp(PNG)
        if path == "/interrupt":
            return _Resp({})
        raise AssertionError(f"unexpected {path}")

    def paths(self):
        return [p for p, _ in self.calls]


DONE = {"pid-1": {"status": {"status_str": "success", "completed": True},
                  "outputs": {"10": {"images": [{"filename": "1400_00001_.png", "subfolder": "", "type": "output"}]}}}}
FAILED = {"pid-1": {"status": {"status_str": "error", "completed": False,
                               "messages": [["execution_error", {"exception_message": "OOM"}]]}}}


class _Env:
    """Temp HOME + no PG + fast clock; model choice pinned so SwarmUI is never asked."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        (self.home / ".openclaw/workspace").mkdir(parents=True)
        self.ps = [mock.patch.object(Path, "home", return_value=self.home),
                   mock.patch.object(iu, "_model_available_via_api", return_value=True),
                   mock.patch.object(iu.time, "sleep"),
                   mock.patch.dict(os.environ, {"NOVA_COMFYUI_URL": BASE}),
                   _quiet()]
        self.out = None
        for p in self.ps:
            r = p.start()
            if isinstance(r, io.StringIO):
                self.out = r
        return self

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()
        self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertFalse(pat.search(SRC))

    def test_default_comfyui_is_private_lan_never_wildcard(self):
        host = iu.urllib.parse.urlparse(iu.COMFYUI_DEFAULT_URL).hostname
        self.assertTrue(ipaddress.ip_address(host).is_private)
        self.assertNotEqual(host, "0.0.0.0")
        self.assertTrue(iu.COMFYUI_DEFAULT_URL.startswith("http://192.168.1."))

    def test_service_config_query_is_parameterized(self):
        self.assertIn('"SELECT value FROM service_config WHERE service = %s AND key = %s"', SRC)
        self.assertNotRegex(SRC, r'execute\(f"[^"]*service_config')


class TestPerformance(unittest.TestCase):
    def test_workflow_build_10k_under_one_second(self):
        t = time.perf_counter()
        for i in range(10_000):
            iu.comfy_workflow(f"prompt {i}", "flux1-dev.safetensors" if i % 2 else "x.safetensors", 1024, 768, 20, seed=i)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_queue_wait_is_bounded_by_budget(self):
        # A job that never leaves the queue must end at TIMEOUT, not loop forever.
        clock = iter(range(0, 10_000, 50))
        fc = FakeComfy([{}], queue_pending=[[1, "pid-1"]])
        with _Env(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=fc), \
                mock.patch.object(iu.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertIsNone(iu._remote_comfyui_generate("p", base=BASE))
        self.assertLess(fc.paths().count("/queue"), 40)


class TestRetry(unittest.TestCase):
    def test_transient_poll_errors_are_tolerated(self):
        fc = FakeComfy([{}, DONE], queue_running=[[1, "pid-1"]], fail_paths={"/history/": 2})
        with _Env(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=fc):
            out = iu._remote_comfyui_generate("p", base=BASE)
        self.assertTrue(out and Path(out).name.startswith("comfy_"))
        self.assertGreaterEqual(fc.paths().count("/history/pid-1"), 3)   # 2 failures + success
        self.assertEqual(fc.paths().count("/prompt"), 1)                 # never resubmitted

    def test_lost_contact_gives_up_after_max_poll_errors(self):
        # RETRY GAP: _remote_comfyui_generate submits once by design (no flooding); polls
        # tolerate REMOTE_MAX_POLL_ERRORS consecutive failures, then fail open to OpenRouter.
        fc = FakeComfy([{}], fail_paths={"/history/": 99})
        with _Env(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=fc):
            self.assertIsNone(iu._remote_comfyui_generate("p", base=BASE))
        self.assertEqual(fc.paths().count("/history/pid-1"), iu.REMOTE_MAX_POLL_ERRORS)


class TestUnit(unittest.TestCase):
    def test_is_local(self):
        self.assertTrue(iu.comfyui_is_local("http://127.0.0.1:8188"))
        self.assertTrue(iu.comfyui_is_local("http://localhost:8188"))
        self.assertFalse(iu.comfyui_is_local(BASE))
        self.assertFalse(iu.comfyui_is_local("not a url"))

    def test_url_env_override_beats_pg(self):
        with mock.patch.dict(os.environ, {"NOVA_COMFYUI_URL": "http://10.0.0.9:8188/"}), \
                mock.patch("psycopg2.connect") as conn:
            self.assertEqual(iu.comfyui_url(), "http://10.0.0.9:8188")
        conn.assert_not_called()

    def test_url_from_service_config_then_default_on_pg_failure(self):
        cur = mock.MagicMock()
        cur.fetchone.return_value = ({"url": "http://192.168.1.9:8188"},)
        c = mock.MagicMock()
        c.cursor.return_value.__enter__.return_value = cur
        env = {k: v for k, v in os.environ.items() if k != "NOVA_COMFYUI_URL"}
        with mock.patch.dict(os.environ, env, clear=True), _quiet():
            iu._comfyui_url_cache = None
            with mock.patch("psycopg2.connect", return_value=c):
                self.assertEqual(iu.comfyui_url(), "http://192.168.1.9:8188")
            self.assertEqual(cur.execute.call_args[0][1], ("image_gen", "comfyui"))
            iu._comfyui_url_cache = None
            with mock.patch("psycopg2.connect", side_effect=Exception("pg down")):
                self.assertEqual(iu.comfyui_url(), iu.COMFYUI_DEFAULT_URL)
            iu._comfyui_url_cache = None

    def test_workflow_shapes(self):
        f = iu.comfy_workflow("a", "flux1-dev.safetensors", 1024, 768, 20, seed=7)
        s = iu.comfy_workflow("a", "Juggernaut_X_RunDiffusion_Hyper.safetensors", 512, 512, 8, seed=7)
        self.assertEqual(f["4"]["class_type"], "UNETLoader")
        self.assertEqual(f["8"]["inputs"]["cfg"], 1.0)
        self.assertEqual(s["4"]["class_type"], "CheckpointLoaderSimple")
        self.assertIn("nudity", s["7"]["inputs"]["text"])
        self.assertEqual((s["5"]["inputs"]["width"], f["8"]["inputs"]["seed"]), (512, 7))


class TestIntegration(unittest.TestCase):
    def test_non_studio_host_routes_to_lan_comfyui_then_openrouter(self):
        with _Env() as e, mock.patch.object(iu, "_remote_comfyui_generate", return_value=None) as rem, \
                mock.patch.object(iu, "_local_comfyui_generate") as loc, \
                mock.patch.object(iu, "_openrouter_generate", return_value="/tmp/or.png") as orr:
            self.assertEqual(iu.generate_image("a young girl"), "/tmp/or.png")
        loc.assert_not_called()
        self.assertEqual(rem.call_args[0][-1], BASE)
        self.assertIn(iu._SAFETY_SENTINEL, rem.call_args[0][0])       # remote gets the safe prompt
        self.assertIn("backend=openrouter", e.out.getvalue())

    def test_studio_host_keeps_shell_path(self):
        with _Env(), mock.patch.dict(os.environ, {"NOVA_COMFYUI_URL": "http://127.0.0.1:8188"}), \
                mock.patch.object(iu, "_local_comfyui_generate", return_value="/tmp/l.png") as loc, \
                mock.patch.object(iu, "_remote_comfyui_generate") as rem:
            self.assertEqual(iu.generate_image("p"), "/tmp/l.png")
        loc.assert_called_once()
        rem.assert_not_called()

    def test_generate_image_sh_uses_shared_workflow_and_comfy_url(self):
        self.assertIn("from nova_image_utils import comfy_workflow", SH)
        self.assertNotIn('COMFY = "http://127.0.0.1:8188"', SH)
        self.assertIn('"$COMFY_URL" "$SCRIPT_DIR"', SH)


class TestFunctional(unittest.TestCase):
    def test_golden_path_queues_waits_and_saves(self):
        fc = FakeComfy([{}, {}, DONE], queue_running=[[1, "other"]], queue_pending=[[2, "pid-1"]])
        with _Env() as e, mock.patch.object(iu.urllib.request, "urlopen", side_effect=fc), \
                mock.patch.object(iu, "_openrouter_generate") as orr:
            out = iu.generate_image("a lighthouse")
            saved = Path(out).read_bytes() if out else b""
            log = e.out.getvalue()
        orr.assert_not_called()
        self.assertEqual(saved, PNG)
        self.assertIn(".openclaw/workspace/comfy_", out)
        self.assertIn("2 job(s) ahead", log)
        self.assertIn("backend=comfyui-remote", log)
        sub = [d for p, d in fc.calls if p == "/prompt"][0]
        self.assertEqual(sub["prompt"]["4"]["inputs"]["unet_name"], iu.MODELS[iu.DEFAULT_MODEL]["file"])

    def test_job_error_falls_back_to_openrouter(self):
        fc = FakeComfy([FAILED])
        with _Env() as e, mock.patch.object(iu.urllib.request, "urlopen", side_effect=fc), \
                mock.patch.object(iu, "_openrouter_generate", return_value="/tmp/or.png") as orr:
            self.assertEqual(iu.generate_image("p"), "/tmp/or.png")
        orr.assert_called_once()
        self.assertIn("OOM", e.out.getvalue())

    def test_unreachable_skips_submit(self):
        def down(req, timeout=None):
            raise OSError("refused")
        with _Env(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=down) as uo:
            self.assertIsNone(iu._remote_comfyui_generate("p", base=BASE))
        self.assertEqual(uo.call_count, 1)                               # health probe only
        self.assertEqual(uo.call_args.kwargs["timeout"], iu.REMOTE_HEALTH_TIMEOUT)

    def test_timeout_withdraws_queued_job_but_leaves_running_one(self):
        for running, expect_delete in ((False, True), (True, False)):
            clock = iter(range(0, 100_000, 100))
            fc = FakeComfy([{}], queue_running=[[1, "pid-1"]] if running else [],
                           queue_pending=[] if running else [[1, "pid-1"]])
            with _Env(), mock.patch.object(iu.urllib.request, "urlopen", side_effect=fc), \
                    mock.patch.object(iu.time, "monotonic", side_effect=lambda: next(clock)):
                self.assertIsNone(iu._remote_comfyui_generate("p", base=BASE))
            deletes = [d for p, d in fc.calls if p == "/queue" and d]
            self.assertEqual(bool(deletes), expect_delete)
            self.assertNotIn("/interrupt", fc.paths())


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        code = ("import sys, urllib.request; sys.path.insert(0, %r)\n"
                "def boom(*a, **k): raise SystemExit('network at import')\n"
                "urllib.request.urlopen = boom\n"
                "import nova_image_utils as m; print(m._comfyui_url_cache)") % str(SCRIPTS)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "None")


if __name__ == "__main__":
    unittest.main()
