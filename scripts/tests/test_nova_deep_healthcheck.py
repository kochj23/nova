#!/usr/bin/env python3
"""Tests for nova_deep_healthcheck.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Every subprocess/ssh/HTTP/PG/Slack call is mocked; NO real service
is restarted, NO mount or DB is touched. Since merge M4 (2026-10-09) main() is a thin wrapper around
`nova_selfcheck.py --deep` and the redline/dry-run gate is nova_selfcheck.fix_gate; the end-to-end run is
exercised through the real nova_selfcheck module with every side effect stubbed. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load("nova_deep_healthcheck_t", SCRIPTS / "nova_deep_healthcheck.py")
SRC = (SCRIPTS / "nova_deep_healthcheck.py").read_text()
sc = hc._sc                      # the real nova_selfcheck module deep imports for its fix gate
_TMP = tempfile.TemporaryDirectory()


@contextmanager
def _modstub(mapping):
    """Install modules and afterwards restore ONLY those keys."""
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in mapping}
    sys.modules.update(mapping)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is missing:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@contextmanager
def _deep_run(checks, dry=False):
    """Run `nova_selfcheck --deep` end to end over these checks: selfcheck's log/PG/shell, the Keychain read +
    Slack HTTP call under the real selfcheck.slack(), and deep's psycopg2 audit write are all stubbed. Yields a namespace with posts / inserts / audit rows."""
    ns = types.SimpleNamespace(posts=[], audit=[], rc=None)
    cur = mock.MagicMock()
    cur.execute.side_effect = lambda sql, params=None: ns.audit.append((sql, params))
    conn = mock.MagicMock(cursor=lambda: cur)
    with mock.patch.object(sc, "LOG", Path(_TMP.name) / "selfcheck.log"), \
         mock.patch.object(sc, "STATE_DIR", Path(_TMP.name) / "state"), \
         mock.patch.object(sc, "pg", mock.MagicMock(return_value=None)) as pg, \
         mock.patch.object(sc, "sh", mock.MagicMock(return_value=(0, ""))), \
         mock.patch.object(sc.subprocess, "check_output", return_value="xoxb-test\n"), \
         mock.patch.object(sc.urllib.request, "urlopen",
                           lambda req, timeout=None: ns.posts.append(tuple(json.loads(req.data).values()))), \
         mock.patch.object(sc, "DRY", False), mock.patch.object(sc, "results", []), \
         _modstub({"nova_deep_healthcheck": hc}), mock.patch.object(hc, "CHECKS", checks), \
         mock.patch("psycopg2.connect", return_value=conn), redirect_stdout(io.StringIO()):
        ns.rc = sc.main(["--deep"] + (["--dry-run"] if dry else []))
        ns.inserts = [c[0][0] for c in pg.call_args_list if "INSERT INTO selfcheck_runs" in c[0][0]]
        yield ns


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_plex_token_comes_from_keychain(self):
        with mock.patch.object(hc, "sh", return_value=types.SimpleNamespace(stdout="tok123\n", returncode=0)) as s:
            self.assertEqual(hc.keychain("nova-plex-token"), "tok123")
        self.assertEqual(s.call_args.args[0][0], "security")

    def test_redline_blocks_destructive_fixes(self):
        for desc in ("plex: delete old libraries", "pg: promote standby to primary", "reboot the host",
                     "buy more storage", "exfiltrate the db"):
            ok, detail = hc.safe_fix(desc, lambda: (True, "ran"))
            self.assertFalse(ok)
            self.assertIn("redline-blocked", detail)

    def test_one_gate_shared_with_selfcheck(self):
        self.assertIs(hc._REDLINE, sc._REDLINE)
        self.assertIn("return _sc.fix_gate(desc, fn)", SRC)
        self.assertNotIn("DRY = False", SRC)              # no second dry-run flag to drift


