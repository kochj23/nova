#!/usr/bin/env python3
"""Tests for nova_inbox_claude.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
herd-mail (subprocess.run), Ollama and the memory server (urlopen) are mocked; herd_config is a stub
module set only for the duration of a call — no real mailbox or roster is ever read."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_inbox_claude.py"
SRC = SCRIPT.read_text()
SHELLY = "a@x.io; touch /tmp/pwned_$(id -u)"


def _load():
    spec = importlib.util.spec_from_file_location("nova_inbox_claude_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ic = _load()
ic.log = lambda m: None


@contextmanager
def _herd(members):
    missing = object()
    old = sys.modules.get("herd_config", missing)
    fake = types.ModuleType("herd_config"); fake.HERD = members
    sys.modules["herd_config"] = fake  # restored below to the SAME object
    try:
        yield
    finally:
        if old is missing:
            sys.modules.pop("herd_config", None)
        else:
            sys.modules["herd_config"] = old


def _resp(obj):
    m = MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(obj).encode()
    return m


def _proc(rc=0, out=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_llm_is_local(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token)\s*=\s*['\"]", SRC, re.I))
        self.assertTrue(ic.OLLAMA_URL.startswith("http://127.0.0.1"))
        self.assertNotIn("openrouter", SRC.lower())

    def test_herd_args_are_argv_list(self):
        with patch.object(ic.subprocess, "run", return_value=_proc()) as run:
            ic.run_herd(["send", "--to", SHELLY])
        argv = run.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertIn(SHELLY, argv)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_body_truncated_in_prompt(self):
        with _herd([]), patch.object(ic, "call_local_llm", return_value="hi") as llm:
            ic.generate_reply("a@x.io", "s", "B" * 5000)
        self.assertLess(llm.call_args[0][0].count("B"), 450)


class TestPerformance(unittest.TestCase):
    def test_main_caps_at_three_messages(self):
        msgs = [{"uid": i, "from_addr": f"u{i}@x.io", "subject": "s"} for i in range(10_000)]
        t0 = time.perf_counter()
        with patch.object(ic, "run_herd", return_value=(0, json.dumps({"messages": msgs}))), \
             patch.object(ic, "process_email", return_value=True) as pe:
            ic.main()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(pe.call_count, 3)


class TestRetry(unittest.TestCase):
    def test_llm_failure_fails_open(self):
        # RETRY GAP: call_local_llm() — one Ollama attempt; error -> None (never falls back to cloud)
        with patch.object(ic.urllib.request, "urlopen", side_effect=OSError("refused")) as u:
            self.assertIsNone(ic.call_local_llm("p"))
        self.assertEqual(u.call_count, 1)

    def test_herd_and_memory_failures_fail_open(self):
        # RETRY GAP: run_herd()/remember()/recall_context() — single attempt, safe defaults
        with patch.object(ic.subprocess, "run", side_effect=subprocess.TimeoutExpired("h", 30)):
            self.assertEqual(ic.run_herd(["list"]), (1, ""))
        with patch.object(ic.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIsNone(ic.remember("x"))
            self.assertEqual(ic.recall_context("q"), [])


class TestUnit(unittest.TestCase):
    def test_llm_strips_think_and_empty_is_none(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"response": "<think>x</think> Hi"})):
            self.assertEqual(ic.call_local_llm("p"), "Hi")
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"response": "  "})):
            self.assertIsNone(ic.call_local_llm("p"))

    def test_sender_name_from_herd_or_localpart(self):
        with _herd([{"email": "bot@x.io", "name": "Sam"}]), patch.object(ic, "call_local_llm", return_value="r") as llm:
            ic.generate_reply("bot@x.io", "s", "b")
            self.assertIn("From: Sam (bot@x.io)", llm.call_args[0][0])
            ic.generate_reply("stranger@y.io", "s", "b")
            self.assertIn("From: stranger (stranger@y.io)", llm.call_args[0][0])

    def test_process_email_read_failure(self):
        with patch.object(ic, "run_herd", return_value=(1, "")) as rh:
            self.assertFalse(ic.process_email({"uid": 5}))
        rh.assert_called_once_with(["read", "5"])


class TestIntegration(unittest.TestCase):
    def test_reply_sent_then_remembered(self):
        calls = [(0, json.dumps({"body": "hello nova"})), (0, "sent")]
        with patch.object(ic, "run_herd", side_effect=calls) as rh, \
             patch.object(ic, "generate_reply", return_value="Thanks —Nova") as gr, \
             patch.object(ic, "remember") as rem:
            self.assertTrue(ic.process_email({"uid": 1, "from_addr": "a@x.io", "subject": "Hey"}))
        gr.assert_called_once_with("a@x.io", "Hey", "hello nova")
        self.assertEqual(rh.call_args[0][0], ["send", "--to", "a@x.io", "--subject", "Re: Hey", "--body", "Thanks —Nova"])
        self.assertEqual(rem.call_args.kwargs["source"], "herd")

    def test_ollama_payload_shape(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"response": "ok"})) as u:
            ic.call_local_llm("p")
        body = json.loads(u.call_args[0][0].data)
        self.assertEqual((body["model"], body["stream"]), (ic.OLLAMA_MODEL, False))


class TestFunctional(unittest.TestCase):
    def test_main_golden_path(self):
        out = json.dumps({"messages": [{"uid": 1}, {"uid": 2}]})
        with patch.object(ic, "run_herd", return_value=(0, out)) as rh, \
             patch.object(ic, "process_email", side_effect=[True, False]) as pe:
            ic.main()
        rh.assert_called_once_with(["list", "--unread"])
        self.assertEqual(pe.call_count, 2)

    def test_main_list_failure_and_bad_json(self):
        for ret in [(1, ""), (0, ""), (0, "not json"), (0, json.dumps({"messages": []}))]:
            with patch.object(ic, "run_herd", return_value=ret), patch.object(ic, "process_email") as pe:
                ic.main()
            pe.assert_not_called()

    def test_no_reply_means_no_send(self):
        with patch.object(ic, "run_herd", return_value=(0, "raw body")) as rh, \
             patch.object(ic, "generate_reply", return_value=None):
            self.assertFalse(ic.process_email({"uid": 1, "from_addr": "a@x.io"}))
        self.assertEqual(rh.call_count, 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_inbox_claude"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
