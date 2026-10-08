#!/usr/bin/env python3
"""7-category gap tests for nova_embodiment.py (presence_state consumer, 2026-10-08 change).

Complements tests/test_nova_embodiment.py: covers the retry/backoff added to remember() and the
main() PG connect, the presence_state freshness contract, and bounded query counts.
All PG / HTTP is stubbed — nothing leaves the process. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_embodiment.py"
SRC = SCRIPT.read_text()


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emb = _load("embodiment_7cat")


class _Cur:
    """Cursor stub: substring routes; anything unrouted returns no rows. Records SQL + params."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self.params = []; self._v = None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._v = None
        for sub, v in self.routes:
            if sub in sql:
                if isinstance(v, Exception):
                    raise v
                self._v = v
                return

    def fetchone(self):
        return self._v[0] if isinstance(self._v, list) and self._v else (None if isinstance(self._v, list) else self._v)

    def fetchall(self):
        return self._v if isinstance(self._v, list) else ([] if self._v is None else [self._v])


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _compute(presence, residents=("jordan", "amy")):
    cur = _Cur([("FROM telemetry.device_owner", [(r,) for r in residents]),
                ("FROM presence_state", list(presence))])
    with redirect_stdout(io.StringIO()):
        return emb.compute(cur), cur


class TestSecurity(unittest.TestCase):
    def test_presence_read_binds_residents_as_a_parameter(self):
        st, cur = _compute([("jordan", "office")], residents=("jordan", "x'; DROP TABLE presence_state;--"))
        (sql, params), = [(s, p) for s, p in zip(cur.sql, cur.params) if "FROM presence_state" in s]
        self.assertNotIn("DROP", sql)                          # hostile name never reaches the SQL text
        self.assertIn("x'; DROP TABLE presence_state;--", params[0])
        self.assertIn("interval '10 minutes'", sql)           # only the module int is interpolated

    def test_offrhythm_memory_is_tagged_private_and_goes_to_the_local_memory_server(self):
        self.assertIn('"privacy": "private"', SRC)
        self.assertTrue(emb.MEMSRV.startswith("http://memory-server.digitalnoise.net"))
        for node in emb.OLLAMA_NODES:                         # LLM only names the state, on LAN nodes
            self.assertRegex(node, r"^http://(192\.168\.|127\.|[\w.-]+\.digitalnoise\.net)")

    def test_dsns_carry_no_password(self):
        for dsn in (emb.OPS_DSN, emb.MEM_DSN):
            self.assertNotIn("password", dsn)


