#!/usr/bin/env python3
"""Tests for nova_weekly_security_review.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weekly_security_review.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.slack_bot_token = MagicMock(return_value="xoxb-test")
    cfg._keychain = MagicMock(return_value="xoxb-test")
    cfg.post_both = MagicMock()
    return {"nova_config": cfg}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):
        spec.loader.exec_module(mod)
    return mod


ws = _load("weekly_sec_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="weekly-sec-test-"))
# Module-level stubs: no ssh, no PG, no Slack, and the alert tally reads a tempdir, never ~/.openclaw/logs.
ws.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: subprocess stubbed")))
ws.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")))
ws.LOG_DIR = TMP


def _uo(mock):
    """post_slack imports urllib.request locally, so the real module attribute is what must be patched."""
    return patch("urllib.request.urlopen", mock)

LINUX_SS = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:5432 0.0.0.0:*\nLISTEN 0 4096 *:8080 *:*\n"
MAC_LSOF = "*:22\n127.0.0.1:18792\n*:5000\n"


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d


class _Cur:
    def __init__(self, prev=None):
        self.prev, self.sql, self.params = prev or {}, [], []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)
        self._host = params[0] if params and "security_snapshots WHERE host" in sql else None

    def fetchone(self):
        ports = self.prev.get(self._host)
        return (ports,) if ports is not None else None

    def ran(self, frag):
        return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


def _runner(outputs):
    """subprocess.run stub keyed by substring of the remote command; returns '' for anything else."""
    def run(argv, **kw):
        cmd = argv[-1]
        for k, v in outputs.items():
            if k in cmd:
                return types.SimpleNamespace(stdout=v, returncode=0)
        return types.SimpleNamespace(stdout="", returncode=0)
    return MagicMock(side_effect=run)


def _run_main(argv, cur, outputs, urlopen=None):
    conn = types.SimpleNamespace(cursor=lambda: cur, autocommit=False)
    uo = urlopen or MagicMock(return_value=_Resp({"ok": True}))
    run = _runner(outputs)
    buf = io.StringIO()
    with patch.object(sys, "argv", ["nova_weekly_security_review.py", *argv]), \
         patch.object(ws.psycopg2, "connect", MagicMock(return_value=conn)) as pg, \
         patch.object(ws.subprocess, "run", run), _uo(uo), redirect_stdout(buf):
        rc = ws.main()
    return rc, pg, run, uo, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ws.OPS_DSN)
        self.assertNotIn("xoxb-", SRC)

    def test_slack_token_comes_from_nova_config_not_source(self):
        uo = MagicMock(return_value=_Resp({"ok": True}))
        with _uo(uo):
            self.assertTrue(ws.post_slack("hi"))
        req = uo.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer xoxb-test")
        self.assertEqual(json.loads(req.data)["channel"], ws.SLACK_CHANNEL)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        cur = _Cur()
        ws.prev_ports(cur, "x'; DROP TABLE security_snapshots; --")
        self.assertNotIn("DROP", cur.sql[0])
        self.assertEqual(cur.params[0], ("x'; DROP TABLE security_snapshots; --",))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"security_snapshots"})

    def test_ssh_is_batch_mode_argv_list_never_shell(self):
        run = MagicMock(return_value=types.SimpleNamespace(stdout="ok"))
        with patch.object(ws.subprocess, "run", run):
            ws.run_on("192.168.1.2", "ss -tlnH")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:5], ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"])
        self.assertEqual(argv[5], "kochj@192.168.1.2")
        self.assertFalse(run.call_args[1].get("shell", False))


class TestPerformance(unittest.TestCase):
    def test_port_parse_fast_on_10k_lines(self):
        out = "".join(f"LISTEN 0 128 0.0.0.0:{1000 + i} 0.0.0.0:*\n" for i in range(10_000))
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout=out))):
            t0 = time.perf_counter()
            ports = ws.listening_ports("192.168.1.2", True)
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(ports), 10_000)

    def test_alert_tally_fast_on_10k_log_lines(self):
        d = TMP / "perf"; d.mkdir(exist_ok=True)
        (d / ws.BB_LOGS[0]).write_text("".join(f"2026-10-05 03:00:{i % 60:02d},123 WARN service {i % 7} down pid {i}\n" for i in range(10_000)))
        with patch.object(ws, "LOG_DIR", d):
            t0 = time.perf_counter()
            tally = ws.alert_tally()
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(tally), 1)                          # numbers normalized away -> one key
        self.assertEqual(tally[0][1], 10_000)


class TestRetry(unittest.TestCase):
    def test_run_on_fails_open_to_empty_string(self):
        # RETRY GAP: run_on — one ssh attempt; timeout/OSError returns '' so the host shows as unreachable
        run = MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 25))
        with patch.object(ws.subprocess, "run", run):
            self.assertEqual(ws.run_on("192.168.1.2", "ss"), "")
            self.assertEqual(ws.listening_ports("192.168.1.2", True), set())
            self.assertEqual(ws.promisc("192.168.1.6", False), [])
            self.assertEqual(ws.pending_updates("192.168.1.2", True), 0)
        self.assertEqual(run.call_count, 4)

    def test_post_slack_fails_open(self):
        # RETRY GAP: post_slack — one urlopen; failure returns False and main exits 1 instead of raising
        uo = MagicMock(side_effect=OSError("slack 500"))
        with _uo(uo), redirect_stdout(io.StringIO()):
            self.assertFalse(ws.post_slack("x"))
        self.assertEqual(uo.call_count, 1)
        rc, *_ = _run_main([], _Cur(), {"ss -tln": LINUX_SS}, urlopen=MagicMock(side_effect=OSError("slack 500")))
        self.assertEqual(rc, 1)

    def test_pg_connect_failure_propagates(self):
        # RETRY GAP: main/psycopg2.connect — one attempt, exception escapes (launchd restarts next Sunday)
        with patch.object(ws.psycopg2, "connect", MagicMock(side_effect=OSError("pg down"))), patch.object(sys, "argv", ["x", "--dry"]):
            with self.assertRaises(OSError):
                ws.main()


class TestUnit(unittest.TestCase):
    def test_run_on_local_vs_remote(self):
        run = MagicMock(return_value=types.SimpleNamespace(stdout="out"))
        with patch.object(ws.subprocess, "run", run):
            self.assertEqual(ws.run_on("192.168.1.6", "echo hi"), "out")
            self.assertEqual(run.call_args[0][0], ["bash", "-lc", "echo hi"])
            ws.run_on("localhost", "x")
            self.assertEqual(run.call_args[0][0][0], "bash")
            self.assertEqual(run.call_args[1]["timeout"], 25)

    def test_listening_ports_linux_and_mac(self):
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout=LINUX_SS))):
            self.assertEqual(ws.listening_ports("192.168.1.2", True), {22, 5432, 8080})
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout=MAC_LSOF))):
            self.assertEqual(ws.listening_ports("192.168.1.6", False), {22, 18792, 5000})
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout=""))):
            self.assertEqual(ws.listening_ports("192.168.1.6", False), set())

    def test_promisc_parsing(self):
        out = "2: eth0: <BROADCAST,MULTICAST,PROMISC,UP> mtu 1500\n3: wlan0: <PROMISC> x\n"
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout=out))):
            self.assertEqual(ws.promisc("192.168.1.2", True), ["eth0", "wlan0"])
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout="en0\n\n"))):
            self.assertEqual(ws.promisc("192.168.1.6", False), ["en0"])

    def test_pending_updates(self):
        self.assertIsNone(ws.pending_updates("192.168.1.6", False))
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout="7\n"))):
            self.assertEqual(ws.pending_updates("192.168.1.2", True), 7)
        with patch.object(ws.subprocess, "run", MagicMock(return_value=types.SimpleNamespace(stdout="garbage"))):
            self.assertIsNone(ws.pending_updates("192.168.1.2", True))

    def test_prev_ports(self):
        self.assertIsNone(ws.prev_ports(_Cur(), "192.168.1.2"))
        self.assertEqual(ws.prev_ports(_Cur({"192.168.1.2": [22, 80]}), "192.168.1.2"), {22, 80})

    def test_alert_tally_normalizes_and_skips_missing_logs(self):
        d = TMP / "unit"; d.mkdir(exist_ok=True)
        (d / ws.BB_LOGS[1]).write_text("2026-10-05T03:00:01.123 [WARN] pid 42 service nova-x down\n"
                                       "2026-10-06T04:00:02.999 [WARN] pid 43 service nova-x down\n"
                                       "just a boring info line\n")
        with patch.object(ws, "LOG_DIR", d):
            tally = ws.alert_tally()
        self.assertEqual(tally, [("service nova-x down", 2)])
        with patch.object(ws, "LOG_DIR", TMP / "does-not-exist"):
            self.assertEqual(ws.alert_tally(), [])


class TestIntegration(unittest.TestCase):
    def test_post_slack_uses_shared_nova_config_token(self):
        self.assertIn("import nova_config", SRC)
        self.assertNotIn("def slack_bot_token", SRC)                 # imported, not re-implemented
        ws.nova_config.slack_bot_token.reset_mock()
        with _uo(MagicMock(return_value=_Resp({"ok": True}))):
            ws.post_slack("x")
        ws.nova_config.slack_bot_token.assert_called_once()

    def test_snapshot_diff_marks_new_ports_with_warning(self):
        cur = _Cur(prev={"192.168.1.2": [22, 5432]})
        rc, pg, run, uo, out = _run_main(["--dry"], cur, {"ss -tln": LINUX_SS})
        self.assertEqual(rc, 0)
        self.assertIn("• nova-core(.2): ＋8080  ⚠️", out)
        self.assertEqual(cur.ran("INSERT INTO security_snapshots"), [])   # --dry never writes

    def test_baseline_and_removed_ports(self):
        cur = _Cur(prev={"192.168.1.2": [22, 5432, 8080, 9999]})
        rc, pg, run, uo, out = _run_main(["--dry"], cur, {"ss -tln": LINUX_SS, "lsof": MAC_LSOF})
        self.assertIn("• nova-core(.2): －9999", out)
        self.assertNotIn("－9999  ⚠️", out)
        self.assertIn("• mac-studio(.6): 3 ports (baseline)", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_snapshots_and_posts(self):
        cur = _Cur()
        rc, pg, run, uo, out = _run_main([], cur, {"ss -tln": LINUX_SS, "lsof": MAC_LSOF, "apt-get": "3\n",
                                                    "PROMISC": "2: eth0: <PROMISC>\n"})
        self.assertEqual(rc, 0)
        pg.assert_called_once_with(ws.OPS_DSN)
        self.assertIn("CREATE TABLE IF NOT EXISTS security_snapshots", cur.sql[0])
        inserts = cur.ran("INSERT INTO security_snapshots")
        self.assertEqual(len(inserts), len(ws.HOSTS))
        self.assertEqual(inserts[1][1], ("192.168.1.2", json.dumps([22, 5432, 8080])))
        msg = json.loads(uo.call_args[0][0].data)["text"]
        self.assertIn("*Weekly Security Review —", msg)
        self.assertIn("• nova-core(.2): eth0", msg)                   # promisc hit
        self.assertIn("• nova-core(.2): 3 pending ⚠️", msg)
        self.assertIn("• (no alerts logged)", msg)
        self.assertIn("posted=True", out)

    def test_unreachable_hosts_are_reported_not_snapshotted(self):
        cur = _Cur()
        rc, pg, run, uo, out = _run_main(["--dry"], cur, {})
        self.assertEqual(rc, 0)
        for _, label, _ in ws.HOSTS:
            self.assertIn(f"• {label}: _unreachable_", out)
        self.assertIn("• none ✅", out); self.assertIn("• all current ✅", out)
        uo.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_weekly_security_review as m; print(len(m.HOSTS))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(len(ws.HOSTS)))


if __name__ == "__main__":
    unittest.main()
