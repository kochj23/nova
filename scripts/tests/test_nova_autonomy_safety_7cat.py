#!/usr/bin/env python3
"""7-category tests for nova_autonomy_safety.py — the Proteus-era additions: physical_guard /
comms_guard re-exports (fail closed), observe_service (P4 objective snapshot), record_ledger with
before_state / after_state / stated_rationale, the kill switch never actuating anything (P8), and
retry/backoff on the PG connection.

All in-memory: no real PG rows, no tripwire left behind. Written by Jordan Koch (via Claude).
"""
import builtins
import importlib
import importlib.util
import json
import os
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
import nova_autonomy_safety as S  # noqa: E402

SCRIPT = SCRIPTS / "nova_autonomy_safety.py"
TMP = Path(tempfile.mkdtemp(prefix="safety7-"))
CHECKED = datetime(2026, 10, 8, 9, 0)


class _Cur:
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last = [], [], None
        self.connection = types.SimpleNamespace(close=mock.MagicMock())

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError("db down")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])


def _hide_guards():
    real = builtins.__import__

    def imp(name, *a, **k):
        if name == "nova_safety_guards":
            raise ImportError("gone")
        return real(name, *a, **k)
    return mock.patch("builtins.__import__", side_effect=imp)


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_reexports_fail_closed_when_guards_module_missing(self):
        with _hide_guards():
            ok, why = S.physical_guard("turn on", entity_ids=["light.office"])
            ok2, _ = S.comms_guard("rename ssid")
        self.assertFalse(ok); self.assertFalse(ok2)
        self.assertIn("refusing", why)

    def test_physical_reexport_blocks_locks_garage_and_extreme_heat(self):
        self.assertFalse(S.physical_guard("x", entity_ids=["lock.front_door"])[0])
        self.assertFalse(S.physical_guard("x", domains=["garage_door"])[0])
        self.assertFalse(S.physical_guard("x", entity_ids=["climate.living"], setpoint_f=95)[0])
        self.assertTrue(S.physical_guard("on", entity_ids=["light.garage_light_2"])[0])

    def test_comms_reexport_refuses_household_device(self):
        ok, _ = S.comms_guard("block", names=["Amys-iPhone"], owner_lookup=lambda m: (None, None))
        self.assertFalse(ok)

    def test_engage_kill_never_actuates_anything(self):
        cur = _Cur()
        kf = str(TMP / "kill-sec")
        with mock.patch.object(S, "KILL_FILE", kf), \
             mock.patch("subprocess.run") as run, mock.patch("urllib.request.urlopen") as url:
            S.engage_kill(cur, note="test")
        run.assert_not_called(); url.assert_not_called()
        self.assertTrue(all("service_config" in s for s in cur.sql))
        os.remove(kf)

    def test_stated_rationale_truncated_to_1000(self):
        cur = _Cur([("INSERT INTO autonomy_ledger", (5,))])
        S.record_ledger(cur, source="t", autonomy_level="r1", action_class="test:x", target="t",
                        action="a", rollback_action="b", stated_rationale="x" * 5000)
        self.assertEqual(len(cur.params[-1][12]), 1000)

    def test_ledger_sql_is_parameterized(self):
        cur = _Cur([("INSERT INTO autonomy_ledger", (5,))])
        S.record_ledger(cur, source="'; DROP TABLE x;--", autonomy_level="r1", action_class="test:x",
                        target=None, action="a", rollback_action="b", before_state={"a": 1})
        self.assertNotIn("DROP TABLE", cur.sql[-1])


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_observe_service_is_one_bounded_query(self):
        cur = _Cur([("FROM health_checks", ("up", CHECKED))])
        S.observe_service(cur, "svc", "node")
        self.assertEqual(len(cur.sql), 1)
        self.assertIn("LIMIT 1", cur.sql[0])

    def test_record_ledger_1000x_under_a_second(self):
        cur = _Cur([("INSERT INTO autonomy_ledger", (1,))])
        t = time.perf_counter()
        for i in range(1000):
            S.record_ledger(cur, source="t", autonomy_level="r1", action_class="test:x", target="t",
                            action="a", rollback_action="b", before_state={"i": i}, after_state={"i": i})
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_physical_guard_reexport_fast(self):
        t = time.perf_counter()
        for _ in range(2000):
            S.physical_guard("turn on the office lamp", entity_ids=["light.office"])
        self.assertLess(time.perf_counter() - t, 2.0)


