#!/usr/bin/env python3
"""Tests for nova_frame_backfill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_frame_backfill.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# nova_frame_index is import-clean (no module-level side effects); its network/ffmpeg paths
# (index_video) are patched on the backfill module in every test that reaches them.
fb = _load("frame_backfill_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="frame-backfill-test-"))
fb.ROOT = str(TMP / "TVShows")                       # never walk the real 7.9 TB library


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


def _pg_stub(rows=(), exc=None):
    """A psycopg2 stand-in whose connect() returns a connection over `rows` (or raises)."""
    calls, cur = [], _Cur(list(rows))

    def connect(dsn, **kw):
        calls.append(dsn)
        if exc:
            raise exc
        return _Conn(cur)
    return types.SimpleNamespace(connect=connect, calls=calls, cur=cur)


def _mk_library(shows):
    """Create <ROOT>/<show>/<file> for every (show, filename) and return the full paths."""
    out = []
    for show, name in shows:
        d = TMP / "TVShows" / show
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(b"\x00")
        out.append(str(p))
    return out


def _run_main(argv, pg=None, index=None):
    pg = pg or _pg_stub()
    index = index if index is not None else MagicMock(return_value=3)
    with patch.object(fb, "psycopg2", pg), patch.object(fb, "index_video", index), \
         patch.object(sys, "argv", ["nova_frame_backfill.py", *argv]), redirect_stdout(io.StringIO()) as out:
        fb.main()
    return out.getvalue(), pg, index


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", SRC.lower())

    def test_sql_is_constant_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP)\b", SRC))
        pg = _pg_stub(rows=[("a.mp4",), (None,)])
        with patch.object(fb, "psycopg2", pg):
            fb.already_indexed()
        self.assertEqual(len(pg.calls), 1)
        sql, params = pg.cur.sql[0]
        self.assertTrue(sql.startswith("SELECT DISTINCT"))
        self.assertIsNone(params)                          # a constant query, nothing interpolated

    def test_only_video_extensions_are_walked(self):
        _mk_library([("Zq0Ext", "ep.mp4"), ("Zq0Ext", "notes.txt"), ("Zq0Ext", "ep.MKV")])
        found = {os.path.basename(p) for p in fb.find_videos("zq0ext", 0)}
        self.assertEqual(found, {"ep.mp4", "ep.MKV"})


class TestPerformance(unittest.TestCase):
    def test_filtering_10k_candidates_against_done_set_is_fast(self):
        done = {f"v{i}.mp4" for i in range(0, 10_000, 2)}
        paths = [f"/x/TVShows/S/v{i}.mp4" for i in range(10_000)]
        t0 = time.perf_counter()
        todo = [fp for fp in paths if os.path.basename(fp) not in done]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(todo), 5_000)

    def test_find_videos_over_a_1k_file_tree_is_bounded(self):
        _mk_library([("Zq8Perf", f"e{i}.mp4") for i in range(1_000)])
        t0 = time.perf_counter()
        n = sum(1 for _ in fb.find_videos("zq8perf", 0))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(n, 1_000)


class TestRetry(unittest.TestCase):
    def test_pg_failure_is_one_shot_and_aborts_before_any_indexing(self):
        # RETRY GAP: already_indexed()/psycopg2.connect — one attempt, no backoff; the error escapes main()
        # BEFORE any index_video call, so a PG blip can never cause double-indexing (the safe side).
        import psycopg2
        pg = _pg_stub(exc=psycopg2.OperationalError("timed out"))
        index = MagicMock()
        with patch.object(fb, "psycopg2", pg), patch.object(fb, "index_video", index), \
             patch.object(sys, "argv", ["nova_frame_backfill.py", "--limit", "1"]):
            with self.assertRaises(psycopg2.OperationalError):
                fb.main()
        self.assertEqual(len(pg.calls), 1)
        index.assert_not_called()

    def test_index_failure_is_logged_and_the_run_continues(self):
        # RETRY GAP: index_video — one attempt per video; a failure is printed and the next video proceeds
        _mk_library([("Zq1Retry", "one.mp4"), ("Zq1Retry", "two.mp4")])
        index = MagicMock(side_effect=[RuntimeError("ollama down"), 7])
        out, _, index = _run_main(["--show", "zq1retry"], index=index)
        self.assertEqual(index.call_count, 2)
        self.assertIn("FAILED", out)
        self.assertIn("7 frames", out)
        self.assertIn("backfill complete.", out)

    def test_unreadable_file_is_skipped_not_fatal(self):
        _mk_library([("Zq2Unread", "gone.mp4")])
        with patch.object(fb.os.path, "getmtime", side_effect=OSError("stat failed")):
            self.assertEqual(list(fb.find_videos("zq2unread", 30)), [])


class TestUnit(unittest.TestCase):
    def test_find_videos_empty_root(self):
        with patch.object(fb, "ROOT", str(TMP / "does-not-exist")):
            self.assertEqual(list(fb.find_videos(None, 0)), [])

    def test_show_filter_is_case_insensitive_substring(self):
        _mk_library([("Zq9 NBC4 News", "a.mp4"), ("Zq9 Other", "b.mp4")])
        self.assertEqual([os.path.basename(p) for p in fb.find_videos("zq9 nbc4", 0)], ["a.mp4"])
        self.assertEqual(list(fb.find_videos("zzz-no-such-show", 0)), [])

    def test_recent_days_filters_by_mtime(self):
        old, new = _mk_library([("Zq7Age", "old.mp4"), ("Zq7Age", "new.mp4")])
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        self.assertEqual([os.path.basename(p) for p in fb.find_videos("zq7age", 30)], ["new.mp4"])
        self.assertEqual(len(list(fb.find_videos("zq7age", 0))), 2)       # 0 = no cutoff

    def test_already_indexed_drops_nulls(self):
        pg = _pg_stub(rows=[("a.mp4",), (None,), ("b.mkv",)])
        with patch.object(fb, "psycopg2", pg):
            self.assertEqual(fb.already_indexed(), {"a.mp4", "b.mkv"})
        self.assertIn("source='frame_vision'", pg.cur.sql[0][0])


class TestIntegration(unittest.TestCase):
    def test_uses_frame_index_helpers_not_copies(self):
        import nova_frame_index as fi
        self.assertIs(fb.index_video, fi.index_video)
        self.assertIs(fb.show_from_path, fi.show_from_path)
        self.assertNotIn("def show_from_path", SRC)

    def test_done_set_matches_the_metadata_video_key(self):
        # the dedup key is metadata->>'video' == basename, exactly what nova_frame_index.remember stores
        self.assertIn("metadata->>'video'", SRC)
        self.assertIn('"video": os.path.basename(video)', (SCRIPTS / "nova_frame_index.py").read_text())
        _mk_library([("Zq3Integ", "done.mp4"), ("Zq3Integ", "todo.mp4")])
        out, _, index = _run_main(["--show", "zq3integ"], pg=_pg_stub(rows=[("done.mp4",)]))
        self.assertEqual(index.call_count, 1)
        self.assertEqual(os.path.basename(index.call_args[0][0]), "todo.mp4")

    def test_show_name_comes_from_path_and_reaches_index_video(self):
        _mk_library([("Zq4 Chain Show", "ep1.mp4")])
        out, _, index = _run_main(["--show", "zq4 chain", "--frames", "5"])
        self.assertEqual(index.call_args[0][1:], ("Zq4 Chain Show", 5))
        self.assertIn("Zq4 Chain Show: 3 frames", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_indexes_scope_and_reports(self):
        _mk_library([("Zq5Gold", "a.mp4"), ("Zq5Gold", "b.mp4"), ("Zq5Gold", "c.mp4")])
        out, pg, index = _run_main(["--show", "zq5gold", "--limit", "2", "--frames", "4"])
        self.assertEqual(len(pg.calls), 1)
        self.assertEqual(index.call_count, 2)
        self.assertIn("backfill scope: 2 videos to index (skipped 0 already done), 4 frames each ≈ 8 VLM calls", out)
        self.assertIn("[2/2]", out)
        self.assertTrue(out.rstrip().endswith("backfill complete."))

    def test_dry_run_previews_without_indexing(self):
        _mk_library([("Zq6Dry", "a.mp4")])
        out, _, index = _run_main(["--show", "zq6dry", "--dry-run"])
        index.assert_not_called()
        self.assertIn("would index:", out)
        self.assertNotIn("backfill complete", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        for flag in ("--show", "--recent-days", "--limit", "--frames", "--dry-run"):
            self.assertIn(flag, r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_frame_backfill"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
