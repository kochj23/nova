"""Tests for nova_face_retention.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Offline: PG connections are MagicMocks, the faces directory is a tempdir, the memory server is a fake urlopen."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_face_retention.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("nova_face_retention_t", SCRIPT)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)
HH = dict(F.DEFAULT_HOUSEHOLD)
NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


class TestSecurity(unittest.TestCase):
    def test_no_credentials_in_source(self):
        self.assertIsNone(re.search(r"xox[bp]-|Bearer [A-Za-z0-9]{10,}|password\s*=\s*['\"]\w", SRC))

    def test_sql_parameterized(self):
        self.assertIsNone(re.search(r"execute\(f[\"']", SRC))

    def test_purge_refuses_non_jordan(self):
        self.assertEqual(F.purge_person("Dave Bloom", by="nova"), 2)
        self.assertEqual(F.purge_person("Dave Bloom", by=""), 2)

    def test_never_deletes_outside_faces_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(F.inside(Path("/etc/passwd"), Path(d)))
            self.assertTrue(F.inside(Path(d) / "unknown" / "x.jpg", Path(d)))


class TestPerformance(unittest.TestCase):
    def test_candidates_10k(self):
        rows = [(str(i), "", "", (NOW - timedelta(days=i % 10)).isoformat(), i % 2, "Dave") for i in range(10000)]
        t = time.time()
        out = F.candidates_to_delete(rows, HH, NOW - timedelta(hours=72))
        self.assertLess(time.time() - t, 2.0)
        self.assertGreater(len(out), 5000)


class TestRetry(unittest.TestCase):
    def test_forget_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(req, timeout=10):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("down")
            return MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        self.assertTrue(F.forget("abc", _open=flaky, _sleep=lambda s: None))
        self.assertEqual(calls["n"], 3)

    def test_forget_gives_up_and_reports(self):
        def down(req, timeout=10):
            raise urllib.error.URLError("down")
        self.assertFalse(F.forget("abc", _open=down, _sleep=lambda s: None))

    def test_404_is_success(self):
        def gone(req, timeout=10):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        self.assertTrue(F.forget("abc", _open=gone, _sleep=lambda s: None))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(F.selftest(), 0)

    def test_household(self):
        self.assertTrue(F.is_household("Amy Mccaine", HH))
        self.assertFalse(F.is_household(None, HH))
        self.assertFalse(F.is_household("Kathleen Koch", HH))

    def test_resolved_household_kept(self):
        rows = [("c", "", "", (NOW - timedelta(days=9)).isoformat(), 1, "Jordan Koch")]
        self.assertEqual(F.candidates_to_delete(rows, HH, NOW), [])

    def test_bad_timestamp_kept(self):
        self.assertEqual(F.candidates_to_delete([("x", "", "", "garbage", 0, None)], HH, NOW), [])


class TestIntegration(unittest.TestCase):
    def test_uses_memory_server_forget_not_sql_delete(self):
        self.assertIn("/forget", SRC)
        self.assertNotIn("DELETE FROM memories", SRC)

    def test_never_deletes_enrollments_in_run(self):
        run_src = SRC.split("def run(")[1].split("def purge_person(")[0]
        self.assertNotIn("DELETE FROM face_encodings", run_src)
        self.assertNotIn("DELETE FROM face_people", run_src)

    def test_uses_watch_common_config(self):
        self.assertIn('get_config(cur, "face_retention", "household"', SRC)


class TestFunctional(unittest.TestCase):
    def test_run_golden_path(self):
        with tempfile.TemporaryDirectory() as d:
            faces = Path(d)
            (faces / "unknown").mkdir()
            (faces / "known" / "Dave_Bloom").mkdir(parents=True)
            old = time.time() - 10 * 86400
            u = faces / "unknown" / "unknown_x.jpg"
            u.write_bytes(b"x")
            os.utime(u, (old, old))
            fresh = faces / "unknown" / "unknown_new.jpg"
            fresh.write_bytes(b"x")
            dave = faces / "known" / "known_Dave_Bloom_carport_latest_1_2.jpg"
            dave.write_bytes(b"x")
            os.utime(dave, (old, old))
            jordan = faces / "known" / "known_Jordan_Koch_carport_latest_1_2.jpg"
            jordan.write_bytes(b"x")
            os.utime(jordan, (old, old))
            enrol = faces / "known" / "Dave_Bloom" / "photo.jpg"
            enrol.write_bytes(b"x")
            os.utime(enrol, (old, old))

            cur = MagicMock()
            results = iter([
                [("a", "", str(u), (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(), 0, None)],
                [("Dave Bloom",), ("Jordan Koch",)],
                [(1, "Dave Bloom"), (2, "Jordan Koch")],
            ])
            cur.fetchall.side_effect = lambda: next(results)
            conn = MagicMock()
            conn.cursor.return_value = cur
            mcur = MagicMock()
            mcur.fetchall.return_value = [("m1", "Dave Bloom"), ("m2", "Jordan Koch")]
            mconn = MagicMock()
            mconn.cursor.return_value = mcur
            with patch.object(F, "_connect", side_effect=[conn, mconn]), \
                    patch.object(F, "settings", return_value=(HH, 72.0)), \
                    patch.object(F, "forget", return_value=True) as fg:
                res = F.run(dry=False, faces=faces)
            self.assertFalse(u.exists())
            self.assertTrue(fresh.exists())
            self.assertFalse(dave.exists())
            self.assertTrue(jordan.exists())
            self.assertTrue(enrol.exists(), "enrollment photos must never be touched")
            fg.assert_called_once_with("m1")
            self.assertEqual(res["face_presence"], 1)
            self.assertEqual(res["memories"], 1)

    def test_memory_failure_exit_nonzero(self):
        with patch.object(F, "run", return_value={"memory_failures": 2}):
            self.assertEqual(F.main([]), 1)


class TestFrame(unittest.TestCase):
    def test_help_and_selftest(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for arg in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_safe(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