class TestPerformance(unittest.TestCase):
    def test_redline_regex_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            hc._REDLINE.search(f"benign fix number {i} restart a service gently")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_sh_failure_fails_open_with_124(self):
        # RETRY GAP: sh()/subprocess.run — single attempt; timeout/error returns a CompletedProcess rc=124
        with mock.patch.object(hc.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)) as r:
            cp = hc.sh(["ls"])
        self.assertEqual(cp.returncode, 124)
        self.assertEqual(r.call_count, 1)

    def test_get_failure_returns_status_zero(self):
        with mock.patch.object(hc.urllib.request, "urlopen", side_effect=OSError("conn refused")):
            body, st = hc.get("http://x/y")
        self.assertEqual(st, 0)
        self.assertIn("conn refused", body)


class TestUnit(unittest.TestCase):
    def test_result_shape(self):
        r = hc.result("x", True, "fine")
        self.assertEqual(set(r), {"name", "ok", "detail", "fixed", "fix_detail", "needs_human"})
        self.assertTrue(r["ok"])

    def test_safe_fix_dry_run_does_not_execute(self):
        ran = []
        with mock.patch.object(sc, "DRY", True):
            ok, detail = hc.safe_fix("gateway: restart", lambda: ran.append(1) or (True, "x"))
        self.assertFalse(ok)
        self.assertEqual(ran, [])
        self.assertIn("[dry-run] would", detail)

    def test_safe_fix_runs_when_allowed(self):
        with mock.patch.object(sc, "DRY", False):
            ok, detail = hc.safe_fix("gateway: restart", lambda: (True, "restarted"))
        self.assertTrue(ok)
        self.assertEqual(detail, "restarted")

    def test_safe_fix_catches_exceptions(self):
        with mock.patch.object(sc, "DRY", False):
            ok, detail = hc.safe_fix("gateway: restart", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertFalse(ok)
        self.assertIn("fix error", detail)


    def test_format_report_states(self):
        ok_only = hc.format_report([hc.result("db", True, "ok")])
        self.assertIn("*1/1 functional*  :white_check_mark:", ok_only)
        self.assertIn("Everything up AND functional", ok_only)
        mixed = hc.format_report([hc.result("plex", False, "empty", fixed=True, fix_detail="refreshed"),
                                  hc.result("dns", False, "wrong", fix_detail="[dry-run] would: x")])
        self.assertIn(":wrench: *Fixed:*\n  • plex: empty → _refreshed_", mixed)
        self.assertIn("  • dns: wrong (fix tried: [dry-run] would: x)", mixed)
        self.assertNotIn(":white_check_mark:", mixed)

    def test_run_checks_contains_crashes(self):
        with mock.patch.object(hc, "CHECKS", [lambda: hc.result("a", True, "x"), lambda: 1 / 0]):
            rows = hc.run_checks()
        self.assertEqual([r["ok"] for r in rows], [True, False])
        self.assertTrue(rows[1]["needs_human"])
        self.assertIn("check crashed", rows[1]["detail"])

    def test_postgres_write_probe_skipped_in_dry_run(self):
        seen = []
        cur = mock.MagicMock(); cur.execute.side_effect = lambda sql: seen.append(sql)
        cur.fetchone.side_effect = [(True,), (2,)]
        with mock.patch("psycopg2.connect", return_value=mock.MagicMock(cursor=lambda: cur)), mock.patch.object(sc, "DRY", True):
            r = hc.check_postgres()
        self.assertTrue(r["ok"])
        self.assertFalse(any("_hc_probe" in q for q in seen))


class TestIntegration(unittest.TestCase):
    def test_checks_registered(self):
        names = {c.__name__ for c in hc.CHECKS}
        for n in ("check_postgres", "check_plex", "check_memory", "check_gateway", "check_inference"):
            self.assertIn(n, names)

    def test_check_plex_functional_pass(self):
        def get(url, timeout=8):
            if "sections?" in url:
                return '<D key="1" size="1"/>', 200
            return 'totalSize="42"', 200
        with mock.patch.object(hc, "keychain", return_value="tok"), mock.patch.object(hc, "get", side_effect=get):
            r = hc.check_plex()
        self.assertTrue(r["ok"])
        self.assertIn("items", r["detail"])

    def test_check_plex_zero_libraries_triggers_guarded_fix(self):
        with mock.patch.object(hc, "keychain", return_value="tok"), \
             mock.patch.object(hc, "get", return_value=("<MediaContainer/>", 200)), \
             mock.patch.object(sc, "DRY", True):
            r = hc.check_plex()
        self.assertFalse(r["ok"])
        self.assertIn("ZERO libraries", r["detail"])
        self.assertIn("[dry-run]", r["fix_detail"])  # fix was gated, nothing restarted


    def test_write_log_keeps_deep_healthcheck_log_shape(self):
        audit = []
        cur = mock.MagicMock(); cur.execute.side_effect = lambda sql, params=None: audit.append((sql, params))
        rows = [hc.result("db", True, "ok"), hc.result("plex", False, "x", fixed=True), hc.result("dns", False, "y")]
        with mock.patch("psycopg2.connect", return_value=mock.MagicMock(cursor=lambda: cur)):
            hc.write_log(rows)
        sql, params = audit[1]
        self.assertIn("INSERT INTO deep_healthcheck_log (healthy, fixed, broken, detail) VALUES (%s,%s,%s,%s)", sql)
        self.assertEqual(params[:3], (1, 1, 1))
        self.assertEqual(json.loads(params[3])[0]["name"], "db")


class TestFunctional(unittest.TestCase):
    def test_deep_run_dry_reports_without_fixing_or_writing(self):
        fixes = []
        checks = [lambda: hc.result("db", True, "ok"),
                  lambda: hc.result("plex", False, "down", *hc.safe_fix("plex: refresh", lambda: fixes.append(1) or (True, "x")))]
        with _deep_run(checks, dry=True) as ns:
            pass
        self.assertEqual(ns.rc, 1)                             # one broken => exit 1
        self.assertEqual(fixes, [])                            # selfcheck's --dry-run reached deep's fix
        self.assertEqual(ns.posts, [])                         # dry-run posts nothing
        self.assertEqual(ns.inserts, [])
        self.assertEqual(ns.audit, [])                         # no deep_healthcheck_log row

    def test_deep_run_broken_posts_alerts_and_logs_both_ledgers(self):
        with _deep_run([lambda: hc.result("db", True, "ok"),
                        lambda: hc.result("plex", False, "down", needs_human=True)]) as ns:
            pass
        self.assertEqual(ns.rc, 1)
        self.assertEqual(ns.posts[0][0], sc.SLACK_ALERT_CHANNEL)
        self.assertIn("Needs you", ns.posts[0][1])
        self.assertEqual(len(ns.inserts), 2)
        self.assertIn("$novaq$deep-plex$novaq$, $novaq$FAIL$novaq$", ns.inserts[1])
        self.assertTrue(any("deep_healthcheck_log" in q for q, _ in ns.audit))

    def test_deep_run_all_healthy_posts_to_digest(self):
        with _deep_run([lambda: hc.result("db", True, "ok")]) as ns:
            pass
        self.assertEqual(ns.rc, 0)
        self.assertEqual(ns.posts[0][0], sc.SLACK_DIGEST_CHANNEL)
        self.assertIn("functional", ns.posts[0][1])

    def test_crashing_check_is_contained(self):
        with _deep_run([lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))]) as ns:
            pass
        self.assertEqual(ns.rc, 1)

    def test_wrapper_delegates_to_selfcheck_deep(self):
        seen = []
        with mock.patch.object(sc, "LOG", Path(_TMP.name) / "selfcheck.log"), \
             mock.patch.object(sc, "main", lambda a: seen.append(a) or 0), redirect_stdout(io.StringIO()):
            self.assertEqual(hc.main([]), 0)
            hc.main(["--dry-run"])
        self.assertEqual(seen, [["--deep"], ["--deep", "--dry-run"]])
        self.assertIn("merged into nova_selfcheck.py --deep on 2026-10-09", (Path(_TMP.name) / "selfcheck.log").read_text())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_deep_healthcheck"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("deep-hc", r.stdout)

    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_deep_healthcheck.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nova_selfcheck.py --deep", r.stdout)


if __name__ == "__main__":
    unittest.main()
