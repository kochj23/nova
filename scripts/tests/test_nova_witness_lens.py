#!/usr/bin/env python3
"""Runs nova_witness_lens.py's pure-logic self-test (no DB, no memory server). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "nova_witness_lens.py"


class TestSelftest(unittest.TestCase):
    def test_demo(self):
        spec = importlib.util.spec_from_file_location("nova_witness_lens", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = io.StringIO()
        with redirect_stdout(out):
            mod.demo()
        self.assertIn("assertions passed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
