#!/usr/bin/env python3
"""Tests for nova_scanners_catch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure detectors on synthetic fixtures (no real people); DB through
fake cursors. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_scanners_catch.py"
spec = importlib.util.spec_from_file_location("scanners_catch_under_test", SCRIPT)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
SRC = SCRIPT.read_text()

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
_EMPTY_SCRIPTS = tempfile.TemporaryDirectory()


def setUpModule():
    # measure() scans every script for purges; point it at an empty dir so the fixtures stay fast
    sc.SCRIPTS_DIR = Path(_EMPTY_SCRIPTS.name)
# synthetic, fictional data only
P25 = [("scanner", T0 - timedelta(days=99), "[LAPD Northeast P25 voice] unit 4-3, 4512 Elm Street, RP is Jane Roe", None),
       ("scanner", T0 - timedelta(days=2), "[LAPD Northeast P25 voice] code 4, clear", None),
       ("scanner", T0 - timedelta(days=1), "[LAPD Northeast P25 voice] plate number 7ABC123", "private")]


def _flat(q):
    """psycopg2.sql.Composed -> plain text with quoted identifiers (no connection needed)."""
    if isinstance(q, str):
        return q
    out = []
    for part in getattr(q, "seq", [q]):
        if hasattr(part, "strings"):
            out.append(".".join(f'"{x}"' for x in part.strings))
        elif hasattr(part, "seq"):
            out.append(_flat(part))
        else:
            out.append(getattr(part, "string", str(part)))
    return "".join(out)


class _Cur:
    """Answers fetchone/fetchall/fetchmany by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""
        self.connection = mock.Mock()
        self._served = set()

    def execute(self, sql, params=None):
        sql = _flat(sql)
        self.sql.append(sql); self.params.append(params); self._last = sql
        self._served.discard(sql)

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])

    def fetchmany(self, n):
        if self._last in self._served:
            return []
        self._served.add(self._last)
        return self._route([])


def _mem(rows=P25, corroborate=(1, 0)):
    def stream(cur):
        srcs = cur.params[-1][0]
        return [r for r in rows if r[0] in srcs]
    return _Cur([("SELECT source, created_at, text, privacy FROM memories", stream),
                 ("phraseto_tsquery", corroborate)])


def _ops(private_ops=("SMITH JOHN Q",)):
    return _Cur([
        ("SELECT to_regclass", (True,)),
        ("FROM \"telemetry\".\"sds200_calls\"", lambda cur: (0, None) if "count" in cur._last else []),
        ("FROM \"telemetry\".\"overhead_flights\"", (100, T0 - timedelta(days=115))),
        ("FROM \"public\".\"aircraft_registry\"", (3, T0 - timedelta(days=100))),
        ("SELECT hex, operator FROM aircraft_registry", [("a1", private_ops[0]), ("a2", "SOUTHWEST AIRLINES CO"),
                                                          ("a3", None)]),
        ("FROM telemetry.overhead_flights WHERE hex = ANY", (12,)),
        ("FROM aircraft_registry WHERE registration", ("a1",)),
    ])


class Security(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[\w.]+")
        self.assertNotRegex(SRC, r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")
        self.assertIn("sql.Identifier", SRC)

    def test_never_deletes_or_retags(self):
        # the only DELETE text in the file is the regex that LOOKS for purges in other scripts
        for verb in (r"\bDROP\s", r"TRUNCATE", r"subprocess", r"os\.system", r"\.unlink\(", r"write_text\(",
                     r"urlopen", r"SET privacy"):
            self.assertNotRegex(SRC, verb)
        self.assertNotRegex(SRC, r"execute\([^)]*DELETE")
        writes = re.findall(r"(INSERT INTO|(?<!DO )UPDATE)\s+([\w.]+)", SRC)
        self.assertTrue(writes)
        self.assertTrue(all(t == "scanners_catch" for _, t in writes), writes)

    def test_personal_data_is_never_printed_or_stored(self):
        rows = sc.measure(_ops(), _mem(), now=T0, journal=Path(tempfile.mkdtemp()))
        dump = repr(rows)
        for s in ("Jane Roe", "4512 Elm", "7ABC123", "SMITH"):
            self.assertNotIn(s, dump)
        out = io.StringIO()
        with redirect_stdout(out):
            sc.print_rows(rows)
        self.assertNotIn("Jane Roe", out.getvalue())

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class Performance(unittest.TestCase):
    def test_detectors_scale_to_a_month_of_transcripts(self):
        texts = [f"[LAPD P25] unit {i}, {i % 900 + 100} Oak Street, code 4, Engine {i}" for i in range(50000)]
        t = time.monotonic()
        n = sum(1 for x in texts if sc.pii_kinds(x))
        self.assertEqual(n, 50000)
        self.assertLess(time.monotonic() - t, 5.0)

    def test_memories_are_streamed_not_loaded(self):
        self.assertIn("fetchmany(FETCH_BATCH)", SRC)
        self.assertIn("cursor(name=", SRC)


class Retry(unittest.TestCase):
    def test_uncheckable_hit_rolls_back_and_is_not_a_leak(self):
        mcur = _Cur()
        mcur.execute = mock.Mock(side_effect=RuntimeError("statement timeout"))
        hits = [{"post": "local/a.md", "kind": "address", "value": "4512 Elm Street"}]
        self.assertEqual(sc.corroborate(mcur, _Cur(), sc.SOURCES[0], hits, None), [])
        mcur.connection.rollback.assert_called()

    def test_failed_notify_returns_false(self):
        rows = sc.measure(_ops(), _mem(), now=T0, journal=Path(tempfile.mkdtemp()))
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=False))}):
            self.assertFalse(sc.alert("2026-10", rows))

    def test_rerun_same_month_upserts(self):
        self.assertIn("ON CONFLICT (month, source) DO UPDATE", SRC)
        self.assertIn("PRIMARY KEY (month, source)", SRC)


