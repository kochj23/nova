#!/usr/bin/env python3
"""Tests for nova_llm_budget_guard.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_llm_budget_guard.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="budget-test-"))

import psycopg2  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bg = _load("budget_guard_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(usage=1813.0, keys=None, fail=None):
    """Fake urlopen for the three OpenRouter endpoints; records (url, method, body, auth)."""
    calls = []

    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append((url, req.get_method(), json.loads(req.data) if req.data else None,
                      req.get_header("Authorization")))
        if fail and fail in url:
            raise OSError("openrouter down")
        if url.endswith("/credits"):
            return _Resp({"data": {"total_usage": usage}})
        if url.endswith("/keys"):
            return _Resp({"data": keys if keys is not None else [{"name": "Nova runtime", "hash": "h-nova"}]})
        if "/keys/" in url:
            return _Resp({"data": {"ok": True}})
        raise AssertionError(url)
    urlopen.calls = calls
    return urlopen


def _kc(runtime="sk-or-runtime", prov="sk-or-prov"):
    def run(cmd, capture_output=False, text=False):
        name = cmd[cmd.index("-s") + 1]
        return types.SimpleNamespace(returncode=0, stdout={bg.RUNTIME_KEY: runtime, bg.PROV_KEY: prov}.get(name, "") or "")
    return run


def _pg(state=None):
    cur = MagicMock(); cur.fetchone.return_value = (state,) if state is not None else None
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _run_main(kc=None, http=None, state=None, today="2026-07-02", notify=None):
    conn, cur = _pg(state)
    out = io.StringIO()
    nn = types.ModuleType("nova_notify"); nn.notify = notify or MagicMock(return_value=True)
    http = http or _http()
    fixed = types.SimpleNamespace(now=lambda: types.SimpleNamespace(strftime=lambda f: today))
    with patch.object(bg.subprocess, "run", kc or _kc()), patch.object(bg.urllib.request, "urlopen", http), \
         patch("psycopg2.connect", return_value=conn), patch.object(bg, "datetime", fixed), \
         patch.dict(sys.modules, {"nova_notify": nn}), redirect_stdout(out):
        bg.main()
    return out.getvalue(), http, cur, nn.notify


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"](sk-|[A-Za-z0-9+/]{20,})", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"security", "find-generic-password"', SRC)       # Keychain, argv form
        self.assertNotIn("shell=True", SRC)

    def test_sql_is_parameterized_and_keys_only_travel_in_headers(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        out, http, cur, _ = _run_main()
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT INTO service_config", sql); self.assertIn("%s", sql)
        self.assertEqual(json.loads(params[0]), {"day": "2026-07-02", "baseline": 1813.0})
        for url, method, body, auth in http.calls:
            self.assertNotIn("sk-or", url)
            self.assertTrue(auth.startswith("Bearer sk-or-"))
        self.assertNotIn("sk-or-", out)          # keys never echoed to stdout/logs


class TestPerformance(unittest.TestCase):
    def test_ceiling_for_10k(self):
        st = {}
        t0 = time.perf_counter()
        for i in range(10_000):
            st, target = bg.ceiling_for(st, f"2026-01-{(i % 28) + 1:02d}", 1000.0 + i * 0.01)
        self.assertLess(time.perf_counter() - t0, 0.3)
        self.assertEqual(target, round(st["baseline"] + bg.DAILY, 2))


class TestRetry(unittest.TestCase):
    def test_credits_outage_is_one_shot_and_escapes(self):
        # RETRY GAP: main()/_get(credits) — a single GET; failure propagates (no state is written)
        http = _http(fail="/credits")
        with self.assertRaises(OSError):
            _run_main(http=http)
        self.assertEqual(len(http.calls), 1)

    def test_load_state_fails_open_and_save_state_does_not(self):
        # RETRY GAP: load_state()/psycopg2.connect — one attempt, {} on failure; save_state()/connect raises
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")) as pc:
            self.assertEqual(bg.load_state(), {})
            with self.assertRaises(RuntimeError):
                bg.save_state({"day": "x"})
        self.assertEqual(pc.call_count, 2)

    def test_missing_keychain_entry_fails_open_to_none(self):
        with patch.object(bg.subprocess, "run", _kc(runtime="")):
            self.assertIsNone(bg.kc(bg.RUNTIME_KEY))


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        with redirect_stdout(io.StringIO()) as out:
            bg.selftest()
        self.assertIn("selftest OK", out.getvalue())

    def test_ceiling_for_edges(self):
        st, t = bg.ceiling_for({}, "2026-07-02", 0.0)
        self.assertEqual((st, t), ({"day": "2026-07-02", "baseline": 0.0}, 10.0))
        st, t = bg.ceiling_for({"day": "2026-07-02"}, "2026-07-02", 12.25)      # same day, no baseline stored
        self.assertEqual(t, 22.25)
        st, t = bg.ceiling_for({"day": "2026-07-01", "baseline": 1.0}, "2026-07-02", 99.999)
        self.assertEqual((st["baseline"], t), (99.999, 110.0))

    def test_kc_strips_whitespace(self):
        with patch.object(bg.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="  sk-or-x\n")):
            self.assertEqual(bg.kc("whatever"), "sk-or-x")


class TestIntegration(unittest.TestCase):
    def test_state_round_trips_through_service_config(self):
        conn, cur = _pg({"day": "2026-07-01", "baseline": 5.0})
        with patch("psycopg2.connect", return_value=conn):
            self.assertEqual(bg.load_state(), {"day": "2026-07-01", "baseline": 5.0})
            bg.save_state({"day": "2026-07-02", "baseline": 7.0})
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        self.assertIn("WHERE service='nova' AND key='llm_budget'", sqls[0])
        self.assertIn("ON CONFLICT (service,key) DO UPDATE", sqls[1])
        self.assertTrue(conn.commit.called)

    def test_same_day_rerun_is_idempotent(self):
        out, http, cur, _ = _run_main(state={"day": "2026-07-02", "baseline": 1813.0}, http=_http(usage=1817.5))
        self.assertIn("spent_today=$4.50 target_limit=$1823.0", out)
        patch_call = [c for c in http.calls if c[1] == "PATCH"][0]
        self.assertEqual(patch_call[2], {"limit": 1823.0})         # ceiling did not move within the day


class TestFunctional(unittest.TestCase):
    def test_enforce_path_patches_the_runtime_key(self):
        out, http, cur, notify = _run_main()
        urls = [(u.split("/v1")[1], m) for u, m, _, _ in http.calls]
        self.assertEqual(urls, [("/credits", "GET"), ("/keys", "GET"), ("/keys/h-nova", "PATCH")])
        self.assertEqual(http.calls[2][2], {"limit": 1823.0})
        self.assertEqual(http.calls[2][3], "Bearer sk-or-prov")
        self.assertIn("hard $10.0/day cap active", out)
        self.assertTrue(cur.execute.called)
        self.assertFalse(notify.called)

    def test_monitor_only_path_warns_when_over_budget(self):
        http = _http(usage=1825.0)
        out, http, cur, notify = _run_main(kc=_kc(prov=""), http=http, state={"day": "2026-07-02", "baseline": 1813.0})
        self.assertEqual([m for _, m, _, _ in http.calls], ["GET"])      # never touches /keys without a provisioning key
        self.assertIn("CANNOT be auto-frozen", out)
        self.assertEqual(notify.call_args[1]["level"], "warning")
        self.assertEqual(notify.call_args[1]["category"], "cost")
        self.assertTrue(cur.execute.called)                              # state still saved

    def test_error_paths(self):
        out, http, cur, _ = _run_main(kc=_kc(runtime=""))
        self.assertEqual((out.strip(), http.calls), ("no runtime key in Keychain", []))
        out, http, cur, _ = _run_main(http=_http(keys=[{"name": "a", "hash": "1"}, {"name": "b", "hash": "2"}]))
        self.assertIn("could not identify runtime key among 2 keys", out)
        self.assertEqual(len([c for c in http.calls if c[1] == "PATCH"]), 0)
        out, http, cur, _ = _run_main(http=_http(keys=[{"name": "only", "hash": "solo"}]))
        self.assertTrue(http.calls[-1][0].endswith("/keys/solo"))        # a single key is assumed to be ours


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_llm_budget_guard"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
