#!/usr/bin/env python3
"""Tests for nova_swarm_agent.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

subprocess.run (the agent's only way to act) and the inference router are mocked in every test."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_swarm_agent.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nsa_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sa = _load()


def _tool_msg(*cmds):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"t{i}", "function": {"name": "run_shell", "arguments": json.dumps({"cmd": c})}}
        for i, c in enumerate(cmds)]}}]}


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _run_main(task, llm_side_effect, shell_out="load 0.1"):
    out = io.StringIO()
    with patch.object(sa, "llm", side_effect=llm_side_effect) as llm, \
         patch.object(sa.subprocess, "run", return_value=SimpleNamespace(stdout=shell_out, stderr="")) as run, \
         patch.object(sys, "stdin", io.StringIO(task)), contextlib.redirect_stdout(out):
        sa.main()
    return json.loads(out.getvalue()), llm, run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_destructive_commands_refused_before_exec(self):
        with patch.object(sa.subprocess, "run") as run:
            for cmd in ("rm -rf /", "sudo reboot", "systemctl stop nginx", "echo x > /etc/hosts",
                        "docker rm -f db", "pkill python", "dd if=/dev/zero of=/dev/sda", "cat a >> b"):
                self.assertTrue(sa.run_shell(cmd).startswith("BLOCKED"), cmd)
        run.assert_not_called()

    def test_llm_cannot_bypass_block_via_tool_call(self):
        res, _, run = _run_main("t", [_tool_msg("rm -rf /var"), _final("FINDINGS: done")])
        run.assert_not_called()
        self.assertEqual(res["steps"], 1)


class TestPerformance(unittest.TestCase):
    def test_loop_bounded_by_max_steps_and_output_capped(self):
        always_tools = [_tool_msg("uptime")] * sa.MAX_STEPS + [_final("FINDINGS: forced")]
        t0 = time.perf_counter()
        res, llm, run = _run_main("t", always_tools, shell_out="x" * 10_000)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(llm.call_count, sa.MAX_STEPS + 1)
        self.assertEqual(res["assessment"], "FINDINGS: forced")
        with patch.object(sa.subprocess, "run", return_value=SimpleNamespace(stdout="y" * 10_000, stderr="")):
            self.assertEqual(len(sa.run_shell("uptime")), 1800)


class TestRetry(unittest.TestCase):
    def test_llm_error_reported_not_raised(self):
        # RETRY GAP: llm()/urlopen — one attempt; the node reports the error as its assessment
        res, llm, _ = _run_main("t", RuntimeError("router down"))
        self.assertEqual(llm.call_count, 1)
        self.assertIn("LLM error: router down", res["assessment"])

    def test_shell_error_returned_as_text(self):
        with patch.object(sa.subprocess, "run", side_effect=subprocess.TimeoutExpired("sh", 20)):
            self.assertTrue(sa.run_shell("uptime").startswith("error:"))


class TestUnit(unittest.TestCase):
    def test_run_shell_empty_output(self):
        with patch.object(sa.subprocess, "run", return_value=SimpleNamespace(stdout="", stderr="")) as run:
            self.assertEqual(sa.run_shell("true"), "(no output)")
        self.assertEqual(run.call_args[0][0], ["/bin/sh", "-c", "true"])

    def test_llm_payload(self):
        resp = MagicMock(); resp.read.return_value = b'{"choices": []}'
        with patch.object(sa.urllib.request, "urlopen", return_value=resp) as u:
            sa.llm([{"role": "user", "content": "x"}])
        req = u.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, sa.ROUTER)
        self.assertEqual(body["tools"][0]["function"]["name"], "run_shell")
        self.assertFalse(body["stream"])


class TestIntegration(unittest.TestCase):
    def test_tool_results_fed_back_to_model(self):
        res, llm, run = _run_main("check disk", [_tool_msg("df -h", "uptime"), _final("FINDINGS: healthy")])
        msgs = llm.call_args_list[1][0][0]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["t0", "t1"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(res, {"node": sa.NODE, "assessment": "FINDINGS: healthy", "steps": 2})


class TestFunctional(unittest.TestCase):
    def test_golden_path_and_default_task(self):
        res, llm, _ = _run_main("", [_final("  FINDINGS: fine  ")])
        self.assertEqual(llm.call_args[0][0][1]["content"], "Assess this host's health.")
        self.assertEqual((res["assessment"], res["steps"]), ("FINDINGS: fine", 0))

    def test_bad_tool_arguments_tolerated(self):
        bad = {"choices": [{"message": {"tool_calls": [{"id": "z", "function": {"arguments": "{not json"}}]}}]}
        res, _, run = _run_main("t", [bad, _final("FINDINGS: ok")])
        self.assertEqual(run.call_args[0][0][-1], "echo no-cmd")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running it reads stdin and calls the router, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_swarm_agent"], cwd=str(SCRIPTS), input="",
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
