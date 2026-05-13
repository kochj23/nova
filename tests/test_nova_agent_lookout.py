"""
test_nova_agent_lookout.py — All 7 test categories for nova_agent_lookout.py
Written by Jordan Koch.
"""

import asyncio
import base64
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_lookout.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg
_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod
_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"; _logger_mock.LOG_ERROR = "ERROR"; _logger_mock.LOG_WARN = "WARN"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock


class _MockSubAgent:
    name = "base"; model = "test"; backend = "ollama"; channels = []
    description = ""; temperature = 0.3; max_tokens = 4096

    def __init__(self):
        self._redis = MagicMock(); self._pubsub = MagicMock()
        self._running = False; self._task_count = 0
        self._start_time = None; self._last_error = None

    def run(self): pass
    def _register(self): pass
    def _deregister(self): pass
    async def infer(self, prompt, system="", **kwargs): return ""
    async def recall(self, query, n=5, source=None): return []
    async def remember(self, text, source="", metadata=None): pass
    async def notify(self, message, channel=None): pass
    async def report_to_jordan(self, message): pass
    async def _slack_post(self, message, channel=None): pass
    async def _publish_result(self, task, result): pass


_subagent_mock = MagicMock()
_subagent_mock.SubAgent = _MockSubAgent
sys.modules["nova_subagent"] = _subagent_mock

_spec = importlib.util.spec_from_file_location("nova_agent_lookout", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

LookoutAgent = _mod.LookoutAgent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

# Minimal 1x1 white PNG in base64
_TINY_PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode()


class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_image_not_sent_to_cloud(self):
        """Vision inference must use local Ollama, not external API."""
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1:11434", src, "Vision inference must use local Ollama")
        self.assertNotIn("api.openai.com", src)

    def test_vehicle_detections_suppressed(self):
        """Lookout must suppress vehicle/license-plate detections (privacy)."""
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value=json.dumps({
            "description": "A car on the street",
            "anomaly_detected": True,
            "anomaly_type": "vehicle",
            "severity": "medium",
            "confidence": 0.9,
            "flag_jordan": False,
            "details": "",
        }))

        task = {"image_base64": _TINY_PNG_B64, "type": "camera", "camera": "street"}
        result = _run(agent.handle(task))

        self.assertFalse(result.get("anomaly_detected"),
                         "Vehicle detections must be suppressed for privacy")
        agent.notify.assert_not_called()
        agent.report_to_jordan.assert_not_called()


class TestPerformance(unittest.TestCase):

    def test_lookout_model_is_vision(self):
        agent = LookoutAgent()
        self.assertIn("vl", agent.model.lower(), "Lookout must use a vision model")

    def test_infer_vision_sends_image_in_payload(self):
        """_infer_vision() must include image in Ollama payload."""
        agent = LookoutAgent()
        captured = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            r = MagicMock()
            r.__enter__ = lambda s: r
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps({"response": "test"}).encode()
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run(agent._infer_vision("What is in this image?", _TINY_PNG_B64))

        self.assertTrue(len(captured) > 0)
        self.assertIn("images", captured[0], "Payload must include 'images' field")
        self.assertEqual(captured[0]["images"][0], _TINY_PNG_B64)


class TestRetry(unittest.TestCase):

    def test_handle_returns_none_on_inference_failure(self):
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(side_effect=OSError("Ollama vision down"))

        task = {"image_base64": _TINY_PNG_B64, "type": "camera", "camera": "front"}
        result = _run(agent.handle(task))
        self.assertIsNone(result)

    def test_handle_returns_none_when_no_image(self):
        agent = LookoutAgent()
        task = {"type": "camera", "camera": "front"}
        result = _run(agent.handle(task))
        self.assertIsNone(result)

    def test_handle_returns_none_on_file_read_failure(self):
        agent = LookoutAgent()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            fname = f.name
        import os
        os.unlink(fname)  # File gone

        task = {"image_path": fname, "type": "camera", "camera": "front"}
        result = _run(agent.handle(task))
        self.assertIsNone(result)


