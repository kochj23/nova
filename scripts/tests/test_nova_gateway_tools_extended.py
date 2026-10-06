#!/usr/bin/env python3
"""Tests for nova_gateway/tools_extended.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "tools_extended.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="gw-toolsx-test-"))
(TMP / ".openclaw" / "logs").mkdir(parents=True)

# importing the package runs main.py's log setup under ~/.openclaw/logs: load with HOME in a tempdir
with patch.dict(os.environ, {"HOME": str(TMP), "NOVA_TEST_QUIET": "1"}):
    import nova_gateway.tools_extended as tx
    import nova_gateway.taskflow as tf


def _ctx(pool="POOL"):
    return types.SimpleNamespace(pg_pool=pool)


def _disp(name, params, rc=0, out="ok", err=""):
    """Dispatch with _run_cmd stubbed; returns (result, list of (argv, timeout))."""
    calls = []

    async def fake(cmd, timeout=30):
        calls.append((cmd, timeout)); return out, err, rc
    with patch.object(tx, "_run_cmd", fake):
        r = asyncio.run(tx.dispatch_extended_tool(_ctx(), name, params))
    return r, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("create_subprocess_shell", SRC)

    def test_hostile_text_is_one_argv_element(self):
        evil = "hello; echo pwned && $(whoami)"
        _, calls = _disp("ui_type", {"text": evil})
        self.assertEqual(calls[0][0], [tx.PEEKABOO, "type", evil])

    def test_output_bounded(self):
        r = asyncio.run(tx._run_cmd([sys.executable, "-c", "print('x' * 20000)"]))
        self.assertEqual(len(r[0]), 5000)
        self.assertEqual(r[2], 0)


class TestPerformance(unittest.TestCase):
    def test_10k_unknown_dispatches_fast(self):
        t0 = time.perf_counter()

        async def many():
            for i in range(10_000):
                await tx.dispatch_extended_tool(_ctx(), f"nope{i}", {})
        asyncio.run(many())
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_subprocess_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: _run_cmd/create_subprocess_exec — one attempt, ("", err, -1) on failure
        r = asyncio.run(tx._run_cmd(["/nonexistent/binary-xyz"]))
        self.assertEqual((r[0], r[2]), ("", -1))
        res, calls = _disp("camera_snap", {"camera": "porch"}, rc=1, err="rtsp timeout")
        self.assertEqual(res, "Error: rtsp timeout")
        self.assertEqual(len(calls), 1)

    def test_timeout_kills_process(self):
        r = asyncio.run(tx._run_cmd([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.5))
        self.assertEqual(r, ("", "Timeout", -1))


class TestUnit(unittest.TestCase):
    def test_screenshot_argv_variants(self):
        _, c = _disp("screenshot", {})
        self.assertEqual(c[0][0], [tx.PEEKABOO, "image"])
        _, c = _disp("screenshot", {"app": "Safari", "analyze": "what's open?"})
        self.assertEqual(c[0][0], [tx.PEEKABOO, "see", "--app", "Safari", "--analyze", "what's open?"])
        r, _ = _disp("screenshot", {}, out="")
        self.assertEqual(r, "Screenshot captured.")

    def test_summarize_json_and_plain(self):
        r, c = _disp("summarize_url", {"url": "https://x"}, out=json.dumps({"summary": "short one"}))
        self.assertEqual(r, "short one")
        self.assertEqual(c[0][0][-3:], ["--length", "medium", "--json"])
        r, _ = _disp("summarize_url", {"url": "https://x"}, out="not json")
        self.assertEqual(r, "not json")

    def test_unknown_tool(self):
        r, c = _disp("bogus", {})
        self.assertEqual((r, c), ("Unknown tool: bogus", []))


class TestIntegration(unittest.TestCase):
    def test_every_tool_has_schema_and_a_dispatch_branch(self):
        body = SRC.split("async def dispatch_extended_tool")[1]
        for name, spec in tx.EXTENDED_TOOLS.items():
            self.assertEqual(spec["parameters"]["type"], "object", name)
            self.assertIn(f'"{name}"', body, name)

    def test_flow_tools_use_taskflow_and_ctx_pool(self):
        with patch.object(tf, "create_flow", AsyncMock(return_value="f-123")) as cf:
            r = asyncio.run(tx.dispatch_extended_tool(_ctx("P"), "flow_create", {"goal": "g", "first_step": "s"}))
        self.assertEqual(r, "Flow created: f-123")
        cf.assert_awaited_once_with("P", "g", "s", state={})

    def test_pool_falls_back_to_session_get_pg(self):
        import nova_gateway.session as sess
        with patch.object(sess, "get_pg", AsyncMock(return_value="FROM_SESSION")):
            self.assertEqual(asyncio.run(tx._get_pool(types.SimpleNamespace(pg_pool=None))), "FROM_SESSION")


class TestFunctional(unittest.TestCase):
    def test_flow_status_lists_active(self):
        flows = [{"flow_id": "abcdef0123", "status": "running", "current_step": "fetch", "goal": "G" * 100}]
        with patch.object(tf, "list_active_flows", AsyncMock(return_value=flows)):
            r = asyncio.run(tx.dispatch_extended_tool(_ctx(), "flow_status", {}))
        self.assertEqual(r, "• abcdef01 [running] step=fetch — " + "G" * 60)
        with patch.object(tf, "list_active_flows", AsyncMock(return_value=[])):
            self.assertEqual(asyncio.run(tx.dispatch_extended_tool(_ctx(), "flow_status", {})), "No active flows.")

    def test_flow_advance_conflict_message(self):
        with patch.object(tf, "advance_step", AsyncMock(return_value=False)):
            r = asyncio.run(tx.dispatch_extended_tool(_ctx(), "flow_advance", {"flow_id": "f", "next_step": "n"}))
        self.assertIn("revision conflict", r)

    def test_camera_clip_golden(self):
        r, c = _disp("camera_clip", {"camera": "porch"})
        self.assertEqual(r, "Clip recorded: /tmp/camsnap_porch_clip.mp4")
        self.assertEqual(c[0][0][:3], [tx.CAMSNAP, "clip", "porch"])


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        tmp = tempfile.mkdtemp(prefix="gw-toolsx-frame-"); os.makedirs(os.path.join(tmp, ".openclaw", "logs"))
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.tools_extended as t; print(len(t.EXTENDED_TOOLS))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": tmp, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "12")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
