#!/usr/bin/env python3
"""7-category gap tests for nova_pursue_skill.py — the skills added 2026-10-07/08 (documentary #143,
aviation-ref #148, crime-drama #149) and the PG connect retry added here. Memory server, LLM and PG
are mocked. Base suite: test_nova_pursue_skill.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_pursue_skill_7cat.py
"""
import importlib.util
import io
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pursue_skill.py"
spec = importlib.util.spec_from_file_location("ps_7cat", SCRIPT)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)
SRC = SCRIPT.read_text()
NEW = {"pursue-interest-documentary": ("documentary", "documentary"),
       "pursue-interest-aviation-ref": ("aviation ref", "aviation_ref"),
       "pursue-interest-crime-drama": ("crime drama", "crime_drama")}


class _Quiet(unittest.TestCase):
    def setUp(self):
        r = redirect_stdout(io.StringIO()); r.__enter__(); self.addCleanup(r.__exit__, None, None, None)
        p = mock.patch.object(ps.time, "sleep"); self.sleep = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Quiet):
    def test_new_skills_stay_off_the_web(self):
        for slug in NEW:
            self.assertFalse(ps.SKILLS[slug]["web"], slug)

    def test_new_skills_read_only_their_own_vector(self):
        for slug, (_, src) in NEW.items():
            self.assertEqual(ps.SKILLS[slug]["sources"], [src])

    def test_unknown_or_hostile_slug_refused_before_db(self):
        with mock.patch.object(ps, "_pg_connect") as c:
            self.assertEqual(ps.run_skill("'; DROP --")["handled"], False)
        c.assert_not_called()

    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"password\s*=|sk-[A-Za-z0-9]{20}|/Users/[a-z]")


class TestPerformance(_Quiet):
    def test_connect_has_timeout_and_bounded_backoff(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("x")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            ps._pg_connect("dsn")
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 10)
        self.assertLessEqual(sum(a.args[0] for a in self.sleep.call_args_list), 10)

    def test_topic_lookup_fast(self):
        t = time.perf_counter()
        for _ in range(20000):
            ps.slug_for_topic("crime drama")
        self.assertLess(time.perf_counter() - t, 2.0)


