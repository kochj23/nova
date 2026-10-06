#!/usr/bin/env python3
"""Tests for nova_osint_amass.py — the 7 house categories (Security, Performance, Retry, Unit,
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_osint_amass.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="osint_amass_"))


def _load():
    spec = importlib.util.spec_from_file_location("osint_amass", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LOG_FILE = TMP / "amass.log"
    mod.notify = MagicMock()
    return mod


oa = _load()


class _Cur:
    def __init__(self, known=()):
        self.known = [{"finding": k} for k in known]; self.sql = []
    def execute(self, sql, params=None): self.sql.append((sql, params))
    def fetchall(self): return self.known
    def close(self): pass


def _main(found_by_domain, known=()):
    cur = _Cur(known)
    conn = MagicMock(); conn.cursor.return_value = cur
    oa.notify.reset_mock()
    with patch.object(oa.psycopg2, "connect", return_value=conn), \
         patch.object(oa, "run_amass", side_effect=lambda d: set(found_by_domain.get(d, ()))), redirect_stdout(io.StringIO()):
        oa.main()
    return cur, conn


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_enumeration_is_passive_only(self):
        with patch.object(oa.subprocess, "run", return_value=MagicMock(stdout="")) as run:
            oa.run_amass("digitalnoise.net")
        argv = run.call_args.args[0]
        self.assertIn("-passive", argv)
        self.assertNotIn("-brute", argv)
        self.assertNotIn("-active", argv)

    def test_hostile_subdomain_is_a_param(self):
        cur, _ = _main({"digitalnoise.net": ["x'); --injected.digitalnoise.net"]})
        for sql, params in cur.sql:
            self.assertNotIn("--injected", sql)
        self.assertIn("x'); --injected.digitalnoise.net", cur.sql[1][1])


class TestPerformance(unittest.TestCase):
    def test_diff_over_10k_subdomains(self):
        found = [f"h{i}.digitalnoise.net" for i in range(10_000)]
        t0 = time.perf_counter()
        cur, _ = _main({"digitalnoise.net": found}, known=found[:9_990])
        self.assertLess(time.perf_counter() - t0, 3.0)
        severities = [p[2] for s, p in cur.sql if "INSERT INTO osint_findings" in s]
        self.assertEqual(severities.count("warning"), 10)


class TestRetry(unittest.TestCase):
    def test_amass_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: run_amass/subprocess.run — one attempt per domain, empty set on failure
        with patch.object(oa.subprocess, "run", side_effect=subprocess.TimeoutExpired("amass", 360)) as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(oa.run_amass("digitalnoise.net"), set())
        self.assertEqual(run.call_count, 1)

    def test_notify_failure_is_swallowed(self):
        oa.notify.side_effect = RuntimeError("slack down")
        try:
            cur = _Cur(); conn = MagicMock(); conn.cursor.return_value = cur
            with patch.object(oa.psycopg2, "connect", return_value=conn), \
                 patch.object(oa, "run_amass", return_value={"new.digitalnoise.net"}), redirect_stdout(io.StringIO()) as out:
                oa.main()
        finally:
            oa.notify.side_effect = None
        self.assertIn("notify failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_run_amass_parses_lines(self):
        with patch.object(oa.subprocess, "run", return_value=MagicMock(stdout="a.x\n\n  b.x  \na.x\n")):
            self.assertEqual(oa.run_amass("x"), {"a.x", "b.x"})

    def test_log_writes_to_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            oa.log("hello")
        self.assertIn("[osint-amass] hello", oa.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_known_set_read_per_domain_from_osint_findings(self):
        cur, _ = _main({"digitalnoise.net": ["a"], "nova.digitalnoise.net": ["b"]})
        reads = [p for s, p in cur.sql if s.startswith("SELECT DISTINCT finding FROM osint_findings")]
        self.assertEqual(reads, [("digitalnoise.net",), ("nova.digitalnoise.net",)])

    def test_new_finding_feeds_shared_observations(self):
        cur, _ = _main({"digitalnoise.net": ["new.digitalnoise.net", "old.digitalnoise.net"]}, known=["old.digitalnoise.net"])
        obs = [p for s, p in cur.sql if "shared_observations" in s][0]
        self.assertIn("1 new subdomain(s)", obs[0])
        self.assertEqual(json.loads(obs[1])["new_subdomains"], ["new.digitalnoise.net"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_notifies_once(self):
        cur, conn = _main({"digitalnoise.net": ["n1.digitalnoise.net"], "nova.digitalnoise.net": ["n2.nova.digitalnoise.net"]})
        oa.notify.assert_called_once()
        self.assertEqual(oa.notify.call_args.kwargs["dedup_key"], "osint-amass-new")
        self.assertEqual(oa.notify.call_args.kwargs["body"], "n1.digitalnoise.net\nn2.nova.digitalnoise.net")
        conn.close.assert_called_once()

    def test_nothing_new_posts_nothing(self):
        cur, _ = _main({"digitalnoise.net": ["a"]}, known=["a"])
        oa.notify.assert_not_called()
        self.assertFalse(any("shared_observations" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_osint_amass as m; print(m.DOMAINS[0])"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "digitalnoise.net")


if __name__ == "__main__":
    unittest.main()
