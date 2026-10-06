#!/usr/bin/env python3
"""Tests for nova_agent_briefer.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_agent_briefer.py"
SRC = SCRIPT.read_text()


class _FakeSubAgent:
    """Offline stand-in for nova_subagent.SubAgent: records every side effect, touches nothing."""
    def __init__(self):
        self.posts, self.notes, self.memories = [], [], []
        self.recalls = []
        self.infer_result = "<think>hmm</think>Focus on the backup."
        self.infer_exc = None
        self.recall_result = []
        self.registered = 0

    async def infer(self, prompt, system=""):
        self.last_prompt = prompt
        if self.infer_exc:
            raise self.infer_exc
        return self.infer_result

    async def recall(self, query, n=5, source=None):
        self.recalls.append((query, n, source))
        return self.recall_result

    async def remember(self, text, source="", metadata=None):
        self.memories.append((text, source, metadata))

    async def notify(self, message, channel=None):
        self.notes.append(message)

    async def report_to_jordan(self, message):
        self.posts.append(message)

    def _register(self): self.registered += 1
    def _deregister(self): self.registered -= 1


def _load():
    sub = types.ModuleType("nova_subagent"); sub.SubAgent = _FakeSubAgent
    lg = types.ModuleType("nova_logger"); lg.log = MagicMock(); lg.LOG_INFO, lg.LOG_ERROR = "info", "error"
    spec = importlib.util.spec_from_file_location("agent_briefer_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_subagent": sub, "nova_logger": lg}):
        spec.loader.exec_module(mod)
    return mod


ab = _load()
ab.log = MagicMock()
# module-level stub: no NovaControl / network from any test
ab.urllib = types.SimpleNamespace(request=types.SimpleNamespace(urlopen=MagicMock(side_effect=OSError("offline"))))


def _run(coro):
    return asyncio.run(coro)


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode(); return r


def _agent():
    return ab.ProactiveBriefer()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_calendar_subprocess_is_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("os.system", SRC)

    def test_novacontrol_api_is_loopback(self):
        self.assertTrue(ab.NOVACONTROL_API.startswith("http://127.0.0.1:"))


class TestPerformance(unittest.TestCase):
    def test_action_items_capped_on_10k(self):
        items = [{"priority": "high", "title": "x" * 500} for _ in range(10_000)]
        a = _agent()
        t0 = time.perf_counter()
        with patch.object(ab.urllib.request, "urlopen", return_value=_resp({"actionItems": items})):
            out = _run(a._get_action_items())
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out.splitlines()), 10)          # capped at 10 lines
        self.assertTrue(all(len(l) < 120 for l in out.splitlines()))


class TestRetry(unittest.TestCase):
    def test_action_items_fail_open(self):
        # RETRY GAP: _get_action_items — one urlopen attempt, failure returns "" not an exception
        m = MagicMock(side_effect=OSError("down"))
        with patch.object(ab.urllib.request, "urlopen", m):
            self.assertEqual(_run(_agent()._get_action_items()), "")
        self.assertEqual(m.call_count, 1)

    def test_health_fail_open(self):
        # RETRY GAP: _get_system_health — one attempt, safe text on failure
        with patch.object(ab.urllib.request, "urlopen", MagicMock(side_effect=OSError("down"))):
            self.assertEqual(_run(_agent()._get_system_health()), "NovaControl API unreachable")

    def test_calendar_subprocess_failure_fails_open(self):
        # RETRY GAP: _get_calendar — subprocess timeout returns "" once, no retry
        import subprocess as sp
        with patch.object(sp, "run", MagicMock(side_effect=sp.TimeoutExpired("x", 30))) as m:
            self.assertEqual(_run(_agent()._get_calendar()), "")
        self.assertEqual(m.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_health_lists_only_unhealthy_services(self):
        data = {"status": "degraded", "services": {"a": {"status": "ok"}, "b": {"status": "down"}, "c": "str"}}
        with patch.object(ab.urllib.request, "urlopen", return_value=_resp(data)):
            out = _run(_agent()._get_system_health())
        self.assertIn("Overall: degraded", out)
        self.assertIn("b:", out)
        self.assertNotIn("a:", out)

    def test_health_all_ok(self):
        with patch.object(ab.urllib.request, "urlopen", return_value=_resp({"status": "ok", "services": {}})):
            self.assertIn("all services healthy", _run(_agent()._get_system_health()))

    def test_recent_emails_empty_and_truncated(self):
        a = _agent()
        self.assertEqual(_run(a._get_recent_emails()), "")
        a.recall_result = [{"text": "z" * 999}]
        self.assertEqual(len(_run(a._get_recent_emails())), 200)
        self.assertEqual(a.recalls[-1][2], "email")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_subagent_base(self):
        self.assertIn("from nova_subagent import SubAgent", SRC)
        self.assertTrue(issubclass(ab.ProactiveBriefer, _FakeSubAgent))
        self.assertEqual(ab.ProactiveBriefer.name, "briefer")

    def test_action_items_accepts_items_alias(self):
        with patch.object(ab.urllib.request, "urlopen", return_value=_resp({"items": [{"text": "pay bill"}]})):
            self.assertEqual(_run(_agent()._get_action_items()), "- [?] pay bill")


class TestFunctional(unittest.TestCase):
    def test_brief_golden_path_strips_think_posts_and_remembers(self):
        a = _agent()
        with patch.object(a, "_get_calendar", return_value="9am standup"):
            out = _run(a._generate_brief())
        self.assertEqual(out["brief"], "Focus on the backup.")
        self.assertEqual(len(a.posts), 1)
        self.assertIn("Morning Brief", a.posts[0])
        self.assertNotIn("<think>", a.posts[0])
        self.assertIn("CALENDAR:\n9am standup", a.last_prompt)
        self.assertEqual(a.memories[0][2]["type"], "daily_brief")

    def test_inference_failure_notifies_and_posts_nothing(self):
        a = _agent(); a.infer_exc = RuntimeError("ollama down")
        out = _run(a._generate_brief())   # health always yields text, so the LLM is reached
        self.assertIsNone(out)
        self.assertEqual(a.posts, [])
        self.assertIn("ollama down", a.notes[0])
        self.assertEqual(a.memories, [])

    def test_run_morning_registers_and_deregisters(self):
        calls = []
        with patch.object(ab.ProactiveBriefer, "_generate_brief", lambda self: asyncio.sleep(0)), \
             patch.object(ab.ProactiveBriefer, "_register", lambda self: calls.append("reg")), \
             patch.object(ab.ProactiveBriefer, "_deregister", lambda self: calls.append("dereg")):
            ab.run_morning()
        self.assertEqual(calls, ["reg", "dereg"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: the non-cron entry starts the Redis subagent loop, so only import is smoke-tested
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, types; from unittest.mock import MagicMock;"
                "s=types.ModuleType('nova_subagent'); s.SubAgent=object; sys.modules['nova_subagent']=s;"
                "import importlib.util as u; sp=u.spec_from_file_location('b', sys.argv[1]);"
                "m=u.module_from_spec(sp); sp.loader.exec_module(m); print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
