#!/usr/bin/env python3
"""Tests for nova_osint_nuclei_sweep.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2         # noqa: F401
import psycopg2.extras  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_osint_nuclei_sweep.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nuclei_sweep_test_"))

import nova_notify  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("nns", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


ns = _load()
ns.LOG_FILE = TMP / "osint_nuclei_sweep.log"
ns.notify = MagicMock(return_value=True)
ns.subprocess = MagicMock(wraps=None)        # module-level guard: nuclei is never really launched
ns.subprocess.run = MagicMock(side_effect=RuntimeError("nuclei must be mocked per test"))

AMASS = "foo.example.com (FQDN) --> a_record --> 1.2.3.4 (IPAddress); bar.example.org (FQDN)"


def _discovery_conn(amass_rows, harv_rows):
    cur = MagicMock(); cur.fetchall.side_effect = [[{"finding": f} for f in amass_rows], [{"finding": f} for f in harv_rows]]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _nuclei(lines, rc=0):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv; seen["kw"] = kw
        seen["targets"] = Path(argv[argv.index("-l") + 1]).read_text()
        return subprocess.CompletedProcess(argv, rc, stdout="\n".join(lines), stderr="")
    return MagicMock(side_effect=run), seen


def _finding(host, sev, name="Exposed panel"):
    return json.dumps({"host": host, "matched-at": f"https://{host}/x", "template-id": "t1",
                       "info": {"name": name, "severity": sev}})


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_safe_template_tags_and_no_shell(self):
        tags = set(ns.NUCLEI_SAFE_TAGS.split(","))
        self.assertTrue(tags.isdisjoint({"intrusive", "dos", "fuzz", "brute-force", "rce", "sqli"}))
        self.assertNotIn("shell=True", SRC)
        run, seen = _nuclei([])
        with patch.object(ns, "discovered_hosts", return_value={"a.example.com"}), \
             patch.object(ns.subprocess, "run", run), _quiet():
            ns.main()
        self.assertIsInstance(seen["argv"], list)
        self.assertEqual(seen["argv"][seen["argv"].index("-rate-limit") + 1], "50")
        self.assertIn("timeout", seen["kw"])

    def test_record_sql_is_parameterized(self):
        cur = MagicMock(); cur.fetchone.return_value = None
        conn = MagicMock(); conn.cursor.return_value = cur
        evil = "x' OR '1'='1"
        with patch.object(ns.psycopg2, "connect", return_value=conn):
            ns.record(evil, "t", evil, "warning", {})
        for c in cur.execute.call_args_list:
            self.assertNotIn(evil, c[0][0])


class TestPerformance(unittest.TestCase):
    def test_fqdn_extraction_10k_rows(self):
        conn, _ = _discovery_conn([AMASS.replace("foo", f"h{i}") for i in range(10_000)], [])
        t0 = time.perf_counter()
        with patch.object(ns.psycopg2, "connect", return_value=conn):
            hosts = ns.discovered_hosts()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(hosts), 10_001)


class TestRetry(unittest.TestCase):
    def test_nuclei_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: main()/subprocess.run(nuclei) — one attempt; a failed run yields 0 findings, no page, temp file removed
        run, seen = _nuclei([], rc=2)
        ns.notify.reset_mock()
        with patch.object(ns, "discovered_hosts", return_value={"a.example.com"}), \
             patch.object(ns.subprocess, "run", run), patch.object(ns, "record") as rec, _quiet():
            ns.main()
        self.assertEqual(run.call_count, 1)
        rec.assert_not_called(); ns.notify.assert_not_called()
        self.assertFalse(Path(seen["argv"][seen["argv"].index("-l") + 1]).exists())


class TestUnit(unittest.TestCase):
    def test_fqdn_regex(self):
        found = [m.group(1) for m in ns.FQDN_RE.finditer(AMASS)]
        self.assertEqual(found, ["foo.example.com", "bar.example.org"])
        self.assertEqual(list(ns.FQDN_RE.finditer("1.2.3.4 (IPAddress)")), [])

    def test_discovered_hosts_filters_junk(self):
        conn, _ = _discovery_conn([AMASS], ["  mail.example.net ", "localhost", ""])
        with patch.object(ns.psycopg2, "connect", return_value=conn):
            self.assertEqual(ns.discovered_hosts(), {"foo.example.com", "bar.example.org", "mail.example.net"})

    def test_record_downgrades_repeat_findings_to_info(self):
        cur = MagicMock(); cur.fetchone.return_value = (1,)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(ns.psycopg2, "connect", return_value=conn):
            self.assertFalse(ns.record("h", "t", "f", "critical", {"a": 1}))
        params = cur.execute.call_args_list[1][0][1]
        self.assertEqual(params[3], "info")
        self.assertEqual(json.loads(params[4]), {"a": 1})

    def test_severity_map(self):
        self.assertEqual(ns.SEVERITY_MAP["high"], "critical")
        self.assertEqual(ns.SEVERITY_MAP.get("weird", "info"), "info")


class TestIntegration(unittest.TestCase):
    def test_reads_and_writes_osint_findings(self):
        self.assertIn("FROM osint_findings WHERE tool='amass'", SRC)
        self.assertIn("tool='theharvester'", SRC)
        self.assertIn("INSERT INTO osint_findings", SRC)
        self.assertIn("from nova_notify import notify", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_records_and_pages_new_notable_only(self):
        run, seen = _nuclei([_finding("a.example.com", "high"), "not json", _finding("b.example.com", "info"),
                             _finding("c.example.com", "medium", "Old one")])
        rec = MagicMock(side_effect=[True, True, False])
        ns.notify.reset_mock()
        with patch.object(ns, "discovered_hosts", return_value={"b.example.com", "a.example.com"}), \
             patch.object(ns.subprocess, "run", run), patch.object(ns, "record", rec), _quiet():
            ns.main()
        self.assertEqual(seen["targets"], "a.example.com\nb.example.com")
        self.assertEqual(rec.call_count, 3)
        body = ns.notify.call_args.kwargs["body"]
        self.assertEqual(body, "[CRITICAL] a.example.com: Exposed panel")
        self.assertEqual(ns.notify.call_args.kwargs["level"], "warning")

    def test_no_hosts_skips_scan(self):
        run, _ = _nuclei([])
        with patch.object(ns, "discovered_hosts", return_value=set()), patch.object(ns.subprocess, "run", run), _quiet():
            ns.main()
        run.assert_not_called()
        self.assertIn("No discovered hosts", ns.LOG_FILE.read_text())


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run queries PG and launches nuclei, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_osint_nuclei_sweep"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
