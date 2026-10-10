#!/usr/bin/env python3
"""7-category tests for nova_buick8_log (the logbook of things nobody can explain).
Centre: no cause without evidence. Offline: no PostgreSQL. Written by Jordan Koch (via Claude).
"""
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_watch_common as W  # noqa: E402
import nova_buick8_log as L  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class LogCur:
    """In-memory unexplained_events for one (kind, signature)."""

    def __init__(self):
        self.row = None
        self.calls = []
        self._r = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT id, evidence FROM unexplained_events"):
            self._r = [(1, self.row["evidence"])] if self.row else []
        elif s.startswith("INSERT INTO unexplained_events"):
            self.row = {"evidence": json.loads(params[5]), "occurrences": 1}
            self._r = [(1,)]
        elif s.startswith("UPDATE unexplained_events SET occurrences"):
            self.row["occurrences"] += 1
            self.row["evidence"] = json.loads(params[2])
            self._r = []
        else:
            self._r = []

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)


class RuleCur:
    def __init__(self, rules):
        self.rules = rules
        self.calls = []
        self._r = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self._r = []
        for sub, rows in self.rules:
            if sub in sql:
                self._r = list(rows(params) if callable(rows) else rows)
                break

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)


class TestSecurity(unittest.TestCase):
    def test_resolve_without_evidence_never_touches_db(self):
        cur = mock.MagicMock()
        for ev in (None, {}, [], ""):
            with self.assertRaises(L.CauseWithoutEvidence):
                L.resolve(1, "a neighbour", ev, cur=cur)
        cur.execute.assert_not_called()

    def test_log_unexplained_never_sets_cause(self):
        cur = LogCur()
        L.log_unexplained("k", "s", "d", {"cause": "aliens"}, cur=cur)
        for sql, _p in cur.calls:
            self.assertNotIn("cause=", sql.replace(" ", ""))

    def test_hypothesis_never_touches_cause(self):
        cur = RuleCur([])
        L.add_hypothesis(7, "wifi extender", cur=cur)
        sql, params = cur.calls[0]
        self.assertNotIn("cause", sql)
        self.assertEqual(json.loads(params[0])[0]["status"], "hypothesis")

    def test_required_fields(self):
        with self.assertRaises(ValueError):
            L.log_unexplained("", "s", "d", cur=LogCur())

    def test_signature_strips_long_numbers(self):
        cur = RuleCur([("FROM telemetry.events", [(1, T0, "ips", "ips", "unknown sig 123456789")])])
        out = L.feed_unknown_events(cur, T0)
        self.assertNotIn("123456789", out[0]["signature"])


class TestPerformance(unittest.TestCase):
    def test_evidence_capped(self):
        cur = LogCur()
        t = time.time()
        for i in range(300):
            L.log_unexplained("k", "s", "d", {}, occurrence_key=str(i), ts=T0, cur=cur)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(cur.row["occurrences"], 300)
        self.assertEqual(len(cur.row["evidence"]), L.MAX_EVIDENCE)

    def test_feed_power_one_prior_query_per_hour(self):
        hours = [("plug", T0 - timedelta(hours=i), 400.0) for i in range(200)]
        cur = RuleCur([("GROUP BY 1, 2", hours), ("SELECT max(watts)", [(100.0,)])])
        t = time.time()
        out = L.feed_power(cur, T0 - timedelta(days=9))
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(out), 200)
        self.assertEqual(len(cur.calls), 201)


