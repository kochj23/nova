#!/usr/bin/env python3
"""Tests for nova_gateway/tools.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Every subprocess (asyncio.create_subprocess_exec), HTTP call (ctx.http), PG connect, Slack/Signal sender
and the audit log are mocked: no script runs, no light/scene/AV device is touched, nothing is posted."""
import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

tl = importlib.import_module("nova_gateway.tools")
SRC = (SCRIPTS / "nova_gateway" / "tools.py").read_text()


class _Proc:
    def __init__(self, out=b"ok", err=b"", rc=0):
        self._o, self._e, self.returncode = out, err, rc

    async def communicate(self):
        return self._o, self._e


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data or {}, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


def _ctx(get=None, post=None):
    return types.SimpleNamespace(pg_pool=None,
                                 http=types.SimpleNamespace(get=get or mock.AsyncMock(return_value=_Resp()),
                                                            post=post or mock.AsyncMock(return_value=_Resp())))


def _exec(out=b"ok", err=b"", rc=0, side_effect=None):
    return mock.patch.object(tl.asyncio, "create_subprocess_exec",
                             mock.AsyncMock(return_value=_Proc(out, err, rc), side_effect=side_effect))


def _run(coro):
    return asyncio.run(coro)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/._-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('keychain("nova-slack-bot-token")', SRC)       # Slack token from Keychain

    def test_run_script_refuses_traversal_and_unknown(self):
        with _exec() as ex:
            self.assertIn("not found", _run(tl._tool_run_script(_ctx(), {"script": "../../etc/passwd"})))
            self.assertIn("not found", _run(tl._tool_run_script(_ctx(), {"script": "no_such_script_xyz.py"})))
            self.assertIn("no script", _run(tl._tool_run_script(_ctx(), {})))
        ex.assert_not_called()

    def test_legacy_exec_outside_scripts_dir_denied(self):
        with _exec() as ex, mock.patch.object(tl, "log_tool_execution", mock.AsyncMock()):
            clean, out = _run(tl.execute_tool_calls_legacy(_ctx(), "sure\nexec bash /etc/evil.sh now\n"))
        ex.assert_not_called()
        self.assertIn("path traversal denied", out)
        self.assertNotIn("exec bash", clean)

    def test_ops_query_rejects_unknown_domain_and_non_int_limit(self):
        self.assertIn("unknown domain", _run(tl._tool_ops_query(_ctx(), {"domain": "pg_shadow"})))
        out = _run(tl._dispatch_now(_ctx(), "ops_query", {"domain": "queue", "limit": "1; DROP TABLE x"}))
        self.assertIn("error", out)                                   # never reaches SQL formatting


class TestPerformance(unittest.TestCase):
    def test_spoken_regex_linear_on_large_text(self):
        text = ("web_search {" + "a" * 50 + " ") * 2000            # unterminated braces: worst case for the regex
        t0 = time.perf_counter()
        clean, out = _run(tl.execute_spoken_tool_calls(_ctx(), text))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(out, "")

    def test_ops_limit_capped(self):
        captured = {}
        class Cur:
            def execute(self, sql): captured["sql"] = sql
            def fetchall(self): return []
            def close(self): pass
        conn = mock.MagicMock(); conn.cursor.return_value = Cur()
        with mock.patch("psycopg2.connect", return_value=conn):
            _run(tl._tool_ops_query(_ctx(), {"domain": "queue", "limit": 100000}))
        self.assertIn("LIMIT 50", captured["sql"])


