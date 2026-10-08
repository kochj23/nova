#!/usr/bin/env python3
"""Tests for nova_homekit_redis_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a daemon BODY: its poll loop sits at module level, gated on a `_shutdown` flag that only the
SIGTERM/SIGINT handler flips. Loading it therefore means running the loop; `_boot()` captures the signal
handler at registration, and the stubbed urlopen calls it so the loop runs exactly one iteration offline."""
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homekit_redis_bridge.py"
SRC = SCRIPT.read_text()


@contextmanager
def _stub_modules(**mods):
    """Install import stubs and afterwards restore ONLY those keys (a whole-dict restore would drop every module
    the script imported for the first time, leaving e.g. a second `urllib` package without `.request`)."""
    mp = pytest.MonkeyPatch()
    for k, v in mods.items():
        mp.setitem(sys.modules, k, v)
    try:
        yield
    finally:
        mp.undo()


class _FakeRedis:
    instances = []

    def __init__(self, **kw):
        self.kw = kw; self.store = {}; self.calls = []; _FakeRedis.instances.append(self)

    def setex(self, key, ttl, value):
        self.calls.append((key, ttl, value)); self.store[key] = value


def _resp(payload):
    r = MagicMock(); r.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return r


def _boot(responses, stop_after=None):
    """Load the bridge with every side effect stubbed. `responses` is a list of payloads or exceptions, one per poll;
    the signal handler is invoked on the last one so the loop ends. Returns (module, fake_redis, stdout, urlopen, sleeps)."""
    handlers = {}
    sleeps = []
    stop_after = len(responses) if stop_after is None else stop_after
    polls = []

    def _urlopen(url, timeout=None):
        polls.append((url, timeout))
        item = responses[len(polls) - 1]
        if len(polls) >= stop_after:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        if isinstance(item, BaseException):
            raise item
        return _resp(item)

    redis_stub = types.ModuleType("redis"); redis_stub.Redis = _FakeRedis
    _FakeRedis.instances.clear()
    spec = importlib.util.spec_from_file_location("hkbridge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with _stub_modules(redis=redis_stub), \
         patch("signal.signal", side_effect=lambda sig, fn: handlers.__setitem__(sig, fn)), \
         patch("urllib.request.urlopen", side_effect=_urlopen) as u, \
         patch("time.sleep", side_effect=sleeps.append), redirect_stdout(io.StringIO()) as out:
        spec.loader.exec_module(mod)
    mod._test_handlers = handlers; mod._test_polls = polls
    return mod, _FakeRedis.instances[0], out.getvalue(), u, sleeps


ACCS = [{"name": "Eve Strip", "room": "Office", "services": []}, {"name": "Eve Door", "room": "Hall", "services": []}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", SRC)

    def test_only_loopback_endpoints_and_no_eval_of_fetched_data(self):
        mod, r, out, u, _ = _boot([ACCS])
        self.assertTrue(mod.HOMEKIT_URL.startswith("http://127.0.0.1:"))
        self.assertEqual((r.kw["host"], r.kw["port"]), ("127.0.0.1", 6379))
        self.assertNotIn("eval(", SRC); self.assertNotIn("exec(", SRC); self.assertNotIn("shell=", SRC)
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))

    def test_payload_is_cached_verbatim_under_the_fixed_key_only(self):
        inj = [{"name": "x'; FLUSHALL; --"}]
        mod, r, out, u, _ = _boot([inj])
        self.assertEqual(r.calls, [(mod.REDIS_KEY, 300, json.dumps(inj))])
        self.assertEqual(set(r.store), {"nova:homekit:accessories"})


class TestPerformance(unittest.TestCase):
    def test_10k_accessory_payload_caches_in_one_iteration_fast(self):
        big = [{"name": f"acc{i}", "room": f"r{i % 7}", "services": [{"type": "t", "characteristics": []}]} for i in range(10_000)]
        t0 = time.perf_counter()
        mod, r, out, u, sleeps = _boot([big])
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("Cached 10000 accessories in Redis", out)
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(sleeps, [])                         # shutdown observed before the first sleep tick


