#!/usr/bin/env python3
"""Tests for nova_transport.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import builtins
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_transport.py"
SRC = SCRIPT.read_text()
FAKE_NAS = "/Volumes/nas/nova/trans"          # the first candidate nas_root() checks; never actually touched
LANDSCAPE = {"canary": "nova-core4", "nodes": {"nova-core4": {"nas": "", "scripts": ""},
                                                "nova-core3": {"nas": "", "scripts": ""},
                                                "nova-core2": {"nas": "", "scripts": ""}}}
_real_isdir, _real_open = os.path.isdir, builtins.open


def _fake_isdir(d):
    return True if d == FAKE_NAS else _real_isdir(d)


def _fake_open(p, *a, **k):
    if str(p).endswith("landscape.json"):
        return io.StringIO(json.dumps(LANDSCAPE))
    return _real_open(p, *a, **k)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.object(os.path, "isdir", _fake_isdir), mock.patch.object(builtins, "open", _fake_open):
        spec.loader.exec_module(mod)            # import-time nas_root()/landscape load, fully faked
    return mod


T = _load("transport_under_test", SCRIPT)


def _tree():
    """A fresh transport tree + per-node scripts dirs in a tempdir; rebinds TRANS/LAND for the test."""
    root = Path(tempfile.mkdtemp(prefix="transport-test-"))
    trans = root / "trans"
    land = json.loads(json.dumps(LANDSCAPE))
    for d in ("bin", "data", "cofiles", "log"):
        (trans / d).mkdir(parents=True)
    for n in land["nodes"]:
        (trans / "buffer" / n).mkdir(parents=True)
        (root / f"scripts-{n}").mkdir(); (root / f"nas-{n}").mkdir()
        land["nodes"][n] = {"nas": str(root / f"nas-{n}"), "scripts": str(root / f"scripts-{n}")}
    (trans / "bin" / "landscape.json").write_text(json.dumps(land))
    return root, trans, land


class _Base(unittest.TestCase):
    def setUp(self):
        self.root, self.trans, self.land = _tree()
        self._p = [mock.patch.object(T, "TRANS", str(self.trans)), mock.patch.object(T, "LAND", self.land)]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])
        self.src = self.root / "hello.py"; self.src.write_text("print('hi')\n")

    def release(self, name="Hello World", hook=None, to=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            T.cmd_release(types.SimpleNamespace(name=name, script=[str(self.src)], hook=hook, to=to))
        return re.search(r"RELEASE (NT\S+)", buf.getvalue()).group(1), buf.getvalue()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_checksum_is_verified_before_any_copy(self):
        tid, _ = self.release()
        (self.trans / "data" / tid / "hello.py").write_text("print('tampered')\n")
        with mock.patch.object(T.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            T.cmd_import(mock.Mock(node="nova-core4"))
        self.assertFalse((self.root / "scripts-nova-core4" / "hello.py").exists())
        self.assertTrue((self.trans / "buffer" / "nova-core4" / tid).exists())
        run.assert_not_called()
        self.assertIn("CHECKSUM FAIL", (self.trans / "log" / "SLOG").read_text())

    def test_hook_comes_from_the_control_file_only(self):
        self.assertIn('co["hook"]', SRC)                       # the operator's --hook line, run via /bin/sh -c
        self.assertNotIn("shell=True", SRC)

    def test_nothing_under_volumes_is_touched(self):
        tid, _ = self.release()
        T.cmd_promote(mock.Mock(id=tid))
        self.assertEqual({str(p) for p in Path(self.trans).rglob("*") if p.is_file()} & {FAKE_NAS}, set())
        self.assertTrue(all(str(p).startswith(str(self.root)) for p in Path(self.trans).rglob("*")))


class TestPerformance(_Base):
    def test_sha256_1mb_and_new_id_fast(self):
        big = self.root / "big.bin"; big.write_bytes(os.urandom(1_000_000))
        t0 = time.perf_counter()
        h = T.sha256(big)
        for i in range(200):
            T.new_id(f"Topic {i} with spaces & punctuation!")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(h), 64)


class TestRetry(_Base):
    def test_failed_hook_leaves_entry_in_buffer_for_next_run(self):
        # RETRY GAP: cmd_import() hook/copy are one-shot per run; the retry is the buffer entry being left in place
        tid, _ = self.release(hook="exit 1")
        cp = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch.object(T.subprocess, "run", return_value=cp) as run, redirect_stdout(io.StringIO()):
            T.cmd_import(mock.Mock(node="nova-core4"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["/bin/sh", "-c", "exit 1"])
        self.assertTrue((self.trans / "buffer" / "nova-core4" / tid).exists())
        self.assertIn("FAILED — left in buffer for retry", (self.trans / "log" / "SLOG").read_text())
        # the next run (hook now succeeding) drains it
        with mock.patch.object(T.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             redirect_stdout(io.StringIO()):
            T.cmd_import(mock.Mock(node="nova-core4"))
        self.assertFalse((self.trans / "buffer" / "nova-core4" / tid).exists())


class TestUnit(_Base):
    def test_sha256_and_new_id(self):
        self.assertEqual(T.sha256(self.src), __import__("hashlib").sha256(self.src.read_bytes()).hexdigest())
        tid = T.new_id("Hello, World! A very long transport name indeed")
        self.assertRegex(tid, r"^NT\d{8}-001-hello--world--a-very$")
        (self.trans / "cofiles" / "x.json").write_text("{}")
        self.assertIn("-002-", T.new_id("x"))

    def test_this_node_by_hostname_and_fallback(self):
        with mock.patch.object(T.socket, "gethostname", return_value="Nova-Core3.local"):
            self.assertEqual(T.this_node(), "nova-core3")
        with mock.patch.object(T.socket, "gethostname", return_value="novacore2"):
            self.assertEqual(T.this_node(), "nova-core2")
        with mock.patch.object(T.socket, "gethostname", return_value="laptop"):
            self.land["nodes"]["nova-core2"]["nas"] = "/nonexistent-xyz"
            self.land["nodes"]["nova-core4"]["nas"] = "/nonexistent-xyz"
            self.assertEqual(T.this_node(), "nova-core3")        # only node whose nas+scripts dirs exist here
            self.land["nodes"]["nova-core3"]["nas"] = "/nonexistent-xyz"
            with self.assertRaises(SystemExit):
                T.this_node()

    def test_release_requires_scripts_and_existing_files(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            T.cmd_release(types.SimpleNamespace(name="x", script=[], hook=None, to=None))
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            T.cmd_release(types.SimpleNamespace(name="x", script=["/nope/missing.py"], hook=None, to=None))

    def test_promote_unknown_and_import_unknown_node(self):
        with self.assertRaises(SystemExit):
            T.cmd_promote(mock.Mock(id="NT0-999-nope"))
        with self.assertRaises(SystemExit):
            T.cmd_import(mock.Mock(node="mars"))

    def test_status_and_list_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            T.cmd_list(None); T.cmd_status(None)
        out = buf.getvalue()
        self.assertIn("(no transports yet)", out); self.assertIn("canary: nova-core4", out)
        self.assertIn("(none)", out); self.assertIn("nova-core3   empty", out)
        tid, _ = self.release(to="nova-core3,nova-core4")
        buf = io.StringIO()
        with redirect_stdout(buf):
            T.cmd_list(None); T.cmd_status(None)
        self.assertIn(f'{tid}  "Hello World"  1f  targets=[\'nova-core3\', \'nova-core4\']', buf.getvalue())
        self.assertIn(f"nova-core4   {tid}", buf.getvalue())


class TestIntegration(_Base):
    def test_release_promote_import_chain(self):
        tid, _ = self.release(hook="echo ok")
        T.cmd_promote(mock.Mock(id=tid))
        for n in ("nova-core3", "nova-core2"):
            self.assertTrue((self.trans / "buffer" / n / tid).exists())
        with mock.patch.object(T.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             mock.patch.object(T.socket, "gethostname", return_value="nova-core3"), redirect_stdout(io.StringIO()):
            T.cmd_import(mock.Mock(node=None))                   # node resolved from hostname
        self.assertEqual((self.root / "scripts-nova-core3" / "hello.py").read_text(), "print('hi')\n")
        self.assertFalse((self.trans / "buffer" / "nova-core3" / tid).exists())
        log = (self.trans / "log" / "SLOG").read_text()
        self.assertEqual([l.split()[2] for l in log.splitlines()], ["RELEASE", "PROMOTE", "IMPORT", "IMPORT"])

    def test_main_dispatches_subcommands(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            T.main(["list"])
        self.assertIn("(no transports yet)", buf.getvalue())
        with self.assertRaises(SystemExit):
            T.main([])


class TestFunctional(_Base):
    def test_release_golden_path(self):
        tid, out = self.release()
        co = json.loads((self.trans / "cofiles" / f"{tid}.json").read_text())
        self.assertEqual((co["id"], co["name"], co["targets"], co["hook"]), (tid, "Hello World", "all", ""))
        self.assertEqual(co["files"], [{"name": "hello.py", "kind": "script", "sha256": T.sha256(self.src)}])
        self.assertTrue((self.trans / "data" / tid / "hello.py").exists())
        self.assertEqual([p.name for p in (self.trans / "buffer" / "nova-core4").iterdir()], [tid])   # canary only
        for n in ("nova-core3", "nova-core2"):
            self.assertEqual(list((self.trans / "buffer" / n).iterdir()), [])
        self.assertIn(f"RELEASE {tid} (1 files) queued to canary 'nova-core4'", (self.trans / "log" / "SLOG").read_text())
        self.assertIn(f"promote {tid}", out)

    def test_import_empty_buffer_is_a_noop(self):
        buf = io.StringIO()
        with mock.patch.object(T.subprocess, "run") as run, redirect_stdout(buf):
            T.cmd_import(mock.Mock(node="nova-core2"))
        self.assertIn("buffer empty", buf.getvalue()); run.assert_not_called()


class TestFrame(unittest.TestCase):
    PRELUDE = ("import os, io, json, builtins; ri, ro = os.path.isdir, builtins.open; "
               f"os.path.isdir = lambda d: True if d == {FAKE_NAS!r} else ri(d); "
               f"builtins.open = lambda p, *a, **k: io.StringIO(json.dumps({LANDSCAPE!r})) if str(p).endswith('landscape.json') else ro(p, *a, **k); ")

    def test_import_never_runs_the_cli(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", self.PRELUDE + "import nova_transport"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, "-c", self.PRELUDE + "import nova_transport; nova_transport.main(['--help'])"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: nova_transport.py", r.stdout)


if __name__ == "__main__":
    unittest.main()