class TestPerformance(unittest.TestCase):
    def test_query_count_does_not_scale_with_residents(self):
        _, few = _compute([], residents=("jordan", "amy"))
        _, many = _compute([], residents=tuple(f"p{i}" for i in range(50)))
        self.assertEqual(len(few.sql), len(many.sql))           # no per-person N+1

    def test_retry_backoff_is_bounded(self):
        sleeps = []
        with mock.patch.object(emb.urllib.request, "urlopen", side_effect=OSError("down")), \
             mock.patch.object(emb.time, "sleep", sleeps.append), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                emb.remember("t", "embodiment", {})
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertLessEqual(sum(sleeps), 10)

    def test_compute_is_fast(self):
        t0 = time.perf_counter()
        for _ in range(200):
            _compute([("jordan", "office"), ("amy", "away")])
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_remember_recovers_on_second_attempt(self):
        uo = mock.MagicMock(side_effect=[OSError("blip"), _Resp({"id": 9})])
        with mock.patch.object(emb.urllib.request, "urlopen", uo), mock.patch.object(emb.time, "sleep"), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(emb.remember("t", "embodiment", {"a": 1}), 9)
        self.assertEqual(uo.call_count, 2)
        self.assertIn("memory write attempt 1 failed", out.getvalue())   # never silent

    def test_connect_retries_operational_error_then_succeeds(self):
        conn = object()
        c = mock.MagicMock(side_effect=[emb.psycopg2.OperationalError("x"), emb.psycopg2.OperationalError("y"), conn])
        with mock.patch.object(emb.psycopg2, "connect", c), mock.patch.object(emb.time, "sleep") as sl, \
             redirect_stdout(io.StringIO()):
            self.assertIs(emb._connect_retry(), conn)
        self.assertEqual(c.call_count, 3)
        self.assertEqual([a.args[0] for a in sl.call_args_list], [2.0, 4.0])

    def test_connect_gives_up_loudly_after_three(self):
        c = mock.MagicMock(side_effect=emb.psycopg2.OperationalError("down"))
        with mock.patch.object(emb.psycopg2, "connect", c), mock.patch.object(emb.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(emb.psycopg2.OperationalError):
                emb._connect_retry()
        self.assertEqual(c.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_presence_rows_split_home_and_away(self):
        st, _ = _compute([("jordan", "office"), ("amy", "away")])
        self.assertEqual(st["occupancy"]["residents_home"], ["jordan"])
        self.assertEqual(st["occupancy"]["residents_away"], ["amy"])

    def test_home_unplaced_counts_as_home(self):
        st, _ = _compute([("jordan", "home")])
        self.assertEqual(st["occupancy"]["residents_home"], ["jordan"])
        self.assertEqual(st["occupancy"]["summary"], "jordan is home")

    def test_connect_uses_a_timeout(self):
        c = mock.MagicMock()
        with mock.patch.object(emb.psycopg2, "connect", c):
            emb._connect_retry()
        self.assertIn("connect_timeout", c.call_args.kwargs)


class TestIntegration(unittest.TestCase):
    def test_presence_read_failure_is_unknown_not_empty(self):
        cur = _Cur([("FROM presence_state", RuntimeError("relation missing"))])
        with redirect_stdout(io.StringIO()):
            st = emb.compute(cur)
        self.assertEqual(st["occupancy"]["summary"], "occupancy unknown (presence engine silent)")
        self.assertNotEqual(st["house_state"], "empty")

    def test_everyone_away_is_empty(self):
        st, _ = _compute([("jordan", "away"), ("amy", "away")])
        self.assertEqual(st["house_state"], "empty")


class TestFunctional(unittest.TestCase):
    def _main(self, leading_errors, argv=("--no-write",)):
        cur = _Cur([("INSERT INTO embodiment_state", (1, emb.datetime.now())),
                    ("FROM presence_state", [("jordan", "office")])])
        conn = types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, close=lambda: None)
        connect = mock.MagicMock(side_effect=[*leading_errors, conn, conn])   # main + accessor preview
        with mock.patch.object(emb.psycopg2, "connect", connect), mock.patch.object(emb.time, "sleep"), \
             mock.patch.object(sys, "argv", ["nova_embodiment.py", *argv]), \
             mock.patch.object(emb.urllib.request, "urlopen", side_effect=OSError("no llm in tests")), \
             mock.patch.object(emb, "lineage_stamp", lambda **kw: {}), redirect_stdout(io.StringIO()) as out:
            rc = emb.main()
        return rc, cur, out.getvalue()

    def test_main_survives_a_transient_pg_blip(self):
        rc, cur, out = self._main([emb.psycopg2.OperationalError("blip")])
        self.assertEqual(rc, 0)
        self.assertIn("PG connect attempt 1 failed", out)
        (p,) = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO embodiment_state" in s]
        self.assertEqual(p[0], "calm")

    def test_main_raises_when_pg_stays_down(self):
        c = mock.MagicMock(side_effect=emb.psycopg2.OperationalError("down"))
        with mock.patch.object(emb.psycopg2, "connect", c), mock.patch.object(emb.time, "sleep"), \
             mock.patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(emb.psycopg2.OperationalError):
                emb.main()


class TestFrame(unittest.TestCase):
    def test_compiles_and_imports_with_entry_points(self):
        r = subprocess.run([sys.executable, "-c",
                            "import nova_embodiment as m; assert callable(m.main) and callable(m._connect_retry) "
                            "and callable(m.current_embodiment); print('ok')"],
                           cwd=SCRIPTS, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
