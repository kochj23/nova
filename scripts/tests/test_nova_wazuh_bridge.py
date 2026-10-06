#!/usr/bin/env python3
"""Tests for nova_wazuh_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module reads the Wazuh indexer password from the 1Password vault (nova_secrets.vault_secret) AT IMPORT;
every load here patches vault_secret on the real nova_secrets module (op/PG are never touched)."""
import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_wazuh_bridge.py"
FAKE_PW = "pw-" + "from-fake-vault"
import nova_secrets  # noqa: E402  (import-clean; only its vault_secret attribute is patched, per load)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_with_vault(name, vault):
    """Load the module with nova_secrets.vault_secret replaced by `vault`; subprocess.run is a tripwire
    so a real `op`/`security` call can never happen during the import."""
    with patch.object(nova_secrets, "vault_secret", vault), \
            patch("subprocess.run", side_effect=RuntimeError("subprocess.run during import")):
        return _load(name, SCRIPT)


_VAULT = MagicMock(return_value=FAKE_PW)
wb = _load_with_vault("wazuh_bridge_t", _VAULT)
VAULT_CALL = _VAULT.call_args
SRC = SCRIPT.read_text()
wb.log = lambda m: None
wb.subprocess = MagicMock()
wb.subprocess.run.side_effect = RuntimeError("subprocess.run not mocked in test")
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
wb.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))
wb.psycopg2 = MagicMock(extras=MagicMock(RealDictCursor=object))
wb.psycopg2.connect.side_effect = RuntimeError("psycopg2.connect not mocked in test")
TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Cur:
    def __init__(self, routes):
        self.routes = routes; self.stmts = []; self._last = ""

    def execute(self, sql, params=None):
        self.stmts.append((sql, params)); self._last = sql

    def _pick(self):
        for k, v in self.routes.items():
            if k in self._last:
                return v
        return None

    def fetchone(self):
        return self._pick()

    def fetchall(self):
        return self._pick() or []


class _Conn:
    def __init__(self, routes=None):
        self.cur = _Cur(routes or {}); self.commits = 0; self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _evt(i, agent="host1", level=10, desc="Bad thing"):
    return {"id": i, "agent_name": agent, "rule_level": level, "rule_description": desc, "rule_groups": ["x"], "ts": TS}


def _sql(conn, needle):
    return [(s, p) for s, p in conn.cur.stmts if needle in s]


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    return r


class TestSecurity(unittest.TestCase):
    def test_password_comes_from_vault_item(self):
        self.assertEqual(VAULT_CALL.args, ("nova-wazuh-indexer-password",))
        self.assertEqual(base64.b64decode(wb.WAZUH_CREDS).decode(), f"admin:{FAKE_PW}")
        self.assertIn("from nova_secrets import vault_secret", SRC)
        self.assertNotIn("find-generic-password", SRC)                # no direct Keychain read any more
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_vendor_default_password_in_source(self):
        self.assertNotIn("SecretPassword", SRC)

    def test_vault_miss_fails_closed(self):
        miss = MagicMock(side_effect=KeyError("nova-wazuh-indexer-password"))
        with self.assertRaises(KeyError):
            _load_with_vault("wazuh_bridge_failclosed_t", miss)
        miss.assert_called_once_with("nova-wazuh-indexer-password")

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        hit = {"_id": "w1", "_source": {"timestamp": "t", "rule": {"description": "x'); --", "level": 5},
                                         "agent": {"name": "a"}, "full_log": "L" * 5000}}
        conn = _Conn({"MAX(ts)": (None,)})
        with patch.object(wb, "pg_connect", return_value=conn), \
                patch.object(wb, "wazuh_query", return_value={"hits": {"hits": [hit]}}):
            self.assertEqual(wb.sync_wazuh_to_pg(), 1)
        sql, params = _sql(conn, "INSERT INTO security_events")[0]
        self.assertNotIn("x');", sql)
        self.assertEqual(len(params[8]), 2000)                       # full_log truncated

    def test_muted_findings_never_open_incidents(self):
        host, desc = next(iter(wb.MUTED_FINDINGS))
        conn = _Conn({"correlated = FALSE": [_evt(1, host, 12, desc)]})
        with patch.object(wb, "pg_connect", return_value=conn):
            wb.correlate_events()
        self.assertEqual(_sql(conn, "INSERT INTO incidents"), [])
        self.assertEqual(_sql(conn, "SET correlated = TRUE WHERE id = ANY")[0][1], ([1],))


class TestPerformance(unittest.TestCase):
    def test_correlate_10k_events(self):
        evts = [_evt(i, f"h{i % 20}") for i in range(10_000)]
        conn = _Conn({"correlated = FALSE": evts, "RETURNING id": {"id": 7}})
        t0 = time.perf_counter()
        with patch.object(wb, "pg_connect", return_value=conn):
            wb.correlate_events()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(_sql(conn, "INSERT INTO incidents")), 20)


