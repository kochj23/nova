#!/usr/bin/env python3
"""Tests for nova_telemetry_observer.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_telemetry_observer.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# basicConfig would point the ROOT logger at ~/.openclaw/logs for the whole test session — never let it run
with patch("psycopg2.connect", side_effect=OSError("offline test")), patch("logging.basicConfig"):
    to = _load("telemetry_observer_t", SCRIPT)
SRC = SCRIPT.read_text()
to.notify = MagicMock()
to.log.disabled = True
to.psycopg2 = MagicMock(extras=MagicMock(RealDictCursor=object))
to.psycopg2.connect.side_effect = RuntimeError("psycopg2.connect not mocked in test")


def _router(routes):
    """query/query_one stand-in: first route whose key is in the SQL wins."""
    def q(conn, sql, params=None):
        for k, v in routes.items():
            if k in sql:
                return v
        return None
    return q


def _with(routes):
    q = _router(routes)
    return patch.object(to, "query", side_effect=lambda c, s, p=None: q(c, s, p) or []), \
        patch.object(to, "query_one", side_effect=q)


class _Cur:
    def __init__(self, count=0):
        self.count = count; self.stmts = []

    def execute(self, sql, params=None):
        self.stmts.append((sql, params))

    def fetchone(self):
        return (self.count,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, count=0):
        self.cur = _Cur(count); self.commits = 0; self.closed = False

    def cursor(self, **k):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _subjects():
    return [(o.category, o.subject, o.severity) for o in to.observations]


class _Base(unittest.TestCase):
    def setUp(self):
        to.observations.clear(); to.notify.reset_mock()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'(execute|query|query_one)\(\s*\w*,?\s*f["\']', SRC))
        self.assertNotIn("password=", SRC)

    def test_new_device_name_is_bound_param(self):
        p1, p2 = _with({"FROM telemetry.network\n        WHERE ts >= %s AND client_mac": [
            {"client_mac": "aa", "client_name": "x'); --", "ip": "10.0.0.9"}]})
        conn = _Conn(count=4)
        with p1, p2:
            to.analyze_network(conn)
        ins = [s for s in conn.cur.stmts if "INSERT INTO telemetry.known_devices" in s[0]][0]
        self.assertNotIn("x');", ins[0])
        self.assertEqual(ins[1], ("aa", "x'); --", "10.0.0.9"))


class TestPerformance(_Base):
    def test_digest_and_dumps_10k(self):
        obs = [to.Observation("energy", f"d{i}", "m", "warning", {"w": Decimal("1.5"), "t": datetime.now()})
               for i in range(10_000)]
        t0 = time.perf_counter()
        to.format_digest(obs)
        for o in obs:
            to._safe_dumps(o.metadata)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_fatal_db_error_pages_once_and_exits_1(self):
        # RETRY GAP: main()/get_nova_ops_conn — one connect per hourly run; failure pages critical and exits 1
        with patch.object(to, "get_nova_ops_conn", side_effect=OSError("pg down")):
            with self.assertRaises(SystemExit) as e:
                to.main()
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(to.notify.call_args.kwargs["dedup_key"], "telemetry-observer-crash")

    def test_crash_alert_failure_still_exits_cleanly(self):
        with patch.object(to, "get_nova_ops_conn", side_effect=OSError("pg down")), \
                patch.object(to, "notify", side_effect=RuntimeError("bus down")):
            with self.assertRaises(SystemExit):
                to.main()


class TestUnit(_Base):
    def test_safe_dumps_and_digest(self):
        self.assertEqual(json.loads(to._safe_dumps({"d": Decimal("2.5")})), {"d": 2.5})
        with self.assertRaises(TypeError):
            to._safe_dumps({"x": object()})
        self.assertEqual(to.format_digest([]), "")
        d = to.format_digest([to.Observation("weather", "s", "hot", "warning"), to.Observation("zzz", "s", "c", "critical")])
        self.assertIn("hot ⚠️", d)
        self.assertIn("• c \U0001f6a8", d)

    def test_weather_thresholds(self):
        p1, p2 = _with({"temp_f FROM telemetry.weather\n        WHERE ts >= %s AND temp_f": [{"temp_f": 60}, {"temp_f": 80}],
                        "pressure_in": [{"pressure_in": 30.0}, {"pressure_in": 29.8}],
                        "uv_index": {"uv_index": 9.0}})
        with p1, p2:
            to.analyze_weather(_Conn())
        self.assertEqual({s for _, s, _ in _subjects()}, {"temp_swing", "pressure_drop", "uv_extreme"})

    def test_rack_room_has_its_own_ceiling(self):
        p1, p2 = _with({"MIN(temp_f)": [
            {"room": "server_rack", "avg_temp": 94, "max_temp": 95, "min_temp": 93},
            {"room": "office", "avg_temp": 79, "max_temp": 80, "min_temp": 78}]})
        with p1, p2:
            to.analyze_climate(_Conn())
        self.assertEqual([s for c, s, _ in _subjects() if c == "climate"], ["office"])


class TestIntegration(_Base):
    def test_network_seeds_silently_then_alerts(self):
        devs = [{"client_mac": f"m{i}", "client_name": None, "ip": f"10.0.0.{i}"} for i in range(7)]
        p1, p2 = _with({"WHERE ts >= %s AND client_mac IS NOT NULL": devs})
        with p1, p2:
            to.analyze_network(_Conn(count=0))
        self.assertEqual(_subjects(), [])                             # first run: seed only
        with p1, p2:
            to.analyze_network(_Conn(count=50))
        subs = [s for _, s, _ in _subjects()]
        self.assertEqual(subs.count("new_device"), 5)
        self.assertIn("new_devices_bulk", subs)

    def test_save_observations_writes_shared_observations(self):
        conn = _Conn()
        to.save_observations(conn, [to.Observation("av", "tv", "on", "info", {"h": Decimal("9")})])
        sql, params = conn.cur.stmts[0]
        self.assertIn("INSERT INTO shared_observations", sql)
        self.assertEqual(params[0], "nova_telemetry_observer")
        self.assertEqual(json.loads(params[5]), {"h": 9.0})


class TestFunctional(_Base):
    def test_fridge_warmup_is_critical_and_batched(self):
        now = datetime.now(timezone.utc)
        rows = [{"ts": now - timedelta(minutes=m), "temp_f": t, "battery": 1}
                for m, t in ((120, 36.0), (14, 47.0), (8, 48.0), (1, 49.0))]
        p1, p2 = _with({"aux_sensors": rows})
        with p1, p2:
            to.analyze_fridge(_Conn())
        self.assertEqual(_subjects(), [("fridge", "warming", "critical")])
        to.post_critical_immediately(to.observations)
        self.assertEqual(to.notify.call_args.kwargs["level"], "critical")

    def test_main_golden_path_saves_and_posts_digest(self):
        ops, mem = _Conn(count=3), _Conn()
        p1, p2 = _with({"uv_index": {"uv_index": 10.0}})
        with p1, p2, patch.object(to, "get_nova_ops_conn", return_value=ops), \
                patch.object(to, "get_nova_memories_conn", return_value=mem):
            to.main()
        self.assertTrue(any("INSERT INTO shared_observations" in s for s, _ in ops.cur.stmts))
        self.assertEqual(to.notify.call_args.kwargs["dedup_key"], "telemetry-hourly-digest")
        self.assertTrue(ops.closed and mem.closed)

    def test_quiet_hour_posts_nothing(self):
        to.post_digest([]); to.post_critical_immediately([])
        to.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_and_logs_stay_in_home(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                    "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                    "import importlib.util as u;s=u.spec_from_file_location('m','nova_telemetry_observer.py');"
                    "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
            r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                               timeout=30, env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "ok")
            self.assertTrue((Path(home) / ".openclaw/logs").is_dir())    # the log dir followed the fake HOME


if __name__ == "__main__":
    unittest.main()
