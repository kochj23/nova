#!/usr/bin/env python3
"""Tests for nova_gateway/autonomy.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The asyncpg pool is a pure-Python fake; the module cache is reset before every test."""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "autonomy.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="gw-autonomy-test-"))
(TMP / ".openclaw" / "logs").mkdir(parents=True)

# the package __init__ imports main.py, which opens ~/.openclaw/logs/nova_gateway_v2.log: HOME -> tempdir
with patch.dict(os.environ, {"HOME": str(TMP), "NOVA_TEST_QUIET": "1"}):
    import nova_gateway.autonomy as au


class _Row(dict):
    def keys(self):
        return super().keys()


def _rule(tool, channel, level, pat=None, prio=0):
    return _Row(action_type=tool, channel=channel, level=level, arg_pattern=pat, priority=prio)


class _Pool:
    def __init__(self, rows=(), fetchrow=None, fail=0):
        self.rows = list(rows); self._fetchrow = fetchrow; self.fail = fail
        self.fetch_calls = 0; self.executed = []; self.fetchrow_args = []

    async def fetch(self, sql, *args):
        self.fetch_calls += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError("pool closed")
        return self.rows

    async def fetchrow(self, sql, *args):
        self.fetchrow_args.append((sql, args))
        if isinstance(self._fetchrow, Exception):
            raise self._fetchrow
        return self._fetchrow

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        au._cache = {}
        au._cache_ts = 0


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'pool\.\w+\(\s*f["\']')
        pool = _Pool(fetchrow={"pending_id": "p1"})
        run(au.request_approval(pool, "t", "s", "shell", {"cmd": "x'; --"}, "c" * 900))
        sql, args = pool.fetchrow_args[0]
        self.assertIn("$1", sql)
        self.assertNotIn("x'; --", sql)
        self.assertEqual(len(args[4]), 500)   # context preview capped

    def test_unknown_tool_defaults_to_notify_not_auto(self):
        self.assertEqual(run(au.check_autonomy(_Pool(), "never_seen_tool", "slack")), "notify")


class TestPerformance(_Base):
    def test_scoped_lookup_10k_fast(self):
        run(au.load_autonomy_cache(_Pool([_rule("shell", "*", "approve", r"rm\s+-rf"),
                                          _rule("shell", "slack", "auto", r"\buptime\b", 1)])))
        t0 = time.perf_counter()
        for i in range(10_000):
            au.scoped_level("shell", "slack", {"cmd": f"uptime {i}"})
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_cache_load_failure_fails_open_and_retries_next_call(self):
        # RETRY GAP: load_autonomy_cache/pool.fetch — one attempt per call; on failure _cache_ts stays stale,
        # so the NEXT check_autonomy retries the load (TTL-driven retry, no backoff)
        pool = _Pool([_rule("shell", "*", "approve")], fail=1)
        self.assertEqual(run(au.check_autonomy(pool, "shell")), "notify")      # safe default
        self.assertEqual(run(au.check_autonomy(pool, "shell")), "approve")     # reloaded
        self.assertEqual(pool.fetch_calls, 2)

    def test_db_writes_fail_open(self):
        # RETRY GAP: request_approval/resolve_pending/get_pending_approvals — one attempt, safe default
        boom = _Pool(fetchrow=RuntimeError("down"), fail=1)
        self.assertEqual(run(au.request_approval(boom, "t", "s", "x", {})), "")
        self.assertIsNone(run(au.resolve_pending(boom, "p", True)))
        self.assertEqual(run(au.get_pending_approvals(boom)), [])


class TestUnit(_Base):
    def test_channel_of(self):
        self.assertEqual(au.channel_of("gw2:slack:C123"), "slack")
        for s in (None, "", "gw2:slack", "x:slack:C1", "gw2::C1"):
            self.assertEqual(au.channel_of(s), "*")

    def test_bad_regex_rule_skipped(self):
        cache = run(au.load_autonomy_cache(_Pool([_rule("shell", "*", "auto", "(unclosed"),
                                                  _rule("web", "*", "auto")])))
        self.assertNotIn(("__scoped__", "shell"), cache)
        self.assertEqual(cache[("web", "*")], "auto")

    def test_scoped_precedence_exact_channel_then_priority(self):
        run(au.load_autonomy_cache(_Pool([_rule("shell", "*", "approve", "rm", 0),
                                          _rule("shell", "slack", "notify", "rm", 0),
                                          _rule("shell", "*", "auto", "rm", 5)])))
        self.assertEqual(au.scoped_level("shell", "slack", {"cmd": "rm x"}), "auto")     # 0+5 beats 2+0
        self.assertIsNone(au.scoped_level("shell", "slack", {"cmd": "ls"}))
        self.assertIsNone(au.scoped_level("other", "slack", {}))


class TestIntegration(_Base):
    def test_check_autonomy_precedence_chain(self):
        pool = _Pool([_rule("shell", "*", "approve"), _rule("shell", "slack", "notify"),
                      _rule("shell", "*", "auto", r'"cmd": "uptime"')])
        self.assertEqual(run(au.check_autonomy(pool, "shell", "slack", {"cmd": "uptime"})), "auto")
        self.assertEqual(run(au.check_autonomy(pool, "shell", "slack", {"cmd": "ls"})), "notify")
        self.assertEqual(run(au.check_autonomy(pool, "shell", "discord", {"cmd": "ls"})), "approve")
        self.assertEqual(pool.fetch_calls, 1)    # cached within TTL

    def test_update_rule_forces_cache_refresh(self):
        au._cache_ts = time.time()
        pool = _Pool()
        run(au.update_rule(pool, "shell", "*", "auto", arg_pattern="uptime"))
        self.assertEqual(au._cache_ts, 0)
        self.assertIn("autonomy_rules", pool.executed[0][0])
        self.assertEqual(pool.executed[0][1][-1], "uptime")


class TestFunctional(_Base):
    def test_approval_round_trip(self):
        pool = _Pool(fetchrow={"pending_id": "p9"})
        pid = run(au.request_approval(pool, "tr", "gw2:slack:C1", "shell", {"cmd": "reboot"}))
        self.assertEqual(pid, "p9")
        pool._fetchrow = {"action_type": "shell", "tool_params": json.dumps({"cmd": "reboot"})}
        self.assertEqual(run(au.resolve_pending(pool, "p9", True)),
                         {"action_type": "shell", "tool_params": {"cmd": "reboot"}})
        self.assertIsNone(run(au.resolve_pending(pool, "p9", False)))
        self.assertEqual(pool.fetchrow_args[-1][1][0], "denied")

    def test_notify_execution_posts_and_swallows_errors(self):
        post = AsyncMock()
        run(au.notify_execution(None, "shell", {"cmd": "uptime"}, "ok" * 300, slack_post_fn=post))
        msg = post.call_args.args[0]
        self.assertIn("Auto-executed:* `shell`", msg)
        run(au.notify_execution(None, "shell", {}, "r", slack_post_fn=AsyncMock(side_effect=RuntimeError("x"))))
        run(au.notify_execution(None, "shell", {}, "r"))  # no poster -> no-op


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import importlib.util as u;"
                            f"s=u.spec_from_file_location('a', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                            "s.loader.exec_module(m); print(m._CACHE_TTL, m._cache)"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "300 {}")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