# ── Retry ────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_connect_retries_with_exponential_backoff(self):
        conn = object()
        with mock.patch.object(S.psycopg2, "connect", side_effect=[OSError("a"), OSError("b"), conn]) as c, \
             mock.patch("time.sleep") as sl:
            self.assertIs(S._connect(), conn)
        self.assertEqual(c.call_count, 3)
        self.assertEqual([x.args[0] for x in sl.call_args_list], [1.0, 2.0])

    def test_connect_reraises_after_three_and_logs_each(self):
        with mock.patch.object(S.psycopg2, "connect", side_effect=OSError("down")) as c, \
             mock.patch("time.sleep"), mock.patch.object(S, "log") as lg:
            with self.assertRaises(OSError):
                S._connect()
        self.assertEqual(c.call_count, 3)
        self.assertEqual(lg.call_count, 3)

    def test_autonomy_status_uses_retry_then_degrades_to_empty_line(self):
        with mock.patch.object(S.psycopg2, "connect", side_effect=OSError("down")) as c, mock.patch("time.sleep"):
            self.assertEqual(S.autonomy_status(), {"line": ""})
        self.assertEqual(c.call_count, 3)

    def test_observe_service_db_error_returns_marked_snapshot(self):
        snap = S.observe_service(_Cur(raise_on=("health_checks",)), "svc")
        self.assertIsNone(snap["status"]); self.assertIn("db down", snap["error"])


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_observe_service_with_and_without_node(self):
        cur = _Cur([("FROM health_checks", ("down", CHECKED))])
        a = S.observe_service(cur, "svc", "nova-core")
        self.assertEqual((a["status"], a["checked_at"]), ("down", CHECKED.isoformat()))
        self.assertEqual(cur.params[-1], ("svc", "nova-core"))
        S.observe_service(cur, "svc")
        self.assertEqual(cur.params[-1], ("svc",))

    def test_observe_service_no_rows(self):
        s = S.observe_service(_Cur(), "svc")
        self.assertIsNone(s["status"]); self.assertIsNone(s["checked_at"])

    def test_record_ledger_legacy_shape_when_no_p4_fields(self):
        cur = _Cur([("INSERT INTO autonomy_ledger", (9,))])
        self.assertEqual(S.record_ledger(cur, source="t", autonomy_level="r1", action_class="test:x",
                                         target="t", action="a", rollback_action="b"), 9)
        self.assertNotIn("before_state", cur.sql[-1])

    def test_record_ledger_p4_fields_serialized_json(self):
        cur = _Cur([("INSERT INTO autonomy_ledger", (9,))])
        S.record_ledger(cur, source="t", autonomy_level="r1", action_class="test:x", target="t",
                        action="a", rollback_action="b", before_state={"ts": CHECKED}, stated_rationale="why")
        p = cur.params[-1]
        self.assertEqual(json.loads(p[10])["ts"], str(CHECKED))
        self.assertIsNone(p[11]); self.assertEqual(p[12], "why")

    def test_schema_adds_p4_columns(self):
        cur = _Cur(); S.ensure_schema(cur)
        joined = " ".join(cur.sql)
        for col in ("before_state jsonb", "after_state jsonb", "stated_rationale text"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col}", joined)


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_reexports_delegate_to_nova_safety_guards(self):
        import nova_safety_guards as G
        with mock.patch.object(G, "physical_guard", return_value=(True, "stub")) as pg:
            self.assertEqual(S.physical_guard("a", ["light.x"], ["light"], setpoint_f=70), (True, "stub"))
        pg.assert_called_once_with("a", ["light.x"], ["light"], setpoint_f=70)
        with mock.patch.object(G, "comms_guard", return_value=(False, "no")) as cg:
            S.comms_guard("block", ["aa"], ["n"], ["e"])
        cg.assert_called_once_with("block", ["aa"], ["n"], ["e"])

    def test_observe_then_record_roundtrip(self):
        cur = _Cur([("FROM health_checks", ("up", CHECKED)), ("INSERT INTO autonomy_ledger", (3,))])
        after = S.observe_service(cur, "svc", "n")
        S.record_ledger(cur, source="actor", autonomy_level="rung1-selfheal", action_class="restart:svc",
                        target="svc@n", action="restart", rollback_action="stop",
                        before_state={"status": "down"}, after_state=after, stated_rationale="was down")
        p = cur.params[-1]
        self.assertEqual(json.loads(p[11])["status"], "up")


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_kill_then_status_line_says_halted(self):
        kf = str(TMP / "kill-func")
        with mock.patch.object(S, "KILL_FILE", kf):
            S.engage_kill(None, note="func")
            st = S.autonomy_status(_Cur([("turing_scoreboard", (0.1,))]))
            self.assertTrue(st["killed"])
            self.assertIn("halted", st["line"])
            ok, why = S.earned_ok(_Cur(), "observe:x")
            self.assertFalse(ok); self.assertIn("kill", why)
        os.remove(kf)

    def test_status_with_own_connection_closes_it(self):
        cur = _Cur([("turing_scoreboard", (0.1,))])
        conn = types.SimpleNamespace(cursor=lambda: cur, autocommit=False)
        cur.connection = types.SimpleNamespace(close=mock.MagicMock())
        with mock.patch.object(S.psycopg2, "connect", return_value=conn), \
             mock.patch.object(S, "KILL_FILE", str(TMP / "absent")):
            st = S.autonomy_status()
        self.assertFalse(st["killed"])
        cur.connection.close.assert_called_once()


# ── Frame ────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_reimport_without_connecting(self):
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            spec = importlib.util.spec_from_file_location("safety_7cat_frame", SCRIPT)
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertTrue(callable(m._connect) and callable(m.physical_guard))

    def test_subprocess_import_ok(self):
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0,{str(SCRIPTS)!r}); "
                            "import nova_autonomy_safety as s; print(s.MIN_CORRECT)"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "5")


if __name__ == "__main__":
    unittest.main()
