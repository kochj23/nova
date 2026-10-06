#!/usr/bin/env python3
"""Tests for nova_adt_matter_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_adt_matter_watch.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("adt_matter_watch_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import nova_notify  # noqa: E402 — import-clean; its notify() is patched per test

aw = _load()


def _dnssd(*names, rmv=()):
    rows = ["Timestamp     A/R    Flags  if Domain   Service Type   Instance Name"]
    rows += [f"15:41:24.656  Add        3  26 local.   _matter._tcp.  {n}" for n in names]
    rows += [f"15:41:24.700  Rmv        3  26 local.   _matter._tcp.  {n}" for n in rmv]
    return "\n".join(rows) + "\n"


class _Env:
    """Redirect STATE to a tempdir and stub nova_notify.notify (the real module) for one test."""
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.state = Path(self.td.name) / "cfg" / "adt.json"
        self._old = aw.STATE
        aw.STATE = self.state
        self._p = patch.object(nova_notify, "notify", return_value=True)
        self.notify = self._p.start()
        return self

    def __exit__(self, *a):
        aw.STATE = self._old
        self._p.stop()
        self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_subprocess_is_argv_list_never_shell(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(aw.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(stdout="")
            aw.browse("_matter._tcp; rm -rf /")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["dns-sd", "-B"])
        self.assertEqual(argv[2], "_matter._tcp; rm -rf /")   # passed as one inert argv element


class TestPerformance(unittest.TestCase):
    def test_parse_10k_lines_fast(self):
        out = _dnssd(*[f"DEV-{i}" for i in range(10_000)])
        t0 = time.perf_counter()
        got = aw._parse_instances(out)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(got), 10_000)


class TestRetry(unittest.TestCase):
    def test_browse_fails_open_on_error(self):
        # RETRY GAP: browse() — one dns-sd attempt; any failure yields an empty set, never raises
        with patch.object(aw.subprocess, "run", side_effect=OSError("no dns-sd")) as run:
            self.assertEqual(aw.browse("_matter._tcp"), set())
        self.assertEqual(run.call_count, 1)

    def test_browse_timeout_uses_partial_output(self):
        exc = subprocess.TimeoutExpired(cmd="dns-sd", timeout=8, output=_dnssd("X1").encode())
        with patch.object(aw.subprocess, "run", side_effect=exc):
            self.assertEqual(aw.browse("_matter._tcp"), {"X1"})

    def test_notify_failure_does_not_crash_main(self):
        with _Env() as env, patch.object(aw, "browse", side_effect=[{"NEW-1"}, set()]):
            env.notify.side_effect = RuntimeError("bus down")
            aw.main()
            self.assertTrue(env.state.exists())


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        aw.selftest()

    def test_parse_edges(self):
        self.assertEqual(aw._parse_instances(""), set())
        self.assertEqual(aw._parse_instances(_dnssd(rmv=["G"])), set())
        self.assertEqual(aw._parse_instances(_dnssd("Name With Spaces")), {"Name With Spaces"})


class TestIntegration(unittest.TestCase):
    def test_browses_both_matter_service_types(self):
        with _Env(), patch.object(aw, "browse", return_value=set()) as b:
            aw.main()
        self.assertEqual([c.args[0] for c in b.call_args_list], ["_matter._tcp", "_matterc._udp"])

    def test_uses_shared_notifier(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("hooks.slack.com", SRC)


class TestFunctional(unittest.TestCase):
    def test_new_device_alerts_once_and_persists(self):
        with _Env() as env, patch.object(aw, "browse", side_effect=[{"NEW-1"} | aw.BASELINE_KNOWN, set()] * 2):
            aw.main()
            self.assertEqual(env.notify.call_count, 1)
            kw = env.notify.call_args.kwargs
            self.assertEqual(kw["level"], "warning")
            self.assertTrue(kw["dedup_key"].startswith("adt-matter-NEW-1"))
            st = json.loads(env.state.read_text())
            self.assertIn("NEW-1", st["fired"])
            aw.main()                                   # second run: already fired, silent
            self.assertEqual(env.notify.call_count, 1)

    def test_baseline_only_is_silent(self):
        with _Env() as env, patch.object(aw, "browse", side_effect=[set(aw.BASELINE_KNOWN), set()]):
            aw.main()
            env.notify.assert_not_called()

    def test_corrupt_state_recovers(self):
        with _Env() as env, patch.object(aw, "browse", side_effect=[set(), {"COMM-1"}]):
            env.state.parent.mkdir(parents=True)
            env.state.write_text("{not json")
            aw.main()
            self.assertEqual(env.notify.call_count, 1)
            self.assertEqual(json.loads(env.state.read_text())["last_com"], ["COMM-1"])


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--selftest"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(subprocess, "run") as run:
            _load()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
