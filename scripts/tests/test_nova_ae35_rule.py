#!/usr/bin/env python3
"""Tests for nova_ae35_rule.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_ae35_rule as A  # noqa: E402

SRC = (SCRIPTS / "nova_ae35_rule.py").read_text()


class FakeCur:
    """route(sql, params) -> rows; records every statement."""

    def __init__(self, route=None, boom=False):
        self.route, self.boom, self.sql, self._last = route or (lambda s, p: []), boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = list(self.route(sql, params))

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def cfg_route(channels=None, hosts=None, extra=None):
    def route(sql, params):
        if "service_config" in sql:
            v = {"channels": channels, "witness_hosts": hosts}.get(params[0])
            return [(json.dumps(v),)] if v is not None else []
        for k, rows in (extra or {}).items():
            if k in sql:
                return rows
        return []
    return route


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_fails_closed_on_error(self):
        with mock.patch("builtins.print"), mock.patch.object(A, "classify", side_effect=RuntimeError("x")):
            d = A.oversight_change_allowed(FakeCur(), text="mute the canary", confirmation_id=1)
        self.assertFalse(d["allowed"])
        self.assertIn("failing closed", d["reason"])

    def test_never_self_approved(self):
        # Nova's own host as "witness" and no Jordan key -> refused
        cur = FakeCur(cfg_route(hosts=["outside-probe"]))
        with mock.patch.object(A.E, "jordan_confirmation", return_value=False):
            d = A.oversight_change_allowed(cur, text="disable the ntfy canary",
                                           witness={"host": "nova-core", "confirms": True})
        self.assertFalse(d["allowed"])

    def test_config_cannot_remove_a_channel(self):
        reg = A.registry(FakeCur(cfg_route(channels={"canary": "nevermatches"})))
        self.assertEqual(set(A.DEFAULT_CHANNELS) - set(reg), set())
        self.assertTrue(A.classify("disable the ntfy canary", reg))

    def test_hostile_text_stays_a_parameter(self):
        evil = "x'; DROP TABLE claude_queue; -- disable the canary"
        cur = FakeCur(lambda s, p: [(1,)] if "RETURNING" in s else [])
        ev = A.events({"coagency": [(1, None, evil, "executed", None)]}, A.DEFAULT_CHANNELS)[0]
        A.file_question(cur, ev)
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO claude_queue" in s][0]
        self.assertNotIn("DROP", ins[0])
        self.assertIn(evil, ins[1][2])


    def test_queue_session_registered_before_queue_row(self):
        # claude_queue.session_id is a foreign key to claude_sessions (the 2026-10-09 audit's FK bug).
        cur = FakeCur(lambda s, p: [(1,)] if "RETURNING" in s else [])
        ev = A.events({"coagency": [(1, None, "disable the canary", "executed", None)]}, A.DEFAULT_CHANNELS)[0]
        A.file_question(cur, ev)
        stmts = [(s, p) for s, p in cur.sql if "INSERT" in s]
        self.assertIn("INSERT INTO claude_sessions", stmts[0][0])
        self.assertEqual(stmts[0][1], (A.QUEUE_SESSION,))
        self.assertIn("INSERT INTO claude_queue", stmts[1][0])


class TestPerformance(unittest.TestCase):
    def test_classify_10k(self):
        texts = [f"restart service {i}" if i % 10 else f"mute the canary {i}" for i in range(10000)]
        t = time.monotonic()
        hits = sum(bool(A.classify(x)) for x in texts)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(hits, 1000)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        n = {"c": 0}

        def flaky(*a, **k):
            n["c"] += 1
            if n["c"] < 3:
                raise OSError("refused")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            import nova_watch_common as W
            W.connect(_sleep=lambda s: None)
        self.assertEqual(n["c"], 3)

    # RETRY GAP: oversight_change_allowed — the Jordan-key read is not retried; it fails CLOSED.
    def test_pg_down_fails_closed(self):
        with mock.patch("builtins.print"):
            d = A.oversight_change_allowed(FakeCur(boom=True), text="disable the canary", confirmation_id=5)
        self.assertFalse(d["allowed"])

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(A._q(FakeCur(boom=True), "SELECT 1"), [])


class TestUnit(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(A.classify(""), [])
        self.assertEqual(A.classify("restart big brother"), [])
        self.assertEqual(A.classify("the action audit is a false positive")[0], ("action_audit", "diagnosis"))
        self.assertIn(("notify", "change"), A.classify("reroute #nova-alerts to the digest"))
        self.assertEqual(A.classify("raise the threshold on the heartbeat"), [("liveness", "change")])

    def test_decide(self):
        self.assertTrue(A.decide([], False, False)["allowed"])
        self.assertFalse(A.decide([("canary", "change")], False, False)["allowed"])
        d = A.decide([("canary", "change")], False, True)
        self.assertEqual(d["key"], "outside_witness")
        self.assertIn("Little Mister", d["line"])

    def test_witness(self):
        self.assertFalse(A.witness_ok(None, ["h"]))
        self.assertFalse(A.witness_ok({"host": "h", "confirms": True}, []))
        self.assertTrue(A.witness_ok({"host": "h", "confirms": True}, ["h"]))

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(A.selftest(), 0)


    def test_audit_cli_is_a_wrapper_onto_action_audit(self):
        import nova_action_audit
        with mock.patch.object(nova_action_audit, "main", return_value=0) as am, mock.patch("builtins.print") as pr:
            self.assertEqual(A.main(["--audit", "--days", "30", "--dry-run"]), 0)
        am.assert_called_once_with(["--oversight", "--days", "30", "--dry-run"])
        self.assertIn("merged into nova_action_audit.py --oversight on 2026-10-09", pr.call_args.args[0])


class TestIntegration(unittest.TestCase):
    def test_reuses_two_man_jordan_key(self):
        self.assertIn("import nova_escalation as E", SRC)
        with mock.patch.object(A.E, "jordan_confirmation", return_value=True) as jc:
            d = A.oversight_change_allowed(FakeCur(), text="silence the dead man's switch", confirmation_id=42)
        jc.assert_called_once()
        self.assertEqual(jc.call_args[0][1], 42)
        self.assertTrue(d["allowed"])
        self.assertEqual(d["key"], "jordan")

    def test_non_oversight_does_not_touch_keys(self):
        with mock.patch.object(A.E, "jordan_confirmation") as jc:
            self.assertFalse(A.oversight_change_allowed(FakeCur(), text="water the garden")["oversight"])
        jc.assert_not_called()

    def test_outside_witness_from_service_config(self):
        cur = FakeCur(cfg_route(hosts=["outside-probe"]))
        d = A.oversight_change_allowed(cur, text="the canary is broken",
                                       witness={"host": "outside-probe", "confirms": True})
        self.assertTrue(d["allowed"])
        self.assertTrue(any("service='ae35'" in s for s, _ in cur.sql))

    def test_schema(self):
        for c in ("ae35_events", "UNIQUE (source, source_id, channel)", "witnessed boolean", "acted boolean"):
            self.assertIn(c, A.SCHEMA)

    def test_library_functions_stay_for_the_reviewer(self):
        self.assertTrue(callable(A.oversight_change_allowed) and callable(A.classify))
        self.assertIn("import nova_ae35_rule as ae35", (SCRIPTS / "nova_claude_reviewer.py").read_text())


ROWS = {"FROM coagency_proposals": [(7, None, "disable the ntfy canary", "executed", "nova:earned-autonomy"),
                                    (8, None, "mute the watchdog", "executed", "jordan"),
                                    (9, None, "pause the kill switch", "rejected", "jordan")],
        "RETURNING id": [(101,)]}


class TestFunctional(unittest.TestCase):
    def _audit(self, dry):
        cur = FakeCur(cfg_route(extra=ROWS))
        with mock.patch("nova_watch_common.connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            evs = A.audit(30, dry=dry)
        return cur, evs

    def test_audit_records_and_questions_unwitnessed(self):
        cur, evs = self._audit(False)
        self.assertEqual(len(evs), 3)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS ae35_events" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO ae35_events" in s for s, _ in cur.sql), 3)
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)                    # only #7: acted, not Jordan
        self.assertIn("#7", q[0][1])

    def test_dry_run_writes_nothing(self):
        cur, evs = self._audit(True)
        self.assertEqual(len(evs), 3)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE")))

    def test_pg_down_raises_before_any_write(self):
        with mock.patch("nova_watch_common.connect", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                A.audit(30, dry=True)


    def test_audit_via_action_audit_oversight_mode(self):
        import nova_action_audit
        cur = FakeCur(cfg_route(extra=ROWS))
        with mock.patch("nova_watch_common.connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            self.assertEqual(nova_action_audit.main(["--oversight"]), 0)
        self.assertEqual(sum("INSERT INTO ae35_events" in s for s, _ in cur.sql), 3)
        self.assertEqual([p for s, p in cur.sql if "FROM coagency_proposals" in s][0], (30,))   # default --days 30


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ae35_rule.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ae35_rule.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
