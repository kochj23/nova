#!/usr/bin/env python3
"""Tests for nova_remediation.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_remediation.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rm = _load("remediation_under_test", SCRIPT)
GPU = {"ctx": ("Office-M4-2", "GPU wedged", "gpu")}


class _Cur:
    def __init__(self, ctx=None, recent=False, row=None):
        self.ctx, self.recent, self.row = ctx, recent, row
        self.sql, self.params, self._last, self.next_id = [], [], "", 0

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchone(self):
        s = self._last
        if "FROM telemetry.incidents" in s:
            return self.ctx
        if "SELECT 1 FROM telemetry.remediations" in s:
            return (1,) if self.recent else None
        if "INSERT INTO telemetry.remediations" in s:
            self.next_id += 1; return (self.next_id,)
        if "FROM telemetry.remediations WHERE id" in s:
            return self.row
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def writes(self, needle):
        return [p for s, p in zip(self.sql, self.params) if needle in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.commits = 0

    def cursor(self, cursor_factory=None):
        return self._cur

    def commit(self):
        self.commits += 1


def _ok(argv, **kw):
    return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_master_switch_on_but_allowlist_is_exact_argv_and_no_shell(self):
        self.assertIs(rm.REMEDIATION_ENABLED, True)
        self.assertNotIn("shell=True", SRC)
        for name, spec in rm.ACTIONS.items():
            self.assertIn(spec["tier"], (rm.SAFE, rm.IMPACTFUL), name)
            self.assertIsInstance(spec["argv"], list)
            self.assertTrue(all(isinstance(a, str) for a in spec["argv"]), name)
            self.assertNotIn("{", " ".join(spec["argv"]), name)  # no interpolation slots
        self.assertEqual(rm.ACTIONS["reboot_host"]["tier"], rm.IMPACTFUL)
        self.assertEqual(rm.ACTIONS["reboot_host"]["argv"][0], "/bin/echo")

    def test_non_allowlisted_name_never_reaches_subprocess(self):
        with mock.patch.object(rm.subprocess, "run") as run:
            res = rm.execute_action("rm_rf_root")
        self.assertFalse(res["ok"]); self.assertIn("allowlist", res["error"]); self.assertEqual(run.call_count, 0)

    def test_safe_step_runs_only_the_exact_argv_with_shell_false(self):
        with mock.patch.object(rm.subprocess, "run", side_effect=_ok) as run:
            res = rm.execute_action("restart_ollama")
        self.assertTrue(res["ok"])
        self.assertEqual(run.call_args[0][0], rm.ACTIONS["restart_ollama"]["argv"])
        self.assertIs(run.call_args[1]["shell"], False); self.assertEqual(run.call_args[1]["timeout"], 60)

    def test_sql_is_parameterized(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn("% (", SRC.split("def _recently_acted")[1].split("def execute_action")[0])


class TestPerformance(unittest.TestCase):
    def test_lookup_and_format_fast_on_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            rm._lookup_runbook("Office-M4-2", "gpu" if i % 2 else "nope")
            rm._fmt_result({"returncode": 0, "stdout": "x" * (i % 500), "stderr": ""})
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_execute_action_fails_open_on_subprocess_error(self):
        # RETRY GAP: execute_action — one attempt per step, by design (not idempotent to loop a kickstart).
        with mock.patch.object(rm.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 60)) as run:
            res = rm.execute_action("restart_notifier")
        self.assertFalse(res["ok"]); self.assertIn("60", res["error"]); self.assertEqual(run.call_count, 1)

    def test_public_api_never_raises_when_notify_or_db_explode(self):
        cur = _Cur(**GPU)
        with mock.patch.object(rm.subprocess, "run", side_effect=_ok), \
             mock.patch.object(rm.nova_notify, "notify", side_effect=RuntimeError("bus down")):
            out = rm.propose_for_incident(_Conn(cur), 1)
        self.assertIn("propose_for_incident failed", out["error"])

        class Boom:
            def cursor(self, **k):
                raise RuntimeError("db")
        self.assertIn("approve failed", rm.approve(Boom(), 1)["error"])


class TestUnit(unittest.TestCase):
    def test_fmt_result(self):
        self.assertEqual(rm._fmt_result({"error": "e"}), "error: e")
        self.assertEqual(rm._fmt_result({"note": "dry_run: not executed"}), "dry_run: not executed")
        self.assertEqual(rm._fmt_result({"returncode": 1, "stdout": " o ", "stderr": "e"}), "rc=1 out=o err=e")

    def test_lookup_runbook_and_dry_run(self):
        self.assertEqual(rm._lookup_runbook("Office-M4-2", "gpu"), ["restart_ollama", "reboot_host"])
        self.assertEqual(rm._lookup_runbook("nope", "gpu"), [])
        with mock.patch.object(rm.subprocess, "run") as run:
            res = rm.execute_action("clear_ollama_cache", dry_run=True)
        self.assertTrue(res["ok"] and res["dry_run"]); self.assertEqual(run.call_count, 0)

    def test_smoke_exists_and_is_guarded(self):
        self.assertTrue(callable(rm._smoke))
        self.assertIn('sys.argv[1] == "--smoke"', SRC)


class TestIntegration(unittest.TestCase):
    def _propose(self, cur, dry_run=False):
        calls = []
        with mock.patch.object(rm.subprocess, "run", side_effect=_ok) as run, \
             mock.patch.object(rm.nova_notify, "notify", side_effect=lambda *a, **k: calls.append((a, k))):
            out = rm.propose_for_incident(_Conn(cur), 42, dry_run=dry_run)
        return out, run, calls

    def test_safe_executes_impactful_only_proposed(self):
        cur = _Cur(**GPU)
        out, run, calls = self._propose(cur)
        st = {s["action"]: s["status"] for s in out["steps"]}
        self.assertEqual(st, {"restart_ollama": "executed", "reboot_host": "proposed"})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0], rm.ACTIONS["restart_ollama"]["argv"])
        recs = cur.writes("INSERT INTO telemetry.remediations")
        self.assertEqual([(r[1], r[3]) for r in recs], [("restart_ollama", "executed"), ("reboot_host", "proposed")])
        self.assertEqual(recs[0][4], " ".join(rm.ACTIONS["restart_ollama"]["argv"]))
        levels = {k["meta"]["action"]: k["level"] for _, k in calls}
        self.assertEqual(levels, {"restart_ollama": "warning", "reboot_host": "critical"})
        self.assertTrue(all(k["category"] == "remediation" for _, k in calls))

    def test_cooldown_skips_everything(self):
        cur = _Cur(ctx=GPU["ctx"], recent=True)
        out, run, calls = self._propose(cur)
        self.assertTrue(all(s["status"] == "skipped" and s["reason"] == "cooldown" for s in out["steps"]))
        self.assertEqual(run.call_count, 0); self.assertEqual(calls, [])
        self.assertEqual(cur.writes("INSERT INTO telemetry.remediations"), [])
        self.assertEqual(cur.writes("SELECT 1 FROM telemetry.remediations")[0][2], rm.COOLDOWN_S)

    def test_dry_run_executes_nothing(self):
        cur = _Cur(**GPU)
        out, run, calls = self._propose(cur, dry_run=True)
        self.assertEqual(run.call_count, 0)
        self.assertIn("dry_run: not executed", out["steps"][0]["result"])

    def test_approve_runs_the_noop_reboot_argv(self):
        cur = _Cur(row=(42, "reboot_host", "impactful", "proposed"))
        with mock.patch.object(rm.subprocess, "run", side_effect=_ok) as run, mock.patch.object(rm.nova_notify, "notify"):
            out = rm.approve(_Conn(cur), 5)
        self.assertEqual(out["status"], "executed")
        self.assertEqual(run.call_args[0][0][0], "/bin/echo")
        self.assertEqual([p[0] for p in cur.writes("UPDATE telemetry.remediations")], ["approved", "executed"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_propose_then_approve(self):
        cur = _Cur(**GPU)
        with mock.patch.object(rm.subprocess, "run", side_effect=_ok) as run, mock.patch.object(rm.nova_notify, "notify") as n:
            out = rm.propose_for_incident(_Conn(cur), 42)
            rid = out["steps"][1]["remediation_id"]
            cur.row = (42, "reboot_host", "impactful", "proposed")
            ap = rm.approve(_Conn(cur), rid)
        self.assertEqual(out["runbook"], ["restart_ollama", "reboot_host"])
        self.assertEqual(ap["status"], "executed"); self.assertEqual(run.call_count, 2)
        self.assertEqual(n.call_args[1]["dedup_key"], f"remediation-approve-{rid}")
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS telemetry.remediations" in s for s in cur.sql))

    def test_error_paths(self):
        self.assertEqual(rm.propose_for_incident(_Conn(_Cur(ctx=None)), 1)["error"], "no open incident / not found")
        self.assertIn("no runbook", rm.propose_for_incident(_Conn(_Cur(ctx=("nas", "t", "disk"))), 1)["error"])
        self.assertEqual(rm.approve(_Conn(_Cur(row=None)), 1)["error"], "remediation not found")
        self.assertIn("not approvable", rm.approve(_Conn(_Cur(row=(1, "reboot_host", "impactful", "executed"))), 1)["error"])
        cur = _Cur(row=(1, "rm_rf_root", "safe", "proposed"))
        with mock.patch.object(rm.subprocess, "run") as run:
            self.assertEqual(rm.approve(_Conn(cur), 1)["error"], "action not in allowlist")
        self.assertEqual(run.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_no_args_prints_state_and_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REMEDIATION_ENABLED = True", r.stdout)

    def test_import_never_runs_smoke(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("smoke ran on import")):
            self.assertTrue(callable(_load("remediation_import_probe", SCRIPT).propose_for_incident))


if __name__ == "__main__":
    unittest.main()
