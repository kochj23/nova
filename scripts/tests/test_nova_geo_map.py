#!/usr/bin/env python3
"""Tests for nova_geo_map.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
PG is mocked; the HTML is written to a tempdir."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_geo_map.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="geomap-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("geo_map_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gm = _load()
gm.OUT = TMP / "radar.html"                      # never the default scratch path
gm.psycopg2 = MagicMock()
gm.psycopg2.connect.side_effect = OSError("offline: pg stubbed")


def _row(src="scanner", text="[2026 Burbank] Units respond to a call", mi=1.234, d="NE"):
    return {"source": src, "text": text, "created_at": datetime(2026, 1, 2, 3, 4), "mi": mi, "dir": d}


def _conn(rows):
    cur = MagicMock(); cur.fetchall.return_value = rows
    con = MagicMock(); con.cursor.return_value = cur
    return con, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", gm.MEM_DSN)

    def test_transcript_cannot_break_out_of_script(self):
        # regression for the fix: "</script>" in a scanner transcript used to end the inline script
        con, _ = _conn([_row(text="</script><img src=x onerror=alert(1)>")])
        with patch.object(gm.psycopg2, "connect", return_value=con, side_effect=None):
            gm.main()
        html = gm.OUT.read_text()
        self.assertEqual(html.count("</script>"), 1)              # only the page's own closing tag
        self.assertIn("<\\/script>", html)

    def test_sql_has_no_runtime_values(self):
        # the only interpolation is the HOURS constant; no caller-supplied value reaches the SQL
        self.assertIsInstance(gm.HOURS, int)
        self.assertEqual(SRC.count("% HOURS"), 1)


class TestPerformance(unittest.TestCase):
    def test_fetch_10k_rows(self):
        con, _ = _conn([_row(mi=i / 1000) for i in range(10_000)])
        t0 = time.perf_counter()
        with patch.object(gm.psycopg2, "connect", return_value=con, side_effect=None):
            rows = gm.fetch()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(rows), 10_000)


class TestRetry(unittest.TestCase):
    def test_pg_failure_raises_and_writes_nothing(self):
        # RETRY GAP: fetch()/psycopg2.connect — single attempt, no retry; main() fails loudly before writing
        out = TMP / "never.html"
        with patch.object(gm, "OUT", out):
            with self.assertRaises(OSError):
                gm.main()
        self.assertFalse(out.exists())
        self.assertGreaterEqual(gm.psycopg2.connect.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_fetch_shapes_rows(self):
        con, _ = _conn([_row(src="fire", text="[hdr] Brush fire " + "x" * 300, mi=3.06, d="SW"),
                        _row(src="scanner", text=None)])
        with patch.object(gm.psycopg2, "connect", return_value=con, side_effect=None):
            rows = gm.fetch()
        self.assertEqual(rows[0]["svc"], "fire")
        self.assertEqual(rows[0]["mi"], 3.1)
        self.assertTrue(rows[0]["txt"].startswith("Brush fire"))
        self.assertEqual(len(rows[0]["txt"]), 140)
        self.assertEqual((rows[1]["svc"], rows[1]["txt"]), ("police", ""))
        con.close.assert_called_once()

    def test_page_template_has_placeholder_and_title(self):
        self.assertEqual(gm.PAGE.count("__DATA__"), 1)
        self.assertTrue(gm.PAGE.startswith("<title>"))
        self.assertEqual(gm.MAX_MI, 12)


class TestIntegration(unittest.TestCase):
    def test_reads_scanner_and_fire_memories_from_nova_memories(self):
        con, cur = _conn([])
        with patch.object(gm.psycopg2, "connect", return_value=con, side_effect=None) as c:
            gm.fetch()
        self.assertIn("dbname=nova_memories", c.call_args[0][0])
        sql = cur.execute.call_args[0][0]
        self.assertIn("source IN ('scanner','fire')", sql)
        self.assertIn("interval '24 hours'", sql)


class TestFunctional(unittest.TestCase):
    def test_main_writes_page_with_data(self):
        con, _ = _conn([_row(), _row(src="fire", mi=0.5, d="S")])
        with patch.object(gm.psycopg2, "connect", return_value=con, side_effect=None):
            gm.main()
        html = gm.OUT.read_text()
        self.assertNotIn("__DATA__", html)
        data = json.loads(re.search(r"const DATA = (\[.*?\]);", html).group(1))
        self.assertEqual([d["svc"] for d in data], ["police", "fire"])

    def test_main_empty_still_writes(self):
        con, _ = _conn([])
        with patch.object(gm.psycopg2, "connect", return_value=con, side_effect=None):
            gm.main()
        self.assertIn("const DATA = [];", gm.OUT.read_text())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script reads PG, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import importlib.util as u, sys; sp=u.spec_from_file_location('g', sys.argv[1]);"
                "m=u.module_from_spec(sp); sp.loader.exec_module(m); print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