class TestRetry(unittest.TestCase):
    def test_tool_errors_fail_open(self):
        # RETRY GAP: _dispatch_now — tools are single-shot; exceptions/timeouts become an error string
        # pin SCRIPTS_DIR: another test file may have imported nova_gateway.config under a temp HOME
        from pathlib import Path as _P
        with _exec(side_effect=OSError("fork failed")), \
                mock.patch.object(tl, "SCRIPTS_DIR", _P(__file__).resolve().parents[1]):
            self.assertIn("error: fork failed", _run(tl._dispatch_now(_ctx(), "school_report", {})))
        with mock.patch.object(tl, "_tool_plex_control", mock.AsyncMock(side_effect=asyncio.TimeoutError())):
            self.assertIn("timed out", _run(tl._dispatch_now(_ctx(), "plex_control", {"action": "playing"})))

    def test_web_search_failure_and_hue_failure(self):
        # RETRY GAP: _tool_web_search / _tool_hue_control — one attempt, error text returned
        ctx = _ctx(get=mock.AsyncMock(side_effect=OSError("searx down")))
        with mock.patch.object(tl, "resolve_url", lambda s, p="": "http://searx.test" + p):
            self.assertIn("web search error", _run(tl._tool_web_search(ctx, {"query": "x"})))
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("hue down")) as m:
            self.assertIn("Hue control failed", _run(tl._tool_hue_control(_ctx(), "kitchen off")))
        self.assertEqual(m.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_registry_shape(self):
        for name, spec in tl.TOOL_REGISTRY.items():
            self.assertIn("description", spec, name)
            self.assertIsInstance(spec["parameters"], dict, name)
            for req in spec.get("required", []):
                self.assertIn(req, spec["parameters"], name)

    def test_param_validation(self):
        for coro in (tl._tool_memory_search(_ctx(), {}), tl._tool_web_search(_ctx(), {}),
                     tl._tool_homekit_scene(_ctx(), {}), tl._tool_home_control(_ctx(), {"device": "x"}),
                     tl._tool_nearest_place(_ctx(), {}), tl._tool_browse_page(_ctx(), {})):
            self.assertTrue(_run(coro).startswith("[error"))
        self.assertIn("unknown tool", _run(tl.dispatch_tool(_ctx(), "rm_rf", {})))

    def test_memory_quality_defaults_to_dry_run(self):
        with mock.patch.object(tl, "_tool_run_script", mock.AsyncMock(return_value="r")) as rs:
            _run(tl._dispatch_now(_ctx(), "memory_quality", {}))
        self.assertEqual(rs.call_args[0][1]["args"], ["--dry-run"])


class TestIntegration(unittest.TestCase):
    def test_structured_calls_dispatch_and_audit(self):
        resp = {"choices": [{"message": {"content": "checking", "tool_calls": [
            {"id": "t1", "function": {"name": "plex_control", "arguments": json.dumps({"action": "playing"})}},
            {"id": "t2", "function": {"name": "bogus", "arguments": "{}"}}]}}]}
        ctx = _ctx(get=mock.AsyncMock(return_value=_Resp(text="Now playing: Alien")))
        audit = mock.AsyncMock()
        with mock.patch.object(tl, "log_tool_execution", audit):
            text, out = _run(tl.execute_tool_calls(ctx, resp, session_id="gw2:slack:C1"))
        self.assertEqual(text, "checking")
        self.assertIn("[t1] Now playing: Alien", out)
        self.assertIn("[t2] [error: unknown tool 'bogus']", out)
        self.assertEqual(audit.call_args[0][2], "plex_control")       # only real tools are audited

    def test_web_search_screens_untrusted_results(self):
        data = {"results": [{"title": "T", "url": "https://x.test", "content": "body"}]}
        screen = types.SimpleNamespace(scan_results=mock.MagicMock(side_effect=lambda r, **k: r))
        with mock.patch.object(tl, "_untrusted", screen), \
             mock.patch.object(tl, "resolve_url", lambda s, p="": "http://searx.test" + p):
            out = _run(tl._tool_web_search(_ctx(get=mock.AsyncMock(return_value=_Resp(data=data))), {"query": "q"}))
        screen.scan_results.assert_called_once()
        self.assertIn("https://x.test", out)


class TestFunctional(unittest.TestCase):
    def test_spoken_call_runs_and_is_stripped(self):
        with mock.patch.object(tl, "_tool_place_categories", mock.AsyncMock(return_value="ghost_town 12")), \
             mock.patch.object(tl, "log_tool_execution", mock.AsyncMock()) as audit:
            clean, out = _run(tl.execute_spoken_tool_calls(_ctx(), 'Let me look. place_categories {}'))
        self.assertEqual((clean, out), ("Let me look.", "ghost_town 12"))
        self.assertEqual(audit.call_args[0][2], "spoken:place_categories")

    def test_send_to_claude_queues_once(self):
        cur = mock.MagicMock(); cur.fetchone.side_effect = [None, ("sess",), (77,)]
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn):
            out = _run(tl._tool_send_message(_ctx(), {"channel": "claude", "text": "fix the thing"}))
        self.assertIn("claude_queue #77", out)
        ins = [c for c in cur.execute.call_args_list if "INSERT INTO claude_queue" in c[0][0]]
        self.assertEqual(ins[0][0][1][1], "[from Nova] fix the thing")   # parameterized value

    def test_send_slack_without_token_refuses(self):
        with mock.patch("nova_gateway.config.keychain", return_value=""):
            self.assertIn("token not available",
                          _run(tl._tool_send_message(_ctx(), {"channel": "slack", "text": "hi"})))
        self.assertIn("unknown channel", _run(tl._tool_send_message(_ctx(), {"channel": "fax", "text": "x"})))


