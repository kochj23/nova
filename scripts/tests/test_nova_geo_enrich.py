#!/usr/bin/env python3
"""Tests for nova_geo_enrich.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_geo_enrich.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ge = _load("ge", SCRIPT)          # import is clean: psycopg2 + nova_geo_distance (no connect at load)


class _Cur:
    """Records every (sql, params); `rows` answers the SELECT."""
    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.closed = False; self.autocommit = False

    def cursor(self, cursor_factory=None):
        return self._cur

    def close(self):
        self.closed = True


def _run(rows, hits, argv=(), ensure=None):
    """Drive main() fully offline. `hits` maps memory id -> locate() result list."""
    mem_cur, ops_cur = _Cur(rows), _Cur()
    mem, ops = _Conn(mem_cur), _Conn(ops_cur)
    by_text = {r["text"]: hits.get(r["id"], []) for r in rows}
    locate = MagicMock(side_effect=lambda text, cur: by_text.get(text, []))
    with patch.object(ge.psycopg2, "connect", side_effect=[mem, ops]) as pg, \
         patch.object(ge.geo, "locate", locate), patch.object(ge.geo, "ensure_cache", ensure or MagicMock()) as ens, \
         patch.object(sys, "argv", ["nova_geo_enrich.py", *argv]), redirect_stdout(io.StringIO()) as out:
        ge.main()
    return mem_cur, ops_cur, out.getvalue(), pg, locate, ens, (mem, ops)


HIT = ("962 Hyperion Avenue", 4.2, "SE", ("home", 4.2, "SE"))
ANCHORED = ("1 Main St", 9.0, "N", ("school", 1.5, "W"))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ge.MEM_DSN); self.assertNotIn("password", ge.OPS_DSN)

    def test_only_write_is_the_parameterized_memories_update(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"memories"})
        inj = "x'; DR" "OP TABLE memories; --"
        mem_cur, *_ = _run([{"id": 1, "text": inj}], {})
        sql, params = mem_cur.ran("UPDATE memories")[0]
        self.assertNotIn("DR" "OP", sql)
        self.assertEqual(params[0], inj); self.assertEqual(params[2], 1)

    def test_the_only_interpolated_sql_values_are_internal_integer_constants(self):
        # the SELECT window/limit are %-formatted, but from fixed ints chosen by the --backfill flag — never argv text
        mem_cur, *_ = _run([], {})
        self.assertIn("interval '3 hours'", mem_cur.sql[0][0]); self.assertIn("LIMIT 600", mem_cur.sql[0][0])
        mem_cur, *_ = _run([], {}, argv=["--backfill", "'; DR" "OP TABLE memories; --"])
        self.assertIn("interval '720 hours'", mem_cur.sql[0][0]); self.assertIn("LIMIT 5000", mem_cur.sql[0][0])
        self.assertNotIn("DR" "OP", mem_cur.sql[0][0])

    def test_enrichment_is_scoped_to_scanner_and_fire_sources(self):
        mem_cur, *_ = _run([], {})
        self.assertIn("source IN ('scanner','fire')", mem_cur.sql[0][0])
        self.assertIn("NOT (coalesce(metadata,'{}'::jsonb) ? 'geo_enriched')", mem_cur.sql[0][0])


class TestPerformance(unittest.TestCase):
    def test_10k_rows_enrich_fast(self):
        rows = [{"id": i, "text": f"unit {i} responding 962 Hyperion Avenue code 3"} for i in range(10_000)]
        hits = {i: [HIT] for i in range(10_000)}
        t0 = time.perf_counter()
        mem_cur, _, out, *_ = _run(rows, hits)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(mem_cur.ran("UPDATE memories")), 10_000)
        self.assertIn("processed 10000 memories, 10000 had geocodable addresses", out)


class TestRetry(unittest.TestCase):
    def test_pg_connect_is_one_shot_and_fails_closed(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt, no backoff; the exception escapes before any UPDATE
        with patch.object(ge.psycopg2, "connect", side_effect=OSError("pg down")) as pg, \
             patch.object(ge.geo, "locate") as loc, patch.object(sys, "argv", ["nova_geo_enrich.py"]):
            with self.assertRaises(OSError):
                ge.main()
        self.assertEqual(pg.call_count, 1); loc.assert_not_called()

    def test_locate_failure_is_not_retried_and_stops_the_pass(self):
        # RETRY GAP: main()/geo.locate — no per-row try/except; rows already processed stay written (autocommit),
        # the failing row and the rest are left for the next scheduled pass (they remain un-enriched).
        rows = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}, {"id": 3, "text": "c"}]
        mem_cur, ops_cur = _Cur(rows), _Cur()
        locate = MagicMock(side_effect=[[], RuntimeError("nominatim 429"), []])
        with patch.object(ge.psycopg2, "connect", side_effect=[_Conn(mem_cur), _Conn(ops_cur)]), \
             patch.object(ge.geo, "locate", locate), patch.object(ge.geo, "ensure_cache", MagicMock()), \
             patch.object(sys, "argv", ["nova_geo_enrich.py"]):
            with self.assertRaises(RuntimeError):
                ge.main()
        self.assertEqual(locate.call_count, 2)
        self.assertEqual([p[2] for _, p in mem_cur.ran("UPDATE memories")], [1])


class TestUnit(unittest.TestCase):
    def test_no_rows_still_reports_and_closes(self):
        mem_cur, ops_cur, out, pg, locate, ens, (mem, ops) = _run([], {})
        self.assertIn("processed 0 memories, 0 had geocodable addresses (window 3h)", out)
        self.assertTrue(mem.closed and ops.closed); locate.assert_not_called()

    def test_plain_tag_and_meta_when_home_is_the_nearest_anchor(self):
        mem_cur, *_ = _run([{"id": 7, "text": "fire at 962 Hyperion Avenue now"}], {7: [HIT]})
        text, meta_json, mid = mem_cur.ran("UPDATE memories")[0][1]
        self.assertEqual(text, "fire at 962 Hyperion Avenue (~4.2 mi SE) now")
        meta = json.loads(meta_json)
        self.assertEqual(meta["nearest_mi"], 4.2); self.assertEqual(meta["nearest_dir"], "SE")
        self.assertEqual(meta["anchor"], {"name": "home", "mi": 4.2, "dir": "SE"})
        self.assertEqual(meta["locations"], [{"addr": "962 Hyperion Avenue", "mi": 4.2, "dir": "SE"}])

    def test_closer_non_home_anchor_is_called_out_inline(self):
        mem_cur, *_ = _run([{"id": 1, "text": "crash 1 Main St"}], {1: [ANCHORED]})
        text, meta_json, _ = mem_cur.ran("UPDATE memories")[0][1]
        self.assertEqual(text, "crash 1 Main St (~9.0 mi N; ~1.5 mi W of school)")
        self.assertEqual(json.loads(meta_json)["anchor"], {"name": "school", "mi": 1.5, "dir": "W"})

    def test_already_tagged_display_is_not_tagged_twice_and_nearest_picks_min(self):
        far = ("5 Far Rd (~20.0 mi N)", 20.0, "N", None)
        mem_cur, *_ = _run([{"id": 1, "text": "x 5 Far Rd (~20.0 mi N) then 1 Main St"}], {1: [far, ANCHORED]})
        text, meta_json, _ = mem_cur.ran("UPDATE memories")[0][1]
        self.assertEqual(text.count("(~20.0 mi N)"), 1)
        meta = json.loads(meta_json)
        self.assertEqual((meta["nearest_mi"], meta["nearest_dir"]), (9.0, "N"))
        self.assertEqual(meta["anchor"]["name"], "school")

    def test_unlocated_row_is_still_marked_enriched_with_null_geo(self):
        mem_cur, _, out, *_ = _run([{"id": 3, "text": "nothing here"}], {})
        text, meta_json, mid = mem_cur.ran("UPDATE memories")[0][1]
        self.assertEqual((text, mid), ("nothing here", 3))
        self.assertEqual(json.loads(meta_json), {"nearest_mi": None, "nearest_dir": None, "anchor": None, "locations": []})
        self.assertIn("'geo_enriched', true", mem_cur.ran("UPDATE memories")[0][0])
        self.assertIn("processed 1 memories, 0 had geocodable addresses", out)


class TestIntegration(unittest.TestCase):
    def test_geocoding_is_delegated_to_nova_geo_distance_not_reimplemented(self):
        self.assertIs(ge.geo, sys.modules["nova_geo_distance"])
        self.assertNotIn("NOMINATIM", SRC); self.assertNotIn("haversine", SRC)
        for fn in ("locate", "ensure_cache"):
            self.assertTrue(callable(getattr(ge.geo, fn)))

    def test_cache_lives_in_nova_ops_and_memories_in_nova_memories(self):
        self.assertIn("dbname=nova_memories", ge.MEM_DSN); self.assertIn("dbname=nova_ops", ge.OPS_DSN)
        mem_cur, ops_cur, out, pg, locate, ens, (mem, ops) = _run([{"id": 1, "text": "t"}], {})
        self.assertEqual([c.args[0] for c in pg.call_args_list], [ge.MEM_DSN, ge.OPS_DSN])
        self.assertTrue(mem.autocommit and ops.autocommit)
        ens.assert_called_once_with(ops_cur)                   # geo_cache table ensured on the OPS cursor
        self.assertIs(locate.call_args.args[1], ops_cur)       # ...and locate() is handed that same cursor
        self.assertEqual(mem_cur.ran("UPDATE memories")[0][1][2], 1)

    def test_backfill_flag_widens_window_and_limit(self):
        mem_cur, _, out, *_ = _run([], {}, argv=["--backfill"])
        self.assertIn("BACKFILL processed 0 memories", out); self.assertIn("(window 720h)", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_two_rows(self):
        rows = [{"id": 10, "text": "struct fire 962 Hyperion Avenue"}, {"id": 11, "text": "medical, no address"}]
        mem_cur, ops_cur, out, pg, locate, ens, (mem, ops) = _run(rows, {10: [HIT]})
        ups = mem_cur.ran("UPDATE memories")
        self.assertEqual([p[2] for _, p in ups], [10, 11])
        self.assertIn("(~4.2 mi SE)", ups[0][1][0]); self.assertEqual(ups[1][1][0], "medical, no address")
        self.assertEqual(locate.call_count, 2)
        self.assertIn("[geo-enrich] processed 2 memories, 1 had geocodable addresses (window 3h)", out)
        self.assertTrue(mem.closed and ops.closed)

    def test_error_path_connect_failure_writes_nothing(self):
        with patch.object(ge.psycopg2, "connect", side_effect=OSError("down")), \
             patch.object(ge.geo, "ensure_cache") as ens, patch.object(sys, "argv", ["nova_geo_enrich.py"]), \
             redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(OSError):
                ge.main()
        ens.assert_not_called(); self.assertEqual(out.getvalue(), "")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse/--help in this script (any argv just toggles --backfill), so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_geo_enrich"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
