#!/usr/bin/env python3
"""Tests for nova_expectations.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_expectations.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="expect-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ex = _load("expectations_under_test", SCRIPT)


class _Cur:
    """Cursor stub: `rows` answer the registry SELECT, `scalar` answers pg_rows/pg_scalar fetchone."""
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar, self.sql, self.params = list(rows), scalar, [], []
        self.description = [(c,) for c in ("name", "kind", "target", "dsn", "host", "max_silence_h", "min_units", "note")]

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchall(self):
        return [tuple(r.get(c[0]) for c in self.description) for r in self.rows]

    def fetchone(self):
        return self.scalar

    def ran(self, frag):
        return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None, close=lambda: None)


def _exp(**kw):
    e = {"name": "nightly-dump", "kind": "file_mtime", "target": str(TMP / "dump.sql"), "dsn": None, "host": None,
         "max_silence_h": 26, "min_units": 0, "note": "pg dump"}
    e.update(kw)
    return e


def _args(quiet=False):
    return types.SimpleNamespace(quiet=quiet)


def _run_check(rows, measure=None, quiet=False, cfg=None):
    cur = _Cur(rows)
    cfg = cfg or types.ModuleType("nova_config"); cfg.post_both = getattr(cfg, "post_both", MagicMock()); cfg.SLACK_ALERTS = "C_ALERTS"
    with patch.object(ex, "conn", lambda dsn=None: _conn(cur)), patch.dict(sys.modules, {"nova_config": cfg}), \
         patch.object(ex, "measure", measure or ex.measure), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        rc = ex.check(_args(quiet))
    return rc, cur, cfg, out.getvalue() + err.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ex.DSN)

    def test_registry_writes_are_parameterized(self):
        cur = _Cur()
        a = types.SimpleNamespace(add="x'; DROP TABLE job_expectations; --", kind="http", target="http://t", dsn=None, host=None,
                                  max_silence_h=1.0, min_units=0, note="")
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)), redirect_stdout(io.StringIO()):
            ex.add(a)
        sql, params = cur.ran("INSERT INTO job_expectations")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], a.add)
        rc, cur, _, _ = _run_check([_exp(name="n'; DROP TABLE x; --", kind="http")], measure=lambda e: (0.0, 1, "HTTP 200"))
        self.assertEqual(cur.ran("UPDATE job_expectations SET last_checked")[0][1], ("n'; DROP TABLE x; --",))

    def test_pg_rows_targets_are_operator_registered_not_user_input(self):
        # pg_rows interpolates table/column into SQL by design (identifiers cannot be bound). The only way in is
        # --add on this box, and the file must say so; the probe itself must never run anything but SELECT.
        self.assertIn('kind == "pg_rows"', SRC)
        cur = _Cur(scalar=(None, 0))
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)):
            ex.measure(_exp(kind="pg_rows", target="telemetry.events:ts", max_silence_h=4))
        self.assertTrue(cur.sql[0].startswith("SELECT max(ts), count(*) FILTER (WHERE ts > now() - interval '4 hours') FROM telemetry.events"))

    def test_remote_commands_run_without_a_shell_and_batchmode(self):
        self.assertNotIn("shell=True", SRC)
        seen = []
        with patch.object(ex.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or types.SimpleNamespace(stdout="1700000000", returncode=0)):
            ex.measure(_exp(host="nas", target="/vol/x"))
        self.assertEqual(seen[0][:5], ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"])


class TestPerformance(unittest.TestCase):
    def test_10k_expectations_classify_fast(self):
        rows = [_exp(name=f"e{i}", kind="http", max_silence_h=1, min_units=1) for i in range(10_000)]
        probe = lambda e: (0.0, 1, "HTTP 200") if int(e["name"][1:]) % 2 else (None, 0, "HTTP 503")
        t0 = time.perf_counter()
        rc, cur, cfg, out = _run_check(rows, measure=probe, quiet=True)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(rc, 1)
        self.assertEqual(len(cur.ran("SET last_checked")), 10_000)
        self.assertEqual(len(cur.ran("last_ok=now()")), 5_000)


class TestRetry(unittest.TestCase):
    def test_probe_errors_fail_open_as_missing(self):
        # RETRY GAP: measure() — every probe (pg, ssh stat, curl) is one attempt; any exception becomes
        # (None, 0, "check error: …") so the expectation reads MISSING, which is the alert, never a crash
        with patch.object(ex, "conn", lambda dsn=None: (_ for _ in ()).throw(OSError("pg refused"))):
            self.assertEqual(ex.measure(_exp(kind="pg_rows", target="t:ts")), (None, 0, "check error: pg refused"))
        with patch.object(ex.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("curl", 30))):
            age, units, detail = ex.measure(_exp(kind="http", target="http://x"))
        self.assertIsNone(age); self.assertTrue(detail.startswith("check error:"))

    def test_alert_failure_is_swallowed_and_still_returns_1(self):
        # RETRY GAP: check()/nova_config.post_both — one attempt; a Slack failure prints to stderr, rc stays 1
        cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(side_effect=OSError("slack down"))
        rc, cur, cfg, out = _run_check([_exp(kind="http")], measure=lambda e: (None, 0, "HTTP 503"), cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertIn("(alert failed: slack down)", out)
        self.assertEqual(len(cur.ran("SET last_checked")), 1)

    def test_unknown_kind_is_missing_not_green(self):
        self.assertEqual(ex.measure(_exp(kind="carrier_pigeon")), (None, 0, "unknown kind carrier_pigeon"))


class TestUnit(unittest.TestCase):
    def test_file_mtime_local(self):
        f = TMP / "dump.sql"; f.write_text("x")
        age, units, detail = ex.measure(_exp(target=str(f)))
        self.assertLess(age, 0.01); self.assertEqual(units, 1); self.assertTrue(detail.startswith("mtime "))
        self.assertEqual(ex.measure(_exp(target=str(TMP / "nope"))), (None, 0, "file missing"))

    def test_file_mtime_remote(self):
        with patch.object(ex.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout="", returncode=0)):
            self.assertEqual(ex.measure(_exp(host="nas", target="/x")), (None, 0, "file missing or host unreachable"))
        old = int(time.time()) - 48 * 3600
        with patch.object(ex.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout=f"{old}\n", returncode=0)):
            age, units, _ = ex.measure(_exp(host="nas", target="/x"))
        self.assertAlmostEqual(age, 48, delta=0.1); self.assertEqual(units, 1)

    def test_http_codes(self):
        for code, ok in (("200", True), ("301", True), ("404", False), ("", False)):
            with patch.object(ex.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout=code, returncode=0)):
                age, units, detail = ex.measure(_exp(kind="http", target="http://x"))
            self.assertEqual((age, units), ((0.0, 1) if ok else (None, 0)), code)
            self.assertEqual(detail, f"HTTP {code}")

    def test_http_contains_today_expansion_and_malformed_target(self):
        today = datetime.now().date().isoformat()
        with patch.object(ex.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout=f"<a href='/ops/{today}-x'>", returncode=0)):
            self.assertEqual(ex.measure(_exp(kind="http_contains", target="http://j||/ops/{today}")), (0.0, 1, f"found '/ops/{today}'"))
            self.assertEqual(ex.measure(_exp(kind="http_contains", target="http://j||/ops/1999"))[:2], (None, 0))
        self.assertEqual(ex.measure(_exp(kind="http_contains", target="http://j")), (None, 0, "malformed target (expected '<url>||<substring>')"))
        self.assertEqual(ex.measure(_exp(kind="http_contains", target="||x"))[0], None)
        with patch.object(ex.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout="", returncode=6)):
            self.assertEqual(ex.measure(_exp(kind="http_contains", target="http://j||x")), (None, 0, "fetch failed rc=6"))

    def test_pg_rows_and_pg_scalar(self):
        newest = datetime.now(timezone.utc) - timedelta(hours=3)
        cur = _Cur(scalar=(newest, 42))
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)):
            age, units, detail = ex.measure(_exp(kind="pg_rows", target="t:ts"))
        self.assertAlmostEqual(age, 3, delta=0.05); self.assertEqual(units, 42); self.assertTrue(detail.startswith("newest "))
        cur = _Cur(scalar=(None, 0))
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)):
            self.assertEqual(ex.measure(_exp(kind="pg_rows", target="t:ts")), (None, 0, "t is empty"))
        cur = _Cur(scalar=(7,))
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)):
            self.assertEqual(ex.measure(_exp(kind="pg_scalar", target="SELECT 7")), (0.0, 7, "query returned 7"))
        cur = _Cur(scalar=None)
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)):
            self.assertEqual(ex.measure(_exp(kind="pg_scalar", target="SELECT 1 WHERE false"))[1], 0)

    def test_argparse_defaults(self):
        with patch.object(sys, "argv", ["nova_expectations.py", "--add", "x", "--kind", "http", "--target", "http://t"]), \
             patch.object(ex, "add", lambda a: a):
            a = ex.main()
        self.assertEqual((a.max_silence_h, a.min_units, a.note, a.quiet), (26, 0, "", False))


class TestIntegration(unittest.TestCase):
    def test_check_classifies_missing_stale_nowork_ok(self):
        rows = [_exp(name="missing", kind="http"), _exp(name="stale", kind="http", max_silence_h=1),
                _exp(name="nowork", kind="http", min_units=5), _exp(name="fine", kind="http")]
        table = {"missing": (None, 0, "HTTP 503"), "stale": (5.0, 1, "x"), "nowork": (0.5, 2, "y"), "fine": (0.5, 1, "z")}
        rc, cur, cfg, out = _run_check(rows, measure=lambda e: table[e["name"]])
        self.assertEqual(rc, 1)
        self.assertIn("[MISSING] missing", out); self.assertIn("[STALE  ] stale", out)
        self.assertIn("[NO WORK] nowork", out); self.assertIn("[ok     ] fine", out)
        self.assertEqual(len(cur.ran("SET last_checked")), 4)
        self.assertEqual(len(cur.ran("last_ok=now()")), 1)
        msg = cfg.post_both.call_args[0][0]
        self.assertTrue(msg.startswith("🔴 *Work that should have happened and did not*"))
        self.assertIn("• *stale* — STALE: 5.0h old (limit 1h) — x", msg)
        self.assertIn("_pg dump_", msg)
        self.assertEqual(cfg.post_both.call_args[1], {"slack_channel": "C_ALERTS"})

    def test_schema_is_created_and_only_enabled_rows_are_read(self):
        rc, cur, _, _ = _run_check([])
        self.assertTrue(cur.sql[0].startswith("CREATE TABLE IF NOT EXISTS job_expectations"))
        self.assertIn("FROM job_expectations WHERE enabled ORDER BY name", cur.sql[1])
        self.assertEqual(rc, 0)

    def test_add_upserts_and_reenables(self):
        cur = _Cur()
        a = types.SimpleNamespace(add="n", kind="pg_rows", target="t:ts", dsn="d", host=None, max_silence_h=2.0, min_units=3, note="why")
        with patch.object(ex, "conn", lambda dsn=None: _conn(cur)), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ex.add(a), 0)
        sql, params = cur.ran("INSERT INTO job_expectations")[0]
        self.assertIn("ON CONFLICT (name) DO UPDATE", sql); self.assertIn("enabled=true", sql)
        self.assertEqual(params, ("n", "pg_rows", "t:ts", "d", None, 2.0, 3, "why"))
        self.assertIn("registered n", out.getvalue())


class TestFunctional(unittest.TestCase):
    def test_golden_path_all_satisfied(self):
        f = TMP / "fresh.bin"; f.write_text("x")
        rows = [_exp(name="dump", target=str(f)), _exp(name="site", kind="http", target="http://x")]
        with patch.object(ex.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout="200", returncode=0)):
            rc, cur, cfg, out = _run_check(rows)
        self.assertEqual(rc, 0)
        self.assertIn("=== 2 expectations, all satisfied ===", out)
        cfg.post_both.assert_not_called()
        self.assertEqual(len(cur.ran("last_ok=now()")), 2)

    def test_quiet_prints_only_problems(self):
        rows = [_exp(name="good", kind="http"), _exp(name="bad", kind="http")]
        rc, cur, cfg, out = _run_check(rows, measure=lambda e: (0.0, 1, "ok") if e["name"] == "good" else (None, 0, "HTTP 500"), quiet=True)
        self.assertNotIn("good", out.split("🔴")[0]); self.assertIn("[MISSING] bad", out)
        self.assertEqual(rc, 1)

    def test_empty_registry_hints_and_exits_zero(self):
        rc, cur, cfg, out = _run_check([])
        self.assertEqual(rc, 0)
        self.assertIn("no expectations registered", out)
        cfg.post_both.assert_not_called()

    def test_main_dispatches_check_vs_add(self):
        with patch.object(sys, "argv", ["nova_expectations.py", "--quiet"]), patch.object(ex, "check", lambda a: ("check", a.quiet)):
            self.assertEqual(ex.main(), ("check", True))
        with patch.object(sys, "argv", ["nova_expectations.py", "--add", "n", "--kind", "http", "--target", "u"]), patch.object(ex, "add", lambda a: ("add", a.add)):
            self.assertEqual(ex.main(), ("add", "n"))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--max-silence-h", r.stdout)
        self.assertIn("http_contains", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_expectations"], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