class TestRetry(unittest.TestCase):
    def test_own_connection_uses_retrying_connect_and_closes(self):
        conn = mock.MagicMock()
        conn.cursor.return_value = RuleCur([])
        with mock.patch.object(W, "connect", return_value=conn) as c:
            self.assertEqual(L.cause_statement("k", "s"), "Cause unknown (not in the logbook).")
        c.assert_called_once()
        conn.close.assert_called_once()

    def test_connect_failure_raises_not_silent(self):
        import psycopg2
        with mock.patch.object(psycopg2, "connect", side_effect=Exception("down")) as pc, \
                mock.patch.object(W.connect, "__defaults__", (W.DSN, 3, 0.0, lambda s: None)):
            with self.assertRaises(Exception):
                L.cause_statement("k", "s")
        self.assertEqual(pc.call_count, 3)

    def test_one_broken_feeder_does_not_block_others(self):
        def boom(cur, since):
            raise RuntimeError("feed down")
        good = mock.MagicMock(return_value=[], __name__="good")
        conn = mock.MagicMock()
        conn.cursor.return_value = RuleCur([])
        with mock.patch.object(L, "FEEDERS", (boom, good)), mock.patch.object(W, "connect", return_value=conn), \
                redirect_stdout(io.StringIO()) as out:
            L.run(2, dry_run=True)
        good.assert_called_once()
        self.assertIn("boom failed: feed down", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_power_spike_edges(self):
        self.assertFalse(L.power_spike(300, 200))     # exactly 1.5x is not beyond
        self.assertTrue(L.power_spike(301, 200))
        self.assertFalse(L.power_spike(299, 0))

    def test_describe_resolved(self):
        row = {"description": "x", "occurrences": 1, "first_seen": T0, "last_seen": T0,
               "cause": "claimed", "status": "resolved", "hypotheses": []}
        self.assertIn("Cause: claimed (evidence on file)", L.describe(row))
        row["cause"] = "unknown"
        self.assertIn("Cause unknown", L.describe(row))

    def test_squawk_and_negative_space_parsing(self):
        sq = L.feed_squawks(RuleCur([("overhead_flights", [(T0, "abc", "N1 ", "7700", 3000, 4.0)])]), T0)
        self.assertEqual(sq[0]["signature"], "abc:7700")
        self.assertIn("general emergency", sq[0]["description"])
        ns = L.feed_negative_space(RuleCur([("nova_negative_space", [
            (T0, "Presence method 'mmwave' silent 3h (last: 2026-10-08 01:00)"), (T0, "garbage")])]), T0)
        self.assertEqual([(n["signature"], n["occurrence_key"]) for n in ns],
                         [("presence:mmwave", "2026-10-08 01:00")])


class TestIntegration(unittest.TestCase):
    def test_feed_network_skips_claimed(self):
        devs = [(T0, "AA:AA", "warning", "NEW DEVICE a"), (T0, "BB:BB", "warning", "NEW DEVICE b"), (T0, None, "", "")]
        cur = RuleCur([("device_owner", lambda p: [("jordan", "phone")] if p[0] == "AA:AA" else []),
                       ("known_devices", [])])
        with mock.patch.object(W, "load_new_devices", return_value=devs):
            out = L.feed_network(cur, T0)
        self.assertEqual([o["signature"] for o in out], ["bb:bb"])

    def test_claimed_device_resolved_with_evidence(self):
        cur = RuleCur([("JOIN telemetry.device_owner", [(5, "jordan", "laptop", "manual")])])
        self.assertEqual(L.resolve_claimed_network(cur), 1)
        sql, params = cur.calls[-1]
        self.assertIn("status='resolved'", sql)
        self.assertIn("device_owner", params[1])

    def test_ble_needs_a_week_of_history(self):
        first = T0 - timedelta(days=1)
        cur = RuleCur([("WITH f AS", [("mac1", first, "Thing", 20, -50)]),
                       ("SELECT min(ts)", [(first - timedelta(days=3),)])])
        self.assertEqual(L.feed_ble(cur, T0 - timedelta(days=2)), [])


class TestFunctional(unittest.TestCase):
    def test_run_live_logs_then_resolves(self):
        conn = mock.MagicMock()
        cur = LogCur()
        conn.cursor.return_value = cur
        item = dict(kind="power_spike", signature="energy:x", ts=T0, description="d", evidence={},
                    occurrence_key="h1")
        feeder = mock.MagicMock(return_value=[item, item], __name__="f")
        with mock.patch.object(L, "FEEDERS", (feeder,)), mock.patch.object(W, "connect", return_value=conn), \
                mock.patch.object(W, "ensure_schema") as es, \
                mock.patch.object(L, "resolve_claimed_network", return_value=0) as rc, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(L.run(2, dry_run=False), 0)
        es.assert_called_once()
        rc.assert_called_once()
        self.assertEqual(cur.row["occurrences"], 1)   # same occurrence_key counted once

    def test_dry_run_writes_nothing(self):
        conn = mock.MagicMock()
        cur = LogCur()
        conn.cursor.return_value = cur
        item = dict(kind="k", signature="s", ts=T0, description="d", evidence={}, occurrence_key="o")
        with mock.patch.object(L, "FEEDERS", (mock.MagicMock(return_value=[item], __name__="f"),)), \
                mock.patch.object(W, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            L.run(2, dry_run=True)
        self.assertEqual(cur.calls, [])
        self.assertIn("would log [k] s", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_feeders_callable(self):
        self.assertTrue(all(callable(f) for f in L.FEEDERS))
        self.assertTrue(issubclass(L.CauseWithoutEvidence, ValueError))

    def test_main_list(self):
        conn = mock.MagicMock()
        conn.cursor.return_value = RuleCur([("ORDER BY last_seen", [
            ("k", "s", "d", 2, T0, T0, "unknown", "open", [])])])
        with mock.patch.object(W, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L.main(["--list"]), 0)
        self.assertIn("Cause unknown", out.getvalue())


# ── Case modes merged in (Rama Window --expiry, Mina's Typescript --case; 2026-10-09, organ audit M13) ──
import nova_rama_window as R  # noqa: E402
import nova_mina_typescript as M  # noqa: E402

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


class RouteCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None):
        self.routes, self.sql, self._last = routes or {}, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None

    def writes(self):
        return [s for s, _ in self.sql if any(k in s for k in ("CREATE", "INSERT", "UPDATE", "DELETE"))]


def open_routes():
    """One open entry whose scanner audio expires in ~14 h of the REAL now (rama's run reads the clock)."""
    return {"WHERE status='open'": [(33, "sensor_silence", datetime.now(timezone.utc) - timedelta(hours=10))],
            "to_regclass": [(None,)]}


def conn_for(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


class TestCaseModesSecurity(unittest.TestCase):
    def test_case_id_must_be_an_int(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                L.main(["--case", "1; select pg_sleep(9)"])

    def test_typescript_still_refuses_the_boot_disk(self):
        with self.assertRaises(ValueError):
            M.safe_dir("/etc")


class TestCaseModesPerformance(unittest.TestCase):
    def test_expiry_ranking_10k_cases(self):
        cases = [{"id": i, "kind": "k", "last_seen": NOW - timedelta(hours=i % 300)} for i in range(10_000)]
        t = time.perf_counter()
        R.due(cases, R.DEFAULT_RETENTION_H, NOW)
        self.assertLess(time.perf_counter() - t, 2.0)


class TestCaseModesRetry(unittest.TestCase):
    def test_expiry_uses_the_retrying_connect(self):
        cur = RouteCur(open_routes())
        with mock.patch.object(W, "connect", return_value=conn_for(cur)) as c, \
                mock.patch.object(R, "face_ttl", return_value=72.0), redirect_stdout(io.StringIO()):
            self.assertEqual(L.run_expiry(dry_run=True), 0)
        c.assert_called_once_with()     # W.connect: 3 tries with backoff (tested in nova_watch_common)

    def test_unknown_case_fails_cleanly(self):
        with mock.patch.object(W, "connect", return_value=conn_for(RouteCur())), redirect_stdout(io.StringIO()):
            self.assertEqual(L.run_case(999, dry_run=True), 1)
            self.assertEqual(L.run_snapshot(999, dry_run=True), 1)


class TestCaseModesUnit(unittest.TestCase):
    def test_horizon_default_is_ramas(self):
        with mock.patch.object(R, "run") as run:
            L.run_expiry()
            L.run_expiry(True, 6)
        self.assertEqual(run.call_args_list, [mock.call(dry=False, horizon_h=R.HORIZON_H),
                                              mock.call(dry=True, horizon_h=6)])

    def test_case_and_snapshot_return_codes(self):
        with mock.patch.object(M, "run", side_effect=["text", None]), mock.patch.object(R, "snapshot", return_value="/p"):
            self.assertEqual(L.run_case(7), 0)
            self.assertEqual(L.run_case(8), 1)
            self.assertEqual(L.run_snapshot(7), 0)


class TestCaseModesIntegration(unittest.TestCase):
    def test_modes_call_the_absorbed_modules(self):
        src = (SCRIPTS / "nova_buick8_log.py").read_text()
        for needle in ("import nova_rama_window as R", "R.run(", "R.snapshot(", "import nova_mina_typescript as M",
                       "M.run("):
            self.assertIn(needle, src)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS rama_window", src)   # the table stays Rama's

    def test_queue_lines_point_at_the_survivor(self):
        it = {"source": "syslog", "case_id": 5}
        self.assertEqual(R.export_cmd(it, NOW, 72), "python3 nova_buick8_log.py --snapshot 5")


class TestCaseModesFunctional(unittest.TestCase):
    def test_expiry_records_and_queues(self):
        cur = RouteCur(open_routes())
        with mock.patch.object(W, "connect", return_value=conn_for(cur)), mock.patch.object(R, "face_ttl", return_value=72.0), \
                mock.patch.object(W, "get_config", return_value={}), mock.patch.object(W, "set_config"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["--expiry"]), 0)
        self.assertTrue(any("INSERT INTO rama_window" in s for s in cur.writes()))
        self.assertTrue(any("INSERT INTO claude_queue" in s for s in cur.writes()))

    def test_expiry_dry_run_writes_nothing(self):
        cur = RouteCur(open_routes())
        with mock.patch.object(W, "connect", return_value=conn_for(cur)), mock.patch.object(R, "face_ttl", return_value=72.0), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L.main(["--expiry", "--dry-run"]), 0)
        self.assertEqual(cur.writes(), [])
        self.assertIn("scanner_audio", out.getvalue())

    def test_case_dry_run_prints_the_typescript(self):
        row = (7, "event_unknown", "sig", "IPS Alert unknown", NOW - timedelta(days=1), NOW, "open")
        cur = RouteCur({"FROM unexplained_events": [row]})
        with mock.patch.object(W, "connect", return_value=conn_for(cur)), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L.main(["--case", "7", "--dry-run"]), 0)
        self.assertIn("# Typescript: Buick 8 #7", out.getvalue())
        self.assertEqual(cur.writes(), [])


class TestCaseModesFrame(unittest.TestCase):
    def test_main_routes_modes(self):
        with mock.patch.object(L, "run_expiry", return_value=0) as ex, mock.patch.object(L, "run_case", return_value=0) as ca, \
                mock.patch.object(L, "run_snapshot", return_value=0) as sn, mock.patch.object(L, "run", return_value=0) as rn:
            L.main(["--expiry", "--horizon", "12"])
            L.main(["--case", "4"])
            L.main(["--snapshot", "5", "--dry-run"])
        ex.assert_called_once_with(False, 12.0)
        ca.assert_called_once_with(4, False)
        sn.assert_called_once_with(5, True)
        rn.assert_not_called()

    def test_help_lists_modes(self):
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_buick8_log.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        for flag in ("--expiry", "--case", "--snapshot", "--horizon"):
            self.assertIn(flag, r.stdout)


if __name__ == "__main__":
    unittest.main()