class TestRulePath20261008(unittest.TestCase):
    """home_control goes through the run_script rule; read-only extended tools are wired, ui_click is not."""

    def test_home_control_is_checked_as_run_script(self):
        seen = {}

        async def check(pool, tool, channel, params):
            seen.update(tool=tool, params=params); return "approve"
        ctx = _ctx(); ctx.pg_pool = object()
        with mock.patch("nova_gateway.autonomy.check_autonomy", check), \
                mock.patch("nova_gateway.autonomy.request_approval", mock.AsyncMock(return_value="p1")), \
                mock.patch.object(tl, "_slack_notify", mock.AsyncMock()), \
                mock.patch.object(tl, "_tool_run_script", mock.AsyncMock(return_value="ran")) as rs:
            out = _run(tl.dispatch_tool(ctx, "home_control", {"device": "living_room", "action": "power off"},
                                        session_id="gw2:slack:C1"))
        self.assertEqual(seen["tool"], "run_script")
        self.assertEqual(seen["params"], {"script": "nova_home_control.py", "args": ["onkyo", "living_room", "power", "off"]})
        self.assertIn("awaiting Jordan's approval", out)
        rs.assert_not_called()

    def test_home_control_scene_volume_args_match_the_notify_rule(self):
        import nova_gateway.autonomy as au
        pat = (r'^\{"args": \[("scene", "[a-z_]+"|"(bose|onkyo)", "[a-z_]+", ("volume", "[0-9]{1,3}"|"mute"(, "(on|off)")?'
               r'|"unmute"))\], "script": "nova_home_control\.py"\}$')
        with mock.patch.object(au, "_cache", {("__scoped__", "run_script"): [("*", re.compile(pat, re.I), "notify", 10)]}):
            lvl = lambda d, a: au.scoped_level("run_script", "slack", {"script": "nova_home_control.py",
                                                                      "args": tl.home_control_args(d, a)})
            self.assertEqual(lvl("scene", "scene movie"), "notify")
            self.assertEqual(lvl("kitchen", "volume 30"), "notify")
            self.assertIsNone(lvl("living_room", "power off"))
            self.assertIsNone(lvl("kitchen", "volume 30; rm -rf /"))

    def test_home_control_args_follow_the_script_cli(self):
        self.assertEqual(tl.home_control_args("kitchen", "volume 30"), ["bose", "kitchen", "volume", "30"])
        self.assertEqual(tl.home_control_args("scene", "scene goodnight"), ["scene", "goodnight"])
        with self.assertRaises(ValueError):
            tl.home_control_args("garage", "power on")

    def test_only_readonly_extended_tools_can_merge(self):
        reg = {}
        with mock.patch.object(tl.os, "access", return_value=True):
            merged = tl._merge_extended_tools(reg)
        self.assertEqual(sorted(merged), ["camera_snap", "screenshot"])
        for bad in ("ui_click", "ui_type", "camera_clip"):
            self.assertNotIn(bad, reg)
        self.assertNotIn("output", reg["camera_snap"]["parameters"])
        self.assertEqual(reg["camera_snap"]["required"], ["camera"])
        with mock.patch.object(tl.os, "access", return_value=False):
            self.assertEqual(tl._merge_extended_tools({}), [])


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.tools as t; print(len(t.TOOL_REGISTRY) > 10)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "True")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
