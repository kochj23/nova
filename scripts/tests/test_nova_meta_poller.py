#!/usr/bin/env python3
"""Tests for nova_meta_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG, the memory server, Ollama, the gateway and df are mocked; the
module's file logger is never attached. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_meta_poller.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("metapoller", SCRIPTS / "nova_meta_poller.py")
    mod = importlib.util.module_from_spec(spec)
    with patch("logging.basicConfig"):          # never attach a handler to the real log file
        spec.loader.exec_module(mod)
    return mod


mp = _load()
mp.log = MagicMock()


class _Cur:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.sql.append((sql, params))

    def fetchone(self):
        return (self.c.val,)

    def fetchall(self):
        return self.c.rows


class _Conn:
    def __init__(self, name="db", val=5, rows=None):
        self.name = name; self.val = val; self.rows = rows or []; self.sql = []; self.closed = False
        self.autocommit = False; self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", SRC)

    def test_insert_is_parameterized(self):
        conn = _Conn()
        mp.insert_metric(conn, "x'); --", 1.5, {"a": 1})
        sql, params = conn.sql[0]
        self.assertNotIn("x');", sql)
        self.assertEqual(params, ("x'); --", 1.5, '{"a": 1}'))
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))


class TestPerformance(unittest.TestCase):
    def test_vector_count_sums_rows_fast(self):
        rows = [(f"s{i}", i) for i in range(10_000)]
        t0 = time.perf_counter()
        total, srcs = mp.collect_vector_count_by_source(_Conn(rows=rows))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(total, float(sum(range(10_000))))
        self.assertEqual(len(srcs), 10_000)


class TestRetry(unittest.TestCase):
    def test_http_collectors_fail_open(self):
        # RETRY GAP: collect_memories_total / ollama / gateway — one GET per 5-minute poll; failures -> (None, None)
        with patch.object(mp.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(mp.collect_memories_total(), (None, None))
            self.assertEqual(mp.collect_ollama_vram_gb(), (None, None))
            self.assertEqual(mp.collect_gateway_latency_ms(), (None, None))
        self.assertEqual(uo.call_count, 3)

    def test_shared_conn_failure_falls_back_to_self_connect(self):
        opened = []

        def gc(db):
            opened.append(db)
            if db == "nova_memories" and opened.count("nova_memories") == 1:
                raise RuntimeError("too many clients")
            return _Conn(db)

        with patch.object(mp, "get_conn", side_effect=gc), \
             patch.object(mp, "COLLECTORS", [("memories_today", mp.collect_memories_today)]):
            mp.poll_once()
        self.assertEqual(opened, ["nova_ops", "nova_memories", "nova_memories"])


class TestUnit(unittest.TestCase):
    def test_api_cost_shapes(self):
        today = date.today().isoformat()     # computed at call time, never pinned
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            with patch.object(mp, "API_COSTS_JSON", p):
                self.assertEqual(mp.collect_api_cost_today()[0], 0.0)              # missing file
                for blob, want in (({today: 1.25}, 1.25), ({"daily": {today: 2}}, 2.0), ({"today": 3}, 3.0),
                                   ({"total_today": 4}, 4.0), ({"x": 1}, 0.0)):
                    p.write_text(json.dumps(blob))
                    self.assertEqual(mp.collect_api_cost_today()[0], want)
                p.write_text("{bad")
                self.assertEqual(mp.collect_api_cost_today(), (None, None))

    def test_disk_df_parsing(self):
        out = "Filesystem 1G-blocks Used Available Capacity Mounted\n/dev/disk1 1000 250 750 25% /\n"
        with patch.object(mp.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=out)):
            total, meta = mp.collect_disk_used_gb()
        self.assertEqual(total, 250.0 * len(mp.DISK_MOUNTS))
        self.assertEqual(meta["/"], 250.0)
        with patch.object(mp.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            self.assertEqual(mp.collect_disk_used_gb(), (0.0, None))

    def test_ollama_vram_sum(self):
        body = json.dumps({"models": [{"name": "a", "size_vram": 2 * 1024 ** 3},
                                      {"name": "b", "size_vram": 1024 ** 3}]}).encode()
        with patch.object(mp.urllib.request, "urlopen", return_value=_Resp(body)):
            self.assertEqual(mp.collect_ollama_vram_gb(), (3.0, {"a": 2.0, "b": 1.0}))

    def test_article_count_today(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a.md").write_text("x"); (Path(td) / "sub").mkdir(); (Path(td) / "sub/b.md").write_text("y")
            old = Path(td) / "old.md"; old.write_text("z"); os.utime(old, (0, 0))
            with patch.object(mp, "JOURNAL_CONTENT_DIR", Path(td)):
                self.assertEqual(mp.collect_article_count_today(), (2.0, None))


class TestIntegration(unittest.TestCase):
    def test_one_shared_memories_conn_for_three_collectors(self):
        conns = []

        def gc(db):
            c = _Conn(db); conns.append(c); return c

        with patch.object(mp, "get_conn", side_effect=gc), patch.object(mp, "COLLECTORS", [
                ("memories_today", mp.collect_memories_today),
                ("ingest_rate_per_hour", mp.collect_ingest_rate_per_hour),
                ("vector_count_by_source", mp.collect_vector_count_by_source)]):
            mp.poll_once()
        self.assertEqual([c.name for c in conns], ["nova_ops", "nova_memories"])
        mem = conns[1]
        self.assertTrue(mem.autocommit and mem.closed)
        self.assertEqual(len(mem.sql), 3)
        self.assertTrue(all("telemetry.nova_meta" in s for s, _ in conns[0].sql))

    def test_memory_stats_url_uses_config(self):
        import nova_config
        self.assertEqual(mp.MEMORY_STATS_URL, f"http://{nova_config.LAN_IP}:18790/stats")


class TestFunctional(unittest.TestCase):
    def test_poll_once_writes_non_null_metrics(self):
        ops = _Conn("nova_ops")
        cols = [("a", lambda: (1.0, None)), ("b", lambda: (None, None)), ("c", lambda: (2.0, {"k": 1})),
                ("d", MagicMock(side_effect=RuntimeError("boom")))]
        with patch.object(mp, "get_conn", side_effect=lambda db: ops if db == "nova_ops" else _Conn(db)), \
             patch.object(mp, "COLLECTORS", cols):
            mp.poll_once()
        self.assertEqual([p[0] for _, p in ops.sql], ["a", "c"])
        self.assertEqual(ops.commits, 2)
        self.assertTrue(ops.closed)

    def test_ops_connect_failure_writes_nothing(self):
        col = MagicMock(return_value=(1.0, None))
        with patch.object(mp, "get_conn", side_effect=RuntimeError("pg down")), \
             patch.object(mp, "COLLECTORS", [("a", col)]):
            self.assertIsNone(mp.poll_once())
        col.assert_not_called()

    def test_main_loop_survives_and_sleeps(self):
        class Stop(Exception):
            pass
        with patch.object(mp, "poll_once", side_effect=RuntimeError("x")), \
             patch.object(mp.time, "sleep", side_effect=Stop) as sl:
            with self.assertRaises(Stop):
                mp.main()
        sl.assert_called_once_with(mp.POLL_INTERVAL)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running the script is a forever poll loop, so the smoke is an import with an isolated HOME
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_meta_poller"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
            lf = Path(home) / ".openclaw/logs/meta_poller.log"
            self.assertFalse(lf.exists() and lf.read_text())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
