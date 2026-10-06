#!/usr/bin/env python3
"""Tests for nova_config_drift.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import hashlib
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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    cd = _load("config_drift_t", SCRIPTS / "nova_config_drift.py")
SRC = (SCRIPTS / "nova_config_drift.py").read_text()
cd.notify = MagicMock()
cd.subprocess = MagicMock()   # no psql / launchctl from a test
cd.subprocess.run.side_effect = RuntimeError("subprocess.run not mocked in test")


class _Env:
    """A temp LaunchAgents dir + a recording fake psql."""
    def __init__(self, plists, baseline=None, loaded=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for n, c in plists.items():
            (self.dir / n).write_text(c)
        self.baseline = baseline or {}; self.loaded = set(loaded); self.queries = []

    def sql(self, q):
        self.queries.append(q)
        out = "\n".join(f"launchd:{k}|{v}" for k, v in self.baseline.items()) if q.startswith("SELECT") else ""
        return SimpleNamespace(stdout=out, returncode=0)

    def __enter__(self):
        self.p = [patch.object(cd, "LIVE_DIR", self.dir), patch.object(cd, "_sql", side_effect=self.sql),
                  patch.object(cd, "_loaded", return_value=self.loaded)]
        for p in self.p:
            p.start()
        cd.notify.reset_mock()
        return self

    def __exit__(self, *a):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_redaction_scrubs_email_and_slack_tokens(self):
        addr = "someone" + "@" + "example.org"
        out = cd._redact(f"<string>{addr}</string><string>xoxb-1234567890-abcdef</string>")
        self.assertNotIn(addr, out)
        self.assertIn("__EMAIL__", out)
        self.assertIn("__TOKEN__", out)

    def test_bless_escapes_single_quotes_in_sql(self):
        with _Env({"com.nova.o'brien.plist": "it's"}) as env, redirect_stdout(io.StringIO()):
            cd.bless()
        ins = [q for q in env.queries if q.startswith("INSERT")][0]
        self.assertIn("launchd:com.nova.o''brien.plist", ins)
        self.assertIn("'it''s'", ins)
        self.assertNotIn("o'brien", ins)


class TestPerformance(unittest.TestCase):
    def test_redact_10k(self):
        blob = "<key>Label</key><string>net.digitalnoise.x</string>" * 3
        t0 = time.perf_counter()
        for _ in range(10_000):
            cd._redact(blob)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_launchctl_failure_fails_open(self):
        # RETRY GAP: _loaded — one launchctl call; failure returns an empty set, never raises
        with patch.object(cd.subprocess, "run", side_effect=OSError("no launchctl")) as r:
            self.assertEqual(cd._loaded(), set())
        self.assertEqual(r.call_count, 1)

    def test_psql_failure_reads_as_no_baseline(self):
        # RETRY GAP: _sql — one psql call; empty stdout means "no baseline", exit 0, no alert
        with _Env({"com.nova.a.plist": "x"}) as env, redirect_stdout(io.StringIO()) as out:
            env.baseline = {}
            self.assertEqual(cd.check(), 0)
        self.assertIn("no baseline", out.getvalue())
        cd.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_loaded_parses_launchctl_list(self):
        out = "PID\tStatus\tLabel\n12\t0\tnet.digitalnoise.a\n-\t0\tcom.apple.x\nbadline\n"
        with patch.object(cd.subprocess, "run", return_value=SimpleNamespace(stdout=out)):
            self.assertEqual(cd._loaded(), {"net.digitalnoise.a"})

    def test_live_filters_prefixes(self):
        with _Env({"com.nova.a.plist": "a", "com.apple.b.plist": "b", "net.digitalnoise.c.txt": "c"}):
            self.assertEqual(set(cd._live()), {"com.nova.a.plist"})


class TestIntegration(unittest.TestCase):
    def test_bless_then_check_is_clean(self):
        with _Env({"com.nova.a.plist": "A", "net.digitalnoise.b.plist": "B"}, loaded={"com.nova.a"}) as env, \
                redirect_stdout(io.StringIO()) as out:
            cd.bless()
            env.baseline = {n: _h(c) for n, c in cd._live().items()}
            self.assertEqual(cd.check(), 0)
        self.assertIn("no drift", out.getvalue())
        self.assertTrue(any("config_snapshots" in q for q in env.queries))
        cd.notify.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_all_four_drift_kinds_alert_once(self):
        base = {"com.nova.mod.plist": _h("old"), "com.nova.gone.plist": _h("x")}
        with _Env({"com.nova.mod.plist": "new", "com.nova.new.plist": "n"}, baseline=base,
                  loaded={"com.nova.ghost"}), redirect_stdout(io.StringIO()):
            self.assertEqual(cd.check(), 0)          # drift is a finding, not a failure
        title = cd.notify.call_args.args[0]
        body = cd.notify.call_args.kwargs["body"]
        self.assertIn("4 launchd", title)
        for k in ("MODIFIED", "UNTRACKED", "MISSING", "ORPHAN"):
            self.assertIn(k, body)
        self.assertEqual(cd.notify.call_args.kwargs["dedup_key"], "config-drift")

    def test_main_bless_flag_exits_zero(self):
        with _Env({}), patch.object(sys, "argv", ["x", "--bless"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                cd.main()
        self.assertEqual(e.exception.code, 0)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2,subprocess;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "subprocess.run=lambda *a,**k:(_ for _ in ()).throw(SystemExit(9));"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_config_drift.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
