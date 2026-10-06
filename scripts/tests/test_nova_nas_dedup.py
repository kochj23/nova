#!/usr/bin/env python3
"""Tests for nova_nas_dedup.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import hashlib
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
SCRIPT = SCRIPTS / "nova_nas_dedup.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


D = _load("nas_dedup_under_test", SCRIPT)
D.notify = MagicMock(return_value=True)                                             # nova_notify would write telemetry.events
D.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=AssertionError("PG reached from a test")))
TMP = Path(tempfile.mkdtemp(prefix="nas-dedup-test-"))
D.ROOT = str(TMP / "NAS")                                                           # never walk the real /Volumes/NAS
MB = D.MIN_SIZE


def _mk(rel, content):
    p = TMP / "NAS" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return str(p)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=()):
        self.rules = list(rules); self.sql = []; self.params = []; self.many = []; self._last = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append(s); self.params.append(params); self._last = None
        for sub, val in self.rules:
            if sub in s:
                self._last = val; return

    def executemany(self, sql, rows):
        self.many.append((" ".join(sql.split()), list(rows)))

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) and self._last else self._last

    def fetchall(self):
        return list(self._last or [])

    def ran(self, frag):
        return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _with_db(cur, fn, *a):
    with patch.object(D, "_db", lambda: _Conn(cur)), redirect_stdout(io.StringIO()) as out:
        fn(*a)
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", D.DSN)

    def test_sql_is_parameterized_and_writes_stay_in_the_dedup_tables(self):
        self.assertIsNone(re.search(r'execute(many)?\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"nas_files", "nas_dedup_proposals", "nas_dedup_runs"})
        evil = "/Volumes/NAS/x'; DROP TABLE nas_files; --"
        cur = _Cur([("status='approved'", [(evil, 5)])])
        with patch.object(D.os, "remove", MagicMock(side_effect=FileNotFoundError)):
            _with_db(cur, D.apply)
        for s, p in cur.sql and zip(cur.sql, cur.params):
            self.assertNotIn("DROP", s)
        self.assertEqual(cur.ran("status='deleted'")[0][1], (evil,))

    def test_protected_guard_cannot_be_bypassed(self):
        self.assertTrue(D.protected("/Volumes/NAS/iTunes/Music/a.m4a"))
        self.assertTrue(D.protected("/Volumes/NAS/Movies/../iTunes/a.m4a"))            # traversal still hits the segment rule
        self.assertTrue(D.protected("/Volumes/NAS/GoogleDriveBackups/x/y.bin"))
        self.assertTrue(D.protected("/Volumes/NAS/Shared Google Drives/w.bin"))
        self.assertFalse(D.protected("/Volumes/NAS/Movies/a.mkv"))
        self.assertTrue(D.protected("/Volumes/NAS/iTunesBackupsOld/a.bin"))             # prefix guard errs wide (conservative)
        self.assertFalse(D.protected("/Volumes/NAS/Music/iTunesBackupsOld/a.bin"))      # a lookalike segment deeper down is not

    def test_apply_never_deletes_a_protected_path_even_when_approved(self):
        cur = _Cur([("status='approved'", [("/Volumes/NAS/GoogleDriveBackups/a.bin", 10), ("/Volumes/NAS/iTunes/b.m4a", 10)])])
        with patch.object(D.os, "remove", MagicMock()) as rm:
            _with_db(cur, D.apply)
        rm.assert_not_called()
        self.assertEqual([p for _, p in cur.ran("skipped_protected")], [("/Volumes/NAS/GoogleDriveBackups/a.bin",), ("/Volumes/NAS/iTunes/b.m4a",)])
        self.assertEqual(cur.ran("INSERT INTO nas_dedup_runs")[0][1], (0, 0.0))

    def test_apply_only_reads_approved_rows(self):
        cur = _Cur([])
        with patch.object(D.os, "remove", MagicMock()) as rm:
            out = _with_db(cur, D.apply)
        self.assertIn("WHERE status='approved'", cur.sql[0])
        rm.assert_not_called()
        self.assertIn("nothing approved", out)


class TestPerformance(unittest.TestCase):
    def test_protected_and_keeper_fast_on_10k_paths(self):
        paths = [f"/Volumes/NAS/{'GoogleDriveBackups' if i % 7 == 0 else 'Movies'}/dir{i % 13}/file {i} copy.mkv" for i in range(10_000)]
        mtimes = [float(i) for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(D.protected(p) for p in paths)
        k = D._keeper(paths, mtimes)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(n, len(range(0, 10_000, 7)))
        self.assertIn(k, paths)


class TestRetry(unittest.TestCase):
    def test_db_failure_is_one_attempt_and_nothing_is_deleted(self):
        # RETRY GAP: _db/psycopg2.connect — no retry; a down PG fails CLOSED (exception, zero deletions), the safe side for a deleter
        with patch.object(D.os, "remove", MagicMock()) as rm:
            for fn in (D.scan, D.propose, D.apply, D.report):
                with self.assertRaises(AssertionError):
                    fn()
        self.assertEqual(D.psycopg2.connect.call_count, 4)
        rm.assert_not_called()

    def test_unremovable_file_is_skipped_and_the_run_continues(self):
        # RETRY GAP: apply/os.remove — one attempt per file; a permission error leaves the row untouched and moves on
        a, b = _mk("Movies/a.bin", b"x"), _mk("Movies/b.bin", b"y")
        cur = _Cur([("status='approved'", [(a, 1), (b, 1)])])
        real_remove = os.remove

        def flaky(p):
            if p == a:
                raise PermissionError("busy")
            real_remove(p)
        with patch.object(D.os, "remove", flaky):
            out = _with_db(cur, D.apply)
        self.assertIn("could not delete", out)
        self.assertTrue(os.path.exists(a)); self.assertFalse(os.path.exists(b))
        self.assertEqual([p for _, p in cur.ran("status='deleted'")], [(b,)])
        self.assertEqual(cur.ran("INSERT INTO nas_dedup_runs")[0][1], (1, 0.0))

    def test_notify_failure_comes_after_the_run_row_is_committed(self):
        # RETRY GAP: notify — one attempt, unguarded; by then the deletions and the nas_dedup_runs row are already recorded
        p = _mk("Movies/n.bin", b"z")
        cur = _Cur([("status='approved'", [(p, 1)])])
        with patch.object(D, "notify", MagicMock(side_effect=OSError("slack down"))):
            with self.assertRaises(OSError):
                _with_db(cur, D.apply)
        self.assertFalse(os.path.exists(p))
        self.assertEqual(len(cur.ran("INSERT INTO nas_dedup_runs")), 1)

    def test_vanished_candidate_during_hashing_is_dropped_not_fatal(self):
        cur = _Cur([("SELECT now()", [("T0",)]), ("partial_hash IS NULL AND size_bytes IN", [(str(TMP / "NAS/gone.bin"), 5 * MB)])])
        out = _with_db(cur, D.scan)
        self.assertEqual(cur.ran("DELETE FROM nas_files WHERE path=%s")[0][1], (str(TMP / "NAS/gone.bin"),))
        self.assertIn("partial-hashed 0", out)


class TestUnit(unittest.TestCase):
    def test_hashes(self):
        small = _mk("Hash/small.bin", b"abc")
        self.assertEqual(D.full_hash(small), hashlib.blake2b(b"abc", digest_size=32).hexdigest())
        self.assertEqual(D.partial_hash(small, 3), hashlib.blake2b(b"abc", digest_size=16).hexdigest())
        head, tail = os.urandom(D.CHUNK), os.urandom(D.CHUNK)
        big1 = _mk("Hash/big1.bin", head + b"\x00" * D.CHUNK + tail)
        big2 = _mk("Hash/big2.bin", head + b"\xff" * D.CHUNK + tail)
        self.assertEqual(D.partial_hash(big1, 3 * D.CHUNK), D.partial_hash(big2, 3 * D.CHUNK))   # same head+tail collide at stage 1
        self.assertNotEqual(D.full_hash(big1), D.full_hash(big2))                              # stage 2 tells them apart
        self.assertEqual(D.partial_hash(big1, 3 * D.CHUNK),
                         hashlib.blake2b(head + tail, digest_size=16).hexdigest())

    def test_hash_of_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            D.full_hash(str(TMP / "NAS/nope.bin"))

    def test_keeper_prefers_clean_name_then_shortest_then_oldest(self):
        self.assertEqual(D._keeper(["/n/a copy.mkv", "/n/long/path/a.mkv"], [1.0, 2.0]), "/n/long/path/a.mkv")
        self.assertEqual(D._keeper(["/n/zz/a.mkv", "/n/a.mkv"], [1.0, 2.0]), "/n/a.mkv")
        self.assertEqual(D._keeper(["/n/b.mkv", "/n/a.mkv"], [5.0, 2.0]), "/n/a.mkv")
        self.assertEqual(D._keeper(["/n/a (1).mkv", "/n/a~.mkv", "/n/conflict.mkv"], [1.0, 1.0, 1.0]), "/n/a~.mkv")
        self.assertEqual(D._keeper(["/only.bin"], [0.0]), "/only.bin")
        with self.assertRaises(ValueError):
            D._keeper([], [])

    def test_report_prints_counts_and_tolerates_nulls(self):
        cur = _Cur([("status='proposed' ORDER BY", [(2e9, "/k", "/d")]), ("status='manual_review'", [(3,)]),
                    ("status='proposed'", [(None, None)])])
        out = _with_db(cur, D.report)
        self.assertIn("Proposed (safe to approve): 0 files, ~0 GB", out)
        self.assertIn("Manual review (inside GoogleDriveBackups): 3 files", out)
        self.assertIn("DELETE /d", out); self.assertIn("KEEP   /k", out)


class TestIntegration(unittest.TestCase):
    def test_shared_notify_and_the_dedup_tables_are_used(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("def notify", SRC)
        self.assertIn("dbname=nova_ops", D.DSN)
        for t in ("nas_files", "nas_dedup_proposals", "nas_dedup_runs"):
            self.assertIn(t, SRC)

    def test_scan_walk_filters_feed_the_insert_batch(self):
        big_a = _mk("Walk/Movies/a.mkv", b"a" * MB)
        big_b = _mk("Walk/Movies/sub/a copy.mkv", b"a" * MB)
        _mk("Walk/Movies/tiny.txt", b"t")                                      # < MIN_SIZE
        _mk("Walk/Movies/.hidden.mkv", b"h" * MB)                              # dotfile
        _mk("Walk/.cache/c.mkv", b"c" * MB)                                    # dot dir
        _mk("Walk/#recycle/r.mkv", b"r" * MB)                                  # skip dir
        os.symlink(big_a, str(TMP / "NAS/Walk/Movies/link.mkv"))               # symlink
        cur = _Cur([("SELECT now()", [("T0",)]), ("partial_hash IS NULL AND size_bytes IN", [(big_a, MB), (big_b, MB)]),
                    ("content_hash IS NULL AND partial_hash IS NOT NULL", [(big_a,), (big_b,)])])
        with patch.object(D, "ROOT", str(TMP / "NAS/Walk")):
            out = _with_db(cur, D.scan)
        self.assertEqual(len(cur.many), 1)
        rows = cur.many[0][1]
        self.assertEqual(sorted(r[0] for r in rows), sorted([big_a, big_b]))
        self.assertEqual({r[1] for r in rows}, {MB})
        self.assertEqual(cur.ran("DELETE FROM nas_files WHERE seen_at")[0][1], ("T0",))
        ph = [p for _, p in cur.ran("SET partial_hash=%s")]
        self.assertEqual(ph[0][0], ph[1][0])                                   # identical content -> same partial hash
        fh = [p for _, p in cur.ran("SET content_hash=%s")]
        self.assertEqual(fh[0][0], fh[1][0], D.full_hash(big_a))
        self.assertEqual(cur.ran("INSERT INTO nas_dedup_runs")[0][1], (2, 2))
        self.assertIn("scanned 2 files; partial-hashed 2, full-hashed 2", out)

    def test_propose_composes_protected_and_keeper(self):
        sets = [("h1", ["/Volumes/NAS/Movies/x copy.mkv", "/Volumes/NAS/GoogleDriveBackups/x.mkv", "/Volumes/NAS/Movies/x.mkv"], [1.0, 2.0, 3.0], 4_000_000_000, 3),
                ("h2", ["/Volumes/NAS/Movies/y copy.mkv", "/Volumes/NAS/Movies/y.mkv"], [1.0, 9.0], 1_000_000_000, 2),
                ("h3", ["/Volumes/NAS/GoogleDriveBackups/z.mkv", "/Volumes/NAS/Google-Drive-kochjpar/z.mkv"], [1.0, 1.0], 7, 2)]
        cur = _Cur([("GROUP BY content_hash", sets)])
        out = _with_db(cur, D.propose)
        ins = [p for _, p in cur.ran("INSERT INTO nas_dedup_proposals")]
        by_path = {p[0]: p for p in ins}
        self.assertEqual(by_path["/Volumes/NAS/Movies/x copy.mkv"][1], "/Volumes/NAS/GoogleDriveBackups/x.mkv")   # Google copy kept
        self.assertEqual(by_path["/Volumes/NAS/Movies/x.mkv"][5], "proposed")
        self.assertEqual(by_path["/Volumes/NAS/Movies/y copy.mkv"][1:6], ("/Volumes/NAS/Movies/y.mkv", "h2", 1_000_000_000, 2, "proposed"))
        self.assertEqual(by_path["/Volumes/NAS/Google-Drive-kochjpar/z.mkv"][5], "manual_review")
        self.assertNotIn("/Volumes/NAS/GoogleDriveBackups/x.mkv", by_path)
        self.assertEqual(cur.ran("INSERT INTO nas_dedup_runs")[0][1], (3, 9.0))
        self.assertIn("3 dup sets: 3 proposed (~9.0 GB), 1 manual-review", out)
        self.assertEqual(cur.sql[0], "DELETE FROM nas_dedup_proposals WHERE status IN ('proposed','manual_review')")


class TestFunctional(unittest.TestCase):
    def test_apply_golden_path_deletes_approved_files_and_notifies(self):
        a, b = _mk("Apply/a.bin", b"a" * 1000), _mk("Apply/b.bin", b"b" * 1000)
        cur = _Cur([("status='approved'", [(a, 500_000_000), (b, 500_000_000)])])
        D.notify.reset_mock()
        out = _with_db(cur, D.apply)
        self.assertFalse(os.path.exists(a)); self.assertFalse(os.path.exists(b))
        self.assertEqual([p for _, p in cur.ran("SET status='deleted'")], [(a,), (b,)])
        self.assertEqual([p for _, p in cur.ran("DELETE FROM nas_files WHERE path=%s")], [(a,), (b,)])
        self.assertEqual(cur.ran("INSERT INTO nas_dedup_runs")[0][1], (2, 1.0))
        title, kw = D.notify.call_args[0][0], D.notify.call_args[1]
        self.assertEqual(title, "NAS dedup: deleted 2 duplicates, freed ~1.0 GB")
        self.assertEqual((kw["category"], kw["source"], kw["level"]), ("dedup", "nova_nas_dedup.py", "info"))
        self.assertIn("deleted 2 files, freed ~1.0 GB", out)

    def test_apply_already_missing_file_is_marked_deleted_without_counting(self):
        gone = str(TMP / "NAS/Apply/gone.bin")
        cur = _Cur([("status='approved'", [(gone, 10)])])
        _with_db(cur, D.apply)
        self.assertEqual(cur.ran("SET status='deleted'")[0][1], (gone,))
        self.assertEqual(cur.ran("DELETE FROM nas_files"), [])
        self.assertEqual(cur.ran("INSERT INTO nas_dedup_runs")[0][1], (0, 0.0))

    def test_propose_posts_a_summary_and_clears_stale_proposals(self):
        D.notify.reset_mock()
        cur = _Cur([("GROUP BY content_hash", [("h", ["/Volumes/NAS/M/a copy.mkv", "/Volumes/NAS/M/a.mkv"], [1.0, 1.0], 2_000_000_000, 2)])])
        _with_db(cur, D.propose)
        self.assertEqual(D.notify.call_args[0][0], "NAS dedup: 1 exact duplicates (~2.0 GB) ready to approve")
        self.assertIn("0 copies are inside GoogleDriveBackups", D.notify.call_args[1]["body"])

    def test_command_dispatch_is_a_closed_map(self):
        self.assertIn('{"scan": scan, "propose": propose, "apply": apply, "report": report}.get(cmd, scan)()', SRC)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_a_subcommand(self):
        # no --help here: any unknown argv falls back to scan(), which walks the NAS — import smoke only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_dedup"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