class Unit(unittest.TestCase):
    def test_address_detector(self):
        self.assertEqual(sc.pii_kinds("units to 3-1-2-0 west temple street"), {"address"})
        self.assertEqual(sc.pii_kinds("4512 N. Elm Ave"), {"address"})
        self.assertEqual(sc.pii_kinds("1200 block of Main Street"), set())       # press convention
        self.assertEqual(sc.pii_kinds("Engine 70 at Fairmont Avenue"), set())
        self.assertEqual(sc.pii_kinds("Engine 32, cancel. Engine 105, available."), set())

    def test_plate_name_phone_dob(self):
        self.assertIn("plate", sc.pii_kinds("license plate 7ABC123"))
        self.assertIn("name", sc.pii_kinds("RP is Jane Roe"))
        self.assertIn("name", sc.pii_kinds("talk to Mrs. Roe"))
        self.assertNotIn("name", sc.pii_kinds("suspect named Male Hispanic"))
        self.assertNotIn("name", sc.pii_kinds("it was named Every time"))
        self.assertIn("phone", sc.pii_kinds("callback 818-555-0142"))
        self.assertIn("dob", sc.pii_kinds("DOB 4/12/1980"))

    def test_private_owner_heuristic(self):
        self.assertTrue(sc.is_private_owner("SMITH JOHN Q"))
        for org in ("SOUTHWEST AIRLINES CO", "LOS ANGELES COUNTY SHERIFF", "N123 HOLDINGS LLC", "WELLS FARGO TRUST",
                    "", None, "X"):
            self.assertFalse(sc.is_private_owner(org), org)

    def test_published_scan_attributes_by_marker(self):
        posts = [("local/a.md", "From the LAPD talkgroup: a call at 4512 Elm Street."),
                 ("local/b.md", "Burbank news: a bakery opened at 4512 Elm Street."),
                 ("local/c.md", "ADS-B: N123AB flew overhead twice.")]
        hits = sc.published_scan(posts)
        self.assertEqual([(h["post"], h["kind"]) for h in hits["p25_police"]], [("local/a.md", "address")])
        self.assertEqual([h["value"] for h in hits["adsb"]], ["N123AB"])
        self.assertEqual(hits["rail"], [])

    def test_monthly_stats_and_private_sources(self):
        st = sc.monthly_stats([("2026-10", 1000, 50, 115.0, 3, 10, 500), ("2026-11", 0, 0, None, 0, 0, 0)])
        self.assertEqual(st[0], {"month": "2026-10", "records": 1000, "third_party": 50, "third_party_pct": 5.0,
                                 "oldest_days": 115.0, "published_hits": 3, "private_pct": 2.0})
        self.assertIsNone(st[1]["third_party_pct"])
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write('PRIVATE_SOURCES = [\n    "email", "imessage",\n]\n')
        self.assertEqual(sc.private_sources(Path(f.name)), {"email", "imessage"})
        self.assertEqual(sc.private_sources(Path("/nonexistent/x.py")), set())

    def test_purge_scan_needs_the_source_next_to_the_delete(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text('# mentions "fire" here\n' + "x = 1\n" * 200 + 'q = "DELETE FROM memories WHERE id=%s"\n')
            Path(d, "b.py").write_text('cur.execute("DELETE FROM memories WHERE source = \'rail\' AND created_at < %s")\n')
            Path(d, "c.py").write_text('RET = {"x": {"table": "overhead_flights", "retention_days": 90}}\n')
            got = sc.purge_scan(scripts_dir=Path(d))
        self.assertEqual(got["fire_dispatch"], [])
        self.assertEqual(got["rail"], ["b.py"])
        self.assertEqual(got["adsb"], ["c.py"])


class Integration(unittest.TestCase):
    def test_measure_builds_one_row_per_source(self):
        with tempfile.TemporaryDirectory() as d:
            rows = sc.measure(_ops(), _mem(), now=T0, journal=Path(d))
        by = {r["source"]: r for r in rows}
        self.assertEqual(set(by), {s["source"] for s in sc.SOURCES})
        p = by["p25_police"]
        self.assertEqual((p["records"], p["third_party_records"], p["privacy_marked_private"]), (3, 2, 1))
        self.assertEqual(p["recallable_third_party"], 1)
        self.assertEqual(p["oldest_days"], 99.0)
        a = by["adsb"]
        self.assertEqual(a["pii"]["private_owner_aircraft"], 1)
        self.assertEqual(a["pii"]["private_owner_sightings"], 12)
        self.assertEqual(a["third_party_records"], 13)
        self.assertEqual(by["pager"]["records"], 0)

    def test_measure_is_read_only(self):
        ops, mem = _ops(), _mem()
        with tempfile.TemporaryDirectory() as d:
            sc.measure(ops, mem, now=T0, journal=Path(d))
        for cur in (ops, mem):
            self.assertTrue(all(not re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER)", str(q)) for q in cur.sql))

    def test_memory_server_private_sources_are_read(self):
        if not sc.MEMORY_SERVER.exists():
            self.skipTest("memory_server.py not beside scripts/")
        self.assertIn("email", sc.private_sources())


class Functional(unittest.TestCase):
    def _rows(self, corroborate):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "content", "local", "x.md")
            p.parent.mkdir(parents=True)
            p.write_text("---\ntitle: x\n---\nOn the LAPD talkgroup tonight: a call at 4512 Elm Street.\n"
                         "ADS-B: N123AB flew overhead.\n")
            return {r["source"]: r for r in sc.measure(_ops(), _mem(corroborate=corroborate), now=T0, journal=Path(d))}

    def test_radio_only_address_in_a_post_is_a_leak(self):
        by = self._rows((1, 0))
        self.assertEqual(by["p25_police"]["published_hits"], 1)
        self.assertEqual(by["p25_police"]["published_posts"], ["local/x.md"])
        self.assertEqual(by["adsb"]["published_hits"], 1)                # private owner's tail number

    def test_address_also_in_the_news_is_public_not_a_leak(self):
        self.assertEqual(self._rows((1, 3))["p25_police"]["published_hits"], 0)

    def test_message_and_level(self):
        by = self._rows((1, 0))
        level, title, body = sc.render_message("2026-10", list(by.values()))
        self.assertEqual(level, "info")   # leaks are reported, never warned (Jordan 2026-10-15)
        self.assertIn("published leak(s) (target 0)", title)
        for s in ("p25_police", "purge NONE", "Cameras:", "proposal only"):
            self.assertIn(s, body)
        level, _t, _b = sc.render_message("2026-10", [dict(r, published_hits=0) for r in by.values()])
        self.assertEqual(level, "info")

    def test_every_source_has_a_proposed_retention(self):
        for s in sc.SOURCES:
            self.assertTrue(s["proposed"], s["source"])


class Frame(unittest.TestCase):
    def test_help_and_no_mode(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            sc.main(["--help"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            sc.main([])

    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sc.main(["--selftest"]), 0)
        self.assertIn("selftest ok", out.getvalue())

    def test_dry_run_writes_nothing_and_notifies_nothing(self):
        ops, mem = _ops(), _mem()
        conns = {"nova_ops": mock.Mock(cursor=mock.Mock(return_value=ops)), "nova_memories": mock.Mock()}
        notify = mock.Mock()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(sc, "connect", side_effect=lambda db="nova_ops", **k: conns[db]), \
             mock.patch.object(sc, "_MemCursor", return_value=mem), \
             mock.patch.object(sc, "journal_root", return_value=Path(d)), \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(sc.main(["--dry-run"]), 0)
        self.assertIn("nothing written", out.getvalue())
        notify.assert_not_called()
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE|ALTER)", str(q)) for q in ops.sql))

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("Better Living Through Circuitry", SRC)
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("nova_journal.HUGO_ROOT", SRC)
        for col in ("month text", "source text", "records bigint", "third_party_records bigint", "oldest_days real",
                    "in_memories bigint", "privacy_marked_private bigint", "published_hits int", "proposed_retention text"):
            self.assertIn(col, SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