class TestRetry(unittest.TestCase):
    def test_homekit_outage_is_logged_and_the_loop_keeps_polling(self):
        # RETRY GAP: poll loop/urlopen — no backoff, a failed poll is swallowed and the fixed INTERVAL cadence resumes
        mod, r, out, u, sleeps = _boot([OSError("connection refused"), ACCS])
        self.assertEqual(len(mod._test_polls), 2)
        self.assertIn("[homekit_bridge] Error: connection refused", out)
        self.assertEqual(len(r.calls), 1)                    # only the good poll reached Redis
        self.assertEqual(sleeps, [1] * mod.INTERVAL)          # one full 60 x 1s wait between the two polls

    def test_redis_outage_fails_open(self):
        # RETRY GAP: poll loop/redis.setex — one attempt per poll; the error is printed, the daemon stays up
        def boom(*a):
            raise ConnectionError("redis down")
        with patch.object(_FakeRedis, "setex", boom):
            mod, r, out, u, sleeps = _boot([ACCS])
        self.assertIn("[homekit_bridge] Error: redis down", out)
        self.assertEqual(len(mod._test_polls), 1)

    def test_bad_json_is_reported_not_fatal(self):
        mod, r, out, u, sleeps = _boot([b"<html>502</html>"])
        self.assertIn("[homekit_bridge] Error:", out)
        # observed behavior (not changed here): the raw body is cached BEFORE json.loads validates it
        self.assertEqual(r.calls[0][2], "<html>502</html>")


class TestUnit(unittest.TestCase):
    def test_constants(self):
        mod, *_ = _boot([[]])
        self.assertEqual(mod.REDIS_KEY, "nova:homekit:accessories")
        self.assertEqual(mod.INTERVAL, 60)
        self.assertGreater(300, mod.INTERVAL)               # cache TTL outlives the poll interval -> no gaps

    def test_signal_handler_flips_the_shutdown_flag(self):
        mod, *_ = _boot([[]])
        self.assertEqual(set(mod._test_handlers), {signal.SIGTERM, signal.SIGINT})
        mod._shutdown = False
        mod._sig(signal.SIGINT, None)
        self.assertTrue(mod._shutdown)

    def test_empty_list_is_a_valid_zero_cache(self):
        mod, r, out, *_ = _boot([[]])
        self.assertIn("Cached 0 accessories", out)
        self.assertEqual(r.calls, [(mod.REDIS_KEY, 300, "[]")])

    def test_urlopen_uses_the_long_homekit_timeout(self):
        mod, *_ = _boot([[]])
        self.assertEqual([(getattr(u, "full_url", u), t) for u, t in mod._test_polls], [(mod.HOMEKIT_URL, 120)])


class TestIntegration(unittest.TestCase):
    def test_bridge_reads_the_same_homekit_endpoint_as_the_other_ingesters(self):
        mod, *_ = _boot([[]])
        outlets = (SCRIPTS / "nova_homekit_outlets.py").read_text()
        sensors = (SCRIPTS / "nova_homekit_sensors.py").read_text()
        self.assertIn(f'HK_URL = "{mod.HOMEKIT_URL}"', outlets)
        self.assertIn(f'HOMEKIT_URL = "{mod.HOMEKIT_URL}"', sensors)

    def test_fetch_then_cache_round_trips_the_accessory_list(self):
        mod, r, out, *_ = _boot([ACCS])
        self.assertEqual(json.loads(r.store[mod.REDIS_KEY]), ACCS)
        self.assertEqual(r.kw.get("decode_responses"), True)


class TestFunctional(unittest.TestCase):
    def test_golden_path_one_poll_then_sigterm(self):
        mod, r, out, u, sleeps = _boot([ACCS])
        self.assertEqual(u.call_count, 1)
        self.assertEqual(r.calls, [("nova:homekit:accessories", 300, json.dumps(ACCS))])
        self.assertEqual(out.strip(), "[homekit_bridge] Cached 2 accessories in Redis")
        self.assertTrue(mod._shutdown)

    def test_two_polls_sleep_the_interval_between_them(self):
        mod, r, out, u, sleeps = _boot([ACCS, ACCS])
        self.assertEqual(u.call_count, 2); self.assertEqual(len(r.calls), 2)
        self.assertEqual(len(sleeps), mod.INTERVAL)

    def test_error_path_every_poll_failing_never_raises(self):
        mod, r, out, u, sleeps = _boot([OSError("a"), OSError("b")])
        self.assertEqual(out.count("[homekit_bridge] Error:"), 2); self.assertEqual(r.calls, [])


class TestFrame(unittest.TestCase):
    def test_compiles_and_is_a_guarded_daemon_body(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        # by design there is no main()/__main__ guard: the loop IS the program, and the only exit is the shutdown flag
        self.assertNotIn("def main", SRC); self.assertNotIn("__main__", SRC)
        self.assertIn("while not _shutdown:", SRC)
        self.assertIn("signal.signal(signal.SIGTERM, _sig)", SRC)

    def test_loading_in_process_runs_exactly_one_iteration_under_stubs(self):
        mod, r, out, u, sleeps = _boot([[]])
        self.assertEqual((u.call_count, len(r.calls)), (1, 1))


if __name__ == "__main__":
    unittest.main()
