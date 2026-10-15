#!/usr/bin/env python3
"""Tests for nova_blood_test.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_blood_test.py"
spec = importlib.util.spec_from_file_location("blood_test_under_test", SCRIPT)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)
SRC = SCRIPT.read_text()

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
KEY = b"k" * 64


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _beat(host="nova-core", age_min=10, key=KEY, nonce="btabc"):
    ts = NOW - timedelta(minutes=age_min)
    mac, kind = bt.mac_for(key, None, "beat", host, nonce, ts.isoformat())
    return {"host": host, "nonce": nonce, "ts": ts, "mac": mac, "mac_kind": kind}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_emails_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token|secret)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[a-z]{2,}\b(?!\.)")
        self.assertNotRegex(SRC, r"(?i)\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")

    def test_writes_only_its_own_tables_and_deletes_only_its_own_canary_rows(self):
        writes = set(t for _, t in re.findall(r"(INSERT INTO|UPDATE)\s+([\w.]+)", SRC))
        self.assertEqual(writes - {"blood_test", "blood_canary", "telemetry.probe_results", "SET"}, set(), writes)
        deletes = re.findall(r"DELETE FROM [^\"]+", SRC)
        self.assertEqual(len(deletes), 4, deletes)
        self.assertIn("DELETE FROM memories WHERE id = %s AND source = %s", SRC)
        self.assertIn("DELETE FROM gateway_traces WHERE trace_id = %s AND channel = 'hc'", SRC)
        for verb in (r"\bDROP\s", r"TRUNCATE", r"launchctl", r"systemctl", r"subprocess", r"os\.system", r"kill\("):
            self.assertNotRegex(SRC, verb)

    def test_key_comes_from_the_secret_store_and_macs_are_compared_in_constant_time(self):
        self.assertIn("nova_secrets.get_secret(KEY_NAME)", SRC)
        self.assertIn("hmac.compare_digest", SRC)
        self.assertEqual(bt.KEY_NAME, "nova-blood-test-key")

    def test_forged_beat_fails(self):
        b = _beat()
        b["nonce"] = "forged"
        ok, detail = bt.beat_verdict([b], NOW, KEY)
        self.assertFalse(ok)
        self.assertIn("signature does not verify", detail)

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class TestPerformance(unittest.TestCase):
    def test_classification_and_share_on_10k_registry_rows(self):
        rows = [(n, f"node{i}", "/health" if i % 2 else None) for i in range(10000)
                for n in ("gateway",)]
        t = time.monotonic()
        inv = bt.inventory(rows)
        sh = bt.imitable_share(inv)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertGreater(sh["chat"]["share"], 0.99)

    def test_integrity_walk_10k_chain_rows(self):
        prev, rows = None, []
        for i in range(10000):
            parts = bt.row_parts(NOW, "pg", "c", "blood", True, i, "d")
            prev, _ = bt.mac_for(None, prev, *parts)
            rows.append((NOW, "pg", "c", "blood", True, i, "d", prev, "chain"))
        cur = _Cur([("FROM blood_test ORDER BY id", rows)])
        t = time.monotonic()
        self.assertEqual(bt.verify_integrity(cur, None), (10000, 0, 0))
        self.assertLess(time.monotonic() - t, 3.0)


class TestRetry(unittest.TestCase):
    def test_http_retries_transport_errors_with_backoff(self):
        calls, sleeps = [], []
        resp = mock.MagicMock()
        resp.__enter__.return_value.getcode.return_value = 200
        resp.__enter__.return_value.read.return_value = b"ok"

        def flaky(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.URLError("refused")
            return resp
        with mock.patch.object(bt.urllib.request, "urlopen", side_effect=flaky):
            self.assertEqual(bt._http("http://x/", _sleep=sleeps.append), (200, "ok"))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_http_error_is_not_retried(self):
        err = urllib.error.HTTPError("http://x/", 503, "down", {}, io.BytesIO(b"busy"))
        with mock.patch.object(bt.urllib.request, "urlopen", side_effect=err) as u:
            self.assertEqual(bt._http("http://x/", _sleep=lambda s: None)[0], 503)
        self.assertEqual(u.call_count, 1)

    def test_memory_search_retries_until_the_replica_catches_up(self):
        seen = {"n": 0}

        def http(url, data=None, **kw):
            if url.endswith("/remember"):
                return 200, json.dumps({"id": "m1", "status": "stored"})
            seen["n"] += 1
            mems = [{"text": "nonce btn1"}] if seen["n"] == 3 else []
            return 200, json.dumps({"memories": mems})
        mc = mock.Mock(cursor=mock.Mock(return_value=_Cur()))
        ok, detail = bt.canary_memory("btn1", _http_fn=http, _mem_conn=lambda: mc, _sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertIn("attempt 3", detail)

    def test_chat_canary_fails_open(self):
        # RETRY GAP: canary_chat — one agent turn per run on purpose (each costs ~10 s of GPU)
        def boom(*a, **k):
            raise urllib.error.URLError("gateway down")
        cur = _Cur()
        ok, detail = bt.canary_chat(cur, "btz", _http_fn=boom)
        self.assertFalse(ok)
        self.assertIn("URLError", detail)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bt.selftest(), 0)

    def test_mac_modes(self):
        m, k = bt.mac_for(KEY, None, "a", 1)
        self.assertEqual(k, "hmac")
        self.assertIsNone(bt.mac_ok(None, m, k, None, "a", 1))       # signed, no key here: unverifiable
        c, k2 = bt.mac_for(None, "prev", "a", 1)
        self.assertEqual(k2, "chain")
        self.assertTrue(bt.mac_ok(None, c, k2, "prev", "a", 1))
        self.assertFalse(bt.mac_ok(None, c, k2, "other", "a", 1))

    def test_echo(self):
        self.assertTrue(bt.echo_ok("bt0a1b", "Sure: `BT0A1B`"))
        self.assertFalse(bt.echo_ok("bt0a1b", "**OK**"))
        self.assertFalse(bt.echo_ok("", "anything"))
        self.assertFalse(bt.echo_ok("bt0a1b", None))

    def test_parse_feed(self):
        xml = ("<rss><channel><lastBuildDate>Thu, 15 Oct 2026 13:37:23 -0700</lastBuildDate>"
               "<item><title>Dogs &amp; Cats</title><link>https://j/x/</link></item>"
               "<item><title>older</title><link>https://j/y/</link></item></channel></rss>")
        built, link, title = bt.parse_feed(xml)
        self.assertEqual(built, datetime(2026, 10, 15, 20, 37, 23, tzinfo=timezone.utc))
        self.assertEqual((link, title), ("https://j/x/", "Dogs & Cats"))
        self.assertEqual(bt.parse_feed(""), (None, None, None))

    def test_beat_verdicts(self):
        self.assertTrue(bt.beat_verdict([], NOW, KEY)[0])
        self.assertTrue(bt.beat_verdict([_beat()], NOW, KEY)[0])
        ok, d = bt.beat_verdict([_beat(age_min=200)], NOW, KEY)
        self.assertFalse(ok)
        self.assertIn("200 min ago", d)
        ok, d = bt.beat_verdict([_beat(age_min=20000)], NOW, KEY)
        self.assertTrue(ok)
        self.assertIn("retired", d)
        ok, d = bt.beat_verdict([_beat(), _beat("mac-studio", 100)], NOW, KEY)
        self.assertFalse(ok)
        self.assertIn("fresh: nova-core", d)

    def test_registry_checks_are_imitable(self):
        self.assertEqual(bt.classify_registry(None)[0], "imitable")
        self.assertIn("4xx", bt.classify_registry("/health")[1])

    def test_impostor(self):
        self.assertTrue(bt.is_impostor(True, False))
        self.assertFalse(bt.is_impostor(None, False))
        self.assertFalse(bt.is_impostor(True, True))

    def test_monthly_stats(self):
        self.assertEqual(bt.monthly_stats([("2026-10", "pg", 4, 3, 1)])[0]["pass_pct"], 75.0)
        self.assertIsNone(bt.monthly_stats([("2026-10", "pg", 0, 0, 0)])[0]["pass_pct"])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("import nova_pg_schema", SRC)

    def test_probe_rows_follow_the_slo_pattern(self):
        cur = _Cur()
        bt.record_probe(cur, "memory", True, 120, "ok")
        self.assertIn("telemetry.probe_results", cur.sql[0])
        self.assertEqual(cur.params[0][0], "blood_memory")

    def test_inventory_covers_every_critical_service_with_a_blood_check(self):
        inv = bt.inventory([("postgresql", "mac-studio", None), ("plex", "nova-core", "/identity")])
        sh = bt.imitable_share(inv)
        for s in bt.CRITICAL:
            self.assertGreaterEqual(sh[s]["blood"], 1, s)
        self.assertFalse(any("plex" in r["check"] for r in inv))     # not a critical service

    def test_record_chains_when_there_is_no_key(self):
        cur = _Cur()
        m1 = bt.record(cur, None, {"service": "pg", "check": "c", "kind": "blood", "ok": True, "ts": NOW}, None)
        m2 = bt.record(cur, None, {"service": "pg", "check": "c", "kind": "blood", "ok": True, "ts": NOW}, m1)
        self.assertNotEqual(m1, m2)
        self.assertEqual(cur.params[1][-2], m2)
        self.assertEqual(cur.params[1][-1], "chain")

    def test_pg_canary_writes_then_reads_on_a_second_connection(self):
        state = {}

        def capture(c):
            return (False,)
        cur = _Cur([("pg_is_in_recovery", capture)])
        orig_exec = cur.execute

        def ex(sql, params=None):
            orig_exec(sql, params)
            if sql.startswith("INSERT INTO blood_canary"):
                state["row"] = params
        cur.execute = ex
        c2cur = _Cur([("FROM blood_canary WHERE key = 'pg'",
                       lambda c: (state["row"][0], state["row"][1], state["row"][3], state["row"][4]))])
        c2 = mock.Mock(cursor=mock.Mock(return_value=c2cur))
        ok, d = bt.canary_pg(cur, KEY, "btpg", NOW, _connect=lambda: c2)
        self.assertTrue(ok, d)
        c2.close.assert_called_once()

    def test_pg_canary_catches_a_standby_posing_as_primary(self):
        ok, d = bt.canary_pg(_Cur([("pg_is_in_recovery", (True,))]), KEY, "n", NOW, _connect=mock.Mock())
        self.assertFalse(ok)
        self.assertIn("in recovery", d)


class TestFunctional(unittest.TestCase):
    def _patch(self, notify, **canaries):
        p = [mock.patch.object(bt, "sibling", return_value=(True, "imitable ok")),
             mock.patch.object(bt, "canary_memory", return_value=canaries.get("memory", (True, "m"))),
             mock.patch.object(bt, "canary_chat", return_value=canaries.get("chat", (True, "c"))),
             mock.patch.object(bt, "canary_pg", return_value=canaries.get("pg", (True, "p"))),
             mock.patch.object(bt, "canary_journal", return_value=canaries.get("journal", (True, "j"))),
             mock.patch.object(bt, "read_beats", return_value=[]),
             mock.patch.object(bt, "short_host", return_value="nova-core"),
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify),
                                           "nova_pg_schema": mock.Mock()})]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_golden_path_records_canaries_probes_inventory_and_beat(self):
        notify = mock.Mock(return_value=True)
        self._patch(notify)
        cur = _Cur([("/ 3600 FROM blood_test WHERE src = 'inventory'", (None,)),
                    ("FROM service_registry ORDER BY", [("gateway", "nova-core", "/health")])])
        out = bt.scheduled(cur, KEY)
        self.assertEqual(len(out["results"]), 5)
        self.assertGreater(out["inventory"], 30)
        ins = [p for q, p in zip(cur.sql, cur.params) if q.startswith("INSERT INTO blood_test")]
        self.assertEqual(sum(1 for p in ins if p[7] == "canary"), 5)
        self.assertEqual(sum(1 for q in cur.sql if "telemetry.probe_results" in q), 5)
        self.assertTrue(any(q.startswith("INSERT INTO blood_canary") for q in cur.sql))   # this host's beat
        notify.assert_not_called()                                   # steady green: silent

    def test_impostor_pages_critical(self):
        notify = mock.Mock(return_value=True)
        self._patch(notify, chat=(False, "reply did not contain the nonce: '**OK**'"))
        cur = _Cur([("/ 3600 FROM blood_test WHERE src = 'inventory'", (1.0,)),
                    ("SELECT ok FROM blood_test", (True,))])
        out = bt.scheduled(cur, KEY)
        chat = [r for r in out["results"] if r["service"] == "chat"][0]
        self.assertTrue(chat["impostor"])
        kw = notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("critical", "blood_test", "blood:chat:impostor"))
        self.assertIn("IMPOSTOR: chat", notify.call_args.args[0])
        self.assertEqual(out["inventory"], 0)                         # classified 1h ago: not again

    def test_dry_run_writes_nothing(self):
        cur = _Cur([("to_regclass('blood_canary')", (True,)), ("FROM service_registry ORDER BY", []),
                    ("FROM blood_canary WHERE key LIKE", [])])
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        notify = mock.Mock()
        with mock.patch.object(bt, "connect", return_value=conn), \
             mock.patch.object(bt, "load_key", return_value=None), \
             mock.patch.object(bt, "sibling", return_value=(True, "ok")), \
             mock.patch.object(bt, "canary_journal", return_value=(True, "j")), \
             mock.patch.object(bt, "canary_pg", return_value=(True, "read-only")) as pg, \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(bt.main(["--dry-run"]), 0)
        self.assertIn("nothing written", out.getvalue())
        self.assertIn("skipped in --dry-run", out.getvalue())
        self.assertFalse(pg.call_args.kwargs["write"])
        notify.assert_not_called()
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER)", q) for q in cur.sql))


class TestFrame(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "NOVA_TEST_QUIET": "1"})

    def test_help_and_selftest_exit_zero(self):
        self.assertEqual(self._run("--help").returncode, 0)
        r = self._run("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertTrue(callable(bt.main))
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for col in ("service text", '"check" text', "kind text", "ok boolean", "latency_ms integer", "detail text"):
            self.assertIn(col, SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