class TestRetry(unittest.TestCase):
    def test_wazuh_down_fails_open(self):
        # RETRY GAP: sync_wazuh_to_pg/wazuh_query — one attempt per 2-min run; failure returns 0, conn closed
        conn = _Conn({"MAX(ts)": (None,)})
        with patch.object(wb, "pg_connect", return_value=conn), \
                patch.object(wb, "wazuh_query", side_effect=OSError("down")) as q:
            self.assertEqual(wb.sync_wazuh_to_pg(), 0)
        self.assertEqual(q.call_count, 1)
        self.assertTrue(conn.closed)

    def test_memory_and_bb_failures_fail_open(self):
        # RETRY GAP: ingest_to_memory / sync_bb_annotations — one HTTP attempt each, failures swallowed
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("down")
        try:
            with patch.object(wb, "pg_connect", return_value=_Conn({"DISTINCT rule_description": [_evt(1)]})) as pc:
                wb.ingest_to_memory()
                wb.sync_bb_annotations()
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual(URLOPEN.call_count, 2)
        self.assertEqual(pc.call_count, 1)                           # bb never opened a PG connection


class TestUnit(unittest.TestCase):
    def test_since_uses_last_ts_or_window(self):
        for last, expect in ((None, f"now-{wb.POLL_WINDOW_MINUTES}m"), (TS, TS.isoformat())):
            conn = _Conn({"MAX(ts)": (last,)})
            with patch.object(wb, "pg_connect", return_value=conn), \
                    patch.object(wb, "wazuh_query", return_value={}) as q:
                wb.sync_wazuh_to_pg()
            self.assertEqual(q.call_args.args[0]["query"]["bool"]["must"][0]["range"]["timestamp"]["gt"], expect)

    def test_threat_score_formula(self):
        conn = _Conn({"FILTER (WHERE rule_level >= 10)": [("h1", 1, 2, 3, 1, 1)]})
        with patch.object(wb, "pg_connect", return_value=conn):
            wb.compute_threat_scores()
        _, params = _sql(conn, "INSERT INTO host_threat_scores")[0]
        self.assertEqual(params[:2], ("h1", 30 + 10 + 3 + 10 + 8))


class TestIntegration(unittest.TestCase):
    def test_open_incident_is_reused_not_duplicated(self):
        conn = _Conn({"correlated = FALSE": [_evt(5)], "status = 'open'": {"id": 42, "events": [{"event_id": 1}]}})
        with patch.object(wb, "pg_connect", return_value=conn):
            wb.correlate_events()
        self.assertEqual(_sql(conn, "INSERT INTO incidents"), [])
        self.assertEqual(_sql(conn, "INSERT INTO grafana_annotations"), [])
        merged = json.loads(_sql(conn, "incidents SET events")[0][1][0])
        self.assertEqual([e["event_id"] for e in merged], [1, 5])

    def test_wazuh_query_auth_and_tls_context(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None; URLOPEN.return_value = _resp({"hits": {}})
        try:
            wb.wazuh_query({"q": 1})
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        req = URLOPEN.call_args.args[0]
        self.assertEqual(req.full_url, f"{wb.WAZUH_URL}/wazuh-alerts-*/_search")
        self.assertEqual(req.headers["Authorization"], f"Basic {wb.WAZUH_CREDS}")
        self.assertIs(URLOPEN.call_args.kwargs["context"], wb._ssl_ctx)


class TestFunctional(unittest.TestCase):
    def test_new_incident_annotates_and_forensics_escalates(self):
        conn = _Conn({"correlated = FALSE": [_evt(1, level=12), _evt(2, level=10)], "RETURNING id": {"id": 9}})
        with patch.object(wb, "pg_connect", return_value=conn):
            wb.correlate_events()
        _, p = _sql(conn, "INSERT INTO incidents")[0]
        self.assertEqual((p[0], p[1]), ("Correlated security events on host1 (2 events)", "critical"))
        self.assertEqual(len(_sql(conn, "INSERT INTO grafana_annotations")), 1)
        conn = _Conn({"auto_response IS NULL": [_evt(3, level=13)]})
        with patch.object(wb, "pg_connect", return_value=conn), \
                patch.object(wb.subprocess, "run", return_value=SimpleNamespace(stdout="out")) as r:
            wb.auto_forensics()
        self.assertEqual([c.args[0][0] for c in r.call_args_list], ["netstat", "ps"])   # read-only probes only
        q = _sql(conn, "INSERT INTO claude_queue")[0][1]
        self.assertTrue(q[0].startswith("SECURITY: L13 alert on host1"))

    def test_main_runs_every_step_and_survives_vault7_bug(self):
        calls = []
        steps = ("sync_wazuh_to_pg", "correlate_events", "compute_threat_scores", "write_observations",
                 "sync_bb_annotations", "ingest_to_memory", "auto_forensics")
        patches = [patch.object(wb, s, side_effect=(lambda s=s: calls.append(s) or 3)) for s in steps]
        v7 = types.SimpleNamespace(scan=MagicMock(side_effect=RuntimeError("detector bug")))
        for p in patches:
            p.start()
        try:
            with patch.dict(sys.modules, {"nova_vault7_ttp": v7}), patch.object(wb, "pg_connect", return_value=_Conn()):
                wb.main()
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(calls, list(steps))
        v7.scan.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_or_touches_real_vault(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys,subprocess,types;sys.path.insert(0,'.');"
                "subprocess.run=lambda *a,**k: types.SimpleNamespace(stdout='', stderr='', returncode=44);"
                "import nova_secrets;nova_secrets.vault_secret=lambda n,f='password': 'smoke';"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_wazuh_bridge.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