class TestUnit(unittest.TestCase):

    def test_handle_extracts_camera_and_source_type(self):
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value=json.dumps({
            "description": "Quiet scene",
            "anomaly_detected": False,
            "anomaly_type": "none",
            "severity": "none",
            "confidence": 0.1,
            "flag_jordan": False,
            "details": "",
        }))

        task = {"image_base64": _TINY_PNG_B64, "type": "motion", "camera": "backyard"}
        result = _run(agent.handle(task))
        self.assertEqual(result["camera"], "backyard")
        self.assertEqual(result["source_type"], "motion")

    def test_critical_anomaly_reports_to_jordan(self):
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value=json.dumps({
            "description": "Unknown person at front door",
            "anomaly_detected": True,
            "anomaly_type": "person",
            "severity": "critical",
            "confidence": 0.95,
            "flag_jordan": True,
            "details": "Unrecognized individual attempting entry",
        }))

        task = {"image_base64": _TINY_PNG_B64, "type": "motion", "camera": "front_door"}
        _run(agent.handle(task))
        agent.report_to_jordan.assert_called_once()

    def test_no_anomaly_does_not_notify(self):
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value=json.dumps({
            "description": "Normal outdoor scene",
            "anomaly_detected": False,
            "anomaly_type": "none",
            "severity": "none",
            "confidence": 0.1,
            "flag_jordan": False,
            "details": "",
        }))

        task = {"image_base64": _TINY_PNG_B64, "type": "motion", "camera": "backyard"}
        _run(agent.handle(task))
        agent.notify.assert_not_called()
        agent.report_to_jordan.assert_not_called()

    def test_fallback_result_on_json_decode_error(self):
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value="Plain text description, not JSON.")

        task = {"image_base64": _TINY_PNG_B64, "type": "camera", "camera": "test"}
        result = _run(agent.handle(task))
        self.assertIsNotNone(result)
        self.assertFalse(result.get("anomaly_detected"))

    def test_reads_image_from_path(self):
        """handle() must read image from file when image_path provided."""
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value=json.dumps({
            "description": "test", "anomaly_detected": False,
            "anomaly_type": "none", "severity": "none",
            "confidence": 0.0, "flag_jordan": False, "details": "",
        }))

        import os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake image data")
            fname = f.name

        try:
            task = {"image_path": fname, "type": "camera", "camera": "test"}
            result = _run(agent.handle(task))
            self.assertIsNotNone(result)
        finally:
            os.unlink(fname)


class TestIntegration(unittest.TestCase):

    def test_license_plate_suppressed(self):
        agent = LookoutAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent._infer_vision = AsyncMock(return_value=json.dumps({
            "description": "License plate ABC-123 visible",
            "anomaly_detected": True,
            "anomaly_type": "licenseplate",
            "severity": "low",
            "confidence": 0.8,
            "flag_jordan": False,
            "details": "",
        }))
        task = {"image_base64": _TINY_PNG_B64, "type": "camera", "camera": "driveway"}
        result = _run(agent.handle(task))
        self.assertFalse(result.get("anomaly_detected"))


class TestFunctional(unittest.TestCase):

    def test_channels_include_vision_camera_motion(self):
        agent = LookoutAgent()
        for ch in ["vision", "camera", "motion"]:
            self.assertIn(ch, agent.channels)

    def test_system_prompt_defines_json_output(self):
        self.assertIn("JSON", _mod.SYSTEM_PROMPT)
        self.assertIn("anomaly_detected", _mod.SYSTEM_PROMPT)
        self.assertIn("flag_jordan", _mod.SYSTEM_PROMPT)


class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_instantiates(self):
        agent = LookoutAgent()
        self.assertEqual(agent.name, "lookout")

    def test_infer_vision_method_exists(self):
        agent = LookoutAgent()
        self.assertTrue(callable(agent._infer_vision))


if __name__ == "__main__":
    unittest.main(verbosity=2)
