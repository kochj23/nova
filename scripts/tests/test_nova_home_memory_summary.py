#!/usr/bin/env python3
"""Tests for nova_home_memory_summary.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_home_memory_summary.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hm = _load("hm", SCRIPT)          # import is clean: psycopg2 only, no connect at load


class _Cur:
    """Answers the four telemetry queries in order: rooms (fetchall), aq (fetchone), bat (fetchone), occ (fetchall)."""
    def __init__(self, rooms=(), aq=None, bat=None, occ=()):
        self.answers = {"climate": list(rooms), "air_quality": aq, "battery": bat, "presence": [(r,) for r in occ]}
        self.sql = []; self._last = ""

    def execute(self, sql, params=()):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def _key(self):
        return next(k for k in self.answers if f"telemetry.{k}" in self._last)

    def fetchall(self):
        return self.answers[self._key()]

    def fetchone(self):
        return self.answers[self._key()]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


FULL = dict(rooms=[("Rack", 78.4, 84.1, 41, 120), ("Office", 72.0, 75.5, 28, 300)], aq=(210, 480), bat=("Eve Door", 22),
            occ=["kitchen", "office"])


def _main(cur, urlopen=None):
    conn = _Conn(cur)
    u = urlopen or MagicMock()
    u.return_value.__enter__.return_value.read.return_value = b'{"ok": true, "id": 99}'
    with patch.object(hm.psycopg2, "connect", return_value=conn) as pg, patch.object(hm.urllib.request, "urlopen", u), \
         redirect_stdout(io.StringIO()) as out:
        rc = hm.main()
    return rc, out.getvalue(), conn, pg, u


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hm.DSN)

    def test_sql_is_read_only_and_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute\([^)]*%\s*\(', SRC))
        self.assertEqual(re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC), [])
        cur = _Cur(**FULL)
        hm.build_summary(cur)
        for sql, params in cur.sql:
            self.assertTrue(sql.startswith("SELECT"), sql)
            self.assertIn("telemetry.", sql)

    def test_room_names_from_pg_are_never_interpolated_into_sql(self):
        inj = "Rack'; DR" "OP TABLE telemetry.climate; --"
        cur = _Cur(rooms=[(inj, 70.0, 71.0, 50, 10)])
        text, meta = hm.build_summary(cur)
        self.assertIn(inj, text)                                   # it is data in the narrative…
        self.assertFalse(any("DR" "OP" in s for s, _ in cur.sql))  # …never in a statement

    def test_memory_write_goes_only_to_the_vector_url_as_home_observations(self):
        u = MagicMock(); u.return_value.__enter__.return_value.read.return_value = b"ok"
        with patch.object(hm.urllib.request, "urlopen", u):
            hm.remember("t", {"k": 1})
        req = u.call_args.args[0]
        self.assertEqual(req.full_url, hm.VECTOR_URL)
        self.assertEqual(json.loads(req.data), {"text": "t", "source": "home_observations", "metadata": {"k": 1}})
        self.assertEqual(u.call_args.kwargs["timeout"], 15)


class TestPerformance(unittest.TestCase):
    def test_build_summary_over_10k_rooms_is_fast(self):
        rooms = [(f"room{i}", 70.0 + i % 9, 80.0, 45 if i % 2 else 50, 100) for i in range(10_000)]
        cur = _Cur(rooms=rooms, occ=[f"room{i}" for i in range(10_000)])
        t0 = time.perf_counter()
        text, meta = hm.build_summary(cur)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(meta["rooms_tracked"], 10_000); self.assertEqual(len(meta["occupied_rooms"]), 10_000)


class TestRetry(unittest.TestCase):
    def test_remember_is_one_shot_and_main_fails_open(self):
        # RETRY GAP: remember() — one POST, no backoff; main() catches it, prints FAILED and returns 1
        u = MagicMock(side_effect=OSError("memory server down"))
        rc, out, conn, pg, u = _main(_Cur(**FULL), urlopen=u)
        self.assertEqual(rc, 1)
        self.assertEqual(u.call_count, 1)
        self.assertIn("FAILED to store: memory server down", out)
        self.assertTrue(conn.closed)                                 # PG was released before the POST

    def test_pg_connect_is_one_shot_and_fails_closed(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt; the error escapes and nothing is posted
        with patch.object(hm.psycopg2, "connect", side_effect=OSError("pg down")) as pg, \
             patch.object(hm.urllib.request, "urlopen") as u:
            with self.assertRaises(OSError):
                hm.main()
        self.assertEqual(pg.call_count, 1); u.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_q1_executes_with_params_and_returns_one_row(self):
        cur = MagicMock(); cur.fetchone.return_value = (1, 2)
        self.assertEqual(hm.q1(cur, "SELECT %s", (5,)), (1, 2))
        cur.execute.assert_called_once_with("SELECT %s", (5,))

    def test_empty_telemetry_yields_only_the_header(self):
        text, meta = hm.build_summary(_Cur())
        self.assertTrue(text.startswith("Home environment summary for "))
        self.assertTrue(text.endswith(", 2026. ") or text.endswith(". "))
        self.assertEqual(meta, {})

    def test_warmest_room_and_only_first_low_humidity_room_are_narrated(self):
        cur = _Cur(rooms=[("Rack", 78.4, 84.1, 25, 120), ("Office", 72.0, 75.5, 20, 300)])
        text, meta = hm.build_summary(cur)
        self.assertIn("Warmest spot was Rack (avg 78.4F, peak 84.1F).", text)
        self.assertIn("Rack humidity ran low (~25%).", text)
        self.assertNotIn("Office humidity", text)
        self.assertEqual(meta["rooms_tracked"], 2)

    def test_null_humidity_is_skipped(self):
        text, _ = hm.build_summary(_Cur(rooms=[("Rack", 70.0, 71.0, None, 1)]))
        self.assertNotIn("humidity", text)

    def test_voc_and_battery_and_occupancy(self):
        text, meta = hm.build_summary(_Cur(**FULL))
        self.assertIn("Rack air VOC averaged 210 (peak 480) ug/m3.", text)
        self.assertEqual(meta["voc_avg"], 210.0)
        self.assertIn("Lowest sensor battery: Eve Door at 22% (needs attention).", text)
        self.assertEqual(meta["lowest_battery"], {"device": "Eve Door", "level": 22})
        self.assertIn("Occupancy was seen in: kitchen, office.", text)
        self.assertEqual(meta["occupied_rooms"], ["kitchen", "office"])

    def test_battery_at_threshold_is_healthy_and_null_rows_are_ignored(self):
        text, meta = hm.build_summary(_Cur(bat=("Eve Motion", 30)))
        self.assertIn("(all healthy)", text)
        text, meta = hm.build_summary(_Cur(aq=(None, None), bat=("x", None)))
        self.assertNotIn("VOC", text); self.assertNotIn("battery", text); self.assertEqual(meta, {})


class TestIntegration(unittest.TestCase):
    def test_summary_reads_the_four_telemetry_tables_and_excludes_zone_pseudo_rooms(self):
        cur = _Cur(**FULL)
        hm.build_summary(cur)
        tables = [re.search(r"FROM (telemetry\.\w+)", s).group(1) for s, _ in cur.sql]
        self.assertEqual(tables, ["telemetry.climate", "telemetry.air_quality", "telemetry.battery", "telemetry.presence"])
        self.assertIn("room NOT IN ('away','home','nearby','unknown')", cur.sql[-1][0])
        self.assertTrue(all("interval '24 hours'" in s for s, _ in cur.sql))

    def test_summary_feeds_remember_with_the_expected_payload(self):
        text, meta = hm.build_summary(_Cur(**FULL))
        u = MagicMock(); u.return_value.__enter__.return_value.read.return_value = b"stored"
        with patch.object(hm.urllib.request, "urlopen", u):
            self.assertEqual(hm.remember(text, meta), "stored")
        body = json.loads(u.call_args.args[0].data)
        self.assertEqual(body["metadata"]["lowest_battery"]["device"], "Eve Door")
        self.assertIn("Warmest spot was Rack", body["text"])
        self.assertEqual(u.call_args.args[0].get_method(), "POST")


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_the_summary(self):
        rc, out, conn, pg, u = _main(_Cur(**FULL))
        self.assertEqual(rc, 0)
        pg.assert_called_once_with(hm.DSN); self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        self.assertEqual(u.call_count, 1)
        self.assertIn("[home-memory] Home environment summary for", out)
        self.assertIn('[home-memory] stored to vector memory: {"ok": true, "id": 99}', out)

    def test_too_little_telemetry_skips_the_memory_write(self):
        rc, out, conn, pg, u = _main(_Cur())
        self.assertEqual(rc, 0)
        u.assert_not_called(); self.assertTrue(conn.closed)
        self.assertIn("not enough telemetry to summarize", out)

    def test_error_path_memory_server_rejects(self):
        u = MagicMock(side_effect=OSError("HTTP 503"))
        rc, out, conn, pg, u = _main(_Cur(**FULL), urlopen=u)
        self.assertEqual(rc, 1); self.assertIn("FAILED to store: HTTP 503", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse/--help (the script takes no flags), so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_home_memory_summary"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
