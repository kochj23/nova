"""
test_nova_mesh_churn_report.py — All 7 test categories for nova_mesh_churn_report.py
Written by Jordan Koch.

nova_mesh_churn_report.py snapshots the Heltec T114's Meshtastic NodeDB (via the
bridge on Jordans-Mac-mini) into telemetry.mesh_nodes and reports daily churn.

HARD SAFETY: nova_notify is stubbed BEFORE load so notify() can never enqueue a
real event; psycopg2 is replaced per-test with fakes so no connection is ever
opened; urllib is patched so the bridge is never contacted; LOG_FILE is redirected.
"""

import ast
import json
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_mesh_churn_report.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

_mod = load_script_compat(_SCRIPT, "nova_mesh_churn_report")

_LOGS = []
_mod.LOG_FILE = Path(tempfile.gettempdir()) / "nova_mesh_churn_test.log"
_mod.log = lambda msg: _LOGS.append(msg)

collect = _mod.collect
report = _mod.report
_SRC = _SCRIPT.read_text()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeCursor:
    """One shared cursor: records executes, hands out scripted fetch results."""

    def __init__(self, fetchone_queue=None, fetchall_queue=None):
        self.executed = []
        self._one = list(fetchone_queue or [])
        self._all = list(fetchall_queue or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._one.pop(0) if self._one else None

    def fetchall(self):
        return self._all.pop(0) if self._all else []

    def close(self):
        self.closed = True

    # Convenience for assertions
    def sql_containing(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0
        self.closed = False

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


class FakeHTTPResponse:
    def __init__(self, obj):
        self._body = json.dumps(obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _fake_pg(cursor):
    fake = MagicMock(name="psycopg2")
    fake.connect.return_value = FakeConn(cursor)
    return fake


NODE_A = {"id": "!aabbccdd", "longName": "Rancho Adjacent", "shortName": "RA",
          "hwModel": "HELTEC_T114", "snr": 8.5, "hopsAway": 0,
          "lastHeard": 1753800000, "batteryLevel": 88,
          "latitude": 34.1, "longitude": -118.4}
NODE_B = {"id": "!11223344", "longName": "Drive By", "shortName": "DB",
          "hwModel": "TBEAM", "snr": -12.0, "hopsAway": 2,
          "lastHeard": 1753803600, "batteryLevel": 40,
          "latitude": None, "longitude": None}


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        for pat in (r"xox[baprs]-\d{5,}", r"\bsk-[A-Za-z0-9]{20,}",
                    r"\bghp_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
                    r"(?i)password\s*=\s*['\"][^'\"]{4,}"):
            self.assertIsNone(re.search(pat, _SRC),
                              f"possible hardcoded credential matching {pat!r}")

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC)

    def test_dsn_has_no_password(self):
        self.assertNotIn("password", _mod.DSN)
        self.assertIn("dbname=nova_ops", _mod.DSN)

    def test_bridge_url_is_lan_only(self):
        """The bridge must be reached on the LAN, never over the internet."""
        self.assertTrue(_mod.BRIDGE_NODES_URL.startswith("http://"))
        self.assertIn(".local", _mod.BRIDGE_NODES_URL,
                      "use the mDNS name so it works with DNS/internet down")
        self.assertNotIn("https://", _mod.BRIDGE_NODES_URL)

    def test_bridge_read_is_get_only(self):
        """This script only READS the radio's NodeDB — never sends to the mesh."""
        self.assertNotIn("/send", _SRC)
        self.assertNotIn("method=\"POST\"", _SRC)
        self.assertNotIn("urllib.request.Request(", _SRC)

    def test_sql_is_parameterized(self):
        """No f-string SQL: every value goes through psycopg2 params."""
        self.assertNotIn('cur.execute(f"', _SRC)
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", _SRC)

    def test_bridge_fetch_has_a_timeout(self):
        self.assertIn("timeout=10", _SRC)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_collect_skips_unchanged_sighting(self):
        """An unchanged (node_id,last_heard) must not be re-inserted every hour."""
        cur = FakeCursor(fetchone_queue=[(1,), None])   # A already seen, B new
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A, NODE_B]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        inserts = cur.sql_containing("INSERT INTO telemetry.mesh_nodes")
        self.assertEqual(len(inserts), 1, "only the new sighting may be inserted")
        self.assertEqual(inserts[0][1][0], NODE_B["id"])

    def test_collect_dedup_probe_is_a_bounded_limit_1_lookup(self):
        cur = FakeCursor(fetchone_queue=[None])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        probes = cur.sql_containing("SELECT 1 FROM telemetry.mesh_nodes")
        self.assertEqual(len(probes), 1)
        self.assertIn("LIMIT 1", probes[0][0])

    def test_collect_commits_once_not_per_node(self):
        cur = FakeCursor(fetchone_queue=[None, None])
        fake = _fake_pg(cur)
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A, NODE_B]})), \
             patch.object(_mod, "psycopg2", fake):
            collect()
        self.assertEqual(fake.connect.return_value.committed, 1)

    def test_report_queries_are_windowed_to_14_days(self):
        """Churn windows must be bounded so the query never scans all history."""
        self.assertEqual(_SRC.count("interval '14 days'"), 2)
        self.assertIn("interval '24 hours'", _SRC)
        self.assertIn("interval '3 days'", _SRC)

    def test_report_output_lists_are_truncated(self):
        self.assertIn("new_nodes[:15]", _SRC)
        self.assertIn("gone_nodes[:15]", _SRC)
        self.assertIn("active[:15]", _SRC)

    def test_report_indexes_exist_for_the_query_shape(self):
        self.assertIn("mesh_nodes_node_ts_idx", _mod.DDL)
        self.assertIn("mesh_nodes_ts_idx", _mod.DDL)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_collect_survives_unreachable_bridge(self):
        """REGRESSION (bug found + fixed 2026-07-29): an unreachable bridge (radio
        unplugged, mini rebooting, mDNS not resolving) must log and exit cleanly.
        The fetch was originally a bare urlopen(), so URLError propagated out of
        collect() and killed the hourly launchd job with a traceback. It now retries
        3x then gives up quietly.
        """
        cur = FakeCursor()
        with patch.object(_mod.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("connection refused")), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)), \
             patch.object(_mod.time, "sleep"):          # don't sleep through the retries
            collect()   # must not raise

    def test_collect_bridge_failure_never_touches_the_database(self):
        """Whatever it does on failure, it must not write a partial snapshot."""
        cur = FakeCursor()
        fake = _fake_pg(cur)
        with patch.object(_mod.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("connection refused")), \
             patch.object(_mod, "psycopg2", fake), \
             patch.object(_mod.time, "sleep"):          # don't sleep through the retries
            collect()          # gives up quietly (fixed 2026-07-29) — must not raise
        # ...and must not have written a partial snapshot on the way out.
        fake.connect.assert_not_called()
        self.assertEqual(cur.executed, [])

    def test_collect_tolerates_empty_and_missing_node_list(self):
        for payload in ({"nodes": []}, {}):
            cur = FakeCursor()
            with patch.object(_mod.urllib.request, "urlopen",
                              return_value=FakeHTTPResponse(payload)), \
                 patch.object(_mod, "psycopg2", _fake_pg(cur)):
                collect()   # must not raise
            self.assertEqual(cur.sql_containing("INSERT INTO telemetry.mesh_nodes"), [])

    def test_collect_tolerates_nodes_with_no_last_heard(self):
        node = dict(NODE_A)
        node.pop("lastHeard")
        cur = FakeCursor(fetchone_queue=[None])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [node]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        inserts = cur.sql_containing("INSERT INTO telemetry.mesh_nodes")
        self.assertEqual(len(inserts), 1)
        self.assertIsNone(inserts[0][1][6], "last_heard must be NULL, not a crash")

    def test_report_notify_failure_is_logged_not_raised(self):
        cur = FakeCursor(fetchall_queue=[[], [], []])
        _notify_stub.notify.side_effect = RuntimeError("bus down")
        try:
            with patch.object(_mod, "psycopg2", _fake_pg(cur)), \
                 patch.object(_mod, "notify", _notify_stub.notify):
                report()   # must not raise
        finally:
            _notify_stub.notify.side_effect = None
        self.assertTrue(any("Notify failed" in m for m in _LOGS))

    def test_log_write_failure_is_swallowed(self):
        fn = next(n for n in ast.walk(ast.parse(_SRC))
                  if isinstance(n, ast.FunctionDef) and n.name == "log")
        self.assertTrue(any(isinstance(n, ast.Try) for n in fn.body),
                        "log() must guard its file write")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_ddl_is_idempotent(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.mesh_nodes", _mod.DDL)
        self.assertEqual(_mod.DDL.count("CREATE INDEX IF NOT EXISTS"), 2)
        self.assertNotIn("DROP ", _mod.DDL.upper())

    def test_collect_applies_ddl_before_writing(self):
        cur = FakeCursor(fetchone_queue=[None])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        self.assertIn("CREATE TABLE IF NOT EXISTS", cur.executed[0][0])

    def test_report_applies_ddl_before_reading(self):
        cur = FakeCursor(fetchall_queue=[[], [], []])
        with patch.object(_mod, "psycopg2", _fake_pg(cur)), \
             patch.object(_mod, "notify", _notify_stub.notify):
            report()
        self.assertIn("CREATE TABLE IF NOT EXISTS", cur.executed[0][0])

    def test_collect_converts_last_heard_epoch_to_utc(self):
        cur = FakeCursor(fetchone_queue=[None])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        params = cur.sql_containing("INSERT INTO telemetry.mesh_nodes")[0][1]
        self.assertIsInstance(params[6], datetime)
        self.assertEqual(params[6].tzinfo, timezone.utc)
        self.assertEqual(params[6],
                         datetime.fromtimestamp(NODE_A["lastHeard"], tz=timezone.utc))

    def test_collect_maps_every_nodedb_field(self):
        cur = FakeCursor(fetchone_queue=[None])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        params = cur.sql_containing("INSERT INTO telemetry.mesh_nodes")[0][1]
        self.assertEqual(params[0], NODE_A["id"])
        self.assertEqual(params[1], NODE_A["longName"])
        self.assertEqual(params[2], NODE_A["shortName"])
        self.assertEqual(params[3], NODE_A["hwModel"])
        self.assertEqual(params[4], NODE_A["snr"])
        self.assertEqual(params[5], NODE_A["hopsAway"])
        self.assertEqual(params[7], NODE_A["batteryLevel"])

    def test_new_node_sql_requires_second_distinct_day_today(self):
        """A one-off drive-by is weather, not churn: day_rank = 2 AND d = today."""
        self.assertIn("day_rank = 2", _SRC)
        self.assertIn("d = current_date", _SRC)
        self.assertIn("row_number() OVER (PARTITION BY node_id ORDER BY d)", _SRC)

    def test_gone_node_sql_requires_regularity_then_silence(self):
        self.assertIn("days_seen >= 7", _SRC)
        self.assertIn("last_heard < now() - interval '3 days'", _SRC)
        self.assertIn("count(DISTINCT date(last_heard))", _SRC)

    def test_argparse_exposes_collect_and_report(self):
        self.assertIn('"--collect"', _SRC)
        self.assertIn('"--report"', _SRC)
        self.assertIn("ap.print_help()", _SRC)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def _rows(self):
        gone_at = datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc)
        new_nodes = [{"node_id": "!newnode1", "long_name": "New Neighbor"}]
        gone_nodes = [{"node_id": "!gonenode", "long_name": "Old Friend",
                       "days_seen": 11, "last_heard": gone_at}]
        active = [{"node_id": "!aabbccdd", "long_name": "Rancho Adjacent",
                   "short_name": "RA", "last_heard": gone_at, "best_snr": 8.5,
                   "min_hops": 0}]
        return new_nodes, gone_nodes, active

    def test_report_writes_shared_observations_for_new_and_gone(self):
        cur = FakeCursor(fetchall_queue=list(self._rows()))
        with patch.object(_mod, "psycopg2", _fake_pg(cur)), \
             patch.object(_mod, "notify", _notify_stub.notify):
            report()
        obs = cur.sql_containing("INSERT INTO shared_observations")
        self.assertEqual(len(obs), 2)
        subjects = " ".join(s for s, p in obs)
        self.assertIn("mesh-new-node", subjects)
        self.assertIn("mesh-node-gone", subjects)
        self.assertIn("'network'", subjects)
        self.assertIn("New Neighbor", obs[0][1][0])
        self.assertIn("Old Friend", obs[1][1][0])

    def test_report_notifies_with_network_category(self):
        cur = FakeCursor(fetchall_queue=list(self._rows()))
        notify = MagicMock()
        with patch.object(_mod, "psycopg2", _fake_pg(cur)), \
             patch.object(_mod, "notify", notify):
            report()
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["category"], "network")
        self.assertEqual(kwargs["level"], "info")
        self.assertEqual(kwargs["dedup_key"], "mesh-churn-daily")
        self.assertEqual(kwargs["meta"]["dedup_window_s"], 72000)

    def test_report_body_summarizes_new_gone_and_active(self):
        cur = FakeCursor(fetchall_queue=list(self._rows()))
        notify = MagicMock()
        with patch.object(_mod, "psycopg2", _fake_pg(cur)), \
             patch.object(_mod, "notify", notify):
            report()
        body = notify.call_args.kwargs["body"]
        self.assertIn("1 new, 1 gone", body)
        self.assertIn("New Neighbor", body)
        self.assertIn("Old Friend", body)
        self.assertIn("Rancho Adjacent", body)
        self.assertIn("SNR 8.5", body)
        self.assertIn("direct", body)

    def test_report_commits_and_closes(self):
        cur = FakeCursor(fetchall_queue=list(self._rows()))
        fake = _fake_pg(cur)
        with patch.object(_mod, "psycopg2", fake), \
             patch.object(_mod, "notify", _notify_stub.notify):
            report()
        conn = fake.connect.return_value
        self.assertEqual(conn.committed, 1)
        self.assertTrue(conn.closed)

    def test_collect_closes_the_connection(self):
        cur = FakeCursor(fetchone_queue=[None])
        fake = _fake_pg(cur)
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", fake):
            collect()
        self.assertTrue(fake.connect.return_value.closed)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_quiet_day_reports_zero_churn_without_observations(self):
        cur = FakeCursor(fetchall_queue=[[], [], []])
        notify = MagicMock()
        with patch.object(_mod, "psycopg2", _fake_pg(cur)), \
             patch.object(_mod, "notify", notify):
            report()
        self.assertEqual(cur.sql_containing("INSERT INTO shared_observations"), [])
        body = notify.call_args.kwargs["body"]
        self.assertIn("0 new, 0 gone", body)

    def test_collect_then_report_cycle(self):
        # 1) hourly collect records one fresh sighting
        ccur = FakeCursor(fetchone_queue=[None])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", _fake_pg(ccur)):
            collect()
        self.assertEqual(len(ccur.sql_containing("INSERT INTO telemetry.mesh_nodes")), 1)
        # 2) the daily report reads it back out and posts
        rcur = FakeCursor(fetchall_queue=[
            [], [], [{"node_id": NODE_A["id"], "long_name": NODE_A["longName"],
                      "short_name": "RA", "last_heard": datetime.now(timezone.utc),
                      "best_snr": 8.5, "min_hops": 2}]])
        notify = MagicMock()
        with patch.object(_mod, "psycopg2", _fake_pg(rcur)), \
             patch.object(_mod, "notify", notify):
            report()
        body = notify.call_args.kwargs["body"]
        self.assertIn("Rancho Adjacent", body)
        self.assertIn("2 hop(s)", body)

    def test_second_collect_of_same_nodedb_inserts_nothing(self):
        """The NodeDB keeps everything forever — an hourly rerun must be a no-op."""
        cur = FakeCursor(fetchone_queue=[(1,)])
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse({"nodes": [NODE_A]})), \
             patch.object(_mod, "psycopg2", _fake_pg(cur)):
            collect()
        self.assertEqual(cur.sql_containing("INSERT INTO telemetry.mesh_nodes"), [])
        self.assertTrue(any("0 new sightings" in m for m in _LOGS))

    def test_no_args_prints_help_and_exits_zero(self):
        """Running with no flags must be inert — no bridge call, no DB, rc=0."""
        r = subprocess.run([sys.executable, str(_SCRIPT)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--collect", r.stdout)
        self.assertIn("--report", r.stdout)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_mesh_churn_report.py has syntax errors: {e}")

    def test_shebang_and_docstring(self):
        self.assertTrue(_SRC.startswith("#!/usr/bin/env python3"))
        doc = ast.get_docstring(ast.parse(_SRC))
        self.assertIn("nova_mesh_churn_report.py", doc)

    def test_public_callables_present(self):
        for fn in ("collect", "report", "log"):
            self.assertTrue(callable(getattr(_mod, fn, None)), f"missing: {fn}")

    def test_constants_present(self):
        for name in ("DSN", "BRIDGE_NODES_URL", "LOG_FILE", "DDL"):
            self.assertIsNotNone(getattr(_mod, name, None), f"missing: {name}")

    def test_entrypoint_guarded(self):
        self.assertIn('if __name__ == "__main__":', _SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
