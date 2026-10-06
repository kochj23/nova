#!/usr/bin/env python3
"""Tests for nova_osint_theharvester.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
theHarvester (subprocess), PG and nova_notify are mocked at module load; the log goes to a tempdir."""
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
SCRIPT = SCRIPTS / "nova_osint_theharvester.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="harvester-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("osint_theharvester_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oh = _load()
# module-level stubs: no theHarvester run, no PG, no notifications, log in a tempdir
oh.LOG_FILE = TMP / "harvester.log"
oh.notify = MagicMock()
oh.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: theHarvester stubbed")))
oh.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=types.SimpleNamespace(RealDictCursor=object))


class _Cur:
    def __init__(self, known=None):
        self.known = known or {}; self.sql = []; self._rows = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if sql.startswith("SELECT"):
            self._rows = [{"finding": f} for f in self.known.get((params[0], params[1]), [])]

    def fetchall(self):
        return self._rows

    def close(self): pass


def _harvest_writer(results):
    """Fake subprocess.run that writes theHarvester's JSON output file like the real tool."""
    def run(argv, **kw):
        domain = argv[argv.index("-d") + 1]
        out = Path(argv[argv.index("-f") + 1] + ".json")
        out.write_text(json.dumps(results.get(domain, {})))
        return types.SimpleNamespace(returncode=0)
    return run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", oh.DSN)

    def test_passive_sources_only_and_own_domains(self):
        self.assertEqual(oh.DOMAINS, ["digitalnoise.net", "nova.digitalnoise.net"])
        for active in ("dnsbrute", "-c", "--dns-brute", "shodan"):
            self.assertNotIn(active, oh.SOURCES.split(","))
        self.assertNotIn("shell=True", SRC)

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        cur = _Cur()
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(oh.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(oh, "run_harvester", return_value={"emails": {"x'); DROP TABLE t;--@d.test"}, "hosts": set()}):
            oh.main()
        ins = [p for s, p in cur.sql if s.startswith("INSERT INTO osint_findings")]
        self.assertIn("x'); DROP TABLE t;--@d.test", ins[0])


class TestPerformance(unittest.TestCase):
    def test_diff_10k_findings(self):
        hosts = {f"h{i}.digitalnoise.net" for i in range(10_000)}
        cur = _Cur({("digitalnoise.net", "host"): list(hosts)[:5000]})
        conn = MagicMock(); conn.cursor.return_value = cur
        t0 = time.perf_counter()
        with patch.object(oh.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(oh, "run_harvester", side_effect=lambda d: {"emails": set(), "hosts": hosts if d == "digitalnoise.net" else set()}):
            oh.main()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(oh.notify.call_args[1]["body"].splitlines()), 30)   # digest capped at 30


class TestRetry(unittest.TestCase):
    def test_harvester_failure_returns_empty_sets(self):
        # RETRY GAP: run_harvester — one subprocess attempt per weekly run; failure -> empty result, no crash
        oh.subprocess.run.reset_mock()
        self.assertEqual(oh.run_harvester("digitalnoise.net"), {"emails": set(), "hosts": set()})
        self.assertEqual(oh.subprocess.run.call_count, 1)

    def test_notify_failure_is_logged_not_raised(self):
        cur = _Cur(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(oh.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(oh, "run_harvester", return_value={"emails": {"a@d.test"}, "hosts": set()}), \
             patch.object(oh, "notify", side_effect=RuntimeError("bus down")):
            oh.main()
        self.assertIn("notify failed", oh.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_run_harvester_parses_and_cleans_up(self):
        with patch.object(oh.subprocess, "run", _harvest_writer({"digitalnoise.net": {"emails": ["a@d"], "hosts": None}})):
            out = oh.run_harvester("digitalnoise.net")
        self.assertEqual(out, {"emails": {"a@d"}, "hosts": set()})
        self.assertFalse(Path("/tmp/theharvester_digitalnoise_net.json").exists())   # temp output removed

    def test_log_writes_to_redirected_file(self):
        oh.log("hello-test")
        self.assertIn("hello-test", oh.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_new_findings_go_to_osint_and_shared_observations(self):
        cur = _Cur({("digitalnoise.net", "host"): ["old.digitalnoise.net"]})
        conn = MagicMock(); conn.cursor.return_value = cur
        data = {"digitalnoise.net": {"hosts": ["old.digitalnoise.net", "new.digitalnoise.net"]}}
        with patch.object(oh.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(oh.subprocess, "run", _harvest_writer(data)):
            oh.main()
        sev = {p[2]: p[3] for s, p in cur.sql if s.startswith("INSERT INTO osint_findings")}
        self.assertEqual(sev, {"old.digitalnoise.net": "info", "new.digitalnoise.net": "warning"})
        obs = [p for s, p in cur.sql if "shared_observations" in s]
        self.assertEqual(json.loads(obs[0][2])["new"], ["new.digitalnoise.net"])


class TestFunctional(unittest.TestCase):
    def test_new_emails_notify_once(self):
        oh.notify.reset_mock()
        cur = _Cur(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(oh.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(oh, "run_harvester", return_value={"emails": {"x@example.test"}, "hosts": set()}):
            oh.main()
        oh.notify.assert_called_once()
        self.assertEqual(oh.notify.call_args[1]["dedup_key"], "osint-theharvester-new")

    def test_nothing_new_is_silent(self):
        oh.notify.reset_mock()
        cur = _Cur({(d, "email"): ["k@d.test"] for d in oh.DOMAINS}); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(oh.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(oh, "run_harvester", return_value={"emails": {"k@d.test"}, "hosts": set()}):
            oh.main()
        oh.notify.assert_not_called()
        self.assertFalse(any("shared_observations" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script launches theHarvester + PG, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_osint_theharvester as m; print(callable(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
