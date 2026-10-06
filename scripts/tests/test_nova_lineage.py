#!/usr/bin/env python3
"""Tests for nova_lineage.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import runpy
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_lineage.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load("lineage_under_test", SCRIPT)
L._clock_cache = "NTP-synced"          # never shell out to timedatectl/systemsetup/sntp from a test by accident


def _run_stub(script):
    """subprocess.run stand-in: `script` maps argv[0] -> CompletedProcess | Exception; records calls."""
    calls = []

    def run(argv, **kw):
        calls.append(argv[0])
        r = script.get(argv[0], FileNotFoundError(argv[0]))
        if isinstance(r, Exception):
            raise r
        return r
    return run, calls


def _cp(code, out=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def _detect(script):
    run, calls = _run_stub(script)
    with patch.object(L, "_clock_cache", None), patch.object(L.subprocess, "run", run):
        return L._detect_clock_source(), calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_subprocess_calls_are_fixed_argv_lists_without_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        argv0 = re.findall(r'subprocess\.run\(\s*\[\s*"([^"]+)"', SRC)
        self.assertEqual(sorted(argv0), ["sntp", "systemsetup", "timedatectl"])   # a closed allowlist, no caller input
        self.assertEqual(SRC.count("timeout="), 3)                                 # every call is bounded

    def test_caller_input_never_reaches_a_subprocess(self):
        run, calls = _run_stub({})
        with patch.object(L.subprocess, "run", run):
            st = L.lineage_stamp(substrate="x; rm -rf /", capture_point="$(id)", clock_source="unverified")
        self.assertEqual(calls, [])                        # explicit clock_source short-circuits detection entirely
        self.assertEqual(st["substrate"], "x; rm -rf /")   # stored verbatim as data, never executed


class TestPerformance(unittest.TestCase):
    def test_stamp_and_line_fast_on_10k(self):
        run, calls = _run_stub({})
        with patch.object(L.subprocess, "run", run):
            t0 = time.perf_counter()
            for i in range(10_000):
                L.lineage_line(L.lineage_stamp(substrate="deterministic (no model)", value_date=date(2026, 1, 1)))
            dt = time.perf_counter() - t0
        self.assertLess(dt, 2.0)
        self.assertEqual(calls, [])                        # the clock cache means zero subprocess calls on the hot path


class TestRetry(unittest.TestCase):
    def test_every_probe_failing_returns_unverified_without_raising(self):
        # RETRY GAP: _detect_clock_source — each probe is tried once; all three failing -> 'unverified', never an exception
        result, calls = _detect({})
        self.assertEqual(result, "unverified")
        self.assertEqual(calls, ["timedatectl", "systemsetup", "sntp"])

    def test_fallback_chain_fails_twice_then_succeeds(self):
        result, calls = _detect({"timedatectl": subprocess.TimeoutExpired("timedatectl", 2),
                                 "systemsetup": _cp(1, "You need administrator access"), "sntp": _cp(0, "+0.001 +/- 0.01")})
        self.assertEqual(result, "NTP-synced")
        self.assertEqual(len(calls), 3)

    def test_result_is_cached_so_a_second_call_makes_no_attempt(self):
        run, calls = _run_stub({"timedatectl": _cp(0, "yes\n")})
        with patch.object(L, "_clock_cache", None), patch.object(L.subprocess, "run", run):
            self.assertEqual(L._detect_clock_source(), "NTP-synced")
            self.assertEqual(L._detect_clock_source(), "NTP-synced")
        self.assertEqual(calls, ["timedatectl"])


class TestUnit(unittest.TestCase):
    def test_clock_parsing(self):
        self.assertEqual(_detect({"timedatectl": _cp(0, "yes\n")})[0], "NTP-synced")
        self.assertEqual(_detect({"timedatectl": _cp(0, "no\n")})[0], "NTP-unsynced")
        self.assertEqual(_detect({"systemsetup": _cp(0, "Network Time: On\n")})[0], "NTP-synced")
        self.assertEqual(_detect({"systemsetup": _cp(0, "Network Time: Off\n")})[0], "NTP-unsynced")
        self.assertEqual(_detect({"sntp": _cp(1, "")})[0], "unverified")

    def test_stamp_value_date_forms(self):
        with patch.object(L, "_clock_cache", "NTP-synced"):
            self.assertEqual(L.lineage_stamp(value_date=date(2026, 9, 14))["value_date"], "2026-09-14")
            self.assertEqual(L.lineage_stamp(value_date="2026-09-14")["value_date"], "2026-09-14")
            self.assertEqual(L.lineage_stamp(value_date=datetime(2026, 9, 14, 3, 4))["value_date"], "2026-09-14T03:04:00")
            today = L.lineage_stamp()["value_date"]
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")

    def test_stamp_defaults_and_overrides(self):
        with patch.object(L, "_clock_cache", "NTP-synced"):
            st = L.lineage_stamp()
            self.assertEqual(st["substrate"], L.DEFAULT_SUBSTRATE)
            self.assertEqual(st["capture_point"], "at send")
            self.assertEqual(st["clock_source"], "NTP-synced")
            st2 = L.lineage_stamp(substrate="deterministic (no model)", capture_point="at write", clock_source="unverified")
        self.assertEqual((st2["substrate"], st2["capture_point"], st2["clock_source"]),
                         ("deterministic (no model)", "at write", "unverified"))
        self.assertEqual(st2["host"], L.socket.gethostname())

    def test_line_format(self):
        st = {"value_date": "2026-09-14", "host": "nova-core", "clock_source": "NTP-synced",
              "capture_point": "at send", "substrate": "Claude Fable 5.1"}
        self.assertEqual(L.lineage_line(st), "value 2026-09-14 / clock: nova-core NTP-synced / "
                                             "capture point: at send / substrate: Claude Fable 5.1")

    def test_line_rejects_a_stamp_missing_fields(self):
        with self.assertRaises(KeyError):
            L.lineage_line({"value_date": "2026-01-01"})


class TestIntegration(unittest.TestCase):
    def test_stamp_feeds_line_and_carries_the_documented_keys(self):
        with patch.object(L, "_clock_cache", "NTP-unsynced"):
            st = L.lineage_stamp(substrate="deterministic (no model)", capture_point="at detection", value_date="2026-10-05")
        self.assertEqual(set(st), {"captured_at", "value_date", "host", "clock_source", "capture_point", "substrate"})
        self.assertEqual(datetime.fromisoformat(st["captured_at"]).utcoffset().total_seconds(), 0)   # UTC, not local
        line = L.lineage_line(st)
        self.assertEqual(line, L.lineage_line(**{k: st[k] for k in ("substrate", "capture_point", "clock_source", "value_date")}))
        self.assertIn(f"clock: {st['host']} NTP-unsynced", line)

    def test_stamp_embeds_in_memory_metadata_as_json(self):
        with patch.object(L, "_clock_cache", "NTP-synced"):
            meta = {"lineage": L.lineage_stamp(value_date=date(2026, 1, 2))}
        self.assertEqual(json.loads(json.dumps(meta))["lineage"]["value_date"], "2026-01-02")

    def test_shared_module_is_imported_by_the_fleet_not_copied(self):
        consumers = [p.name for p in SCRIPTS.glob("nova_*.py") if p.name != "nova_lineage.py" and "nova_lineage" in p.read_text()]
        self.assertGreaterEqual(len(consumers), 3)
        for name in consumers:
            self.assertRegex((SCRIPTS / name).read_text(), r"(from nova_lineage import|import nova_lineage)", name)


class TestFunctional(unittest.TestCase):
    def test_main_prints_a_json_stamp_then_the_signature_line(self):
        run, calls = _run_stub({"timedatectl": _cp(0, "yes\n")})
        with patch.object(subprocess, "run", run), redirect_stdout(io.StringIO()) as out:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        text = out.getvalue()
        body, last = text.rstrip("\n").rsplit("\n", 1)
        st = json.loads(body)
        self.assertEqual(st["clock_source"], "NTP-synced")
        self.assertEqual(last, L.lineage_line(st))
        self.assertEqual(calls, ["timedatectl"])

    def test_main_still_prints_when_every_clock_probe_breaks(self):
        run, calls = _run_stub({})
        with patch.object(subprocess, "run", run), redirect_stdout(io.StringIO()) as out:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertIn("clock: ", out.getvalue())
        self.assertIn("unverified", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_lineage"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
