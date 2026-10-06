#!/usr/bin/env python3
"""Tests for nova_identity_graph.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import math
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_identity_graph.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ig = _load("identity_graph_under_test", SCRIPT)


class _Cur:
    """Answers each collect() SELECT in order (ble, wifi, person, face, zone); records every statement."""
    def __init__(self, answers=(), face_raises=False):
        self.answers = list(answers); self.face_raises = face_raises
        self.sql, self.params, self.many = [], [], []
        self._last = []
        self.connection = types.SimpleNamespace(rollback=MagicMock())

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)
        if sql.lstrip().startswith("CREATE TABLE"):
            return
        self._last = self.answers.pop(0) if self.answers else []
        if "face_presence" in sql and self.face_raises:
            raise RuntimeError("relation face_presence does not exist")

    def fetchall(self):
        return self._last

    def executemany(self, sql, rows):
        self.many.append((" ".join(sql.split()), list(rows)))


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _slots(*ranges):
    out = set()
    for a, b in ranges:
        out.update(range(a, b))
    return out


def _rows(key, slots):
    return [(key, s) for s in slots]


def _run_main(cur, days=7, dry_run=False):
    conn = _Conn(cur)
    with patch.object(ig, "_connect", lambda dsn, tries=3: conn), redirect_stdout(io.StringIO()) as out:
        rc = ig.main(days, dry_run)
    return rc, out.getvalue(), conn


# A small, legible world: 100 five-minute slots. "amy_phone" (ble) and "amy" (person) are present in
# the same 30 slots and absent together otherwise -> phi 1.0. "fridge" (wifi) is on in every slot ->
# always-on, dropped. "stranger" (ble) overlaps amy in only 2 slots -> below MIN_SLOTS.
def _world():
    amy = _slots((10, 40))
    return _Cur([
        _rows("amy_phone", amy) + _rows("stranger", _slots((38, 60))),     # ble
        _rows("aa:bb", range(100)),                                         # wifi (always on)
        _rows("amy", amy),                                                  # person
        [],                                                                 # face
        _rows("camera_vision:kitchen", _slots((10, 40), (70, 80))),         # zone
    ])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ig.DSN)

    def test_sql_interpolation_is_limited_to_typed_integers(self):
        # the f-string SQL in collect() splices only {days} (argparse type=int) and SLOT_MINUTES (a module int)
        names = set(re.findall(r"\{(\w+)[^}]*\}", "".join(re.findall(r'f"""(.*?)"""', SRC, re.S))))
        self.assertEqual(names, {"days", "slot"})
        self.assertIn('ap.add_argument("--days", type=int', SRC)
        self.assertIsInstance(ig.SLOT_MINUTES, int)
        cur = _Cur([[], [], [], [], []])
        ig.collect(cur, 7)
        for sql in cur.sql:
            self.assertIn("interval '7 days'", sql)
        # the guard is argparse: a non-int --days is rejected before collect() is ever called
        r = subprocess.run([sys.executable, str(SCRIPT), "--days", "7;DROP"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid int value", r.stderr)

    def test_edges_are_written_with_placeholders_and_nothing_else_is_written(self):
        rc, out, conn = _run_main(_world())
        sql, rows = conn.cur.many[0]
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", sql)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.identity_graph_edge"})
        self.assertNotIn("device_owner", SRC.split('"""', 2)[2])     # evidence, not conclusions

    def test_presence_placeholders_are_not_identities(self):
        cur = _Cur([[], [], [], [], []])
        ig.collect(cur, 1)
        person_sql = [s for s in cur.sql if "FROM telemetry.presence" in s and "person NOT IN" in s][0]
        for ph in ("'unknown'", "'occupant'", "'motion'", "'vehicle'"):
            self.assertIn(ph, person_sql)


class TestPerformance(unittest.TestCase):
    def test_collect_reduces_10k_rows_quickly(self):
        rows = [(f"dev{i % 500}", i // 500) for i in range(10_000)]
        cur = _Cur([rows, [], [], [], []])
        t0 = time.perf_counter()
        nodes = ig.collect(cur, 7)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(nodes), 500)
        self.assertEqual(len(nodes[("ble", "dev0")]), 20)

    def test_pairwise_phi_over_200_nodes_is_bounded(self):
        # 200 nodes with staggered presence -> ~20k candidate pairs through the inline phi loop
        ble = [(f"d{i}", s) for i in range(100) for s in range(i, i + 40)]
        wifi = [(f"m{i}", s) for i in range(100) for s in range(i, i + 40)]
        cur = _Cur([ble, wifi, [], [], []])
        t0 = time.perf_counter()
        rc, out, conn = _run_main(cur, dry_run=True)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(rc, 0)
        self.assertIn("nodes: 200 across 2 identity spaces", out)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff_then_succeeds(self):
        import psycopg2
        conn = object()
        connect = MagicMock(side_effect=[psycopg2.OperationalError("timed out"), psycopg2.OperationalError("timed out"), conn])
        with patch.object(ig.psycopg2, "connect", connect), patch("time.sleep") as sleep:
            self.assertIs(ig._connect("dsn"), conn)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])
        self.assertEqual(connect.call_args.kwargs, {"connect_timeout": 10})

    def test_connect_gives_up_after_tries_and_raises_the_last_error(self):
        import psycopg2
        connect = MagicMock(side_effect=psycopg2.OperationalError("still down"))
        with patch.object(ig.psycopg2, "connect", connect), patch("time.sleep") as sleep:
            with self.assertRaises(psycopg2.OperationalError):
                ig._connect("dsn", tries=2)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(sleep.call_count, 1)                  # no sleep after the final attempt

    def test_non_operational_errors_are_not_retried(self):
        import psycopg2
        connect = MagicMock(side_effect=psycopg2.ProgrammingError("bad dsn"))
        with patch.object(ig.psycopg2, "connect", connect), patch("time.sleep") as sleep:
            with self.assertRaises(psycopg2.ProgrammingError):
                ig._connect("dsn")
        self.assertEqual(connect.call_count, 1)
        sleep.assert_not_called()

    def test_missing_face_table_fails_open_with_rollback(self):
        # RETRY GAP: collect()/face_presence — one attempt; a missing table rolls back and the other four spaces survive
        cur = _Cur([_rows("x", range(10)), [], [], [], _rows("mmwave:den", range(10))], face_raises=True)
        nodes = ig.collect(cur, 7)
        cur.connection.rollback.assert_called_once()
        self.assertEqual({k[0] for k in nodes}, {"ble", "zone"})


