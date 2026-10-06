#!/usr/bin/env python3
"""Tests for nova_crontab_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import hashlib
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_crontab_audit.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="crontab-audit-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    spec = importlib.util.spec_from_file_location("crontab_audit_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    with _stub_modules({"nova_config": cfg, "nova_notify": nn}), patch.dict(os.environ, {"HOME": str(TMP)}), \
         patch.object(subprocess, "run", side_effect=AssertionError("crontab at import")):
        spec.loader.exec_module(mod)
    mod.STATE_FILE = TMP / "crontab_hashes.json"
    return mod


ca = _load()


def _crontabs(table):
    """subprocess.run stub keyed by (host-ish argv token, user) -> crontab text or None for rc!=0."""
    def run(argv, **kw):
        if argv[0] == "sudo":
            key = ("local", argv[3])
        else:
            key = (argv[3].split("@")[1], argv[4].split("-u ")[1].split(" ")[0])
        txt = table.get(key)
        return types.SimpleNamespace(returncode=0 if txt is not None else 1, stdout=txt or "", stderr="")
    return MagicMock(side_effect=run)


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.commits = 0; self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _run(table, prev=None, connect=None):
    if prev is None:
        ca.STATE_FILE.unlink(missing_ok=True)
    else:
        ca.STATE_FILE.write_text(json.dumps(prev))
    cur = _Cur(); conn = _Conn(cur)
    ca.notify = MagicMock(return_value=True)
    with patch.object(subprocess, "run", _crontabs(table)) as sp, \
         patch.object(ca.psycopg2, "connect", connect or MagicMock(return_value=conn)) as pg, \
         redirect_stdout(io.StringIO()) as out:
        ca.main()
    return sp, cur, conn, pg, out.getvalue()


ONLY_LOCAL = {("local", "root"): "0 3 * * * /usr/bin/backup\n", ("local", "kochj"): "* * * * * echo hi\n"}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_remote_user_in_the_ssh_line_comes_only_from_the_host_table(self):
        # the only f-string shell line is `sudo crontab -u {target_user} -l`, with target_user in ("root", HOSTS[].user)
        self.assertIn('for target_user in ["root", user]:', SRC)
        for h in ca.HOSTS:
            self.assertRegex(h.get("user", "kochj"), r"^[a-z]+$")
        sp, cur, conn, pg, _ = _run({("192.168.1.2", "root"): "x\n"})
        remote = [c[0][0] for c in sp.call_args_list if c[0][0][0] == "ssh"]
        self.assertTrue(all(a[1:3] == ["-o", "ConnectTimeout=10"] for a in remote))
        self.assertEqual({a[4] for a in remote}, {"sudo crontab -u root -l 2>/dev/null", "sudo crontab -u kochj -l 2>/dev/null"})

    def test_sql_is_parameterized_and_crontab_content_is_truncated(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        big = "'); DROP TABLE shared_observations; --\n" * 100
        prev = {"mac-studio:root": {"hash": "stale", "lines": 1}}
        sp, cur, conn, pg, _ = _run({("local", "root"): big}, prev=prev)
        self.assertEqual(len(cur.sql), 1)
        self.assertNotIn("DROP", cur.sql[0][0])
        self.assertLessEqual(len(ca.notify.call_args[1]["body"]), 600)


class TestPerformance(unittest.TestCase):
    def test_hash_10k_crontabs_fast(self):
        bodies = [f"{i} * * * * /bin/job{i}\n" * 3 for i in range(10_000)]
        t0 = time.perf_counter()
        hashes = {ca.hash_content(b) for b in bodies}
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(hashes), 10_000)


class TestRetry(unittest.TestCase):
    def test_crontab_read_failure_is_one_shot_and_skipped(self):
        # RETRY GAP: get_crontabs — one sudo/ssh attempt per user; an exception simply omits that entry
        sp = MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 15))
        with patch.object(subprocess, "run", sp):
            self.assertEqual(ca.get_crontabs(ca.HOSTS[1]), {})
        self.assertEqual(sp.call_count, 2)

    def test_observation_write_failure_never_blocks_the_alert(self):
        # RETRY GAP: shared_observations insert — single connect; failure is swallowed after notify() already fired
        prev = {"mac-studio:root": {"hash": "stale", "lines": 1}}
        sp, cur, conn, pg, out = _run(ONLY_LOCAL, prev=prev, connect=MagicMock(side_effect=RuntimeError("pg down")))
        self.assertEqual(pg.call_count, 1)
        self.assertEqual(ca.notify.call_count, 1)
        self.assertIn("1 change(s) detected and alerted", out)


class TestUnit(unittest.TestCase):
    def test_hash_is_sha256_of_content(self):
        self.assertEqual(ca.hash_content("abc"), hashlib.sha256(b"abc").hexdigest())
        self.assertNotEqual(ca.hash_content("a"), ca.hash_content("a\n"))

    def test_state_round_trip_and_corrupt_file(self):
        ca.STATE_FILE.write_text("{nope")
        self.assertEqual(ca.load_state(), {})
        ca.save_state({"k": {"hash": "h", "lines": 2}})
        self.assertEqual(ca.load_state(), {"k": {"hash": "h", "lines": 2}})

    def test_get_crontabs_local_skips_non_zero_rc(self):
        with patch.object(subprocess, "run", _crontabs({("local", "root"): "r\n"})):
            self.assertEqual(ca.get_crontabs(ca.HOSTS[0]), {"root": "r\n"})

    def test_hosts_table_shape(self):
        self.assertEqual(ca.HOSTS[0], {"name": "mac-studio", "ip": "127.0.0.1", "local": True})
        for h in ca.HOSTS[1:]:
            self.assertEqual(set(h), {"name", "ip", "user"})


class TestIntegration(unittest.TestCase):
    def test_root_change_is_critical_security_and_user_change_is_warning(self):
        prev = {"mac-studio:root": {"hash": "old", "lines": 1}, "mac-studio:kochj": {"hash": "old", "lines": 1}}
        sp, cur, conn, pg, _ = _run(ONLY_LOCAL, prev=prev)
        calls = {c[0][0]: c[1] for c in ca.notify.call_args_list}
        root = calls["Crontab change detected on mac-studio (root, P1)"]
        user = calls["Crontab change detected on mac-studio (kochj, P2)"]
        self.assertEqual((root["level"], root["category"], root["dedup_key"]), ("critical", "security", "crontab-change-mac-studio-root"))
        self.assertEqual(user["level"], "warning")
        self.assertEqual(root["meta"], {"host": "mac-studio", "user": "root", "priority": 1})
        subjects = [p for s, p in cur.sql]
        self.assertEqual(subjects[0], ("Crontab changed on mac-studio", "root crontab modified", "critical",
                                       json.dumps({"host": "mac-studio", "user": "root"})))
        self.assertEqual(conn.commits, 2); self.assertTrue(conn.closed)

    def test_first_run_establishes_baselines_without_alerting(self):
        sp, cur, conn, pg, out = _run(ONLY_LOCAL)
        ca.notify.assert_not_called(); pg.assert_not_called()
        self.assertIn("New baseline: mac-studio:root (1 lines)", out)
        state = json.loads(ca.STATE_FILE.read_text())
        self.assertEqual(state["mac-studio:root"], {"hash": ca.hash_content(ONLY_LOCAL[("local", "root")]), "lines": 1})


class TestFunctional(unittest.TestCase):
    def test_golden_path_detects_only_the_changed_host(self):
        table = dict(ONLY_LOCAL); table[("192.168.1.2", "root")] = "0 4 * * * /opt/evil\n"
        prev = {"mac-studio:root": {"hash": ca.hash_content(ONLY_LOCAL[("local", "root")]), "lines": 1},
                "nova-core:root": {"hash": "previous", "lines": 1}}
        sp, cur, conn, pg, out = _run(table, prev=prev)
        self.assertEqual(ca.notify.call_count, 1)
        self.assertEqual(ca.notify.call_args[0][0], "Crontab change detected on nova-core (root, P1)")
        self.assertIn("/opt/evil", ca.notify.call_args[1]["body"])
        self.assertIn("CHANGE DETECTED: nova-core:root", out)
        state = json.loads(ca.STATE_FILE.read_text())
        self.assertEqual(set(state), {"mac-studio:root", "mac-studio:kochj", "nova-core:root"})

    def test_unchanged_fleet_is_quiet(self):
        prev = {"mac-studio:root": {"hash": ca.hash_content(ONLY_LOCAL[("local", "root")]), "lines": 1},
                "mac-studio:kochj": {"hash": ca.hash_content(ONLY_LOCAL[("local", "kochj")]), "lines": 1}}
        sp, cur, conn, pg, out = _run(ONLY_LOCAL, prev=prev)
        ca.notify.assert_not_called(); self.assertIn("No changes detected", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, unittest.mock as um, subprocess, psycopg2, runpy; "
                "sys.modules['nova_config'] = um.MagicMock(); sys.modules['nova_notify'] = um.MagicMock(); "
                "subprocess.run = um.MagicMock(side_effect=AssertionError('crontab at import')); "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
