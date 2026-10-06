#!/usr/bin/env python3
"""Tests for nova_rem_sleep.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.parse    # noqa: F401
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rem_sleep.py"
SRC = SCRIPT.read_text()

import nova_config  # noqa: E402,F401
import nova_notify  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("nrs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


rs = _load()
rs.notify = MagicMock(name="notify")                               # never reach Slack


class _Cur:
    """Scripted cursor: answers by SQL shape; records every statement."""
    def __init__(self, sources=(("slack", 120),), pairs=None, members=None, cross=(), rowcount=3):
        self.sources = list(sources)
        self.pairs = pairs if pairs is not None else [(1, 2, 0.9, "a", "b"), (2, 3, 0.88, "b", "c"), (7, 8, 0.95, "x", "y")]
        self.members = members or {1: "alpha text", 2: "alpha again", 3: "alpha thrice", 7: "x one", 8: "x two"}
        self.cross = list(cross); self.rowcount = rowcount
        self.sql = []; self._ans = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "GROUP BY source" in sql:
            self._ans = self.sources
        elif "SELECT id, text, embedding" in sql and "WITH" not in sql:
            self._ans = [(i, "t", "e") for i in range(10)]
        elif "a.id, b.id, 1 -" in sql:
            self._ans = self.pairs
        elif "WHERE id IN" in sql:
            self._ans = [(i, self.members[i]) for i in params if i in self.members]
        elif "recent_diverse" in sql:
            self._ans = self.cross
        elif "GROUP BY tier" in sql:
            self._ans = [("long_term", 1000), ("scratchpad", 5)]
        elif "COUNT(*) FROM memory_links" in sql:
            self._ans = [(42,)]
        else:
            self._ans = []

    def fetchall(self):
        return list(self._ans)

    def fetchone(self):
        return self._ans[0]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.commits = 0; self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _resp(payload):
    r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
    return r


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", rs.PG_CONN)

    def test_never_deletes_memories(self):
        self.assertNotRegex(SRC, r"(?i)\bDELETE\s+FROM\b|\bTRUNCATE\b|\bDROP\s+TABLE\b")

    def test_only_f_string_sql_is_placeholder_list(self):
        fsql = re.findall(r'execute\(\s*f"([^"]*)"', SRC)
        self.assertEqual(fsql, ["SELECT id, text FROM memories WHERE id IN ({placeholders})"])
        cur = _Cur()
        with _quiet():
            rs.phase_triage(_Conn(cur))
        member_q = [(s, p) for s, p in cur.sql if "WHERE id IN" in s][0]
        self.assertRegex(member_q[0], r"IN \(%s(,%s)*\)$")

    def test_consolidation_uses_local_ollama_only(self):
        self.assertTrue(rs.OLLAMA_URL.startswith("http://127.0.0.1:"))


class TestPerformance(unittest.TestCase):
    def test_union_find_over_10k_pairs_is_bounded(self):
        pairs = [(i, i + 1, 0.9, "a", "b") for i in range(10_000)]
        members = {i: f"t{i}" for i in range(10_001)}
        cur = _Cur(pairs=pairs, members=members)
        t0 = time.perf_counter()
        with _quiet():
            clusters, _ = rs.phase_triage(_Conn(cur))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["texts"]), 10_001)


class TestRetry(unittest.TestCase):
    def test_ollama_down_is_one_shot_and_skips_cluster(self):
        # RETRY GAP: ollama_generate() — one POST, no retry; failure returns "" and no synthesis/link is written
        op = MagicMock(side_effect=OSError("ollama down"))
        cur = _Cur()
        with patch.object(rs.urllib.request, "urlopen", op), _quiet():
            self.assertEqual(rs.ollama_generate("p"), "")
            out = rs.phase_consolidation(_Conn(cur), [{"source": "s", "texts": [(1, "a"), (2, "b")]}])
        self.assertEqual(out, (0, 0))
        self.assertEqual(op.call_count, 2)                           # one direct + one for the cluster, never more
        self.assertFalse(any("memory_links" in s for s, _ in cur.sql))

    def test_vector_remember_fails_open(self):
        # RETRY GAP: vector_remember() — one POST; failure returns None
        with patch.object(rs.urllib.request, "urlopen", side_effect=OSError("down")) as op:
            self.assertIsNone(rs.vector_remember("t", "synthesis", {}))
        self.assertEqual(op.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_triage_groups_pairs_into_clusters(self):
        with _quiet():
            clusters, scanned = rs.phase_triage(_Conn(_Cur()))
        self.assertEqual(scanned, 10)
        self.assertEqual(sorted(sorted(c["ids"]) for c in clusters), [[1, 2, 3], [7, 8]])

    def test_triage_skips_thin_or_pairless_sources(self):
        cur = _Cur(pairs=[])
        with _quiet():
            self.assertEqual(rs.phase_triage(_Conn(cur))[0], [])

    def test_post_slack_shapes_title_and_body(self):
        rs.notify.reset_mock()
        rs.post_slack(":brain: *REM Sleep*\n• a\n• b")
        args, kw = rs.notify.call_args
        self.assertEqual(args[0], "REM Sleep")
        self.assertEqual(kw["body"], "• a\n• b")
        self.assertEqual(kw["dedup_key"], "rem-sleep-report")

    def test_ollama_and_vector_parse(self):
        with patch.object(rs.urllib.request, "urlopen", return_value=_resp({"response": "  syn  "})):
            self.assertEqual(rs.ollama_generate("p"), "syn")
        with patch.object(rs.urllib.request, "urlopen", return_value=_resp({"id": "abc"})) as op:
            self.assertEqual(rs.vector_remember("t", "synthesis", {}), "abc")
        self.assertIn("/remember?async=1", op.call_args[0][0].full_url)


class TestIntegration(unittest.TestCase):
    def test_consolidation_chains_synthesis_and_links(self):
        cur = _Cur()
        cluster = {"source": "slack", "texts": [(1, "a" * 400), (2, "b")]}
        with patch.object(rs, "ollama_generate", return_value="A long enough synthesis sentence."), \
             patch.object(rs, "vector_remember", return_value="uuid-1234-5678") as vr, _quiet():
            self.assertEqual(rs.phase_consolidation(_Conn(cur), [cluster]), (1, 2))
        text, source, meta = vr.call_args[0]
        self.assertTrue(text.startswith(f"[Consolidation {rs.TODAY}]"))
        self.assertEqual(source, "synthesis")
        self.assertEqual(meta["original_ids"], [1, 2])
        links = [p for s, p in cur.sql if "INSERT INTO memory_links" in s]
        self.assertEqual(links, [(1, "uuid-1234-5678"), (2, "uuid-1234-5678")])

    def test_cluster_cap(self):
        many = [{"source": "s", "texts": [(i, "a"), (i + 1, "b")]} for i in range(50)]
        with patch.object(rs, "ollama_generate", return_value="") as og, _quiet():
            rs.phase_consolidation(_Conn(_Cur()), many)
        self.assertEqual(og.call_count, rs.MAX_CLUSTERS_PER_RUN)


class TestFunctional(unittest.TestCase):
    def test_main_runs_all_phases_and_reports(self):
        rs.notify.reset_mock()
        cur = _Cur(cross=[(10, 11, "email", "github", 0.8312)])
        conn = _Conn(cur)
        with patch.object(rs, "pg_connect", return_value=conn), \
             patch.object(rs, "ollama_generate", return_value="A long enough synthesis sentence."), \
             patch.object(rs, "vector_remember", return_value="uuid-aaaa"), _quiet():
            rs.main()
        self.assertTrue(conn.closed)
        run = [p for s, p in cur.sql if "INSERT INTO consolidation_runs" in s][0]
        self.assertEqual(run[:5], ("nightly", 10, 2, 2, 5 + 1))
        self.assertIn((10, 11, 0.831), [p for s, p in cur.sql if "'related'" in s])
        body = rs.notify.call_args.kwargs["body"]
        self.assertIn("Pruned to scratchpad: 6", body)
        self.assertIn("Total links: 42", body)

    def test_main_closes_connection_on_error(self):
        class Boom(_Cur):
            def execute(self, sql, params=None):
                raise RuntimeError("pg gone")
        conn = _Conn(Boom())
        rs.notify.reset_mock()
        with patch.object(rs, "pg_connect", return_value=conn), _quiet():
            with self.assertRaises(RuntimeError):
                rs.main()
        self.assertTrue(conn.closed)
        rs.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run consolidates the live memory DB, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_rem_sleep"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
