#!/usr/bin/env python3
"""Tests for nova_strix_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script runs its poll loop at import time (no main()), so every test loads it with ssh
(subprocess.run), time.sleep, nova_notify.notify and nova_maintenance.stop mocked."""
import importlib.util
import io
import py_compile
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_strix_monitor.py"
SRC = PATH.read_text()

import nova_maintenance  # noqa: E402 — import-clean; stop() patched per run
import nova_notify  # noqa: E402 — import-clean; notify() patched per run


def _run(logs, alive_seq=(False,), summary="", ssh_exc=None, stop_exc=None, argv=("strix.log", "3000")):
    """Load (= run) the monitor. `logs` is a list of log snapshots returned per loop iteration."""
    logs, alive_seq, cmds = list(logs), list(alive_seq), []

    def fake_run(argv_, **kw):
        cmd = argv_[-1]; cmds.append((argv_, kw))
        if ssh_exc:
            raise ssh_exc
        if cmd.startswith("cat "):
            return types.SimpleNamespace(stdout=logs.pop(0) if logs else "")
        if cmd.startswith("pgrep"):
            return types.SimpleNamespace(stdout="yes\n" if (alive_seq.pop(0) if alive_seq else False) else "")
        return types.SimpleNamespace(stdout=summary)

    spec = importlib.util.spec_from_file_location("strix_monitor_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(subprocess, "run", side_effect=fake_run), patch.object(time, "sleep") as sl, \
            patch.object(nova_notify, "notify", return_value=True) as nt, \
            patch.object(nova_maintenance, "stop", side_effect=stop_exc) as stop, \
            patch.object(sys, "argv", ["nova_strix_monitor.py", *argv]), redirect_stdout(io.StringIO()) as out:
        spec.loader.exec_module(mod)
    return types.SimpleNamespace(mod=mod, notify=nt, stop=stop, sleep=sl, cmds=cmds, out=out.getvalue())


FINDING = "\x1b[31m[HIGH] SQL injection vulnerability in /login parameter user\x1b[0m"
NOISE = "DEBUG openai.agents Calling LLM with conversation_id=abc vulnerability"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_ssh_is_batchmode_argv_list(self):
        r = _run([""])
        argv, kw = r.cmds[0]
        self.assertEqual(argv[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["timeout"], 30)

    def test_post_cap_bounds_slack_volume(self):
        log = "\n".join(f"critical vulnerability number {i} found here" for i in range(100))
        r = _run([log])
        streamed = [c for c in r.notify.call_args_list if c.args[0].startswith("🔎")]
        self.assertEqual(len(streamed), r.mod.POST_CAP)


class TestPerformance(unittest.TestCase):
    def test_filter_10k_lines_fast(self):
        r = _run([""])
        lines = [FINDING if i % 2 else NOISE for i in range(10_000)]
        t0 = time.perf_counter()
        kept = [ln for ln in lines if not r.mod.DROP.search(ln) and r.mod.KEEP.search(r.mod.ANSI.sub("", ln))]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(kept), 5_000)


class TestRetry(unittest.TestCase):
    def test_ssh_failure_fails_open(self):
        # RETRY GAP: ssh() — one attempt per poll; an exception becomes "" so the run is treated as finished
        r = _run([], ssh_exc=OSError("host down"))
        self.assertIn("no executive summary captured", r.notify.call_args_list[0].kwargs["body"])
        self.assertIn("monitor done — 0 findings", r.out)

    def test_maintenance_stop_failure_swallowed(self):
        r = _run([""], stop_exc=RuntimeError("pg down"))
        titles = [c.args[0] for c in r.notify.call_args_list]
        self.assertTrue(any("COMPLETE" in t for t in titles))
        self.assertFalse(any("Maintenance window closed" in t for t in titles))


class TestUnit(unittest.TestCase):
    def test_regexes(self):
        r = _run([""])
        self.assertEqual(r.mod.ANSI.sub("", "\x1b[1;31mX\x1b[0m"), "X")
        self.assertTrue(r.mod.KEEP.search("CVE-2024-1234 present"))
        self.assertFalse(r.mod.KEEP.search("hello world line"))
        self.assertTrue(r.mod.DROP.search("Starting turn 4"))

    def test_argv_defaults(self):
        r = _run([""], argv=())
        self.assertTrue(r.mod.RUN_LOG.endswith("strix.log"))
        self.assertEqual(r.mod.PROC, "3000")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_notify_and_maintenance(self):
        r = _run([""])
        self.assertIs(r.mod.nova_notify, nova_notify)
        r.stop.assert_called_once()
        self.assertTrue(all(c.kwargs["category"] == "strix" for c in r.notify.call_args_list))


class TestFunctional(unittest.TestCase):
    def test_golden_path_streams_dedups_and_closes(self):
        r = _run([f"{FINDING}\n{NOISE}\nshort", f"{FINDING}\nexposed admin panel at /admin found"],
                 alive_seq=(True, False), summary="executive_summary: 2 highs")
        titles = [c.args[0] for c in r.notify.call_args_list]
        self.assertEqual(len([t for t in titles if t.startswith("🔎")]), 2)   # dup + noise + short dropped
        self.assertNotIn("\x1b", titles[0])
        self.assertEqual(r.sleep.call_count, 1)
        done = [c for c in r.notify.call_args_list if "COMPLETE" in c.args[0]][0]
        self.assertIn("2 findings streamed", done.kwargs["body"])
        self.assertIn("executive_summary: 2 highs", done.kwargs["body"])
        self.assertIn("Maintenance window closed", titles[-1])


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        py_compile.compile(str(PATH), doraise=True)

    def test_loop_terminates_when_run_not_alive(self):
        # Import == run (no __main__ guard by design: it is a one-shot launched per pentest).
        r = _run(["", "", ""], alive_seq=(True, True, False))
        self.assertEqual(r.sleep.call_count, 2)
        self.assertIn("monitor done", r.out)


if __name__ == "__main__":
    unittest.main()
