#!/usr/bin/env python3
"""Tests for nova_agent_lookout.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_agent_lookout.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lk = _load("lk", SCRIPT)
lk.log = MagicMock()                       # nova_logger.log appends to ~/.openclaw/logs/nova.jsonl; never from a test


def _agent(response="", exc=None):
    """A LookoutAgent without SubAgent.__init__ (no Redis), with Ollama and the Slack bus stubbed."""
    a = lk.LookoutAgent.__new__(lk.LookoutAgent)
    a._infer_vision = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=response)
    a.report_to_jordan = AsyncMock(); a.notify = AsyncMock()
    return a


def _handle(agent, task):
    lk.log.reset_mock()
    return asyncio.run(agent.handle(task))


def _img():
    p = Path(tempfile.mkdtemp()) / "cam.jpg"; p.write_bytes(b"\xff\xd8jpegbytes"); return p


ANOMALY = json.dumps({"description": "unknown person at gate", "anomaly_detected": True, "anomaly_type": "person",
                      "severity": "high", "confidence": 0.9, "details": "hooded figure", "flag_jordan": True})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_ollama_is_loopback_only(self):
        self.assertIn('"http://127.0.0.1:11434/api/generate"', SRC)
        self.assertNotIn("0.0.0.0", SRC)

    def test_model_output_is_truncated_before_it_reaches_slack(self):
        a = _agent(json.dumps({"anomaly_detected": True, "anomaly_type": "person", "severity": "high",
                               "description": "D" * 5000, "details": "E" * 5000, "confidence": 0.5}))
        _handle(a, {"image_base64": "aGk=", "camera": "front"})
        msg = a.report_to_jordan.call_args[0][0]
        self.assertLess(len(msg), 700)
        self.assertEqual(msg.count("D"), 300 + msg.replace("D" * 300, "").count("D"))

    def test_unreadable_image_path_is_not_sent_anywhere(self):
        a = _agent(ANOMALY)
        self.assertIsNone(_handle(a, {"image_path": "/nonexistent/cam.jpg"}))
        a._infer_vision.assert_not_called(); a.report_to_jordan.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_10k_benign_results_handled_under_bound(self):
        a = _agent(json.dumps({"description": "cat", "anomaly_detected": False, "severity": "none"}))
        t0 = time.perf_counter()
        for _ in range(10_000):
            r = asyncio.run(a.handle({"image_base64": "aGk=", "camera": "yard"}))
        self.assertLess(time.perf_counter() - t0, 8.0)
        self.assertFalse(r["anomaly_detected"])
        a.notify.assert_not_called()


class TestRetry(unittest.TestCase):
    def test_inference_failure_fails_open_with_none(self):
        # RETRY GAP: _infer_vision()/urlopen — one attempt per task; failure logs and returns None, no exception escapes
        a = _agent(exc=ConnectionRefusedError("ollama down"))
        self.assertIsNone(_handle(a, {"image_base64": "aGk=", "camera": "front"}))
        self.assertEqual(a._infer_vision.await_count, 1)
        self.assertIn("Vision inference failed", lk.log.call_args[0][0])
        a.notify.assert_not_called(); a.report_to_jordan.assert_not_called()

    def test_unparseable_model_output_degrades_to_a_description(self):
        a = _agent("{this is not json")
        r = _handle(a, {"image_base64": "aGk=", "camera": "front"})
        self.assertEqual((r["anomaly_detected"], r["severity"], r["flag_jordan"]), (False, "none", False))
        self.assertTrue(r["description"].startswith("{this is not json"))


class TestUnit(unittest.TestCase):
    def test_no_image_returns_none(self):
        a = _agent(ANOMALY)
        self.assertIsNone(_handle(a, {"camera": "front"}))
        a._infer_vision.assert_not_called()

    def test_think_block_is_stripped_before_parsing(self):
        a = _agent("<think>hmm {not this}</think>\n" + json.dumps({"anomaly_detected": False, "description": "quiet"}))
        r = _handle(a, {"image_base64": "aGk="})
        self.assertEqual(r["description"], "quiet")

    def test_prose_without_json_becomes_a_benign_description(self):
        a = _agent("Just a driveway, nothing unusual.")
        r = _handle(a, {"image_base64": "aGk=", "type": "ocr"})
        self.assertEqual((r["description"], r["anomaly_detected"], r["source_type"]), ("Just a driveway, nothing unusual.", False, "ocr"))

    def test_image_path_is_base64_encoded_for_the_model(self):
        a = _agent(json.dumps({"anomaly_detected": False}))
        p = _img()
        _handle(a, {"image_path": str(p), "prompt": "what?"})
        self.assertEqual(a._infer_vision.call_args[0], ("what?", base64.b64encode(p.read_bytes()).decode()))

    def test_class_contract(self):
        self.assertEqual((lk.LookoutAgent.name, lk.LookoutAgent.model, lk.LookoutAgent.backend), ("lookout", "qwen3-vl:4b", "ollama"))
        self.assertEqual(lk.LookoutAgent.channels, ["vision", "camera", "motion"])


class TestIntegration(unittest.TestCase):
    def test_vehicle_detections_are_suppressed_before_the_bus(self):
        for kind in ("vehicle", "Car", "licensePlate", "license_plate"):
            a = _agent(json.dumps({"anomaly_detected": True, "anomaly_type": kind, "severity": "high"}))
            r = _handle(a, {"image_base64": "aGk=", "camera": "street"})
            self.assertFalse(r["anomaly_detected"], kind)
            a.notify.assert_not_called(); a.report_to_jordan.assert_not_called()

    def test_severity_routes_between_notify_and_jordan(self):
        a = _agent(json.dumps({"anomaly_detected": True, "anomaly_type": "animal", "severity": "medium", "confidence": 0.4}))
        _handle(a, {"image_base64": "aGk=", "camera": "yard"})
        a.notify.assert_awaited_once(); a.report_to_jordan.assert_not_called()
        self.assertTrue(a.notify.call_args[0][0].startswith(":eyes: *Lookout Alert* (MEDIUM)"))
        a = _agent(json.dumps({"anomaly_detected": True, "anomaly_type": "object", "severity": "low", "flag_jordan": True}))
        _handle(a, {"image_base64": "aGk="})
        a.report_to_jordan.assert_awaited_once(); a.notify.assert_not_called()

    def test_infer_vision_posts_the_system_prompt_and_image_to_ollama(self):
        a = lk.LookoutAgent.__new__(lk.LookoutAgent)
        resp = MagicMock(); resp.read.return_value = json.dumps({"response": "ok"}).encode()
        with patch.object(lk.urllib.request, "urlopen", MagicMock(return_value=resp)) as u:
            self.assertEqual(asyncio.run(a._infer_vision("p", "aGk=")), "ok")
        req = u.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["images"], body["stream"], body["system"]), ("qwen3-vl:4b", ["aGk="], False, lk.SYSTEM_PROMPT))
        self.assertEqual(body["options"], {"temperature": 0.2, "num_predict": 2048})
        self.assertEqual(u.call_args[1]["timeout"], 120)


class TestFunctional(unittest.TestCase):
    def test_golden_path_flags_jordan_on_a_person_at_the_gate(self):
        a = _agent(ANOMALY)
        r = _handle(a, {"image_path": str(_img()), "camera": "gate", "type": "motion"})
        self.assertEqual((r["camera"], r["source_type"], r["anomaly_type"]), ("gate", "motion", "person"))
        msg = a.report_to_jordan.call_args[0][0]
        self.assertEqual(msg.split("\n")[0], ":warning: *Lookout Alert* (HIGH)")
        self.assertIn("*Camera:* gate | *Type:* person", msg)
        self.assertIn("*Confidence:* 90%", msg)
        a.notify.assert_not_called()

    def test_error_path_benign_scene_stays_silent(self):
        a = _agent(json.dumps({"description": "delivery driver", "anomaly_detected": False, "severity": "none"}))
        r = _handle(a, {"image_base64": "aGk=", "camera": "porch"})
        self.assertFalse(r["anomaly_detected"])
        a.notify.assert_not_called(); a.report_to_jordan.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_never_starts_the_agent(self):
        self.assertIn('if __name__ == "__main__":\n    LookoutAgent().run()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_agent_lookout"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
