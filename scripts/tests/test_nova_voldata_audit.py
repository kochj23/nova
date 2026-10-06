#!/usr/bin/env python3
"""Tests for nova_voldata_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The script does its work at import time, so it is NEVER imported:
helpers are lifted with ast, and full runs exec the source with /Volumes paths and HOME rewritten
into a tempdir. Written by Jordan Koch (via Claude)."""
import ast
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SRC = (SCRIPTS / "nova_voldata_audit.py").read_text()


def _helpers():
    """Compile only the def nodes (human, dirsize, w) — no module-level side effects."""
    tree = ast.parse(SRC)
    tree.body = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Import))]
    ns = {}
    exec(compile(tree, "nova_voldata_audit_helpers", "exec"), ns)
    return ns


H = _helpers()


def _run_sandboxed(td):
    """Exec the whole audit with every /Volumes path and ~ pointed into td. Returns the log text."""
    home = Path(td) / "home"; (home / ".openclaw/logs").mkdir(parents=True)
    src = SRC.replace("/Volumes/MoreData", f"{td}/MoreData").replace("/Volumes/Data", f"{td}/Data")
    with mock.patch.dict(os.environ, {"HOME": str(home)}):
        exec(compile(src, "nova_voldata_audit_sandbox", "exec"), {"__name__": "nova_voldata_audit_sandbox"})
    return (home / ".openclaw/logs/voldata-audit.log").read_text()


def _fake_volumes(td):
    data = Path(td) / "Data"
    models = data / ".ollama/models"
    (models / "blobs").mkdir(parents=True)
    for b, sz in (("sha256-aaa", 10), ("sha256-bbb", 20), ("sha256-orphan", 300)):
        (models / "blobs" / b).write_bytes(b"x" * sz)
    man = models / "manifests/registry/lib/m"; man.mkdir(parents=True)
    (man / "latest").write_text(json.dumps({"config": {"digest": "sha256:aaa"}, "layers": [{"digest": "sha256:bbb"}]}))
    (man / "broken").write_text("{not json")
    (data / "backups/postgres").mkdir(parents=True)
    (data / "backups/postgres/d1.sql").write_bytes(b"y" * 5)
    (data / ".Spotlight-V100").mkdir()
    (Path(td) / "MoreData").mkdir()
    return data


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_audit_never_deletes(self):
        for bad in ("os.remove", "os.unlink", "shutil.rmtree", "rmdir", "subprocess", "ollama rm\")"):
            self.assertNotIn(bad, SRC.replace("`ollama rm`", ""))


class TestPerformance(unittest.TestCase):
    def test_human_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            H["human"](i * 7919)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_unreadable_volume_fails_open(self):
        # RETRY GAP: the scandir per volume is one-shot; a missing/unreadable volume is logged as ERROR and the
        # audit still finishes (AUDIT DONE), never crashes.
        with tempfile.TemporaryDirectory() as td:
            log = _run_sandboxed(td)
        self.assertIn("ollama models dir not found", log)
        self.assertIn("ERROR:", log)
        self.assertTrue(log.rstrip().endswith("AUDIT DONE"))

    def test_dirsize_swallows_lstat_errors(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "f").write_bytes(b"abc")
            with mock.patch.object(os, "lstat", side_effect=OSError("gone")):
                self.assertEqual(H["dirsize"](td), 0)


class TestUnit(unittest.TestCase):
    def test_human_units(self):
        self.assertEqual(H["human"](0), "0.0B")
        self.assertEqual(H["human"](1023), "1023.0B")
        self.assertEqual(H["human"](1024), "1.0K")
        self.assertEqual(H["human"](1024 ** 3 * 2), "2.0G")
        self.assertEqual(H["human"](1024 ** 5), "1.0P")

    def test_dirsize_counts_nested(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "a").write_bytes(b"x" * 10)
            Path(td, "s").mkdir(); Path(td, "s/b").write_bytes(b"x" * 5)
            self.assertEqual(H["dirsize"](td), 15)
            self.assertEqual(H["dirsize"](str(Path(td) / "missing")), 0)

    def test_skip_list_covers_system_dirs(self):
        for d in (".Spotlight-V100", ".fseventsd", ".Trashes"):
            self.assertIn(f"\"{d}\"", SRC)


class TestIntegration(unittest.TestCase):
    def test_orphan_detection_uses_manifests(self):
        with tempfile.TemporaryDirectory() as td:
            _fake_volumes(td)
            log = _run_sandboxed(td)
        self.assertIn("3 blobs, 330.0B total", log)
        self.assertIn("referenced by a model: 2", log)
        self.assertIn("ORPHANED (reclaimable via `ollama rm` leftovers): 1 blobs, 300.0B", log)


class TestFunctional(unittest.TestCase):
    def test_full_run_writes_all_sections(self):
        with tempfile.TemporaryDirectory() as td:
            _fake_volumes(td)
            log = _run_sandboxed(td)
        self.assertIn("##### PG dump backups", log)
        self.assertIn("5.0B, 1 entries", log)
        self.assertIn("--- sorted ---", log)
        self.assertNotIn(".Spotlight-V100", log.split("#####")[-1])
        self.assertTrue(log.rstrip().endswith("AUDIT DONE"))


class TestFrame(unittest.TestCase):
    def test_source_compiles_and_has_no_main_guard_side_effects_in_tests(self):
        compile(SRC, "nova_voldata_audit.py", "exec")
        self.assertTrue(callable(H["human"]) and callable(H["dirsize"]))

    def test_log_path_under_openclaw_logs(self):
        self.assertIn('os.path.expanduser("~/.openclaw/logs/voldata-audit.log")', SRC)


if __name__ == "__main__":
    unittest.main()