class TestRetry(_Quiet):
    def test_pg_connect_retries_then_succeeds(self):
        conn = mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("failover"), conn]) as c:
            self.assertIs(ps._pg_connect("dsn"), conn)
        self.assertEqual(c.call_count, 2)
        self.sleep.assert_called_once_with(2.0)

    def test_pg_connect_gives_up_after_three(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            ps._pg_connect("dsn")
        self.assertEqual(c.call_count, 3)

    def test_non_operational_error_not_retried(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.ProgrammingError("bad dsn")) as c, \
                self.assertRaises(psycopg2.ProgrammingError):
            ps._pg_connect("dsn")
        self.assertEqual(c.call_count, 1)

    def test_run_skill_uses_retrying_connect(self):
        cur = mock.MagicMock(); cur.fetchone.return_value = None
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch.object(ps, "_pg_connect", return_value=conn) as c, \
                mock.patch.object(ps, "load_card", return_value=None):
            ps.run_skill("pursue-interest-crime-drama")
        self.assertEqual([a.args[0] for a in c.call_args_list], [ps.OPS_DSN, ps.MEM_DSN])


class TestUnit(_Quiet):
    def test_new_topics_map_to_their_skill(self):
        for slug, (topic, src) in NEW.items():
            self.assertEqual(ps.slug_for_topic(topic), slug)
            self.assertEqual(ps.slug_for_topic(topic.upper() + "  "), slug)
            self.assertEqual(ps.slug_for_source(src), slug)

    def test_crime_drama_looks_back_a_week(self):
        self.assertEqual(ps.SKILLS["pursue-interest-crime-drama"]["recent_days"], 7)

    def test_topics_unique_across_skills(self):
        topics = [c["topic"] for c in ps.SKILLS.values() if c.get("topic")]
        self.assertEqual(len(topics), len(set(topics)))


class TestIntegration(_Quiet):
    def test_build_query_uses_hint_for_new_skill(self):
        q = ps.build_query(ps.SKILLS["pursue-interest-aviation-ref"], None)
        self.assertIn("aviation", q.lower())

    def test_unapproved_card_never_runs(self):
        with mock.patch.object(ps, "load_card", return_value={"status": "proposed"}):
            r = ps.run_skill("pursue-interest-aviation-ref", oc=mock.MagicMock(), mc=mock.MagicMock())
        self.assertEqual(r, {"handled": False, "why": "status proposed"})


class TestFunctional(_Quiet):
    def test_selftest_passes_with_new_skills(self):
        self.assertEqual(ps.selftest(), 0)

    def test_pg_down_surfaces_error(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")), \
                self.assertRaises(psycopg2.OperationalError):
            ps.run_skill("pursue-interest-documentary")


class TestFrame(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_entrypoints(self):
        for n in ("main", "run_skill", "_pg_connect", "selftest"):
            self.assertTrue(callable(getattr(ps, n)))


# ─────────────── 2026-10-08: model routing by nova_llm_ping ranking (GPU first, CPU-only .125 last) ───────────────
import json  # noqa: E402
_rspec = importlib.util.spec_from_file_location("ps_7cat_routing", SCRIPT)
psr = importlib.util.module_from_spec(_rspec); _rspec.loader.exec_module(psr)
psr.log = lambda m: None

CPU = "192.168.1.125"


def _row(ip, status="up", lat=100, loaded=("qwen3:8b",), chat=True):
    return {"node": ip, "url": f"http://{ip}:11434", "status": status, "latency_ms": lat,
            "has_chat_model": chat, "model": "qwen3:8b", "loaded": list(loaded)}


# The ranking as llm-ping wrote it on 2026-10-08 except .125 made FAST — the case that used to win.
RANK = {"ollama": [_row(CPU, lat=90), _row("192.168.1.252", lat=122), _row("192.168.1.77", lat=164),
                   _row("192.168.1.86", "slow", 9000), _row("192.168.1.5", "down", None)]}


class _RResp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _rconn(value):
    cur = mock.MagicMock(); cur.fetchone.return_value = (value,)
    conn = mock.MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _rfresh():
    psr._RANK_CACHE.update(ts=0.0, val=None)


class TestRoutingSecurity(unittest.TestCase):
    def test_ranking_sql_is_parameterized_and_read_only(self):
        body = SRC.split("def load_ranking")[1].split("def ranked_endpoints")[0]
        self.assertIn("service=%s AND key=%s", body)
        for verb in ("INSERT", "UPDATE", "DELETE"):
            self.assertNotIn(verb, body)

    def test_local_fleet_only_no_cloud_endpoints(self):
        for e in psr.LLM_ENDPOINTS + [psr.ROUTER_FALLBACK]:
            self.assertRegex(e.split("|")[0], r"^http://192\.168\.1\.\d+:\d+/")

    def test_no_user_paths_or_secrets(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]+/")
        self.assertNotRegex(SRC.lower(), r"(api_key|password)\s*=\s*['\"]")

    def test_malformed_ranking_rows_are_ignored(self):
        bad = {"ollama": [None, "x", {"status": "up"}, {"url": "", "status": "up"}, _row("192.168.1.77")]}
        eps = psr.ranked_endpoints(bad)
        self.assertTrue(eps[0].startswith("http://192.168.1.77:11434/"))


class TestRoutingPerformance(unittest.TestCase):
    def test_ranking_is_cached(self):
        _rfresh()
        conn, cur = _rconn(RANK)
        with mock.patch("psycopg2.connect", return_value=conn) as pc:
            for _ in range(50):
                psr.load_ranking()
        self.assertEqual(pc.call_count, 1)

    def test_ordering_large_ranking_fast(self):
        big = {"ollama": [_row(f"10.0.{i // 250}.{i % 250}", lat=i) for i in range(5000)]}
        t = time.perf_counter(); psr.ranked_endpoints(big)
        self.assertLess(time.perf_counter() - t, 0.5)


class TestRoutingRetry(unittest.TestCase):
    def test_ranking_read_retries_then_succeeds(self):
        import psycopg2
        _rfresh()
        conn, _ = _rconn(json.dumps(RANK))
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("x"),
                                                         psycopg2.OperationalError("y"), conn]) as pc, \
                mock.patch.object(psr.time, "sleep") as sl:
            self.assertEqual(psr.load_ranking()["ollama"][0]["node"], CPU)
        self.assertEqual(pc.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.5, 1.0])

    def test_ranking_unreadable_fails_open_and_logs(self):
        import psycopg2
        _rfresh()
        msgs = []
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("pg down")), \
                mock.patch.object(psr.time, "sleep"), mock.patch.object(psr, "log", msgs.append):
            self.assertIsNone(psr.load_ranking())
        self.assertTrue(any("ranking unreadable" in m for m in msgs))     # never silent
        eps = psr.ranked_endpoints(None)
        self.assertEqual(eps, [e for e in psr.LLM_ENDPOINTS if CPU not in e and e != psr.ROUTER_FALLBACK]
                         + [e for e in psr.LLM_ENDPOINTS if CPU in e] + [psr.ROUTER_FALLBACK])

    def test_llm_fails_over_down_the_ranked_list(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            if len(seen) < 3:
                raise OSError("node busy")
            return _RResp({"choices": [{"message": {"content": "ok"}}]})
        psr._RANK_CACHE.update(ts=time.time(), val=RANK)
        with mock.patch.object(psr.urllib.request, "urlopen", urlopen):
            self.assertEqual(psr.llm("x"), "ok")
        self.assertEqual([psr._host(u) for u in seen], ["192.168.1.252", "192.168.1.77", "192.168.1.86"])


class TestRoutingUnit(unittest.TestCase):
    def test_cpu_only_node_ranks_behind_every_gpu_node_even_when_fastest(self):
        hosts = [psr._host(e) for e in psr.ranked_endpoints(RANK)]
        self.assertEqual(hosts[:3], ["192.168.1.252", "192.168.1.77", "192.168.1.86"])
        gpu = [h for h in hosts if h not in (CPU, "192.168.1.2")]
        self.assertLess(max(hosts.index(h) for h in gpu), hosts.index(CPU))
        self.assertEqual(hosts[-1], "192.168.1.2")                     # router last of all

    def test_down_nodes_dropped_slow_after_up(self):
        hosts = [psr._host(e) for e in psr.ranked_endpoints(RANK)]
        self.assertNotIn("192.168.1.5", hosts)
        self.assertLess(hosts.index("192.168.1.77"), hosts.index("192.168.1.86"))

    def test_resident_model_beats_cold_node(self):
        r = {"ollama": [_row("192.168.1.6", lat=50, loaded=()), _row("192.168.1.7", lat=300)]}
        self.assertEqual(psr._host(psr.ranked_endpoints(r)[0]), "192.168.1.7")

    def test_nodes_without_chat_model_skipped(self):
        r = {"ollama": [_row("192.168.1.6", chat=False), _row("192.168.1.7")]}
        hosts = [psr._host(e) for e in psr.ranked_endpoints(r)]
        self.assertEqual(hosts[0], "192.168.1.7")

    def test_static_default_never_starts_on_cpu_node(self):
        self.assertNotIn(CPU, psr.LLM_ENDPOINTS[0])
        self.assertEqual(psr.LLM_ENDPOINTS[-1], psr.ROUTER_FALLBACK)

    def test_env_override_wins(self):
        with mock.patch.object(psr, "LLM_ENDPOINTS_ENV", ["http://192.168.1.9:1/v1/chat/completions|m"]):
            self.assertEqual(psr.ranked_endpoints(RANK), ["http://192.168.1.9:1/v1/chat/completions|m"])


class TestRoutingIntegration(unittest.TestCase):
    def test_reads_llm_ping_row_shape(self):
        """The real llm-ping rank() output feeds ranked_endpoints unchanged."""
        spec = importlib.util.spec_from_file_location("lp_for_ps", SCRIPTS / "nova_llm_ping.py")
        lp = importlib.util.module_from_spec(spec); spec.loader.exec_module(lp)
        res = [dict(_row(CPU, lat=50), kind="ollama"), dict(_row("192.168.1.77", lat=200), kind="ollama")]
        eps = psr.ranked_endpoints(lp.rank(res))
        self.assertEqual(psr._host(eps[0]), "192.168.1.77")

    def test_jsonb_string_value_accepted(self):
        _rfresh()
        conn, cur = _rconn(json.dumps(RANK))
        with mock.patch("psycopg2.connect", return_value=conn):
            self.assertEqual(psr.load_ranking(), RANK)
        self.assertEqual(cur.execute.call_args.args[1], ("nova_llm_ping", "ranking"))
        conn.close.assert_called_once()


class TestRoutingFunctional(unittest.TestCase):
    def test_pursuit_model_call_goes_to_best_gpu_node_first(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.full_url); body = json.loads(req.data)
            self.assertEqual(body["model"], "qwen3:8b")
            return _RResp({"choices": [{"message": {"content": "next step [1]"}}]})
        psr._RANK_CACHE.update(ts=time.time(), val=RANK)
        with mock.patch.object(psr.urllib.request, "urlopen", urlopen):
            self.assertTrue(psr.llm("pursue"))
        self.assertEqual(seen, ["http://192.168.1.252:11434/v1/chat/completions"])


class TestRoutingFrame(unittest.TestCase):
    def test_compiles_and_selftest_runs(self):
        subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], check=True)
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_public_symbols(self):
        for name in ("load_ranking", "ranked_endpoints", "llm", "CPU_ONLY_HOSTS", "ROUTER_FALLBACK"):
            self.assertTrue(hasattr(psr, name), name)



if __name__ == "__main__":
    unittest.main()
