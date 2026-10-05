#!/usr/bin/env python3
"""Tests for nova_reconciler.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reconciler.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load("reconciler_under_test", SCRIPT)
COLS = ["name", "kind", "target", "extract", "dsn", "claim_re", "compare", "tolerance", "scope", "severity", "note"]
PG_FACT = next(s for s in rc.SEEDS if s["name"] == "pg.primary.host")
MEM_FACT = next(s for s in rc.SEEDS if s["name"] == "memory.vector_count")


def _fact_row(seed):
    return tuple(seed.get(c) for c in COLS)


class _Cur:
    def __init__(self, facts=(), docs=(), mems=(), open_drift=(), rowcount=1):
        self.facts, self.docs, self.mems, self.open_drift = list(facts), list(docs), list(mems), list(open_drift)
        self.rowcount = rowcount
        self.sql, self.params, self._last, self.next_id = [], [], "", 0

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    @property
    def description(self):
        return [(c,) for c in COLS]

    def fetchall(self):
        s = self._last
        if "FROM doc_facts WHERE enabled" in s:
            return self.facts
        if "FROM agent_docs" in s:
            return self.docs
        if "FROM claude_memories" in s:
            return self.mems
        if "FROM doc_drift WHERE status='open' AND fact_name" in s:
            return self.open_drift
        return []

    def fetchone(self):
        if "INSERT INTO doc_drift" in self._last:
            self.next_id += 1; return (self.next_id, "open")
        return None

    def writes(self, needle):
        return [p for s, p in zip(self.sql, self.params) if needle in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


STALE_DOC = ("nova-system-map", "The 5-machine fleet.\nPG PRIMARY IS THE PG17 DOCKER CONTAINER ON NOVA-CORE .2 and so on.")
ARCHIVE = ("incident-2026-07-06", "PG PRIMARY IS NOVA-CORE .2 at the time; historic note.")
ARGS = argparse.Namespace(quiet=True, no_slack=True)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_drift_rows_are_parameterized_never_interpolated(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn('execute(f\'', SRC)
        cur = _Cur(facts=[_fact_row(PG_FACT)], docs=[STALE_DOC])
        with mock.patch.object(rc, "probe", return_value=("192.168.1.10", "pg")), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             redirect_stdout(StringIO()):
            rc.check(ARGS)
        ins = cur.writes("INSERT INTO doc_drift")[0]
        self.assertEqual(ins[:4], ("pg.primary.host", "agent_docs", "nova-system-map", "NOVA-CORE .2"))

    def test_http_probe_uses_argv_not_shell_and_scopes_are_anchored(self):
        with mock.patch.object(rc.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0, stdout='{"count": 2216095}')
            self.assertEqual(rc.probe(MEM_FACT), ("2216095", "http"))
        self.assertIsInstance(run.call_args[0][0], list); self.assertNotIn("shell", run.call_args[1])
        for s in rc.SEEDS:
            self.assertTrue(s["scope"].startswith("^") and s["scope"].endswith("$"), s["name"])
        self.assertIn("DEFAULT '$^'", rc.SCHEMA)  # a careless new fact matches nothing

    def test_reports_never_rewrites_the_corpus(self):
        tables = set(re.findall(r"INSERT INTO\s+(\w+)", SRC)) | set(re.findall(r"UPDATE\s+(\w+)\s+SET", SRC)) \
            | set(re.findall(r"DELETE FROM\s+(\w+)", SRC))
        self.assertEqual(tables, {"doc_facts", "doc_drift"})


class TestPerformance(unittest.TestCase):
    def test_comparators_and_claim_scan_fast_on_10k(self):
        claim = re.compile(PG_FACT["claim_re"]); scope = re.compile(PG_FACT["scope"])
        body = [("agent_docs", "nova-system-map" if i % 2 else "archive", STALE_DOC[1]) for i in range(10_000)]
        t0 = time.perf_counter()
        hits = 0
        for _, sid, text in body:
            if not scope.match(sid):
                continue
            for m in claim.finditer(text):
                hits += not rc.agrees(PG_FACT, m.group(1), "192.168.1.10")
        for i in range(10_000):
            rc.norm_num(f"{i},000+"); rc.agrees(MEM_FACT, "2.2M", "2216095")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(hits, 5_000)


class TestRetry(unittest.TestCase):
    def test_probe_fails_open_on_every_kind(self):
        # RETRY GAP: probe — one attempt per fact; None means "unmeasured", drift stays open.
        with mock.patch.object(rc.subprocess, "run", side_effect=OSError("no curl")):
            live, detail = rc.probe(MEM_FACT)
        self.assertIsNone(live); self.assertIn("probe error", detail)
        with mock.patch.object(rc.subprocess, "run", return_value=types.SimpleNamespace(returncode=7, stdout="")):
            self.assertEqual(rc.probe(MEM_FACT), (None, "fetch failed rc=7"))
        with mock.patch("psycopg2.connect", side_effect=OSError("pg down")):
            self.assertIsNone(rc.probe(PG_FACT)[0])
        with mock.patch.object(rc.socket, "gethostbyname", side_effect=OSError("nxdomain")):
            self.assertIsNone(rc.probe({"kind": "dns", "target": "x"})[0])
        self.assertEqual(rc.probe({"kind": "bogus", "target": ""}), (None, "unknown kind bogus"))

    def test_slack_failure_is_swallowed(self):
        cur = _Cur(facts=[_fact_row(PG_FACT)], docs=[STALE_DOC])
        cfg = types.ModuleType("nova_config"); cfg.SLACK_ALERTS = "A"; cfg.SLACK_DIGEST = "D"
        cfg.post_both = mock.Mock(side_effect=OSError("slack down"))
        with mock.patch.object(rc, "probe", return_value=("192.168.1.10", "pg")), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.dict(sys.modules, {"nova_config": cfg}), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(rc.check(argparse.Namespace(quiet=True, no_slack=False)), 1)
        self.assertEqual(cfg.post_both.call_args[1]["slack_channel"], "A")


class TestUnit(unittest.TestCase):
    def test_norm_host(self):
        for v, want in ((".2", "192.168.1.2"), ("192.168.1.2/32", "192.168.1.2"), ("NOVA-CORE .2", "192.168.1.2"),
                        ("192.168.1.10", "192.168.1.10"), ("(.10)", "192.168.1.10")):
            self.assertEqual(rc.norm_host(v), want, v)

    def test_norm_num(self):
        self.assertEqual(rc.norm_num("2,216,095"), 2216095.0)
        self.assertEqual(rc.norm_num("1.6M"), 1.6e6)
        self.assertEqual(rc.norm_num("877,000+"), 877000.0)
        self.assertIsNone(rc.norm_num("abc"))

    def test_agrees_modes(self):
        self.assertTrue(rc.agrees({"compare": "exact"}, "a", "a ")); self.assertFalse(rc.agrees({"compare": "exact"}, "a", "b"))
        self.assertTrue(rc.agrees({"compare": "ci"}, "ReplicA", "replica"))
        self.assertTrue(rc.agrees({"compare": "host"}, "NOVA-CORE .10", "192.168.1.10"))
        self.assertTrue(rc.agrees({"compare": "numeric", "tolerance": 0.10}, "2.2M", "2216095"))
        self.assertFalse(rc.agrees({"compare": "numeric", "tolerance": 0.10}, "1.6M", "2216095"))
        self.assertFalse(rc.agrees({"compare": "numeric", "tolerance": 0}, "x", "0"))
        self.assertFalse(rc.agrees({"compare": "nope"}, "a", "a"))

    def test_seed_claim_regexes_catch_the_documented_drift(self):
        self.assertEqual(re.search(PG_FACT["claim_re"], STALE_DOC[1]).group(1), "NOVA-CORE .2")
        self.assertEqual(re.search(MEM_FACT["claim_re"], "a 1.6M-vector memory").group(1).strip(), "1.6M")
        self.assertEqual(re.search(MEM_FACT["claim_re"], "877,000+ memories").group(1).strip(), "877,000")
        host = next(s for s in rc.SEEDS if s["name"] == "memory.server.host")
        self.assertEqual(re.search(host["claim_re"], "Memory server likewise runs on .2").group(1), "2")


class TestIntegration(unittest.TestCase):
    def test_corpus_flattens_both_sources(self):
        cur = _Cur(docs=[("d", "a\n  b")], mems=[("m", "c\t\td")])
        self.assertEqual(rc.corpus(cur), [("agent_docs", "d", "a b"), ("claude_memories", "m", "c d")])

    def test_scope_protects_the_archive_and_files_the_live_doc(self):
        cur = _Cur(facts=[_fact_row(PG_FACT)], docs=[STALE_DOC, ARCHIVE])
        with mock.patch.object(rc, "probe", return_value=("192.168.1.10", "pg")), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             redirect_stdout(StringIO()):
            self.assertEqual(rc.check(ARGS), 1)
        drift = cur.writes("INSERT INTO doc_drift")
        self.assertEqual([d[2] for d in drift], ["nova-system-map"])
        self.assertEqual(cur.writes("UPDATE doc_facts SET last_checked")[0], ("192.168.1.10", "pg.primary.host"))

    def test_resolution_only_for_measured_facts(self):
        stale = ("pg.primary.host", "agent_docs", "nova-system-map", "NOVA-CORE .2")
        cur = _Cur(facts=[_fact_row(PG_FACT)], docs=[("nova-system-map", "PG PRIMARY IS NOVA-CORE .10")],
                   open_drift=[(4,) + stale])
        with mock.patch.object(rc, "probe", return_value=("192.168.1.10", "pg")), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             redirect_stdout(StringIO()):
            self.assertEqual(rc.check(ARGS), 0)
        self.assertEqual(cur.writes("SET status='fixed'"), [(4,)])
        cur = _Cur(facts=[_fact_row(PG_FACT)], docs=[STALE_DOC], open_drift=[(4,) + stale])
        with mock.patch.object(rc, "probe", return_value=(None, "pg down")), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             redirect_stdout(StringIO()):
            self.assertEqual(rc.check(ARGS), 0)
        self.assertEqual(cur.writes("SET status='fixed'"), [])
        self.assertFalse(any("FROM doc_drift WHERE status='open' AND fact_name" in s for s in cur.sql))


class TestFunctional(unittest.TestCase):
    def test_main_check_reports_drift(self):
        cur = _Cur(facts=[_fact_row(PG_FACT)], docs=[STALE_DOC])
        with mock.patch.object(rc, "probe", return_value=("192.168.1.10", "pg")), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(sys, "argv", ["nova_reconciler.py", "--no-slack", "--quiet"]), redirect_stdout(StringIO()) as out:
            self.assertEqual(rc.main(), 1)
        self.assertIn("claims *NOVA-CORE .2* for `pg.primary.host` — live is *192.168.1.10*", out.getvalue())
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS doc_facts" in s for s in cur.sql))

    def test_main_seed_and_wontfix(self):
        cur = _Cur()
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(sys, "argv", ["x", "--seed"]), \
             redirect_stdout(StringIO()):
            self.assertEqual(rc.main(), 0)
        self.assertEqual(len(cur.writes("INSERT INTO doc_facts")), len(rc.SEEDS))
        cur = _Cur(rowcount=0)
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(sys, "argv", ["x", "--wontfix", "12"]), \
             redirect_stdout(StringIO()) as out:
            self.assertEqual(rc.main(), 0)
        self.assertEqual(cur.writes("status='wontfix'"), [(12,)]); self.assertIn("no such row #12", out.getvalue())

    def test_no_facts_registered_is_clean(self):
        cur = _Cur()
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), redirect_stdout(StringIO()) as out:
            self.assertEqual(rc.check(ARGS), 0)
        self.assertIn("--seed", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("--wontfix", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("reconciler_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
