#!/usr/bin/env python3
"""Tests for nova_autonomy_actor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_autonomy_actor.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


actor = _load("actor_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="actor-test-"))
NO_KILL = str(TMP / "absent-kill-file")


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last = [], [], None
        self.connection = types.SimpleNamespace(rollback=lambda: None, close=lambda: None)

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val(sql, params) if callable(val) else val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


def _conn(cur):
    c = types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, commit=lambda: None,
                              close=lambda: None, rollback=lambda: None)
    cur.connection = c
    return c


HEALTH = [("nova-soil-monitor", "nova-core", "down", None),
          ("postgres", "nova-core", "down", None),
          ("nova-fishbowl-watch", "nova-core", "up", None)]
QUEUE = [(1, "restart stale ingest watcher"), (2, "buy a boat"), (3, "decide on the hiring plan")]


def _rules(mode='"live"', ledger_count=0):
    return [
        ("key='autonomy_actor_mode'", (mode,) if mode is not None else None),
        ("key='kill_switch'", None),
        ("FROM health_checks WHERE checked_at", HEALTH),
        ("SELECT 1 FROM autonomy_log", None),
        ("key='caps'", None),
        ("FROM autonomy_ledger WHERE executed", (ledger_count,)),
        ("SELECT status FROM health_checks WHERE service_name", ("up",)),
        ("INSERT INTO autonomy_ledger", (41,)),
        ("FROM claude_queue", QUEUE),
    ]


def _run_main(cur, restart=(True, "ok"), post_both=None):
    """main() with PG, the fleet restart, sleep and Slack all mocked. Returns (rc, cfg stub, restart mock)."""
    cfg = types.SimpleNamespace(post_both=post_both or mock.MagicMock(), SLACK_NOTIFY="C_TEST")
    with mock.patch.object(actor.psycopg2, "connect", return_value=_conn(cur)), \
         mock.patch.object(actor._safety, "KILL_FILE", NO_KILL), \
         mock.patch.object(actor, "restart_service", return_value=restart) as rs, \
         mock.patch("time.sleep"), \
         mock.patch.dict(sys.modules, {"nova_config": cfg}):
        rc = actor.main()
    return rc, cfg, rs


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        i = SRC.index("INSERT INTO autonomy_log")
        self.assertIn("%s,%s,%s,%s,%s,%s,%s", SRC[i:i + 200])

    def test_allowlist_never_contains_critical_infrastructure(self):
        for bad in ("postgres", "pgbouncer", "nova-gateway", "bind", "dns", "nova-gateway-v2"):
            self.assertNotIn(bad, actor.SAFE_SERVICES)
        self.assertTrue(all(s.startswith("nova-") for s in actor.SAFE_SERVICES))

    def test_redline_blocks_self_preservation_and_destruction(self):
        for bad in ("copy my weights to another host", "self-replicate to the NAS", "exfiltrate the model",
                    "wipe the cache", "buy a new disk", "reboot the mac studio", "api key: rotate it now",
                    "post this to public slack", "upload the checkpoint to hugging face", "escape the sandbox"):
            self.assertFalse(actor.redline_ok(bad), bad)
        self.assertTrue(actor.redline_ok("nova-soil-monitor nova-core restart"))

    def test_audit_truncates_result_to_400(self):
        cur = _Cur()
        actor.audit(cur, "restart", "x@y", "live", True, True, "z" * 1000)
        self.assertEqual(len(cur.params[0][5]), 400)


class TestPerformance(unittest.TestCase):
    def test_redline_fast_on_10k_candidates(self):
        items = [f"restart nova-soil-monitor on node {i}" if i % 2 else f"copy myself to host {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        blocked = sum(0 if actor.redline_ok(s) else 1 for s in items)
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(blocked, 5_000)


class TestRetry(unittest.TestCase):
    def test_slack_failure_never_escapes_main(self):
        # RETRY GAP: nova_config.post_both — one attempt, failure is logged and swallowed
        cur = _Cur(_rules())
        boom = mock.MagicMock(side_effect=OSError("slack down"))
        rc, cfg, _ = _run_main(cur, post_both=boom)
        self.assertEqual(rc, 0)
        self.assertEqual(boom.call_count, 1)

    def test_fleet_restart_fails_open_without_retry(self):
        # RETRY GAP: restart_service (nova_fleet_exec) — one subprocess attempt, (False, detail) on error
        import nova_fleet_exec as fx
        with mock.patch.object(fx.subprocess, "run", side_effect=OSError("ssh down")) as run, \
             mock.patch.object(fx.socket, "getaddrinfo", return_value=[]), \
             mock.patch("psycopg2.connect", side_effect=RuntimeError("offline")):
            ok, detail = actor.restart_service("nova-core", "nova-soil-monitor")
        self.assertFalse(ok)
        self.assertIn("ssh down", detail)
        self.assertGreaterEqual(run.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_get_mode_defaults_and_normalizes(self):
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", None)])), "dry_run")
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", (None,))])), "dry_run")
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", ('"LIVE"',))])), "live")
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", (" off ",))])), "off")
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", (123,))])), "123")

    def test_redline_edge_cases(self):
        self.assertTrue(actor.redline_ok(""))
        self.assertTrue(actor.redline_ok(None))
        self.assertTrue(actor.redline_ok("restart nova-freshness-monitor"))      # plain service restart is fine
        self.assertFalse(actor.redline_ok("restart the mac mini"))               # host reboot is not

    def test_audit_writes_one_parameterized_row(self):
        cur = _Cur()
        actor.audit(cur, "restart", "svc@node", "dry_run", False, False, "would restart", blocked=True)
        self.assertEqual(len(cur.sql), 1)
        self.assertEqual(cur.params[0], ("restart", "svc@node", "dry_run", False, False, "would restart", True))


class TestIntegration(unittest.TestCase):
    def test_safety_net_is_the_shared_module_not_a_copy(self):
        self.assertIs(actor._safety, sys.modules["nova_autonomy_safety"])
        self.assertNotIn("def kill_switch_engaged", SRC)
        self.assertNotIn("def rate_ok", SRC)

    def test_ledger_class_matches_safety_normalization(self):
        self.assertEqual(actor._safety.action_class_of("restart", "nova-soil-monitor"), "restart:nova-soil-monitor")

    def test_coagency_borrows_this_allowlist(self):
        import nova_coagency as C
        self.assertEqual(C.SAFE_SERVICES, frozenset(actor.SAFE_SERVICES))

    def test_audit_table_and_mode_key(self):
        self.assertIn("INSERT INTO autonomy_log", SRC)
        self.assertIn("key='autonomy_actor_mode'", SRC)


class TestFunctional(unittest.TestCase):
    def test_live_golden_path_restarts_only_allowlisted_down_service(self):
        cur = _Cur(_rules())
        rc, cfg, rs = _run_main(cur)
        self.assertEqual(rc, 0)
        rs.assert_called_once_with("nova-core", "nova-soil-monitor")
        audits = [p for _, p in cur.stmts("INSERT INTO autonomy_log")]
        self.assertEqual(audits, [("restart", "nova-soil-monitor@nova-core", "live", True, True, "ok", False)])
        self.assertFalse(any("postgres" in (p[1] or "") for p in audits))
        ledger = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0][1][2], "restart:nova-soil-monitor")
        self.assertIn("stop nova-soil-monitor on nova-core", ledger[0][1][5])
        msg = cfg.post_both.call_args[0][0]
        self.assertIn("restarted nova-soil-monitor@nova-core — verified up", msg)
        self.assertIn("~1 look reversible-maintenance, 2 need your decision", msg)

    def test_dry_run_proposes_and_never_restarts(self):
        cur = _Cur(_rules(mode=None))       # absent flag => dry_run
        rc, cfg, rs = _run_main(cur)
        self.assertEqual(rc, 0)
        rs.assert_not_called()
        self.assertEqual([p[5] for _, p in cur.stmts("INSERT INTO autonomy_log")], ["would restart (dry_run)"])
        self.assertIn("would-do [restart nova-soil-monitor@nova-core (down)]", cfg.post_both.call_args[0][0])

    def test_kill_switch_file_stands_everything_down(self):
        kill = TMP / "kill"
        kill.write_text("engaged")
        cur = _Cur(_rules())
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_NOTIFY="C")
        try:
            with mock.patch.object(actor.psycopg2, "connect", return_value=_conn(cur)), \
                 mock.patch.object(actor._safety, "KILL_FILE", str(kill)), \
                 mock.patch.object(actor, "restart_service") as rs, \
                 mock.patch.dict(sys.modules, {"nova_config": cfg}):
                rc = actor.main()
        finally:
            kill.unlink()
        self.assertEqual(rc, 0)
        rs.assert_not_called()
        self.assertFalse(cur.stmts("FROM health_checks"))
        cfg.post_both.assert_not_called()

    def test_mode_off_touches_nothing(self):
        cur = _Cur(_rules(mode='"off"'))
        rc, cfg, rs = _run_main(cur)
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.sql), 1)
        rs.assert_not_called()

    def test_rate_cap_blocks_execution(self):
        cur = _Cur(_rules(ledger_count=100))
        rc, cfg, rs = _run_main(cur)
        self.assertEqual(rc, 0)
        rs.assert_not_called()
        self.assertTrue(any(p[5].startswith("rate-capped") for _, p in cur.stmts("INSERT INTO autonomy_log")))


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_autonomy_actor"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_main_is_guarded(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("actor_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
