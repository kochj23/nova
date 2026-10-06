#!/usr/bin/env python3
"""Tests for nova_lb.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_lb.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LB = _load("nova_lb_under_test", SCRIPT)


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d


def _node(name):
    return next(n for n in LB._pool if n.name == name)


def _set_all(status, latency=10.0):
    for n in LB._pool:
        n.status = status
        n.latencies.clear()
        n.latencies.append(latency)
        n.active_connections = 0


class _Base(unittest.TestCase):
    def setUp(self):
        LB._init_pool()
        LB._sticky_map.clear()
        LB._last_dns_ip = None


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_tsig_secret_comes_from_keychain_at_runtime(self):
        self.assertIn('"security", "find-generic-password"', SRC)
        self.assertIn("nova-bind-tsig-key", SRC)
        self.assertNotRegex(SRC, r"hmac-sha256:nova-dns-key:[A-Za-z0-9+/=]{8,}")   # the -y arg is built from the lookup
        with mock.patch("subprocess.run", return_value=types.SimpleNamespace(stdout="s3cr3t\n")) as sp:
            self.assertEqual(LB._tsig_secret(), "s3cr3t")
        self.assertIn("-w", sp.call_args[0][0])

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, now())", SRC)

    def test_dns_update_never_prints_the_secret(self):
        _set_all("up")
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(LB, "_tsig_secret", return_value="TOPSECRET"), \
             mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=0, stderr="")), \
             redirect_stdout(buf), redirect_stderr(err):
            LB._report_to_dns()
        self.assertNotIn("TOPSECRET", buf.getvalue() + err.getvalue())


class TestPerformance(_Base):
    def test_10k_picks_under_two_seconds(self):
        _set_all("up")
        for i, n in enumerate(LB._pool):
            n.latencies.append(float(i))
        t0 = time.perf_counter()
        for i in range(10_000):
            LB.pick_node(strategy="least_conn" if i % 2 else "latency", protocol="ollama")
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_latency_window_is_bounded(self):
        n = _node("mac-studio")
        for i in range(10_000):
            n.latencies.append(float(i))
        self.assertEqual(len(n.latencies), LB.WINDOW_SIZE)


class TestRetry(_Base):
    def test_single_probe_is_one_shot_and_fails_open(self):
        # RETRY GAP: _probe_node — one urlopen per cycle; a failure returns (False, elapsed) and never raises
        with mock.patch.object(LB.urllib.request, "urlopen", side_effect=OSError("refused")) as uo:
            ok, ms = LB._probe_node(_node("nova-core2"))
        self.assertFalse(ok)
        self.assertGreaterEqual(ms, 0.0)
        self.assertEqual(uo.call_count, 1)

    def test_pool_marks_down_after_consecutive_failures_only(self):
        n = _node("nova-core7")
        n.status = "up"
        with mock.patch.object(LB, "_probe_node", return_value=(False, 5.0)):
            LB._probe_all()
            self.assertEqual(n.status, "up")                    # one failure is tolerated
            LB._probe_all()
        self.assertEqual(n.status, "down")
        self.assertEqual(n.consecutive_failures, LB.MARK_DOWN_AFTER)

    def test_pool_ramps_back_after_successes(self):
        n = _node("nova-core7")
        n.status = "down"
        with mock.patch.object(LB, "_probe_node", return_value=(True, 7.0)):
            LB._probe_all()
            self.assertEqual(n.status, "ramping")
            self.assertTrue(n.is_healthy)                       # ramping already takes work
            for _ in range(LB.RAMP_BACK_PROBES - 1):
                LB._probe_all()
        self.assertEqual(n.status, "up")
        self.assertEqual(n.consecutive_successes, LB.RAMP_BACK_PROBES)

    def test_pg_report_failure_is_swallowed(self):
        # RETRY GAP: _report_to_pg — one connect attempt per cycle, error printed to stderr only
        err = io.StringIO()
        with mock.patch("psycopg2.connect", side_effect=OSError("pg down")), redirect_stderr(err):
            LB._report_to_pg()
        self.assertIn("PG report failed", err.getvalue())


class TestUnit(_Base):
    def test_probe_node_parses_health(self):
        with mock.patch.object(LB.urllib.request, "urlopen", return_value=_Resp({"status": "ok"})):
            ok, ms = LB._probe_node(_node("nova-core2"))
        self.assertTrue(ok)
        with mock.patch.object(LB.urllib.request, "urlopen", return_value=_Resp({"status": "degraded"})):
            self.assertFalse(LB._probe_node(_node("nova-core2"))[0])

    def test_pick_latency_vs_least_conn(self):
        _set_all("up")
        fast, slow = _node("nova-core2"), _node("nova-core7")
        fast.latencies.clear(); fast.latencies.append(1.0)
        slow.latencies.clear(); slow.latencies.append(2.0)
        for n in LB._pool:
            if n not in (fast, slow):
                n.latencies.clear(); n.latencies.append(100.0)
        self.assertEqual(LB.pick_node()["name"], "nova-core2")
        fast.active_connections = 5
        self.assertEqual(LB.pick_node(strategy="least_conn")["name"], "nova-core7")

    def test_filters_gpu_exclude_protocol(self):
        _set_all("up")
        self.assertTrue(LB.pick_node(require_gpu=True)["name"] != "nova-core2")
        names = {LB.pick_node(exclude=[n.name for n in LB._pool if n.name != "mac-studio"])["name"]}
        self.assertEqual(names, {"mac-studio"})
        r = LB.pick_node(protocol="llamacpp")
        self.assertEqual((r["name"], r["port"]), ("mac-studio", 11435))
        self.assertIsNone(LB.pick_node(protocol="nope"))

    def test_sticky_sessions_restick_when_node_drains(self):
        _set_all("up")
        first = LB.pick_node(session_id="s1")
        self.assertFalse(first["sticky"])
        again = LB.pick_node(session_id="s1")
        self.assertEqual((again["name"], again["sticky"]), (first["name"], True))
        LB.drain_node(first["name"])
        moved = LB.pick_node(session_id="s1")
        self.assertNotEqual(moved["name"], first["name"])
        self.assertEqual(LB._sticky_map["s1"], moved["name"])

    def test_capacity_and_drain(self):
        _set_all("up")
        for n in LB._pool:
            n.active_connections = n.max_connections
        self.assertIsNone(LB.pick_node())
        self.assertIsNotNone(LB.pick_node(respect_capacity=False))
        self.assertTrue(LB.drain_node("mac-studio"))
        self.assertFalse(LB.drain_node("ghost"))
        self.assertFalse(_node("mac-studio").accepts_new_work)
        self.assertTrue(LB.undrain_node("mac-studio"))
        self.assertEqual(_node("mac-studio").status, "ramping")
        self.assertFalse(LB.undrain_node("mac-studio"))             # only a draining node can undrain

    def test_connection_counters_never_go_negative(self):
        LB.mark_done("nova-core2")
        self.assertEqual(_node("nova-core2").active_connections, 0)
        LB.mark_start("nova-core2"); LB.mark_start("nova-core2"); LB.mark_done("nova-core2")
        n = _node("nova-core2")
        self.assertEqual((n.active_connections, n.total_connections), (1, 2))

    def test_empty_pool_and_inf_latency(self):
        n = _node("nova-core2")
        self.assertEqual(n.p50_ms, float("inf"))
        self.assertEqual(n.avg_ms, float("inf"))
        self.assertIsNone(LB.pick_node())                             # everything "unknown"
        self.assertEqual(len(LB.get_pool_status()), len(LB.NODES))


class TestIntegration(_Base):
    def test_pick_node_shared_reads_lb_pool_status(self):
        rows = [("nova-core7", "up", 50.0, 0, None, 5.0),
                ("nova-core10", "up", 5.0, 0, None, 99.0),        # stale — daemon silent
                ("ghost", "up", 1.0, 0, None, 1.0),               # not in NODES
                ("mac-studio", "down", 1.0, 0, None, 1.0),
                ("tv-movies-mini", "ramping", 20.0, 3, None, 2.0)]
        cur = mock.MagicMock(); cur.fetchall.return_value = rows
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn):
            best = LB.pick_node_shared(protocol="ollama")
            lc = LB.pick_node_shared(strategy="least_conn")
        self.assertIn("FROM lb_pool_status", cur.execute.call_args[0][0])
        self.assertEqual((best["name"], best["port"], best["latency_ms"]), ("tv-movies-mini", 11434, 20.0))
        self.assertEqual(lc["name"], "nova-core7")
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertIsNone(LB.pick_node_shared())

    def test_report_to_pg_writes_every_node(self):
        _set_all("up")
        cur = mock.MagicMock(); conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn):
            LB._report_to_pg()
        inserts = [c for c in cur.execute.call_args_list if "INSERT INTO lb_pool_status" in c[0][0]]
        self.assertEqual(len(inserts), len(LB.NODES))
        self.assertEqual(inserts[0][0][1][0], LB.NODES[0]["name"])

    def test_dns_follows_fastest_gpu_node_and_only_on_change(self):
        with mock.patch.object(LB, "pick_node", return_value={"name": "nova-core7", "ip": "192.168.1.125"}) as pn, \
             mock.patch.object(LB, "_tsig_secret", return_value="k"), \
             mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=0, stderr="")) as sp, \
             redirect_stdout(io.StringIO()):
            LB._report_to_dns(); LB._report_to_dns()
        self.assertEqual(pn.call_args.kwargs, {"require_gpu": True})
        self.assertEqual(sp.call_count, 1)
        script = sp.call_args.kwargs["input"]
        self.assertIn(f"update add ollama.{LB.DNS_ZONE}. {LB.DNS_TTL} A 192.168.1.125", script)
        self.assertIn("update add cluster.", script)
        self.assertEqual(sp.call_args[0][0][0], "nsupdate")


class TestFunctional(_Base):
    def test_probe_loop_one_cycle(self):
        LB._running = True
        def _stop(_): LB._running = False
        with mock.patch.object(LB, "_probe_all") as pa, mock.patch.object(LB, "_report_to_pg") as rp, \
             mock.patch.object(LB, "_report_to_dns") as rd, mock.patch.object(LB.time, "sleep", side_effect=_stop) as sl:
            LB._probe_loop()
        self.assertEqual((pa.call_count, rp.call_count, rd.call_count), (1, 1, 1))
        sl.assert_called_once_with(LB.PROBE_INTERVAL)
        LB._running = True

    def test_full_cycle_with_everything_down_leaves_dns_alone(self):
        with mock.patch.object(LB.urllib.request, "urlopen", side_effect=OSError("x")), \
             mock.patch("subprocess.run") as sp, redirect_stderr(io.StringIO()):
            LB._probe_all(); LB._probe_all(); LB._report_to_dns()
        self.assertTrue(all(n.status == "down" for n in LB._pool))
        self.assertIsNone(LB.pick_node())
        sp.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_lb; print(len(nova_lb.get_pool_status()))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(len(LB.NODES)))


if __name__ == "__main__":
    unittest.main()
