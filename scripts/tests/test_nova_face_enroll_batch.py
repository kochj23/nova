#!/usr/bin/env python3
"""Tests for nova_face_enroll_batch.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_face_enroll_batch.py"
SRC = SCRIPT.read_text()

ENROLL = MagicMock(name="enroll")


def _load():
    """sam_faces lives on the NAS; stub it (scoped to the load) so the test never touches the people DB."""
    pkg = types.ModuleType("sam_faces"); sub = types.ModuleType("sam_faces.enroll")
    sub.enroll = ENROLL; pkg.enroll = sub
    spec = importlib.util.spec_from_file_location("nfeb", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"sam_faces": pkg, "sam_faces.enroll": sub}):
        spec.loader.exec_module(mod)
    return mod


fe = _load()


def _tree(spec):
    """spec: {folder: [filenames]} under a fresh temp KNOWN dir."""
    root = Path(tempfile.mkdtemp(prefix="faces_known_"))
    for folder, files in spec.items():
        (root / folder).mkdir()
        for f in files:
            (root / folder / f).write_bytes(b"x")
    return root


def _run(root, argv):
    out = io.StringIO()
    with patch.object(fe, "KNOWN", root), patch.object(sys, "argv", argv), redirect_stdout(out):
        fe.main()
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_default_is_dry_run_no_enroll_no_markers(self):
        ENROLL.reset_mock()
        root = _tree({"amy_mccaine": ["a.jpg", "b.png"]})
        out = _run(root, ["x"])
        self.assertIn("DRY RUN", out)
        ENROLL.assert_not_called()
        self.assertEqual(list(root.rglob("*.enrolled")), [])

    def test_only_image_extensions_are_collected(self):
        root = _tree({"bob": ["a.JPG", "evil.sh", "notes.txt", "c.jpeg"]})
        with patch.object(fe, "KNOWN", root):
            names = sorted(f.name for _, f in fe.collect())
        self.assertEqual(names, ["a.JPG", "c.jpeg"])


class TestPerformance(unittest.TestCase):
    def test_norm_name_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            fe.norm_name(f"mary-ann_riordan_{i}")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_enroll_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: main()/enroll — one attempt per photo; failure leaves no marker so the next run retries it
        ENROLL.reset_mock(); ENROLL.side_effect = [RuntimeError("no face found"), None]
        try:
            root = _tree({"amy": ["a.jpg", "b.jpg"]})
            out = _run(root, ["x", "--commit"])
        finally:
            ENROLL.side_effect = None
        self.assertEqual(ENROLL.call_count, 2)
        self.assertIn("Enrolled 1, failed 1", out)
        self.assertFalse((root / "amy" / "a.jpg.enrolled").exists())
        self.assertTrue((root / "amy" / "b.jpg.enrolled").exists())


class TestUnit(unittest.TestCase):
    def test_norm_name_edges(self):
        self.assertEqual(fe.norm_name("mary-ann_riordan"), "Mary Ann Riordan")
        self.assertEqual(fe.norm_name("amy_mccaine"), "Amy Mccaine")
        self.assertEqual(fe.norm_name("__"), "")
        self.assertEqual(fe.norm_name(".hidden."), "Hidden")

    def test_marker_path(self):
        self.assertEqual(fe._marker(Path("/x/y/a.jpg")), Path("/x/y/a.jpg.enrolled"))

    def test_collect_missing_dir_and_skips(self):
        with patch.object(fe, "KNOWN", Path(tempfile.mkdtemp()) / "nope"):
            self.assertEqual(fe.collect(), [])
        root = _tree({"amy": ["a.jpg", "a.jpg.enrolled", "b.jpg"], "__": ["c.jpg"]})
        (root / "stray.jpg").write_bytes(b"x")                 # file at top level, not a person dir
        with patch.object(fe, "KNOWN", root):
            todo = fe.collect()
        self.assertEqual([(n, f.name) for n, f in todo], [("Amy", "b.jpg")])


class TestIntegration(unittest.TestCase):
    def test_uses_sam_faces_enroll_not_a_reimplementation(self):
        self.assertIn("from sam_faces.enroll import enroll", SRC)
        self.assertIs(fe.enroll, ENROLL)
        self.assertEqual(fe.KNOWN.parts[-3:], ("workspace", "faces", "known"))

    def test_collect_feeds_main_counts(self):
        root = _tree({"amy": ["a.jpg", "b.jpg"], "bob": ["c.png"]})
        out = _run(root, ["x"])
        self.assertIn("3 photo(s) across 2 people", out)


class TestFunctional(unittest.TestCase):
    def test_commit_enrolls_and_writes_markers_then_is_idempotent(self):
        ENROLL.reset_mock()
        root = _tree({"amy_mccaine": ["a.jpg"]})
        out = _run(root, ["x", "--commit"])
        ENROLL.assert_called_once_with("Amy Mccaine", str(root / "amy_mccaine" / "a.jpg"))
        self.assertIn("Enrolled 1, failed 0", out)
        self.assertTrue((root / "amy_mccaine" / "a.jpg.enrolled").exists())
        self.assertIn("Nothing new to enroll", _run(root, ["x", "--commit"]))
        self.assertEqual(ENROLL.call_count, 1)


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # sam_faces is on the NAS; stub it inside the throwaway subprocess, then import and prove main() did not run
        code = ("import sys,types,importlib.util as u; p=types.ModuleType('sam_faces'); e=types.ModuleType('sam_faces.enroll');"
                "e.enroll=lambda *a: 1/0; sys.modules['sam_faces']=p; sys.modules['sam_faces.enroll']=e;"
                f"s=u.spec_from_file_location('m', {str(SCRIPT)!r}); m=u.module_from_spec(s); s.loader.exec_module(m)")
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
