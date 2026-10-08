#!/usr/bin/env python3
"""7-category tests for nova_autonomy_actor.py — the Proteus-era additions: Proteus redline
(nova_safety_guards.safety_redline_ok), honest stopping (report_block), the P4 objective ledger
(before_state / after_state / stated_rationale), and retry/backoff on every external call
(PG connect, fleet restart, Slack post).

Everything external is mocked: no PG, no launchctl/ssh, no Slack. Written by Jordan Koch (via Claude).
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_autonomy_actor.py"
SRC = SCRIPT.read_text()


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


actor = _load("actor_7cat_under_test")
NO_KILL = str(Path(tempfile.mkdtemp(prefix="actor7-")) / "absent-kill-file")
CHECKED = datetime(2026, 10, 8, 9, 0)


class _Cur:
    """First matching SQL substring wins; records statements + params."""
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.sql, self.params, self._last = [], [], None
        self.connection = types.SimpleNamespace(close=lambda: None)

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
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
    return types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, close=lambda: None)


def _rules(health, mode='"live"', queue=()):
    return [
        ("key='autonomy_actor_mode'", (mode,)),
        ("key='kill_switch'", None),
        ("FROM health_checks WHERE checked_at", list(health)),
        ("SELECT 1 FROM autonomy_log", None),
        ("key='caps'", None),
        ("FROM autonomy_ledger WHERE executed", (0,)),
        ("SELECT status FROM health_checks WHERE service_name", ("up",)),
        ("SELECT status, checked_at FROM health_checks", ("up", CHECKED)),
        ("INSERT INTO autonomy_ledger", (77,)),
        ("FROM claude_queue", list(queue)),
    ]


def _main(cur, restart=None, post_both=None, connect=None):
    cfg = types.SimpleNamespace(post_both=post_both or mock.MagicMock(), SLACK_NOTIFY="C_TEST")
    restart = restart or mock.MagicMock(return_value=(True, "ok"))
    with mock.patch.object(actor.psycopg2, "connect", connect or mock.MagicMock(return_value=_conn(cur))), \
         mock.patch.object(actor._safety, "KILL_FILE", NO_KILL), \
         mock.patch.object(actor, "restart_service", restart), \
         mock.patch("time.sleep") as sl, \
         mock.patch.dict(sys.modules, {"nova_config": cfg}):
        rc = actor.main()
    return rc, cfg, restart, sl


DOWN = [("nova-soil-monitor", "nova-core", "down", CHECKED)]


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_redline_fails_closed_when_proteus_guards_missing(self):
        with mock.patch.object(actor, "_guards", None):
            self.assertFalse(actor.redline_ok("restart nova-soil-monitor"))

    def test_proteus_redline_vetoes_even_when_word_filter_passes(self):
        fake = types.SimpleNamespace(safety_redline_ok=lambda t: False, report_block=mock.MagicMock())
        with mock.patch.object(actor, "_guards", fake):
            self.assertFalse(actor.redline_ok("restart nova-soil-monitor"))

    def test_physical_and_comms_phrasings_hit_the_redline(self):
        for t in ("unlock the front door", "open the garage door", "block amy's iphone",
                  "clone jordan's voice", "disarm the alarm"):
            self.assertFalse(actor.redline_ok(t), t)

    def test_redline_block_is_reported_and_never_executed(self):
        fake = types.SimpleNamespace(safety_redline_ok=lambda t: False, report_block=mock.MagicMock())
        cur = _Cur(_rules(DOWN))
        with mock.patch.object(actor, "_guards", fake):
            _, _, rs, _ = _main(cur)
        rs.assert_not_called()
        fake.report_block.assert_called_once()
        self.assertEqual(fake.report_block.call_args.kwargs["source"], "autonomy-actor")
        blocked = cur.stmts("INSERT INTO autonomy_log")
        self.assertTrue(blocked and blocked[0][1][-1] is True)

    def test_safety_module_missing_degrades_live_to_dry_run(self):
        cur = _Cur(_rules(DOWN))
        with mock.patch.object(actor, "_safety", None), \
             mock.patch.object(actor.psycopg2, "connect", return_value=_conn(cur)), \
             mock.patch.object(actor, "restart_service") as rs, \
             mock.patch.dict(sys.modules, {"nova_config": types.SimpleNamespace(post_both=mock.MagicMock())}):
            actor.main()
        rs.assert_not_called()

    def test_stated_rationale_and_states_contain_no_secrets(self):
        cur = _Cur(_rules(DOWN))
        _main(cur)
        (sql, params), = cur.stmts("INSERT INTO autonomy_ledger")
        blob = json.dumps(params, default=str).lower()
        for bad in ("password", "token", "secret", "/users/"):
            self.assertNotIn(bad, blob)


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_queue_triage_is_bounded_by_limit_40(self):
        self.assertRegex(SRC, r"FROM claude_queue WHERE status='queued'[\s\S]{0,120}LIMIT 40")

    def test_main_with_200_down_rows_is_fast_and_restarts_only_allowlisted(self):
        health = [(f"svc-{i}", "nova-core", "down", CHECKED) for i in range(200)] + DOWN
        cur = _Cur(_rules(health))
        t = time.perf_counter()
        _, _, rs, _ = _main(cur)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(rs.call_count, 1)

    def test_retry_backoff_is_bounded(self):
        # worst case total sleep across one call's retries stays under 10s
        self.assertLessEqual(sum(actor.RETRY_BACKOFF_S * 2 ** i for i in range(actor.RETRY_ATTEMPTS - 1)), 10)


# ── Retry ────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_retry_returns_on_second_attempt_with_backoff(self):
        fn = mock.MagicMock(side_effect=[OSError("blip"), "ok"])
        with mock.patch("time.sleep") as sl:
            self.assertEqual(actor._retry(fn, what="t"), "ok")
        self.assertEqual(fn.call_count, 2)
        sl.assert_called_once_with(actor.RETRY_BACKOFF_S)

    def test_retry_reraises_after_last_attempt_and_logs_each(self):
        fn = mock.MagicMock(side_effect=OSError("down"))
        with mock.patch("time.sleep") as sl, mock.patch.object(actor, "log") as lg:
            with self.assertRaises(OSError):
                actor._retry(fn, what="t")
        self.assertEqual(fn.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [2.0, 4.0])
        self.assertEqual(lg.call_count, 3)

    def test_pg_connect_is_retried(self):
        cur = _Cur(_rules([]))
        connect = mock.MagicMock(side_effect=[RuntimeError("pg blip"), _conn(cur)])
        rc, _, _, _ = _main(cur, connect=connect)
        self.assertEqual(rc, 0)
        self.assertEqual(connect.call_count, 2)

    def test_failed_restart_is_retried_then_recorded_unverified(self):
        cur = _Cur(_rules(DOWN))
        rs = mock.MagicMock(side_effect=[(False, "ssh down"), (False, "ssh down"), (False, "ssh down")])
        _main(cur, restart=rs)
        self.assertEqual(rs.call_count, 3)
        (sql, p), = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertIn(False, p[6:8])   # executed / verified false

    def test_restart_succeeding_on_retry_counts_once(self):
        cur = _Cur(_rules(DOWN))
        rs = mock.MagicMock(side_effect=[(False, "busy"), (True, "ok")])
        _main(cur, restart=rs)
        self.assertEqual(rs.call_count, 2)
        self.assertEqual(len(cur.stmts("INSERT INTO autonomy_ledger")), 1)

    def test_slack_post_retried_then_succeeds(self):
        cur = _Cur(_rules(DOWN))
        pb = mock.MagicMock(side_effect=[OSError("slack"), None])
        _main(cur, post_both=pb)
        self.assertEqual(pb.call_count, 2)


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_retry_ok_predicate_returns_last_result_when_never_ok(self):
        with mock.patch("time.sleep"):
            r = actor._retry(lambda: (False, "x"), ok=lambda r: r[0])
        self.assertEqual(r, (False, "x"))

    def test_get_mode_handles_non_string_jsonb(self):
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", ("LIVE",))])), "live")
        self.assertEqual(actor.get_mode(_Cur([("autonomy_actor_mode", (None,))])), "dry_run")

    def test_safe_services_excludes_db_dns_gateway(self):
        for s in actor.SAFE_SERVICES:
            self.assertNotRegex(s, r"postgres|pgbouncer|gateway|dns|bind")


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_ledger_row_carries_objective_before_after_and_rationale(self):
        cur = _Cur(_rules(DOWN))
        _main(cur)
        (sql, p), = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertIn("before_state", sql)
        before, after, why = json.loads(p[10]), json.loads(p[11]), p[12]
        self.assertEqual(before["status"], "down")
        self.assertEqual(after["status"], "up")          # observed from health_checks, not Nova's word
        self.assertIn("SAFE_SERVICES", why)
        self.assertEqual(p[2], "restart:nova-soil-monitor")

    def test_guards_module_is_the_real_proteus_module(self):
        import nova_safety_guards
        self.assertIs(actor._guards, nova_safety_guards)

    def test_physical_guard_reachable_via_safety_reexport(self):
        self.assertFalse(actor._safety.physical_guard("x", entity_ids=["lock.front_door"])[0])


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_golden_path_restart_verify_ledger_and_one_summary(self):
        cur = _Cur(_rules(DOWN, queue=[(1, "restart stale watcher"), (2, "delete old logs")]))
        rc, cfg, rs, _ = _main(cur)
        self.assertEqual(rc, 0)
        rs.assert_called_once_with("nova-core", "nova-soil-monitor")
        cfg.post_both.assert_called_once()
        msg = cfg.post_both.call_args.args[0]
        self.assertIn("verified up", msg)
        self.assertIn("1 look reversible-maintenance, 1 need your decision", msg)

    def test_kill_switch_db_flag_stands_down(self):
        rules = _rules(DOWN)
        rules.insert(0, ("key='kill_switch'", ('"true"',)))
        cur = _Cur(rules)
        rc, cfg, rs, _ = _main(cur)
        self.assertEqual(rc, 0)
        rs.assert_not_called()
        cfg.post_both.assert_not_called()

    def test_pg_down_for_all_attempts_raises_not_silent(self):
        with mock.patch.object(actor.psycopg2, "connect", side_effect=RuntimeError("pg down")), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                actor.main()

    def test_nothing_to_do_posts_nothing(self):
        cur = _Cur(_rules([]))
        _, cfg, rs, _ = _main(cur)
        rs.assert_not_called()
        cfg.post_both.assert_not_called()


# ── Frame ────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_compiles_and_imports_without_connecting(self):
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            m = _load("actor_7cat_frame")
        self.assertTrue(callable(m.main) and callable(m._retry))

    def test_startup_in_subprocess_defines_main(self):
        r = subprocess.run([sys.executable, "-c", f"import runpy,sys; sys.path.insert(0,{str(SCRIPTS)!r}); "
                            f"m=runpy.run_path({str(SCRIPT)!r}, run_name='not_main'); print(sorted(k for k in m if k=='main'))"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("['main']", r.stdout)


if __name__ == "__main__":
    unittest.main()
