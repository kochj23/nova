#!/usr/bin/env python3
"""Tests for nova_peaslee_hand.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_peaslee_hand as P  # noqa: E402

SRC = (SCRIPTS / "nova_peaslee_hand.py").read_text()
BOTH = {"values", "never_do"}


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []
        self.connection = mock.MagicMock()
        self.description = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = []
        for k, v in self.routes.items():
            if k in sql:
                if isinstance(v, Exception):
                    raise v
                cols, rows = v if isinstance(v, tuple) else ([], v)
                self.description = [(c,) for c in cols]
                self._last = list(rows)
                break

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def entries_for(rows):
    es = P.chain(P.GENESIS, P.diff({}, rows, BOTH))
    for i, e in enumerate(es):
        e["seq"] = i + 1
    return es


def chain_rows(es):
    return [(e["seq"], e["table_name"], e["row_key"], e["op"], e["row"], e["row_hash"], e["prev_hash"],
             e["entry_hash"]) for e in es]


VAL_COLS = ["id", "version", "value", "statement", "source", "priority_hint", "supersedes", "status"]
ND_COLS = ["id", "kind", "text", "evidence", "active"]


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"(?i)(password|token)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn(str(Path.home()), SRC)

    def test_never_writes_root_to_boot_volume(self):
        with tempfile.TemporaryDirectory() as d:   # tempdir lives on the boot volume
            with mock.patch.object(P, "_boot_dev", return_value=os.stat(d).st_dev):
                err = P.write_root(os.path.join(d, "roots"), "x y 1\n")
            self.assertIn("off-host", err)
            self.assertFalse(os.path.exists(os.path.join(d, "roots")))

    def test_tampered_row_is_caught(self):
        es = entries_for({("never_do", "20"): {"id": 20, "text": "No sexual content"}})
        es[0]["row"] = {"id": 20, "text": "Sexual content is fine"}
        self.assertEqual(P.verify(es)[0], 1)


class TestPerformance(unittest.TestCase):
    def test_chain_and_verify_10k(self):
        rows = {("values", str(i)): {"id": i, "statement": "s" * 50} for i in range(10000)}
        t = time.monotonic()
        es = entries_for(rows)
        self.assertIsNone(P.verify(es)[0])
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertEqual(len(es), 10000)


class TestRetry(unittest.TestCase):
    def test_query_failure_fails_open(self):
        # RETRY GAP: load_chain / snapshot — connection retry lives in nova_watch_common.connect;
        # a failed query returns empty and an unread table is never reported as removed.
        cur = FakeCur(boom=True)
        self.assertEqual(P.load_chain(cur), [])
        self.assertEqual(P.snapshot(cur), ({}, set()))

    def test_connect_retries_with_backoff(self):
        import nova_watch_common as W
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            W.connect(attempts=3, delay=0, _sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_nas_write_error_is_returned_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file"
            blocker.write_text("x")
            with mock.patch.object(P, "_boot_dev", return_value=-1):
                err = P.write_root(str(blocker / "sub"), "x y 1\n")
            self.assertTrue(err)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(P.selftest(), 0)

    def test_diff_empty(self):
        self.assertEqual(P.diff({}, {}, BOTH), [])

    def test_p11_flow(self):
        old = {"id": 1, "value": "a", "status": "pending"}
        self.assertTrue(P.p11_signed("values", old, dict(old, status="active")))
        self.assertFalse(P.p11_signed("values", old, dict(old, status="retired")))
        self.assertFalse(P.p11_signed("values", old, None))

    def test_broken_prev_link(self):
        es = entries_for({("values", "1"): {"id": 1}, ("values", "2"): {"id": 2}})
        es[1]["prev_hash"] = "a" * 64
        self.assertEqual(P.verify(es)[0], 2)

    def test_check_roots(self):
        es = entries_for({("values", "1"): {"id": 1}})
        self.assertIsNone(P.check_roots(["", P.root_line("d", P.GENESIS, 0)], es))
        self.assertIn("not in the chain", P.check_roots([P.root_line("d", es[0]["entry_hash"], 5)], es))


class TestIntegration(unittest.TestCase):
    def test_uses_shared_helpers(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("W.get_config(cur, SERVICE, \"root_dir\"", SRC)
        self.assertIn("from nova_buick8_log import log_unexplained", SRC)

    def test_root_dir_from_service_config(self):
        cur = FakeCur({"service_config": [('"/mnt/off"',)]})
        self.assertEqual(P.root_dir(cur), "/mnt/off")
        self.assertEqual(P.root_dir(FakeCur(boom=True)), P.DEFAULT_ROOT_DIR)

    def test_snapshot_feeds_diff(self):
        cur = FakeCur({"FROM values": (VAL_COLS, [(1, 1, "a", "s", "src", 5, None, "active")]),
                       "relationship_ledger": (ND_COLS, [(20, "never_do", "t", "e", True)])})
        now, read = P.snapshot(cur)
        self.assertEqual(read, BOTH)
        ops = [c[2] for c in P.diff({}, now, read)]
        self.assertEqual(ops, ["added", "added"])


class TestFunctional(unittest.TestCase):
    def _routes(self, chain, val_status="active", nd=True, actions=()):
        r = {"FROM peaslee_chain": chain_rows(chain),
             "FROM values": (VAL_COLS, [(1, 1, "a", "s", "src", 5, None, val_status)]),
             "relationship_ledger": (ND_COLS, [(20, "never_do", "t", "e", True)] if nd else []),
             "service_config": [],
             "claude_actions": list(actions),
             "SELECT id FROM claude_queue": [],
             "INSERT INTO claude_queue": [(77,)],
             "FROM peaslee_roots": [(None,)]}
        return r

    def _first_chain(self):
        cur = FakeCur(self._routes([]))
        now, _ = P.snapshot(cur)
        return entries_for(now)

    def test_golden_path_unsigned_removal_filed_and_root_written(self):
        base = self._first_chain()
        with tempfile.TemporaryDirectory() as d:
            cur = FakeCur(self._routes(base, nd=False))
            with mock.patch.object(P, "_boot_dev", return_value=-1), \
                    mock.patch.object(P, "DEFAULT_ROOT_DIR", d):
                self.assertEqual(P.run(cur, dry=False), 0)
            line = (Path(d) / P.ROOT_FILE).read_text().split()
        sqls = [s for s, _ in cur.sql]
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS peaslee_chain" in s for s in sqls))
        ins = [p for s, p in cur.sql if "INSERT INTO peaslee_chain" in s]
        self.assertEqual([(p[0], p[1], p[2]) for p in ins], [("never_do", "20", "removed")])
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)
        self.assertIn("never_do row 20 removed without a signature", q[0][1])
        self.assertEqual(line[1], ins[0][6])            # off-host root == new entry hash
        self.assertEqual(line[2], "3")

    def test_p11_status_flow_is_not_a_finding(self):
        base = self._first_chain()
        cur = FakeCur(self._routes(base, val_status="retired"))
        with mock.patch.object(P, "write_root", return_value=None):
            P.run(cur, dry=False)
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))
        self.assertTrue(any("INSERT INTO peaslee_chain" in s for s, _ in cur.sql))

    def test_claude_action_signs_change(self):
        base = self._first_chain()
        cur = FakeCur(self._routes(base, nd=False, actions=[(5,)]))
        with mock.patch.object(P, "write_root", return_value=None):
            P.run(cur, dry=False)
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))

    def test_dry_run_writes_nothing(self):
        base = self._first_chain()
        cur = FakeCur(self._routes(base, nd=False))
        with mock.patch.object(P, "write_root") as wr:
            self.assertEqual(P.run(cur, dry=True), 0)
        wr.assert_not_called()
        for s, _ in cur.sql:
            self.assertRegex(s.lstrip(), r"^SELECT ")

    def test_broken_chain_appends_nothing(self):
        base = self._first_chain()
        base[0]["entry_hash"] = "b" * 64
        cur = FakeCur(self._routes(base))
        with mock.patch("nova_buick8_log.log_unexplained") as lu:
            self.assertEqual(P.run(cur, dry=False), 1)
        lu.assert_called_once()
        self.assertFalse(any("INSERT INTO peaslee_chain" in s for s, _ in cur.sql))

    def test_nas_down_records_unwritten_root(self):
        cur = FakeCur(self._routes([]))
        with mock.patch.object(P, "write_root", return_value="not mounted"):
            P.run(cur, dry=False)
        roots = [p for s, p in cur.sql if "INSERT INTO peaslee_roots" in s]
        self.assertEqual(roots[0][3:], (None, "not mounted"))


class TestFrame(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "nova_peaslee_hand.py"), *args],
                              capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))

    def test_selftest_cli(self):
        r = self._run("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        self.assertEqual(self._run("--help").returncode, 0)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
