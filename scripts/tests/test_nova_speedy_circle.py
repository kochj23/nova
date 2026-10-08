#!/usr/bin/env python3
"""Tests for nova_speedy_circle.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_speedy_circle as S  # noqa: E402

SRC = (SCRIPTS / "nova_speedy_circle.py").read_text()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 7, tzinfo=timezone.utc)
ORBIT = ("nova-hue", 6, 7, ["autonomy_ledger", "claude_actions"], T0, T1)


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def py(rx):
    return re.compile(rx.replace(r"\m", r"\b").replace(r"\M", r"\b"), re.I)


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotIn("%(", S.ORBIT_SQL.replace("%(off)s", "").replace("%(on)s", "").replace(
            "%(days)s", "").replace("%(cycles)s", "").replace("%(skip)s", ""))
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_query_is_read_only(self):
        self.assertNotRegex(S.ORBIT_SQL, r"(?i)\b(insert|update|delete|drop|alter|create)\b")

    def test_hostile_target_stays_a_parameter(self):
        evil = "x'; DROP TABLE claude_actions; --"
        with mock.patch("nova_buick8_log.log_unexplained") as lu, mock.patch("builtins.print"):
            cur = FakeCur({"FROM claude_actions": [(evil,) + ORBIT[1:]]})
            with mock.patch.object(S.W, "connect", return_value=fake_conn(cur)):
                S.run(dry=False)
        self.assertEqual(lu.call_args[0][1], evil)                   # passed as data, never spliced
        self.assertFalse(any(evil in s for s, _ in cur.sql))

    def test_observer_never_acts(self):
        self.assertNotRegex(SRC, r"\bnotify\(|post_slack\(|post_both\(|import subprocess")


class TestPerformance(unittest.TestCase):
    def test_findings_10k(self):
        rows = [(f"t{i}", 6 + i % 5, 9, ["claude_actions"], T0, T1) for i in range(10000)]
        t = time.monotonic()
        out = [S.describe(S.finding(r)) for r in rows]
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(len(out), 10000)

    def test_loops_on_10k_node_graph(self):
        g = {}
        for i in range(5000):                         # a ring of 5000 services through 5000 tables
            g[f"svc:s{i}"] = {f"table:t{i}", "node:studio"}
            g[f"table:t{i}"] = {f"svc:s{(i + 1) % 5000}"}
        t = time.monotonic()
        found = S.loops(g)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]), 10000)


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection refused")
            return mock.MagicMock()
        with mock.patch.dict(sys.modules, {"psycopg2": mock.MagicMock(connect=flaky)}):
            S.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_query_failure_fails_open(self):
        # RETRY GAP: orbits — a failed query is not retried; it returns no findings.
        with mock.patch("builtins.print"):
            self.assertEqual(S.orbits(FakeCur(boom=True)), [])


class TestUnit(unittest.TestCase):
    def test_polarity_regexes(self):
        off, on = py(S.OFF), py(S.ON)
        for t in ("launchctl unload x", "disabled the task", "proposal rejected", "reverted dial"):
            self.assertTrue(off.search(t), t)
        for t in ("enabled nova-hue", "started daemon", "approved #12"):
            self.assertTrue(on.search(t) and not off.search(t), t)
        self.assertIsNone(on.search("restarted the daemon"))          # a restart is not a toggle
        self.assertIsNone(off.search("restarted the daemon"))

    def test_finding_and_describe(self):
        f = S.finding(ORBIT)
        self.assertEqual((f["cycles"], f["pulls"], f["sources"]), (3, "unknown", ["autonomy_ledger", "claude_actions"]))
        self.assertIn("pulls are unknown", S.describe(f))
        self.assertEqual(S.finding(("x", 0, 0, None, T0, T1))["sources"], [])

    def test_loops_edge_cases(self):
        self.assertEqual(S.loops({}), [])
        self.assertEqual(S.loops({"svc:a": {"table:t"}, "table:t": {"svc:a"}}), [])   # self read/write
        g = {"svc:a": {"table:t"}, "table:t": {"svc:b"}, "svc:b": {"table:a"}, "table:a": {"svc:a"}}
        self.assertEqual(S.loops(g), [["svc:a", "svc:b", "table:a", "table:t"]])
        self.assertEqual(S.loops(g, hub=0), [])

    def test_cfg_fallback(self):
        with mock.patch("builtins.print"):
            self.assertEqual(S._cfg(FakeCur(boom=True), "min_cycles", 3), 3)
        self.assertEqual(S._cfg(FakeCur({"service_config": [(5,)]}), "min_cycles", 3), 5)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(S.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_buick8_log import log_unexplained", SRC)
        self.assertIn("from nova_usher_fissure import build_graph", SRC)

    def test_reads_the_three_ledgers_and_config(self):
        for t in ("FROM claude_actions", "FROM autonomy_ledger", "FROM coagency_proposals"):
            self.assertIn(t, S.ORBIT_SQL)
        cur = FakeCur({"service_config": [(4,)]})
        with mock.patch("builtins.print"):
            S.orbits(cur)
        params = [p for s, p in cur.sql if "WITH ev" in s][0]
        self.assertEqual((params["cycles"], params["days"]), (4, 4))
        self.assertIn("command", params["skip"])
        self.assertEqual([p[:2] for s, p in cur.sql if "service_config" in s],
                         [("nova_speedy_circle", "window_days"), ("nova_speedy_circle", "min_cycles")])

    def test_loops_over_usher_graph(self):
        import nova_usher_fissure as U
        svcs = [{"name": "task:a", "script": "a.py", "host": "studio", "hops": [], "watched": [],
                 "writes": ["x"], "reads": ["y"]},
                {"name": "task:b", "script": "b.py", "host": "studio", "hops": [], "watched": [],
                 "writes": ["y"], "reads": ["x"]}]
        with mock.patch.object(U, "services", return_value=svcs), \
                mock.patch.object(U.W, "connect", return_value=fake_conn(FakeCur())), \
                mock.patch("builtins.print") as pr:
            self.assertEqual(S.show_loops(), 0)
        self.assertIn("svc:a.py, svc:b.py, table:x, table:y", " ".join(str(c) for c in pr.call_args_list))


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes):
        cur = FakeCur(routes)
        with mock.patch.object(S.W, "connect", return_value=fake_conn(cur)), \
                mock.patch("nova_buick8_log.log_unexplained") as lu, mock.patch("builtins.print"):
            found = S.run(dry=dry)
        return found, cur, lu

    def test_run_files_oscillation_to_buick8(self):
        found, cur, lu = self._run(False, {"FROM claude_actions": [ORBIT]})
        self.assertEqual(found[0]["target"], "nova-hue")
        args, kw = lu.call_args
        self.assertEqual((args[0], args[1]), ("oscillation", "nova-hue"))
        self.assertEqual((kw["source"], kw["evidence"]["pulls"]), ("speedy_circle", "unknown"))
        self.assertNotIn("cause", kw)

    def test_dry_run_writes_nothing(self):
        found, cur, lu = self._run(True, {"FROM claude_actions": [ORBIT]})
        self.assertEqual(len(found), 1)
        lu.assert_not_called()
        self.assertFalse(any(re.search(r"\b(INSERT|UPDATE|CREATE)\b", s) for s, _ in cur.sql))

    def test_pg_error_path(self):
        found, cur, lu = self._run(False, None)
        self.assertEqual(found, [])
        lu.assert_not_called()
        cur = FakeCur(boom=True)
        with mock.patch.object(S.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            self.assertEqual(S.run(dry=False), [])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_speedy_circle.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_speedy_circle.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn("--loops", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
