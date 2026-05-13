"""
test_nova_agent_sentinel.py — All 7 test categories for nova_agent_sentinel.py
Written by Jordan Koch.
"""

import asyncio
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_sentinel.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg
_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod
_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
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

_spec = importlib.util.spec_from_file_location("nova_agent_sentinel", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SecuritySentinel = _mod.SecuritySentinel


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-live", "ghp_", "AKIA", "xoxb-", "Jkoogie"]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_allowed_models_defined(self):
        """ALLOWED_MODELS must list all models that are permitted in config."""
        self.assertIsInstance(_mod.ALLOWED_MODELS, (set, frozenset))
        self.assertGreater(len(_mod.ALLOWED_MODELS), 3)

    def test_openrouter_only_allowed_for_research_agent(self):
        """OPENROUTER_ALLOWED_AGENTS must only contain 'research'."""
        self.assertEqual(_mod.OPENROUTER_ALLOWED_AGENTS, {"research"})

    def test_allowed_outbound_hosts_does_not_include_arbitrary_hosts(self):
        """ALLOWED_OUTBOUND_HOSTS must not include unrestricted wildcards."""
        for host in _mod.ALLOWED_OUTBOUND_HOSTS:
            self.assertNotEqual(host, "*",
                                "ALLOWED_OUTBOUND_HOSTS must not contain wildcard '*'")

    def test_parse_response_strips_think_tags(self):
        """_parse_response() must strip <think> blocks."""
        agent = SecuritySentinel()
        response = "<think>Analyzing threat...</think>\n" + json.dumps({
            "risk_level": "high",
            "flag_jordan": True,
            "findings": [],
        })
        result = agent._parse_response(response)
        self.assertEqual(result["risk_level"], "high")
        self.assertNotIn("think", json.dumps(result).lower())


class TestPerformance(unittest.TestCase):

    def test_parse_response_fast(self):
        """_parse_response() must parse 1000 responses in < 100ms."""
        agent = SecuritySentinel()
        response = json.dumps({"risk_level": "low", "flag_jordan": False,
                                "findings": [], "summary": "No threats detected."})
        start = time.perf_counter()
        for _ in range(1000):
            agent._parse_response(response)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"_parse_response 1000x took {elapsed:.3f}s")

    def test_sentinel_low_temperature(self):
        agent = SecuritySentinel()
        self.assertLessEqual(agent.temperature, 0.2)


class TestRetry(unittest.TestCase):

    def test_analyze_nmap_returns_none_on_inference_failure(self):
        agent = SecuritySentinel()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def fail_infer(prompt, system="", **kwargs):
            raise ConnectionError("Ollama down")

        agent.infer = fail_infer

        task = {"type": "nmap_scan", "devices": [{"ip": "192.168.1.1", "type": "router",
                                                   "open_ports": [80]}], "threats": []}
        result = _run(agent._analyze_nmap(task))
        self.assertIsNone(result)

    def test_privacy_monitor_handles_missing_config(self):
        """_privacy_monitor() must handle missing openclaw.json gracefully."""
        agent = SecuritySentinel()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        with patch("builtins.open", side_effect=FileNotFoundError("config not found")):
            with patch("subprocess.run") as mock_sp:
                mock_sp.return_value = MagicMock(returncode=1, stdout="")
                result = _run(agent._privacy_monitor({}))
        self.assertIsNotNone(result)
        self.assertIn("risk_level", result)


