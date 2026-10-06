#!/usr/bin/env python3
"""Tests for nova_cluster_render.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cluster_render.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("ncrender", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load()


def _fake_render(size):
    def go(tmp):
        tmp.write_bytes(b"\x89PNG" + b"0" * size)
        return size >= cr.MIN_BYTES
    return go


def _main(out, scp_rc=0, size=50_000):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd); return MagicMock(returncode=scp_rc, stderr=b"no route")
    buf = io.StringIO()
    with patch.object(cr, "CHROME", "/fake/chrome"), patch.object(cr, "render", side_effect=_fake_render(size)), \
         patch.object(cr.subprocess, "run", side_effect=run), patch.object(sys, "argv", ["x", "--out", str(out)]), \
         redirect_stdout(buf):
        rc = cr.main()
    return rc, calls, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_unmounted_share_never_renders_to_studio_disk(self):
        out = Path(tempfile.mkdtemp()) / "missing" / "dash"
        with patch.object(cr, "CHROME", "/fake/chrome"), patch.object(cr, "render") as r, \
             patch.object(sys, "argv", ["x", "--out", str(out)]), redirect_stdout(io.StringIO()):
            self.assertEqual(cr.main(), 1)
        r.assert_not_called()
        self.assertFalse(out.exists())

    def test_scp_is_non_interactive(self):
        _, calls, _ = _main(Path(tempfile.mkdtemp()) / "dash")
        scp = [c for c in calls if c[0] == "scp"][0]
        self.assertIn("BatchMode=yes", scp)


class TestPerformance(unittest.TestCase):
    def test_prune_10k_names(self):
        names = [f"nova-cluster-{i:08d}-00.png" for i in range(10_000)]
        t0 = time.perf_counter()
        gone = cr.prune(names)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(gone), 10_000 - cr.KEEP_HOURLY)


class TestRetry(unittest.TestCase):
    def test_scp_failure_is_best_effort(self):
        # RETRY GAP: main()/scp push — one attempt; failure is logged and the NAS frame still stands
        out = Path(tempfile.mkdtemp()) / "dash"
        rc, calls, logtxt = _main(out, scp_rc=1)
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for c in calls if c[0] == "scp"), 1)
        self.assertIn("failed", logtxt)
        self.assertTrue((out / "nova-cluster.png").exists())

    def test_failed_render_keeps_last_good_frame(self):
        out = Path(tempfile.mkdtemp()) / "dash"; out.mkdir()
        (out / "nova-cluster.png").write_bytes(b"good")
        rc, _, _ = _main(out, size=10)
        self.assertEqual(rc, 1)
        self.assertEqual((out / "nova-cluster.png").read_bytes(), b"good")


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as b:
            cr.selftest()
        self.assertIn("selftest ok", b.getvalue())

    def test_prune_edges(self):
        self.assertEqual(cr.prune([]), [])
        self.assertEqual(cr.prune(["b", "a", "c"], keep=1), ["a", "b"])

    def test_render_checks_min_bytes(self):
        tmp = Path(tempfile.mkdtemp()) / "x.png"
        def run(cmd, **kw):
            tmp.write_bytes(b"0" * 100); return MagicMock(returncode=0)
        with patch.object(cr, "CHROME", "/fake/chrome"), patch.object(cr.subprocess, "run", side_effect=run) as r:
            self.assertFalse(cr.render(tmp))
        self.assertIn("--headless=new", r.call_args[0][0])

    def test_no_chrome_returns_2(self):
        with patch.object(cr, "CHROME", None), redirect_stdout(io.StringIO()):
            self.assertEqual(cr.main(), 2)


class TestIntegration(unittest.TestCase):
    def test_hourly_name_matches_prune_ordering(self):
        a, b = cr.hourly_name(1_700_000_000), cr.hourly_name(1_700_000_000 + 3600)
        self.assertEqual(cr.prune([b, a], keep=1), [a])
        self.assertRegex(a, r"^nova-cluster-\d{8}-\d{2}\.png$")


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_latest_and_hourly_and_prunes(self):
        out = Path(tempfile.mkdtemp()) / "dash"; (out / "hourly").mkdir(parents=True)
        for i in range(cr.KEEP_HOURLY + 5):
            (out / "hourly" / f"nova-cluster-2000{i:04d}-00.png").write_bytes(b"x")
        rc, calls, _ = _main(out)
        self.assertEqual(rc, 0)
        self.assertTrue((out / "nova-cluster.png").exists())
        self.assertFalse((out / ".nova-cluster.tmp.png").exists())        # atomic rename consumed the temp
        self.assertEqual(len(list((out / "hourly").glob("*.png"))), cr.KEEP_HOURLY)
        self.assertEqual(calls[0][-1], f"{cr.TV_HOST}:{cr.TV_CACHE}/")

    def test_existing_hourly_frame_is_not_repushed(self):
        out = Path(tempfile.mkdtemp()) / "dash"; (out / "hourly").mkdir(parents=True)
        (out / "hourly" / cr.hourly_name(time.time())).write_bytes(b"x")
        rc, calls, _ = _main(out)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch("subprocess.run", side_effect=AssertionError("import must not render")):
            _load()


if __name__ == "__main__":
    unittest.main()
