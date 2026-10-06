#!/usr/bin/env python3
"""Tests for nova_chp_traffic.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chp = _load("nova_chp_traffic_t", SCRIPTS / "nova_chp_traffic.py")
SRC = (SCRIPTS / "nova_chp_traffic.py").read_text()


def _log(i, area="Central LA", loc="Sr110 N / E Glenarm St", latlon="34127470:118147168", detail=None):
    det = (f'<LogDetails><details><IncidentDetail>"old"</IncidentDetail></details>'
           f'<details><IncidentDetail>"{detail}"</IncidentDetail></details></LogDetails>') if detail else ""
    return (f'<Log ID="{i}"><LogTime>"Jun 24 2026 3:02PM"</LogTime><LogType>"1182-Trfc Collision"</LogType>'
            f'<Location>"{loc}"</Location><LocationDesc>""</LocationDesc><Area>"{area}"</Area>'
            f'<LATLON>"{latlon}"</LATLON>{det}</Log>')


def _feed(*logs, center="LAHB"):
    return (f'<State><Center ID="{center}"><Dispatch ID="LACC">' + "".join(logs) +
            '</Dispatch></Center></State>').encode()


def _conn(was_insert=(True,)):
    cur = MagicMock()
    cur.fetchone.side_effect = [(w,) for w in was_insert]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_store_is_parameterized(self):
        conn, cur = _conn()
        chp.store(conn, [{"incident_id": "x'; --"}])
        sql, params = cur.execute.call_args.args
        self.assertIn("%(incident_id)s", sql)
        self.assertNotIn("x'; --", sql)
        self.assertEqual(params["incident_id"], "x'; --")

    def test_raw_capped(self):
        big = _log("A", detail="d" * 20000)
        self.assertLessEqual(len(chp.parse_incidents(_feed(big))[0]["raw"]), 8000)


class TestPerformance(unittest.TestCase):
    def test_parse_2k_logs_fast(self):
        xml = _feed(*[_log(i, area="Fresno" if i % 2 else "West LA") for i in range(2000)])
        t0 = time.perf_counter()
        out = chp.parse_incidents(xml)
        for i in range(10_000):
            chp._parse_latlon(f"{34000000 + i}:118000000")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(out), 1000)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_then_succeeds(self):
        fetch = MagicMock(side_effect=[OSError("reset"), OSError("reset"), _feed()])
        with patch.object(chp, "fetch_xml", fetch), patch.object(chp.time, "sleep") as sl, \
                patch.object(chp.psycopg2, "connect") as pg, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(chp.main(["--quiet"]), 0)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sl.call_count, 2)
        pg.assert_not_called()
        self.assertIn("no LA-area incidents", out.getvalue())

    def test_feed_outage_soft_skips(self):
        fetch = MagicMock(side_effect=OSError("down"))
        with patch.object(chp, "fetch_xml", fetch), patch.object(chp.time, "sleep"), \
                patch.object(chp.psycopg2, "connect") as pg, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(chp.main([]), 0)
        self.assertEqual(fetch.call_count, 3)
        pg.assert_not_called()
        self.assertIn("giving up after 3 attempts", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_parse_latlon(self):
        self.assertEqual(chp._parse_latlon('"34127470:118147168"'), (34.12747, -118.147168))
        for bad in (None, "", "nocolon", "a:b", "0:0"):
            self.assertEqual(chp._parse_latlon(bad), (None, None))

    def test_filters_center_area_and_keywords(self):
        xml = _feed(_log("A"), _log("B", area="Fresno", loc="I5 / SR152"),
                    _log("C", area="Fresno", loc="Sr134 E / Glendale Ave"), _log("", area="West LA"))
        self.assertEqual([i["incident_id"] for i in chp.parse_incidents(xml)], ["A", "C"])
        self.assertEqual(chp.parse_incidents(_feed(_log("Z"), center="FRCC")), [])

    def test_latest_detail_and_clean(self):
        inc = chp.parse_incidents(_feed(_log("A", detail="lanes blocked")))[0]
        self.assertEqual(inc["detail"], "lanes blocked")
        self.assertEqual(inc["log_time"], "Jun 24 2026 3:02PM")
        self.assertIsNone(inc["location_desc"])

    def test_truncated_feed_is_salvaged(self):
        full = _feed(_log("A"), _log("B")).decode()
        cut = full[: full.rfind("</Log>") - 5].encode()  # mid-token in the second Log
        with redirect_stdout(io.StringIO()):
            self.assertEqual([i["incident_id"] for i in chp.parse_incidents(cut)], ["A"])
        self.assertIsNone(chp._repair_truncated(b"<State><Center"))


class TestIntegration(unittest.TestCase):
    def test_fetch_sends_browser_ua_and_wildcard_accept(self):
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b"<State/>"
        with patch.object(chp.urllib.request, "urlopen", return_value=resp) as uo:
            self.assertEqual(chp.fetch_xml(), b"<State/>")
        req = uo.call_args.args[0]
        self.assertEqual(req.full_url, chp.FEED_URL)
        self.assertIn("Mozilla", req.get_header("User-agent"))
        self.assertEqual(req.get_header("Accept"), "*/*")

    def test_store_counts_insert_vs_update(self):
        conn, _ = _conn((True, False, True))
        self.assertEqual(chp.store(conn, [{}, {}, {}]), (2, 1))


class TestFunctional(unittest.TestCase):
    def test_golden_path_upserts_and_logs(self):
        conn, cur = _conn((True,))
        with patch.object(chp, "fetch_xml", return_value=_feed(_log("A"))), \
                patch.object(chp.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(chp.main([]), 0)
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.chp_incidents", cur.execute.call_args_list[0].args[0])
        self.assertIn("new=1 updated=0", out.getvalue())
        self.assertIn("(34.1275,-118.1472)", out.getvalue())
        conn.close.assert_called_once()

    def test_db_error_propagates_but_closes(self):
        conn, cur = _conn()
        cur.execute.side_effect = RuntimeError("ddl fail")
        with patch.object(chp, "fetch_xml", return_value=_feed(_log("A"))), \
                patch.object(chp.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                chp.main([])
        conn.close.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() fetches the live feed for any argv, so the smoke is an import in a child process
        r = subprocess.run([sys.executable, "-c", "import nova_chp_traffic as m; print(m.LA_CENTER)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "LAHB")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