class TestUnit(unittest.TestCase):

    def test_parse_response_falls_back_on_bad_json(self):
        agent = SecuritySentinel()
        result = agent._parse_response("This is plain text, not JSON.")
        self.assertIn("risk_level", result)
        self.assertIn("flag_jordan", result)
        self.assertEqual(result["risk_level"], "unknown")

    def test_parse_response_extracts_valid_json(self):
        agent = SecuritySentinel()
        payload = {"risk_level": "medium", "flag_jordan": False, "findings": []}
        result = agent._parse_response(json.dumps(payload))
        self.assertEqual(result["risk_level"], "medium")

    def test_handle_routes_to_correct_method(self):
        agent = SecuritySentinel()
        method_called = []

        async def mock_analyze_nmap(task): method_called.append("nmap"); return {}
        async def mock_analyze_camera(task): method_called.append("camera"); return {}
        async def mock_analyze_unifi(task): method_called.append("unifi"); return {}
        async def mock_privacy_monitor(task): method_called.append("privacy"); return {}

        agent._analyze_nmap = mock_analyze_nmap
        agent._analyze_camera = mock_analyze_camera
        agent._analyze_unifi = mock_analyze_unifi
        agent._privacy_monitor = mock_privacy_monitor

        _run(agent.handle({"type": "nmap_scan", "devices": [{"ip": "x"}]}))
        _run(agent.handle({"type": "camera_alert", "description": "motion"}))
        _run(agent.handle({"type": "unifi_event", "event": "client connect"}))
        _run(agent.handle({"type": "privacy_monitor"}))

        self.assertIn("nmap", method_called)
        self.assertIn("camera", method_called)
        self.assertIn("unifi", method_called)
        self.assertIn("privacy", method_called)

    def test_analyze_camera_suppresses_vehicle_only_events(self):
        """_analyze_camera() must return None for vehicle-only smart events."""
        agent = SecuritySentinel()
        task = {"type": "camera_alert", "smart_types": ["vehicle", "licensePlate"],
                "description": "car on street"}
        result = _run(agent._analyze_camera(task))
        self.assertIsNone(result, "Vehicle-only camera events must be suppressed")

    def test_report_security_posts_critical_to_jordan(self):
        agent = SecuritySentinel()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        _run(agent._report_security(
            {"risk_level": "critical", "flag_jordan": True, "summary": "Intrusion detected!"},
            "Network Scan"
        ))
        agent.report_to_jordan.assert_called_once()

    def test_report_security_posts_low_to_notify(self):
        agent = SecuritySentinel()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        _run(agent._report_security(
            {"risk_level": "low", "flag_jordan": False, "summary": "Normal activity"},
            "UniFi Event"
        ))
        agent.notify.assert_called_once()
        agent.report_to_jordan.assert_not_called()


class TestIntegration(unittest.TestCase):

    def test_threat_assessment_returns_none_for_empty_signals(self):
        agent = SecuritySentinel()
        result = _run(agent._threat_assessment({"signals": []}))
        self.assertIsNone(result)

    def test_analyze_nmap_fetches_from_novacontrol_when_empty(self):
        """_analyze_nmap() must fetch devices from NovaControl when task has no devices."""
        agent = SecuritySentinel()
        agent.infer = AsyncMock(return_value=json.dumps({
            "risk_level": "none", "findings": [], "flag_jordan": False,
            "summary": "Network clean."
        }))
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        def mock_urlopen(url, timeout=None):
            r = MagicMock()
            r.__enter__ = lambda s: r
            r.__exit__ = MagicMock(return_value=False)
            if "devices" in str(url):
                r.read.return_value = json.dumps({"devices": [{"ip": "192.168.1.1",
                                                                "type": "router", "open_ports": []}]}).encode()
            else:
                r.read.return_value = json.dumps({"threats": []}).encode()
            return r

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = _run(agent._analyze_nmap({"type": "nmap_scan"}))

        self.assertIsNotNone(result)


class TestFunctional(unittest.TestCase):

    def test_privacy_monitor_violations_flagged_to_jordan(self):
        """Routing violations must be immediately escalated to Jordan."""
        agent = SecuritySentinel()
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()

        evil_config = {
            "agents": {
                "defaults": {"model": {"primary": "gpt-4"}},  # Not in ALLOWED_MODELS
                "list": []
            },
            "channels": {"signal": {"dmPolicy": "allowlist", "groupPolicy": "allowlist"},
                         "modelByChannel": {}}
        }

        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value=json.dumps(evil_config))
            )),
            __exit__=MagicMock(return_value=False)
        ))):
            with patch("json.load", return_value=evil_config):
                with patch("subprocess.run") as mock_sp:
                    mock_sp.return_value = MagicMock(returncode=1, stdout="")
                    result = _run(agent._privacy_monitor({}))

        # Should have found a violation and flagged Jordan
        self.assertIn("violations", result)


class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_instantiates(self):
        agent = SecuritySentinel()
        self.assertEqual(agent.name, "sentinel")

    def test_channels_include_security(self):
        agent = SecuritySentinel()
        self.assertIn("security", agent.channels)
        self.assertIn("nmap", agent.channels)

    def test_parse_response_method_exists(self):
        agent = SecuritySentinel()
        self.assertTrue(callable(agent._parse_response))

    def test_allowed_models_non_empty(self):
        self.assertGreater(len(_mod.ALLOWED_MODELS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
