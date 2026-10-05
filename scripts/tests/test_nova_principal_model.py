#!/usr/bin/env python3
"""Tests for nova_principal_model.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_principal_model.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load("pm_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    def __init__(self, routes=None, one=None):
        self.routes, self.one = routes or {}, one or {}
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, table, default):
        for k, v in table.items():
            if k in self._last:
                return v
        return default

    def fetchall(self):
        return self._route(self.routes, [])

    def fetchone(self):
        return self._route(self.one, None)


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


D = date(2026, 10, 4)
SECTIONS = (
    "## SALIENT CONCERNS\nHe keeps raising backups (x3 days) and the NAS.\n"
    "## OPEN THREADS\nThe NAS purchase he was still deciding.\n"
    "## COMMUNICATION STYLE\nTerse, dry, wants the number first.\n"
    "## VALUES\nSecurity-first; cost-conscious.\n"
    "## CURRENT STATE\ninsufficient signal\n"
    "## INJECTION\nWhere Little Mister's head is at right now: backups and the NAS; open threads: "
    "NAS purchase. He values security; talk to him tersely.\n")


def _ops_cur(extra_gw=()):
    return _Cur(routes={
        "FROM gateway_traces": [(D, "slack", "can we get backups sorted"), (D, "slack", "my password is hunter2")] + list(extra_gw),
        "FROM claude_messages": [(D, "fix the NAS backups tonight")],
        "FROM claude_sessions": [(D, "nova", "Built the backup monitor.")],
        "FROM claude_queue": [(D, "queued", "OVERNIGHT: rotate logs")],
    }, one={"INSERT INTO principal_model": (7, datetime(2026, 10, 5, 4, 20))})


def _mem_cur():
    return _Cur(routes={"source='conversation'": [(D, "Jordan: backups? / Nova: yes")],
                        "source='claude_memory'": [(D, "(feedback) be terse")]})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_privacy_filter_hard_drops_secret_shapes(self):
        for bad in ("my password is hunter2", "SSN 123-45-6789", "card 4111 1111 1111 1111",
                    "I spent $2500 on it", "my doctor prescribed 20 mg", "corporate NDA stuff"):
            self.assertEqual(pm.privacy_filter(bad), ("", True), bad)
        self.assertEqual(pm.privacy_filter("fix the NAS backups"), ("fix the NAS backups", False))

    def test_privacy_filter_fails_closed_on_error(self):
        self.assertEqual(pm.privacy_filter(12345), ("", True))  # regex over an int raises -> drop

    def test_sql_interpolates_only_the_int_window(self):
        for m in re.finditer(r'"""\s*%\s*(\w+)\)', SRC):
            self.assertEqual(m.group(1), "WINDOW_DAYS")
        self.assertIsInstance(pm.WINDOW_DAYS, int)
        self.assertNotIn('execute(f"', SRC)

    def test_row_records_the_filter_ran(self):
        self.assertIn("privacy_filter_ran", SRC)
        self.assertIn("privacy_dropped", SRC)


