#!/usr/bin/env python3
"""Tests for nova_media_registry.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_media_registry.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_media_reg_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nmediareg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mr = _load()


class FakeDB:
    """In-memory media_files table that understands the registry's handful of statements."""
    def __init__(self):
        self.rows = {}; self.sql = []; self.commits = 0; self.rollbacks = 0; self.closed = 0

    def connect(self, dsn):
        db = self

        class Cur:
            def __init__(s): s._res = []
            def execute(s, sql, params=None):
                db.sql.append((sql, params)); s._res = []
                q = " ".join(sql.split())
                if q.startswith("INSERT INTO media_files"):
                    db.rows.setdefault(params["path"], {**params, "status": "pending"})
                elif q.startswith("SELECT * FROM media_files WHERE path"):
                    r = db.rows.get(params[0]); s._res = [r] if r else []
                elif q.startswith("SELECT status FROM"):
                    r = db.rows.get(params[0]); s._res = [(r["status"],)] if r else []
                elif q.startswith("UPDATE media_files"):
                    r = db.rows.get(params["path"])
                    if r:
                        r["status"] = params.get("status", "ingested")
                        for k in ("error_msg", "notes", "chunks", "source_label"):
                            if k in params and ("%(" + k + ")s") in q:
                                r[k] = params[k]
                elif q.startswith("SELECT path FROM"):
                    s._res = [(p,) for p, r in db.rows.items() if r["status"] == "pending"
                              and all(v in (r.get("source_label"), r.get("show_name")) for v in params)]
                elif "GROUP BY status ORDER" in q:
                    c = {}
                    for r in db.rows.values():
                        c[r["status"]] = c.get(r["status"], 0) + 1
                    s._res = sorted(c.items())
                elif "GROUP BY source_label, status" in q:
                    c = {}
                    for r in db.rows.values():
                        k = (r.get("source_label") or "unknown", r["status"]); c[k] = c.get(k, 0) + 1
                    s._res = [(a, b, n) for (a, b), n in sorted(c.items())]
            def fetchone(s): return s._res[0] if s._res else None
            def fetchall(s): return s._res

        class Con:
            def cursor(s, **kw): return Cur()
            def commit(s): db.commits += 1
            def rollback(s): db.rollbacks += 1
            def close(s): db.closed += 1
        return Con()


def _db():
    db = FakeDB()
    p = patch.object(mr.psycopg2, "connect", side_effect=db.connect)
    p.start()
    return db, p


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_dynamic_sql_only_joins_fixed_fragments(self):
        db, p = _db(); self.addCleanup(p.stop)
        evil = "x'; DROP TABLE media_files;--"
        mr.register_file(evil)
        mr.mark_status(evil, "error", error_msg=evil, notes=evil)
        mr.pending_files(source_label=evil, show_name=evil)
        for sql, _ in db.sql:
            self.assertNotIn("DROP", sql)
        self.assertEqual(db.rows[evil]["error_msg"], evil)


class TestPerformance(unittest.TestCase):
    def test_10k_registrations_and_report(self):
        db, p = _db(); self.addCleanup(p.stop)
        t0 = time.perf_counter()
        for i in range(10_000):
            mr.register_file(f"/media/{i}.mp4", source_label=f"s{i % 5}")
        rep = mr.coverage_report()
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(rep["total"], 10_000)
        self.assertEqual(len(rep["by_source"]), 5)


class TestRetry(unittest.TestCase):
    def test_connect_failure_propagates_once(self):
        # RETRY GAP: _conn — one connect per call, no retry; callers (ingest scripts) see the error
        with patch.object(mr.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c:
            with self.assertRaises(psycopg2.OperationalError):
                mr.get_status("/x")
        self.assertEqual(c.call_count, 1)

    def test_error_rolls_back_and_closes(self):
        con = MagicMock(); con.cursor.return_value.execute.side_effect = RuntimeError("boom")
        with patch.object(mr.psycopg2, "connect", return_value=con):
            with self.assertRaises(RuntimeError):
                mr.mark_status("/x", "error")
        con.rollback.assert_called_once(); con.close.assert_called_once(); con.commit.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_huge_inode_is_nulled(self):
        db, p = _db(); self.addCleanup(p.stop)
        f = TMP / "a.mp4"; f.write_bytes(b"1234")
        st = MagicMock(st_size=4, st_ino=2 ** 64 - 1)
        with patch.object(mr.Path, "stat", return_value=st):
            mr.register_file(str(f))
        self.assertIsNone(db.rows[str(f)]["inode"])
        self.assertEqual(db.rows[str(f)]["file_size"], 4)

    def test_missing_file_registers_with_nulls(self):
        db, p = _db(); self.addCleanup(p.stop)
        row = mr.register_file(str(TMP / "nope.mp4"))
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["file_size"])

    def test_is_done_statuses(self):
        db, p = _db(); self.addCleanup(p.stop)
        self.assertFalse(mr.is_done("/unknown"))
        for st, done in (("ingested", True), ("trash", True), ("error", False), ("downloaded", False)):
            mr.register_file(f"/{st}"); mr.mark_status(f"/{st}", st)
            self.assertEqual(mr.is_done(f"/{st}"), done, st)


class TestIntegration(unittest.TestCase):
    def test_register_never_overwrites_processed_row(self):
        db, p = _db(); self.addCleanup(p.stop)
        mr.register_file("/v.mp4", show_name="A")
        mr.mark_ingested("/v.mp4", 12, source_label="docs")
        row = mr.register_file("/v.mp4", show_name="B")
        self.assertEqual((row["status"], row["show_name"], row["chunks"]), ("ingested", "A", 12))
        self.assertIn("dbname=nova_media", mr.DSN)


class TestFunctional(unittest.TestCase):
    def test_pipeline_pending_to_report(self):
        db, p = _db(); self.addCleanup(p.stop)
        for i in range(4):
            mr.register_file(f"/m{i}", source_label="mil", show_name="WW2")
        mr.mark_ingested("/m0", 3)
        mr.mark_status("/m1", "trash", notes="dup")
        self.assertEqual(mr.pending_files(source_label="mil", show_name="WW2"), ["/m2", "/m3"])
        rep = mr.coverage_report()
        self.assertEqual(rep["by_status"], {"ingested": 1, "pending": 2, "trash": 1})
        self.assertEqual(rep["by_source"]["mil"]["pending"], 2)
        self.assertEqual(db.commits, db.closed)

    def test_mark_ingested_without_source_omits_clause(self):
        db, p = _db(); self.addCleanup(p.stop)
        mr.mark_ingested("/z", 1)
        self.assertNotIn("source_label", db.sql[-1][0])


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_media_registry as m; print(sorted(m._DONE_STATUSES)[0])"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "audio_failed")

    def test_library_never_connects_on_import(self):
        self.assertNotIn("__main__", SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