class TestUnit(unittest.TestCase):
    def test_collect_skips_empty_keys_and_stringifies(self):
        cur = _Cur([[(None, 1), ("", 2), (b"k", 3)], [], [], [], []])
        nodes = ig.collect(cur, 7)
        self.assertEqual(list(nodes), [("ble", "b'k'")])

    def test_collect_empty_world(self):
        self.assertEqual(ig.collect(_Cur([[], [], [], [], []]), 7), {})

    def test_phi_formula_matches_the_loop(self):
        # hand-computed contingency: together 30, a_only 0, b_only 0, neither 70 -> phi 1.0
        n11, n10, n01, n00 = 30, 0, 0, 70
        den = math.sqrt((n11+n10)*(n11+n01)*(n00+n10)*(n00+n01))
        self.assertEqual((n11*n00 - n10*n01) / den, 1.0)
        rc, out, conn = _run_main(_world(), dry_run=True)
        self.assertIn("1.00  ble:amy_phone", out)

    def test_thresholds_are_sane(self):
        self.assertGreaterEqual(ig.MIN_SLOTS, 2)
        self.assertTrue(0 < ig.MIN_PHI < 1)
        self.assertTrue(0.5 < ig.ALWAYS_ON < 1)

    def test_log_prefix(self):
        with redirect_stdout(io.StringIO()) as out:
            ig.log("hi")
        self.assertEqual(out.getvalue(), "[identity-graph] hi\n")


class TestIntegration(unittest.TestCase):
    def test_reads_the_four_identity_spaces_from_telemetry(self):
        cur = _Cur([[], [], [], [], []])
        ig.collect(cur, 3)
        tables = [re.search(r"FROM (\S+)", s).group(1) for s in cur.sql]
        self.assertEqual(tables, ["telemetry.bluetooth", "telemetry.unifi_metrics", "telemetry.presence",
                                  "face_presence", "telemetry.presence"])
        self.assertIn("coalesce(fingerprint, device_mac)", cur.sql[0])      # fingerprint survives MAC rotation
        self.assertIn("metric='unifi_client_signal_dbm'", cur.sql[1])
        self.assertIn(f"/{ig.SLOT_MINUTES * 60})::bigint", cur.sql[0])

    def test_collect_then_main_drops_always_on_and_keeps_cross_space_edge(self):
        rc, out, conn = _run_main(_world(), dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("dropped 1 always-present nodes", out)
        self.assertIn("ble=2, person=1, zone=1", out)
        self.assertIn("1.00  ble:amy_phone", out)
        self.assertIn("<-> person:amy", out)
        self.assertNotIn("stranger", out.split("CROSS-SPACE")[1])

    def test_table_is_created_before_use_and_main_uses_the_retrying_connector(self):
        rc, out, conn = _run_main(_world(), dry_run=True)
        self.assertTrue(conn.cur.sql[0].startswith("CREATE TABLE IF NOT EXISTS telemetry.identity_graph_edge"))
        self.assertTrue(conn.autocommit)
        self.assertIn("conn = _connect(DSN)", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_edges(self):
        rc, out, conn = _run_main(_world(), days=7)
        self.assertEqual(rc, 0)
        self.assertTrue(conn.closed)
        sql, rows = conn.cur.many[0]
        self.assertIn("INSERT INTO telemetry.identity_graph_edge", sql)
        self.assertIn("ON CONFLICT (a_kind,a_key,b_kind,b_key) DO UPDATE", sql)
        self.assertIn(("ble", "amy_phone", "person", "amy", 1.0, 30, 0, 0), rows)
        self.assertEqual(rows, sorted(rows, key=lambda e: -e[4]))
        self.assertIn(f"stored {len(rows)} edges", out)
        self.assertTrue(all(r[4] >= ig.MIN_PHI for r in rows))

    def test_dry_run_writes_nothing(self):
        rc, out, conn = _run_main(_world(), dry_run=True)
        self.assertEqual(conn.cur.many, [])
        self.assertNotIn("stored", out)
        self.assertTrue(conn.closed)

    def test_empty_world_reports_no_joins_and_writes_nothing(self):
        rc, out, conn = _run_main(_Cur([[], [], [], [], []]))
        self.assertEqual(rc, 0)
        self.assertIn("nodes: 0 across 0 identity spaces, 0 time slots", out)
        self.assertIn("(none yet", out)
        self.assertEqual(conn.cur.many, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--days", r.stdout)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_identity_graph"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
