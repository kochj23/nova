#!/usr/bin/env python3
"""7-category gap tests for nova_face_recognition.describe_scene() and nova_face_integration.remember()
— the retries added here (both were 'RETRY GAP' single attempts) plus the P3 privacy tagging on face
memories (Proteus rules, 2026-10-08). Ollama, the memory server and Slack are mocked.
Base suites: test_nova_face_recognition.py, test_nova_face_integration.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_face_7cat.py
"""
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_config  # noqa: E402


def _load(name, fname):
    with patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"):
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / fname)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


fr = _load("face_rec_7cat", "nova_face_recognition.py")
fi = _load("face_int_7cat", "nova_face_integration.py")
fr.slack_post = MagicMock(); fr.slack_upload_image = MagicMock(); fr.vector_remember = MagicMock()


def resp(obj):
    m = MagicMock(); m.__enter__.return_value.read.return_value = json.dumps(obj).encode()
    return m


class _Base(unittest.TestCase):
    def setUp(self):
        self.img = Path(tempfile.mkdtemp()) / "f.jpg"; self.img.write_bytes(b"\xff\xd8\xff")
        for p in (patch("time.sleep"), patch.object(fr, "log")):
            p.start(); self.addCleanup(p.stop)
        self.sleep = time.sleep
        self.log = fr.log


class TestSecurity(_Base):
    def test_face_memory_tagged_private(self):
        with patch.object(fi.urllib.request, "urlopen", return_value=resp({"id": "m1"})) as u:
            fi.remember("Jordan seen at door")
        md = json.loads(u.call_args.args[0].data)["metadata"]
        self.assertEqual(md.get("privacy"), "private")

    def test_vision_stays_local(self):
        self.assertRegex(fr.OLLAMA_URL, r"^http://(127\.0\.0\.1|localhost|192\.168\.)")

    def test_description_truncated(self):
        with patch.object(fr.urllib.request, "urlopen", return_value=resp({"response": "x" * 5000})):
            self.assertEqual(len(fr.describe_scene(str(self.img))), 200)


class TestPerformance(_Base):
    def test_bounded_attempts_and_timeouts(self):
        with patch.object(fr.urllib.request, "urlopen", side_effect=OSError("x")) as u:
            fr.describe_scene(str(self.img))
        self.assertEqual(u.call_count, 3)
        self.assertTrue(all(c.kwargs["timeout"] == 30 for c in u.call_args_list))
        with patch.object(fi.urllib.request, "urlopen", side_effect=OSError("x")) as u, redirect_stderr(io.StringIO()):
            fi.remember("t")
        self.assertTrue(all(c.kwargs["timeout"] == 5 for c in u.call_args_list))


class TestRetry(_Base):
    def test_describe_recovers_after_model_swap(self):
        with patch.object(fr.urllib.request, "urlopen", side_effect=[OSError("loading"), resp({"response": " 1 person "})]):
            self.assertEqual(fr.describe_scene(str(self.img)), "1 person")
        self.sleep.assert_called_once_with(2)

    def test_describe_final_failure_logged(self):
        with patch.object(fr.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIsNone(fr.describe_scene(str(self.img)))
        self.assertIn("after 3 tries", self.log.call_args.args[0])

    def test_remember_recovers(self):
        with patch.object(fi.urllib.request, "urlopen", side_effect=[OSError("restart"), resp({"id": "m9"})]):
            self.assertEqual(fi.remember("t"), "m9")

    def test_remember_final_failure_not_silent(self):
        err = io.StringIO()
        with patch.object(fi.urllib.request, "urlopen", side_effect=OSError("down")) as u, redirect_stderr(err):
            self.assertIsNone(fi.remember("t"))
        self.assertEqual(u.call_count, 3)
        self.assertIn("remember failed after 3 tries", err.getvalue())


class TestUnit(_Base):
    def test_unreadable_image_no_network(self):
        with patch.object(fr.urllib.request, "urlopen") as u:
            self.assertIsNone(fr.describe_scene("/nonexistent.jpg"))
        u.assert_not_called()


class TestIntegration(_Base):
    def test_describe_sends_image_to_vision_model(self):
        with patch.object(fr.urllib.request, "urlopen", return_value=resp({"response": "ok"})) as u:
            fr.describe_scene(str(self.img))
        body = json.loads(u.call_args.args[0].data)
        self.assertEqual(body["model"], fr.VISION_MODEL)
        self.assertEqual(len(body["images"]), 1)


class TestFunctional(_Base):
    def test_flaky_then_good_memory_write_returns_id(self):
        with patch.object(fi.urllib.request, "urlopen", side_effect=[OSError("a"), OSError("b"), resp({"id": "z"})]):
            self.assertEqual(fi.remember("t"), "z")
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])


class TestFrame(unittest.TestCase):
    def test_compile(self):
        for f in ("nova_face_recognition.py", "nova_face_integration.py"):
            r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
