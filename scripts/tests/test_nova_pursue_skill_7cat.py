#!/usr/bin/env python3
"""7-category tests for the 2026-10-08 nova_pursue_skill model routing: model calls follow nova_llm_ping's live
ranking (best GPU node first), CPU-only nova-core7/.125 is always behind every GPU node, the ranking read is
retried and fails open to a static GPU-first list. Security, Performance, Retry, Unit, Integration, Functional,
Frame. No production writes (PG is mocked; the ranking row is only ever SELECTed). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_pursue_skill.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("ps7", SCRIPT)
ps = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(ps)
ps.log = lambda m: None

CPU = "192.168.1.125"


def row(ip, status="up", lat=100, loaded=("qwen3:8b",), chat=True):
    return {"node": ip, "url": f"http://{ip}:11434", "status": status, "latency_ms": lat,
            "has_chat_model": chat, "model": "qwen3:8b", "loaded": list(loaded)}


# The ranking as llm-ping wrote it on 2026-10-08 except .125 made FAST — the case that used to win.
RANK = {"ollama": [row(CPU, lat=90), row("192.168.1.252", lat=122), row("192.168.1.77", lat=164),
                   row("192.168.1.86", "slow", 9000), row("192.168.1.5", "down", None)]}


class _Resp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _conn(value):
    cur = mock.MagicMock(); cur.fetchone.return_value = (value,)
    conn = mock.MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _fresh():
    ps._RANK_CACHE.update(ts=0.0, val=None)


class TestSecurity(unittest.TestCase):
    def test_ranking_sql_is_parameterized_and_read_only(self):
        body = SRC.split("def load_ranking")[1].split("def ranked_endpoints")[0]
        self.assertIn("service=%s AND key=%s", body)
        for verb in ("INSERT", "UPDATE", "DELETE"):
            self.assertNotIn(verb, body)

    def test_local_fleet_only_no_cloud_endpoints(self):
        for e in ps.LLM_ENDPOINTS + [ps.ROUTER_FALLBACK]:
            self.assertRegex(e.split("|")[0], r"^http://192\.168\.1\.\d+:\d+/")

    def test_no_user_paths_or_secrets(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]+/")
        self.assertNotRegex(SRC.lower(), r"(api_key|password)\s*=\s*['\"]")

    def test_malformed_ranking_rows_are_ignored(self):
        bad = {"ollama": [None, "x", {"status": "up"}, {"url": "", "status": "up"}, row("192.168.1.77")]}
        eps = ps.ranked_endpoints(bad)
        self.assertTrue(eps[0].startswith("http://192.168.1.77:11434/"))


class TestPerformance(unittest.TestCase):
    def test_ranking_is_cached(self):
        _fresh()
        conn, cur = _conn(RANK)
        with mock.patch("psycopg2.connect", return_value=conn) as pc:
            for _ in range(50):
                ps.load_ranking()
        self.assertEqual(pc.call_count, 1)

    def test_ordering_large_ranking_fast(self):
        big = {"ollama": [row(f"10.0.{i // 250}.{i % 250}", lat=i) for i in range(5000)]}
        t = time.perf_counter(); ps.ranked_endpoints(big)
        self.assertLess(time.perf_counter() - t, 0.5)


class TestRetry(unittest.TestCase):
    def test_ranking_read_retries_then_succeeds(self):
        import psycopg2
        _fresh()
        conn, _ = _conn(json.dumps(RANK))
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("x"),
                                                         psycopg2.OperationalError("y"), conn]) as pc, \
                mock.patch.object(ps.time, "sleep") as sl:
            self.assertEqual(ps.load_ranking()["ollama"][0]["node"], CPU)
        self.assertEqual(pc.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.5, 1.0])

    def test_ranking_unreadable_fails_open_and_logs(self):
        import psycopg2
        _fresh()
        msgs = []
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("pg down")), \
                mock.patch.object(ps.time, "sleep"), mock.patch.object(ps, "log", msgs.append):
            self.assertIsNone(ps.load_ranking())
        self.assertTrue(any("ranking unreadable" in m for m in msgs))     # never silent
        eps = ps.ranked_endpoints(None)
        self.assertEqual(eps, [e for e in ps.LLM_ENDPOINTS if CPU not in e and e != ps.ROUTER_FALLBACK]
                         + [e for e in ps.LLM_ENDPOINTS if CPU in e] + [ps.ROUTER_FALLBACK])

    def test_llm_fails_over_down_the_ranked_list(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            if len(seen) < 3:
                raise OSError("node busy")
            return _Resp({"choices": [{"message": {"content": "ok"}}]})
        ps._RANK_CACHE.update(ts=time.time(), val=RANK)
        with mock.patch.object(ps.urllib.request, "urlopen", urlopen):
            self.assertEqual(ps.llm("x"), "ok")
        self.assertEqual([ps._host(u) for u in seen], ["192.168.1.252", "192.168.1.77", "192.168.1.86"])


class TestUnit(unittest.TestCase):
    def test_cpu_only_node_ranks_behind_every_gpu_node_even_when_fastest(self):
        hosts = [ps._host(e) for e in ps.ranked_endpoints(RANK)]
        self.assertEqual(hosts[:3], ["192.168.1.252", "192.168.1.77", "192.168.1.86"])
        gpu = [h for h in hosts if h not in (CPU, "192.168.1.2")]
        self.assertLess(max(hosts.index(h) for h in gpu), hosts.index(CPU))
        self.assertEqual(hosts[-1], "192.168.1.2")                     # router last of all

    def test_down_nodes_dropped_slow_after_up(self):
        hosts = [ps._host(e) for e in ps.ranked_endpoints(RANK)]
        self.assertNotIn("192.168.1.5", hosts)
        self.assertLess(hosts.index("192.168.1.77"), hosts.index("192.168.1.86"))

    def test_resident_model_beats_cold_node(self):
        r = {"ollama": [row("192.168.1.6", lat=50, loaded=()), row("192.168.1.7", lat=300)]}
        self.assertEqual(ps._host(ps.ranked_endpoints(r)[0]), "192.168.1.7")

    def test_nodes_without_chat_model_skipped(self):
        r = {"ollama": [row("192.168.1.6", chat=False), row("192.168.1.7")]}
        hosts = [ps._host(e) for e in ps.ranked_endpoints(r)]
        self.assertEqual(hosts[0], "192.168.1.7")

    def test_static_default_never_starts_on_cpu_node(self):
        self.assertNotIn(CPU, ps.LLM_ENDPOINTS[0])
        self.assertEqual(ps.LLM_ENDPOINTS[-1], ps.ROUTER_FALLBACK)

    def test_env_override_wins(self):
        with mock.patch.object(ps, "LLM_ENDPOINTS_ENV", ["http://192.168.1.9:1/v1/chat/completions|m"]):
            self.assertEqual(ps.ranked_endpoints(RANK), ["http://192.168.1.9:1/v1/chat/completions|m"])


class TestIntegration(unittest.TestCase):
    def test_reads_llm_ping_row_shape(self):
        """The real llm-ping rank() output feeds ranked_endpoints unchanged."""
        spec = importlib.util.spec_from_file_location("lp_for_ps", SCRIPTS / "nova_llm_ping.py")
        lp = importlib.util.module_from_spec(spec); spec.loader.exec_module(lp)
        res = [dict(row(CPU, lat=50), kind="ollama"), dict(row("192.168.1.77", lat=200), kind="ollama")]
        eps = ps.ranked_endpoints(lp.rank(res))
        self.assertEqual(ps._host(eps[0]), "192.168.1.77")

    def test_jsonb_string_value_accepted(self):
        _fresh()
        conn, cur = _conn(json.dumps(RANK))
        with mock.patch("psycopg2.connect", return_value=conn):
            self.assertEqual(ps.load_ranking(), RANK)
        self.assertEqual(cur.execute.call_args.args[1], ("nova_llm_ping", "ranking"))
        conn.close.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_pursuit_model_call_goes_to_best_gpu_node_first(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.full_url); body = json.loads(req.data)
            self.assertEqual(body["model"], "qwen3:8b")
            return _Resp({"choices": [{"message": {"content": "next step [1]"}}]})
        ps._RANK_CACHE.update(ts=time.time(), val=RANK)
        with mock.patch.object(ps.urllib.request, "urlopen", urlopen):
            self.assertTrue(ps.llm("pursue"))
        self.assertEqual(seen, ["http://192.168.1.252:11434/v1/chat/completions"])


class TestFrame(unittest.TestCase):
    def test_compiles_and_selftest_runs(self):
        subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], check=True)
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_public_symbols(self):
        for name in ("load_ranking", "ranked_endpoints", "llm", "CPU_ONLY_HOSTS", "ROUTER_FALLBACK"):
            self.assertTrue(hasattr(ps, name), name)


if __name__ == "__main__":
    unittest.main()
