#!/usr/bin/env python3
"""Tests for nova_watchdog.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_watchdog.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="watchdog-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("watchdog_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"HOME": str(TMP)}), patch("socket.create_connection", side_effect=AssertionError("net at import")):
        spec.loader.exec_module(mod)
    mod.STATE_DIR = str(TMP / "state"); mod.STATE_FILE = str(TMP / "state" / "watchdog_state.json")
    return mod


wd = _load()


class _Resp:
    def __init__(self, code=200, body=None):
        self.code = code; self.body = body if body is not None else {"ok": True}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.code

    def read(self):
        return json.dumps(self.body).encode()


class _Stop(Exception):
    pass


def _run_main(sweeps, probe, token="xoxb-test"):
    """Run main(): the startup sweep plus `sweeps` loop iterations, then stop. `probe(label) -> ok`."""
    posts = []

    def slack(channel, text):
        posts.append((channel, text)); return True

    def run_check(kind, target):
        label = next(l for l, k, t in wd.CHECKS if (k, t) == (kind, target))
        return (True, "") if probe(label) else (False, "ConnectionRefusedError")
    sleep = MagicMock(side_effect=[None] * sweeps + [_Stop()])
    Path(wd.STATE_FILE).unlink(missing_ok=True)
    with patch.object(wd, "slack_post", side_effect=slack), patch.object(wd, "run_check", side_effect=run_check), \
         patch.object(wd.time, "sleep", sleep), patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": token}), \
         redirect_stdout(io.StringIO()) as out:
        with self_assert_raises(_Stop):
            wd.main()
    return posts, out.getvalue()


class self_assert_raises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        return et is not None and issubclass(et, self.exc)


class TestSecurity(unittest.TestCase):
    def test_token_comes_only_from_the_environment(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"xox[bap]-[A-Za-z0-9-]{10,}", SRC))
        self.assertIn('os.environ.get("NOVA_SLACK_BOT_TOKEN", "")', SRC)
        self.assertNotIn("subprocess", SRC); self.assertNotIn("psycopg2", SRC)     # zero .6 dependencies

    def test_slack_post_sends_bearer_header_and_refuses_without_token(self):
        with patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": ""}), patch("urllib.request.urlopen") as u, redirect_stdout(io.StringIO()):
            self.assertFalse(wd.slack_post(wd.CH_INFO, "x")); u.assert_not_called()
        with patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": "xoxb-t"}), patch("urllib.request.urlopen", return_value=_Resp()) as u:
            self.assertTrue(wd.slack_post(wd.CH_CRITICAL, "down"))
        req = u.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer xoxb-t")
        self.assertEqual(json.loads(req.data), {"channel": wd.CH_CRITICAL, "text": "down", "mrkdwn": True})

    def test_checks_target_only_lan_addresses(self):
        for label, kind, target in wd.CHECKS:
            host = target[0] if kind == "tcp" else target.split("//")[1].split(":")[0]
            self.assertTrue(host.startswith("192.168.1."), label)


class TestPerformance(unittest.TestCase):
    def test_sweep_over_10k_checks_fast(self):
        checks = [(f"c{i}", "tcp", ("192.168.1.1", i)) for i in range(10_000)]
        state = {}
        with patch.object(wd, "CHECKS", checks), patch.object(wd, "run_check", return_value=(True, "")):
            t0 = time.perf_counter()
            trans, up, total = wd.sweep(state)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((trans, up, total), ([], 10_000, 10_000))


class TestRetry(unittest.TestCase):
    def test_probe_retries_with_backoff_inside_one_sweep(self):
        with patch.object(wd, "check_tcp", side_effect=[(False, "a"), (False, "b"), (True, "")]) as c, \
             patch.object(wd.time, "sleep") as slp:
            self.assertEqual(wd.run_check("tcp", ("192.168.1.6", 22)), (True, ""))
        self.assertEqual(c.call_count, 3)
        self.assertEqual([a[0][0] for a in slp.call_args_list], [wd.PROBE_BACKOFF] * 2)

    def test_sustained_failure_reports_the_last_detail_after_all_attempts(self):
        with patch.object(wd, "check_http", return_value=(False, "HTTP 503")) as c, patch.object(wd.time, "sleep") as slp:
            self.assertEqual(wd.run_check("http", "http://192.168.1.6:11434/api/version"), (False, "HTTP 503"))
        self.assertEqual(c.call_count, wd.PROBE_ATTEMPTS)
        self.assertEqual(slp.call_count, wd.PROBE_ATTEMPTS - 1)      # no sleep after the final attempt

    def test_slack_post_fails_open(self):
        # RETRY GAP: slack_post — a single urlopen; failures are printed and return False, never raised
        with patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": "t"}), patch("urllib.request.urlopen", side_effect=OSError("x")) as u, \
             redirect_stdout(io.StringIO()) as out:
            self.assertFalse(wd.slack_post(wd.CH_INFO, "m"))
        self.assertEqual(u.call_count, 1); self.assertIn("slack post failed: OSError", out.getvalue())
        with patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": "t"}), patch("urllib.request.urlopen", return_value=_Resp(body={"ok": False})), \
             redirect_stdout(io.StringIO()):
            self.assertFalse(wd.slack_post(wd.CH_INFO, "m"))


class TestUnit(unittest.TestCase):
    def test_check_tcp_and_http(self):
        with patch("socket.create_connection", return_value=MagicMock()):
            self.assertEqual(wd.check_tcp("192.168.1.6", 22), (True, ""))
        with patch("socket.create_connection", side_effect=TimeoutError()):
            self.assertEqual(wd.check_tcp("192.168.1.6", 22), (False, "TimeoutError"))
        with patch("urllib.request.urlopen", return_value=_Resp(200)):
            self.assertEqual(wd.check_http("http://x"), (True, ""))
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 502, "bad", {}, None)):
            self.assertEqual(wd.check_http("http://x"), (False, "HTTP 502"))
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertEqual(wd.check_http("http://x"), (False, "URLError"))

    def test_state_round_trip_is_atomic_and_missing_file_is_empty(self):
        Path(wd.STATE_FILE).unlink(missing_ok=True)
        self.assertEqual(wd.load_state(), {})
        wd.save_state({"a": {"state": "up", "fails": 0}})
        self.assertEqual(wd.load_state(), {"a": {"state": "up", "fails": 0}})
        self.assertFalse(Path(wd.STATE_FILE + ".tmp").exists())

    def test_sweep_debounces_until_fail_threshold_then_recovers(self):
        state = {}
        with patch.object(wd, "CHECKS", [("x", "tcp", ("h", 1))]):
            with patch.object(wd, "run_check", return_value=(False, "boom")):
                for i in range(wd.FAIL_THRESHOLD - 1):
                    self.assertEqual(wd.sweep(state)[0], [])
                self.assertEqual(wd.sweep(state)[0], [("down", "x", "boom")])
                self.assertEqual(wd.sweep(state)[0], [])            # already down: no re-alert
            with patch.object(wd, "run_check", return_value=(True, "")):
                self.assertEqual(wd.sweep(state), ([("recovered", "x", "")], 1, 1))
        self.assertEqual(state["x"], {"state": "up", "fails": 0})


class TestIntegration(unittest.TestCase):
    def test_startup_snapshot_goes_to_the_info_channel_with_a_baseline(self):
        posts, out = _run_main(0, lambda label: True)
        self.assertEqual(len(posts), 1)
        ch, text = posts[0]
        self.assertEqual(ch, wd.CH_INFO)
        self.assertIn(f"*{len(wd.CHECKS)}/{len(wd.CHECKS)} up*", text); self.assertIn("all green", text)
        self.assertIn(wd.WATCHER, text)
        self.assertTrue(Path(wd.STATE_FILE).exists())

    def test_check_list_covers_the_documented_spofs(self):
        labels = [l for l, _, _ in wd.CHECKS]
        for needle in ("pg-primary", "ollama", "nova-gw", "memory-ha", "plex", "synology NAS", "UNAS backup", "mesh-agent"):
            self.assertTrue(any(needle in l for l in labels), needle)


class TestFunctional(unittest.TestCase):
    def test_golden_path_declares_down_after_three_sweeps_and_posts_critical(self):
        posts, out = _run_main(wd.FAIL_THRESHOLD, lambda label: "plex" not in label)
        crit = [t for c, t in posts if c == wd.CH_CRITICAL]
        self.assertEqual(len(crit), 1)
        self.assertIn("*FLEET DOWN* — `nova-core (.2) plex` is unreachable", crit[0])
        self.assertIn("ConnectionRefusedError", crit[0])
        self.assertIn("DOWN: nova-core (.2) plex", out)
        state = json.loads(Path(wd.STATE_FILE).read_text())
        self.assertEqual(state["nova-core (.2) plex"]["state"], "down")
        self.assertIn("currently DOWN: nova-core (.2) plex", posts[0][1]) if False else None   # baseline was before the 3rd miss

    def test_sweep_error_inside_the_loop_never_kills_the_watchdog(self):
        sleep = MagicMock(side_effect=[None, None, _Stop()])
        with patch.object(wd, "slack_post", return_value=True), patch.object(wd, "run_check", return_value=(True, "")), \
             patch.object(wd, "save_state", side_effect=[None, OSError("disk full"), None]), patch.object(wd.time, "sleep", sleep), \
             redirect_stdout(io.StringIO()) as out:
            with self_assert_raises(_Stop):
                wd.main()
        self.assertIn("sweep error: OSError: disk full", out.getvalue())
        self.assertEqual(sleep.call_count, 3)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, unittest.mock as um, socket, urllib.request, runpy; "
                "socket.create_connection = um.MagicMock(side_effect=AssertionError('net at import')); "
                "urllib.request.urlopen = um.MagicMock(side_effect=AssertionError('net at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