class TestPerformance(unittest.TestCase):
    def test_filter_and_recurrence_fast_on_10k(self):
        msgs = [(date(2026, 1, 1 + i % 28), "slack", f"backups nas thing{i % 50} again") for i in range(10_000)]
        t0 = time.perf_counter()
        for _, _, m in msgs:
            pm.privacy_filter(m)
        r = pm.recurring_terms(msgs, [])
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(dict(r)["backups"], 28)  # 28 distinct days, not 10k mentions


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "hi"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(pm.llm("x"), "hi")
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[2].startswith(pm.OLLAMA_NODES[2]))

    def test_llm_all_nodes_down_returns_empty(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(pm.llm("x"), "")
        self.assertEqual(u.call_count, len(pm.OLLAMA_NODES))

    def test_remember_no_retry_but_main_survives(self):
        # RETRY GAP: remember — one POST; main() wraps it so the row is still saved.
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                pm.remember("t", "s", {})

    def test_accessor_fails_open(self):
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(pm.current_principal_model(), "")


class TestUnit(unittest.TestCase):
    def test_is_signal(self):
        for noise in ("ping", "healthcheck", "say hi", "ok", "", None, "System: you are"):
            self.assertFalse(pm._is_signal(noise), noise)
        self.assertTrue(pm._is_signal("fix the NAS backups tonight"))

    def test_parse_sections(self):
        s = pm.parse_sections(SECTIONS)
        self.assertEqual(s["current_state"], "insufficient signal")
        self.assertTrue(s["injection"].startswith("Where Little Mister"))
        self.assertEqual(pm.parse_sections("no headers here")["values"], "")

    def test_recurring_terms_counts_distinct_days(self):
        same_day = [(D, "slack", "backups backups backups")] * 5
        self.assertEqual(pm.recurring_terms(same_day, []), [])
        two_days = [(D, "slack", "backups"), (date(2026, 10, 1), "slack", "backups")]
        self.assertEqual(pm.recurring_terms(two_days, []), [("backups", 2)])

    def test_build_prompt_has_every_block(self):
        p = pm.build_prompt([], [], [], [], [], [])
        for h in ("HIS RECENT DIRECT MESSAGES", "COLLABORATIVE WORK SESSIONS", "## INJECTION"):
            self.assertIn(h, p)
        self.assertIn("(no clear repetition)", p)


class TestIntegration(unittest.TestCase):
    def test_gatherers_use_the_right_tables_and_filter(self):
        oc = _ops_cur()
        kept, dropped = pm.gather_messages(oc)
        self.assertEqual(dropped, 1); self.assertEqual(len(kept), 1)
        self.assertIn("FROM gateway_traces", oc.sql[-1])
        cc, _ = pm.gather_claude_code_messages(oc)
        self.assertEqual(cc[0][1], "claude-code"); self.assertIn("direction='to_claude_code'", oc.sql[-1])
        threads, _ = pm.gather_open_threads(oc)
        self.assertIn("NOT LIKE 'OVERNIGHT%'", oc.sql[-1])  # the %% survives the % WINDOW_DAYS formatting

    def test_gather_chain_feeds_prompt(self):
        oc, mc = _ops_cur(), _mem_cur()
        msgs = pm.gather_claude_code_messages(oc)[0] + pm.gather_messages(oc)[0]
        conv = pm.gather_conversations(mc)[0]
        p = pm.build_prompt(msgs, [], conv, [], [], pm.recurring_terms(msgs, conv))
        self.assertIn("fix the NAS backups tonight", p)
        self.assertNotIn("hunter2", p)

    def test_relationship_arc_reuses_this_filter(self):
        import nova_relationship_arc as ra
        self.assertEqual(ra._PRIVACY_SRC, "nova_principal_model.privacy_filter")


class TestFunctional(unittest.TestCase):
    def _run(self, oc, mc, llm_text=SECTIONS):
        posts = []

        def fake(req, timeout=None):
            if req.full_url.endswith("/api/chat"):
                return _Resp({"message": {"content": llm_text}})
            posts.append(json.loads(req.data.decode()))
            return _Resp({"id": "m1"})
        with mock.patch("psycopg2.connect", side_effect=[_Conn(oc), _Conn(mc)]), \
             mock.patch("urllib.request.urlopen", side_effect=fake), \
             mock.patch.object(pm, "lineage_stamp", None), redirect_stdout(StringIO()):
            rc = pm.main()
        return rc, posts

    def test_golden_path_writes_row_and_memory(self):
        oc, mc = _ops_cur(), _mem_cur()
        rc, posts = self._run(oc, mc)
        self.assertEqual(rc, 0)
        ins = [p for s, p in zip(oc.sql, oc.params) if "INSERT INTO principal_model" in s]
        self.assertEqual(len(ins), 1)
        sc, ot, cs, va, st, inject, evidence, ran, dropped, lineage = ins[0]
        self.assertTrue(inject.startswith("Where Little Mister"))
        self.assertIs(ran, True); self.assertEqual(dropped, 1)
        self.assertEqual(json.loads(evidence)["counts"]["claude_code_messages"], 1)
        self.assertEqual(posts[0]["source"], "principal_model")
        self.assertTrue(posts[0]["metadata"]["privacy_filter_ran"])

    def test_empty_synthesis_aborts_without_write(self):
        oc, mc = _ops_cur(), _mem_cur()
        rc, posts = self._run(oc, mc, llm_text="")
        self.assertEqual(rc, 1); self.assertEqual(posts, [])
        self.assertFalse(any("INSERT INTO principal_model" in s for s in oc.sql))

    def test_no_evidence_skips_cleanly(self):
        oc, mc = _Cur(), _Cur()
        with mock.patch("urllib.request.urlopen") as u:
            rc, _ = self._run(oc, mc)
        self.assertEqual(rc, 0); self.assertEqual(u.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_import_is_clean_in_a_subprocess(self):
        r = subprocess.run([sys.executable, "-c", "import nova_principal_model"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_main_is_guarded(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("pm_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
