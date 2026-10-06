#!/usr/bin/env python3
"""Tests for nova_make_part.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). build123d/trimesh are stubbed; no real CAD run.
Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mp = _load("nova_make_part_t", SCRIPTS / "nova_make_part.py")
SRC = (SCRIPTS / "nova_make_part.py").read_text()


@contextlib.contextmanager
def _modules(**mods):
    """Set only these sys.modules keys and restore exactly those (never the whole dict)."""
    old = {k: sys.modules.get(k, None) for k in mods}
    had = {k: k in sys.modules for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k in mods:
            if had[k]:
                sys.modules[k] = old[k]
            else:
                sys.modules.pop(k, None)


class _Mesh:
    def __init__(self, watertight=True):
        self.is_watertight = watertight
        self.is_winding_consistent = True
        self.extents = [10.0, 20.004, 5.0]
        self.volume = 1000.123
        self.faces = [0] * 12


def _fakes(mesh=None, load_exc=None):
    exported = []
    b3d = types.SimpleNamespace(export_stl=lambda obj, p: (exported.append(obj), Path(p).write_text("solid")))
    def load(p, force=None):
        if load_exc:
            raise load_exc
        return mesh or _Mesh()
    return b3d, types.SimpleNamespace(load=load), exported


def _main(src_text, mesh=None, load_exc=None, render=None):
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "gen.py"; src.write_text(src_text)
        b3d, tm, exported = _fakes(mesh, load_exc)
        out = io.StringIO()
        with _modules(build123d=b3d, trimesh=tm), \
             mock.patch.object(sys, "argv", ["x", str(src), f"{td}/o.stl", f"{td}/o.png"]), \
             mock.patch.object(mp, "render", render or (lambda m, p: None)), \
             contextlib.redirect_stdout(out):
            mp.main()
        return json.loads(out.getvalue().strip().splitlines()[-1]), exported


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_generated_code_never_runs_as_main_and_no_secret_access(self):
        self.assertIn('run_name="nova_make_generated"', SRC)
        for bad in ("os.environ", "subprocess", "urllib", "requests", "security find-generic-password"):
            self.assertNotIn(bad, SRC)

    def test_generated_dunder_main_block_is_inert(self):
        res, _ = _main('result = "solid"\nif __name__ == "__main__":\n    raise SystemExit(9)\n')
        self.assertTrue(res["ok"])


class TestPerformance(unittest.TestCase):
    def test_validation_path_fast(self):
        t0 = time.perf_counter()
        for _ in range(50):
            _main("result = 1\n")
        self.assertLess(time.perf_counter() - t0, 5.0)


class TestRetry(unittest.TestCase):
    def test_render_failure_fails_open(self):
        # RETRY GAP: render() — one attempt (local CPU render); failure is recorded, geometry result kept
        def boom(m, p): raise RuntimeError("no display")
        res, _ = _main("result = 1\n", render=boom)
        self.assertTrue(res["ok"])
        self.assertEqual(res["render_error"], "no display")

    def test_mesh_load_failure_is_reported_not_raised(self):
        res, _ = _main("result = 1\n", load_exc=ValueError("bad stl"))
        self.assertFalse(res["ok"])
        self.assertIn("bad stl", res["error"])


class TestUnit(unittest.TestCase):
    def test_missing_result_is_error(self):
        res, exported = _main("x = 1\n")
        self.assertFalse(res["ok"])
        self.assertIn("must assign the final solid", res["error"])
        self.assertEqual(exported, [])

    def test_buildpart_context_unwrapped(self):
        res, exported = _main("class C: part = 'inner'\nresult = C()\n")
        self.assertEqual(exported, ["inner"])

    def test_part_name_fallback(self):
        _, exported = _main("part = 'p'\n")
        self.assertEqual(exported, ["p"])

    def test_non_watertight_has_no_volume(self):
        res, _ = _main("result = 1\n", mesh=_Mesh(watertight=False))
        self.assertIsNone(res["volume_mm3"])
        self.assertFalse(res["watertight"])


class TestIntegration(unittest.TestCase):
    def test_json_contract_for_orchestrator(self):
        res, _ = _main("result = 1\n")
        for k in ("ok", "watertight", "winding_consistent", "dims_mm", "volume_mm3", "faces", "render"):
            self.assertIn(k, res)
        self.assertEqual(res["dims_mm"], [10.0, 20.0, 5.0])
        self.assertEqual(res["faces"], 12)

    def test_real_render_writes_png(self):
        import numpy as np
        mesh = types.SimpleNamespace(
            triangles=np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 0], [0, 1, 0], [0, 0, 1]]], float),
            bounds=np.array([[0, 0, 0], [1, 1, 1]], float), extents=np.array([1, 1, 1], float))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.png"
            mp.render(mesh, str(p))
            self.assertGreater(p.stat().st_size, 100)


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        res, exported = _main("result = 'solid'\n")
        self.assertTrue(res["ok"])
        self.assertEqual(exported, ["solid"])
        self.assertEqual(res["volume_mm3"], 1000.12)

    def test_syntax_error_in_generated_code(self):
        res, _ = _main("def (:\n")
        self.assertFalse(res["ok"])
        self.assertIn("SyntaxError", res["error"])


class TestFrame(unittest.TestCase):
    def test_runs_without_build123d_and_emits_json(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "g.py").write_text("result = 1\n")
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_make_part.py"), f"{td}/g.py", f"{td}/o.stl",
                                f"{td}/o.png"], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1"}, cwd=td)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"ok"', r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
