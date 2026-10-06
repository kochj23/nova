#!/usr/bin/env python3
"""Tests for nova_face_from_photos.py — the 7 house categories (Security, Performance, Retry, Unit,
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

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_face_from_photos.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ff = _load("ff", SCRIPT)
UUID = "ABCDEF12-3456-7890-ABCD-EF1234567890"


def _lib(uuids=(UUID,), size=(1024, 768)):
    """A fake Photos library with one derivative per uuid; returns (lib_dir, out_dir)."""
    root = Path(tempfile.mkdtemp())
    lib, out = root / "lib", root / "out"
    for u in uuids:
        d = lib / "resources" / "derivatives" / u[0].lower()
        d.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), "red").save(d / f"{u}_small.jpeg")           # a smaller decoy
        Image.new("RGB", size, "blue").save(d / f"{u}_1_105_c.jpeg")
    return lib, out


def _sqlite(rows):
    con = MagicMock()
    con.execute.return_value.fetchall.return_value = rows
    return con


def _enroll_stub(exc=None):
    pkg = types.ModuleType("sam_faces"); en = types.ModuleType("sam_faces.enroll")
    en.calls = []

    def enroll(name, path, note=None):
        en.calls.append((name, path, note))
        if exc:
            raise exc
    en.enroll = enroll; pkg.enroll = en
    return {"sam_faces": pkg, "sam_faces.enroll": en}


def _main(argv, rows, lib=None, out=None, enroll=None):
    lib_dir, out_dir = _lib() if lib is None else (lib, out)
    con = _sqlite(rows)
    mods = enroll or _enroll_stub()
    buf = io.StringIO()
    real_path = list(sys.path)
    with patch.object(ff.sqlite3, "connect", return_value=con) as connect, patch.object(ff, "LIB", str(lib_dir)), \
         patch.object(ff, "OUT", str(out_dir)), patch.object(sys, "argv", ["nova_face_from_photos.py"] + argv), \
         patch.dict(sys.modules, mods), redirect_stdout(buf):
        ff.main()
    sys.path[:] = real_path                                       # main() inserts the NAS skills path; keep it out of the test process
    return con, mods["sam_faces.enroll"], buf.getvalue(), out_dir, connect


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sqlite_query_is_parameterized_and_read_only(self):
        con, _, _, _, connect = _main(["Bob'; SELECT 1; --", "--no-enroll"], [])
        sql, params = con.execute.call_args[0]
        self.assertNotIn("Bob", sql)
        self.assertEqual(params, ("Bob'; SELECT 1; --", 32))
        self.assertEqual(sql.count("?"), 2)
        self.assertIn("immutable=1", connect.call_args[0][0])      # the Photos DB is opened read-only
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE)\b", SRC))

    def test_crops_land_under_the_workspace_out_dir(self):
        self.assertIn("/.openclaw/workspace/faces/photos_crops", ff.OUT)
        _, _, out, out_dir, _ = _main(["Brady Riordan", "--no-enroll"], [(UUID, 0.5, 0.5, 0.2)])
        self.assertTrue((out_dir / "Brady_Riordan" / f"{UUID}.jpg").exists())


class TestPerformance(unittest.TestCase):
    def test_crop_face_200_crops_under_bound(self):
        lib, _ = _lib()
        with patch.object(ff, "LIB", str(lib)):
            src = ff.derivative(UUID)
        t0 = time.perf_counter()
        for i in range(200):
            ff.crop_face(src, 0.5, 0.5, 0.1 + (i % 5) / 50)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_missing_derivative_is_skipped_not_fatal(self):
        # RETRY GAP: derivative() — one glob per row; a missing file skips the row and the loop continues
        lib, out = _lib(uuids=("BBBB-1",))
        _, en, text, _, _ = _main(["X", "--no-enroll"], [("AAAA-0", 0.5, 0.5, 0.2), ("BBBB-1", 0.5, 0.5, 0.2)], lib, out)
        self.assertIn("Extracted 1 crops", text)

    def test_crop_failure_is_logged_and_the_loop_continues(self):
        # RETRY GAP: crop_face().save — one attempt per row, failure printed and skipped
        lib, out = _lib(uuids=(UUID, "CCCC-2"))
        bad = lib / "resources" / "derivatives" / "c" / "CCCC-2_1_105_c.jpeg"
        bad.write_bytes(b"not an image")
        _, _, text, _, _ = _main(["X", "--no-enroll"], [("CCCC-2", 0.5, 0.5, 0.2), (UUID, 0.5, 0.5, 0.2)], lib, out)
        self.assertIn("crop fail CCCC-2", text)
        self.assertIn("Extracted 1 crops", text)

    def test_enroll_failure_is_per_crop_and_fails_open(self):
        # RETRY GAP: sam_faces.enroll — one attempt per crop; a rejected face is skipped, the rest still enroll
        lib, out = _lib(uuids=(UUID, "DDDD-3"))
        _, en, text, _, _ = _main(["X"], [(UUID, 0.5, 0.5, 0.2), ("DDDD-3", 0.5, 0.5, 0.2)], lib, out, enroll=_enroll_stub(ValueError("no face")))
        self.assertEqual(len(en.calls), 2)
        self.assertIn("Enrolled 0/2", text)
        self.assertEqual(text.count("enroll skip"), 2)


class TestUnit(unittest.TestCase):
    def test_derivative_prefers_the_105_rendition_and_largest_file(self):
        lib, _ = _lib()
        with patch.object(ff, "LIB", str(lib)):
            self.assertTrue(ff.derivative(UUID).endswith("_1_105_c.jpeg"))
            self.assertIsNone(ff.derivative("ZZZZ"))

    def test_crop_face_flips_y_and_clamps_to_the_image(self):
        lib, _ = _lib(size=(1000, 500))
        with patch.object(ff, "LIB", str(lib)):
            src = ff.derivative(UUID)
        im = ff.crop_face(src, 0.5, 0.5, 0.1, margin=1.0)         # centre: half = 0.1*1000/2 = 50 -> 100x100
        self.assertEqual(im.size, (100, 100))
        corner = ff.crop_face(src, 0.0, 1.0, 0.1, margin=1.0)       # top-left in Photos coords (bottom-left origin)
        self.assertEqual(corner.size, (50, 50))
        huge = ff.crop_face(src, 0.5, 0.5, 5.0)
        self.assertEqual(huge.size, (1000, 500))

    def test_main_limit_is_four_times_the_target(self):
        con, _, _, _, _ = _main(["X", "-n", "3", "--no-enroll"], [])
        self.assertEqual(con.execute.call_args[0][1], ("X", 12))


class TestIntegration(unittest.TestCase):
    def test_enroll_receives_each_crop_with_the_photos_note(self):
        _, en, _, out_dir, _ = _main(["Brady Riordan"], [(UUID, 0.5, 0.5, 0.2)])
        self.assertEqual(en.calls, [("Brady Riordan", str(out_dir / "Brady_Riordan" / f"{UUID}.jpg"), "photos-people")])

    def test_stops_after_n_crops(self):
        lib, out = _lib(uuids=(UUID, "EEEE-4", "FFFF-5"))
        _, en, text, _, _ = _main(["X", "-n", "2"], [(UUID, .5, .5, .2), ("EEEE-4", .5, .5, .2), ("FFFF-5", .5, .5, .2)], lib, out)
        self.assertIn("Extracted 2 crops", text)
        self.assertEqual(len(en.calls), 2)


class TestFunctional(unittest.TestCase):
    def test_golden_path_extracts_and_enrolls(self):
        _, en, text, out_dir, _ = _main(["Brady Riordan"], [(UUID, 0.5, 0.5, 0.2)])
        self.assertIn("Extracted 1 crops for 'Brady Riordan'", text)
        self.assertIn("Enrolled 1/1 into PG for 'Brady Riordan'", text)
        self.assertEqual(Image.open(out_dir / "Brady_Riordan" / f"{UUID}.jpg").mode, "RGB")

    def test_error_path_no_matches_enrolls_nothing(self):
        _, en, text, _, _ = _main(["Nobody"], [])
        self.assertIn("Extracted 0 crops", text)
        self.assertEqual(en.calls, [])
        self.assertNotIn("Enrolled", text)

    def test_no_enroll_flag_skips_pg(self):
        _, en, text, _, _ = _main(["Bailey", "--no-enroll"], [(UUID, 0.5, 0.5, 0.2)])
        self.assertEqual(en.calls, [])
        self.assertNotIn("Enrolled", text)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--no-enroll", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
