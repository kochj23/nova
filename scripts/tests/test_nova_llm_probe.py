#!/usr/bin/env python3
"""Tests for cluster/nova_llm_probe.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Focus: the 2026-10-17 job attribution — which process has a model request
in flight on this machine's LLM ports (clients) and which of its processes call LLMs elsewhere (calls),
named by script/app only, never arguments. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
PROBE = SCRIPTS / "cluster" / "nova_llm_probe.py"
_spec = importlib.util.spec_from_file_location("nova_llm_probe", PROBE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)
SRC = PROBE.read_text()


def conn(l, r, rx=0, tx=0, pid=None, name=""):
    return {"l": l, "r": r, "rx": rx, "tx": tx, "pid": pid, "name": name}


class TestSecurity(unittest.TestCase):
    def test_labels_never_carry_arguments(self):
        for cmd in ("/usr/bin/python3 /x/nova_journal.py --prompt 'my secret' --token abc123",
                    "python3 -m nova_gateway --api-key=sk-123", "node /srv/app/index.js --password hunter2"):
            lab = probe.proc_label(cmd)
            self.assertNotRegex(lab, r"secret|abc123|sk-123|hunter2|prompt|token|password")

    def test_label_charset_is_restricted(self):
        lab = probe.proc_label("/opt/<b>evil</b>;rm -rf")
        self.assertRegex(lab, r"^[A-Za-z0-9._ -]{1,40}$")
        self.assertEqual(probe.proc_label("/x/" + "a" * 90), "a" * 40)

    def test_no_hardcoded_tokens(self):
        self.assertIsNone(re.search(r"(?i)(token|secret)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]", SRC))

    def test_ps_called_without_shell(self):
        with mock.patch.object(probe.subprocess, "run", return_value=mock.Mock(stdout="python3 x.py")) as run:
            probe._PROC.clear()
            probe.label_of(4242, "Python")
        args, kw = run.call_args
        self.assertIsInstance(args[0], tuple)
        self.assertNotIn("shell", kw)


class TestPerformance(unittest.TestCase):
    def test_track_10k_connections(self):
        conns = [conn(f"127.0.0.1:11434", f"127.0.0.1:{i}", rx=900, tx=0) for i in range(10000)]
        st = {"primed": True}
        t = time.perf_counter()
        out = probe.track_clients(conns, {11434}, st)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(len(out), 10000)

    def test_who_caps_payload(self):
        conns = [conn("127.0.0.1:11434", f"10.0.0.1:{i}", rx=900) for i in range(200)]
        conns += [conn(f"10.0.0.2:{i}", "10.0.0.3:11434", name="x") for i in range(200)]
        with mock.patch.object(probe, "label_of", return_value="x"):
            w = probe.who({11434}, conns, {"primed": True})
        self.assertLessEqual(len(w["clients"]), 32)
        self.assertLessEqual(len(w["calls"]), 64)


class TestRetry(unittest.TestCase):
    def test_connection_table_failure_fails_open(self):
        # RETRY GAP: connections — no retry; the next 1 s sample is the retry. A failure must not stop the push.
        with mock.patch.object(probe, "who", side_effect=OSError("netstat gone")), \
                mock.patch.object(probe, "runners", return_value=[]), \
                mock.patch.object(probe, "api_names", return_value={}), \
                mock.patch.object(probe, "http", side_effect=OSError("no ollama")), \
                mock.patch.object(probe, "log"):
            snap = probe.snapshot({"node": "n", "ollama_ports": [11434]})
        self.assertEqual((snap["clients"], snap["calls"]), ([], []))
        self.assertFalse(snap["llm"])

    def test_sh_swallows_failures(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=OSError("x")):
            self.assertEqual(probe.sh("netstat"), "")


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(probe.selftest(), 0)

    def test_addr_forms(self):
        self.assertEqual(probe._addr("192.168.1.6.65246"), "192.168.1.6:65246")
        self.assertEqual(probe._addr("[::ffff:192.168.1.2]:11434"), "192.168.1.2:11434")
        self.assertEqual(probe._addr("[::1]:5"), "::1:5")
        self.assertEqual(probe._addr("fe80::1%lo0.11434"), "fe80::1:11434")

    def test_generic_script_named_by_folder(self):
        self.assertEqual(probe.proc_label("python3 server.py", cwd="/a/nova-control-web/"), "nova-control-web")

    def test_empty_inputs(self):
        self.assertEqual(probe.parse_netstat(""), [])
        self.assertEqual(probe.parse_ss(""), [])
        self.assertEqual(probe.track_clients([], {11434}, {}), [])


class TestIntegration(unittest.TestCase):
    def test_snapshot_shape_for_hub(self):
        with mock.patch.object(probe, "runners", return_value=[]), \
                mock.patch.object(probe, "api_names", return_value={}), \
                mock.patch.object(probe, "http", return_value={"models": [{"name": "qwen3:8b", "size": 5e9, "size_vram": 5e9}]}), \
                mock.patch.object(probe, "who", return_value={"clients": [{"peer": "1.2.3.4:5", "port": 11434, "proc": None}],
                                                              "calls": [], "ips": ["1.2.3.9"]}):
            snap = probe.snapshot({"node": "n", "ollama_ports": [11434]})
        self.assertTrue(snap["llm"])
        self.assertEqual(snap["probe"], "1.1")
        for k in ("clients", "calls", "ips", "runners", "loaded"):
            self.assertIn(k, snap)
        json.dumps(snap)

    def test_listen_ports_include_standalone_runners(self):
        seen = {}
        with mock.patch.object(probe, "runners", return_value=[{"port": 11435, "blob": "b", "models_dir": "", "ctx": 1,
                                                                 "standalone": True, "pid": 1}]), \
                mock.patch.object(probe, "api_names", return_value={}), \
                mock.patch.object(probe, "http", side_effect=OSError()), \
                mock.patch.object(probe, "who", side_effect=lambda listen: seen.setdefault("l", listen) and
                                  {"clients": [], "calls": [], "ips": []}):
            probe.snapshot({"node": "n", "ollama_ports": [11434], "llm_ports": [5050]})
        self.assertEqual(seen["l"], {11434, 11435, 5050})


class TestFunctional(unittest.TestCase):
    def test_streaming_request_named_then_gone(self):
        st = {}
        server = lambda rx, tx: conn("127.0.0.1:11434", "127.0.0.1:777", rx, tx, 1, "ollama")   # noqa: E731
        client = conn("127.0.0.1:777", "127.0.0.1:11434", 0, 0, 99, "Python")
        with mock.patch.object(probe, "label_of", side_effect=lambda pid, name="": "nova_journal" if pid == 99 else None):
            self.assertEqual(probe.who({11434}, [server(0, 0), client], st)["clients"], [])          # learn
            got = probe.who({11434}, [server(1200, 0), client], st)["clients"]
            self.assertEqual(got, [{"peer": "127.0.0.1:777", "port": 11434, "proc": "nova_journal"}])
            self.assertTrue(probe.who({11434}, [server(1200, 400), client], st)["clients"])          # streaming
            self.assertEqual(probe.who({11434}, [server(1200, 400), client], st)["clients"], [])     # done

    def test_main_without_token_exits_2(self):
        with mock.patch.object(sys, "argv", ["probe"]), \
                mock.patch("builtins.open", mock.mock_open(read_data='{"node": "n", "hub": "http://h"}')), \
                mock.patch.object(probe, "token", return_value=""), mock.patch.object(probe, "log"):
            self.assertEqual(probe.main(), 2)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(PROBE), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertTrue(callable(probe.main))


if __name__ == "__main__":
    unittest.main()
