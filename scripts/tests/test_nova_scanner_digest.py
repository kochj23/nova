#!/usr/bin/env python3
"""Tests for nova_scanner_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_scanner_digest.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load("scanner_digest_under_test", SCRIPT)
_NET_DOWN = patch.object(sd.urllib.request, "urlopen", side_effect=AssertionError("network must be mocked"))
_PG_DOWN = patch.object(sd.psycopg2, "connect", side_effect=AssertionError("pg must be mocked"))


def setUpModule():
    _NET_DOWN.start(); _PG_DOWN.start()


def tearDownModule():
    _NET_DOWN.stop(); _PG_DOWN.stop()


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Cur:
    def __init__(self, groups, raise_on=()):
        self.groups = groups; self.raise_on = raise_on; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")

    def fetchall(self):
        return list(self.groups)


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda: cur, autocommit=False)


def _group(grp="LAPD-West", n=12, ids=None):
    return (grp, ids or [f"id-{i}" for i in range(n)], "\n".join(f"unit {i} roger" for i in range(n)), n)


def _run_main(groups, llm_out="Routine traffic across the hour; one structure fire reported near Olive Ave.",
              remember_ids=None, raise_on=()):
    cur = _Cur(groups, raise_on)
    remember = MagicMock(side_effect=remember_ids or (lambda *a, **k: "mem-1"))
    buf = io.StringIO()
    with patch.object(sd.psycopg2, "connect", return_value=_conn(cur)), \
         patch.object(sd, "llm", MagicMock(return_value=llm_out)) as llm, \
         patch.object(sd, "remember", remember), redirect_stdout(buf):
        rc = sd.main()
    return rc, cur, llm, remember, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", sd.MEM_DSN)

    def test_sql_is_parameterized_and_raw_rows_untouched(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"memory_links"})                  # never-prune guardrail: memories is read-only
        rc, cur, _, _, _ = _run_main([_group(grp="x'; DELETE FROM memories; --")])
        for sql, params in cur.sql:
            self.assertNotIn("DELETE", sql)
        self.assertEqual(cur.sql[0][1], (sd.MIN_ROWS,))
        self.assertEqual(cur.sql[1][1], ("mem-1", [f"id-{i}" for i in range(12)]))

    def test_digest_is_private_and_prompt_is_bounded(self):
        rc, cur, llm, remember, _ = _run_main([_group(n=3000)])
        meta = remember.call_args[0][1]
        self.assertEqual(meta["privacy"], "private")
        self.assertEqual(meta["type"], "scanner_digest")
        prompt = llm.call_args[0][0]
        self.assertLess(len(prompt), 6000 + 400)                     # blob[:6000] + the instruction header


class TestPerformance(unittest.TestCase):
    def test_10k_groups_roll_up_quickly(self):
        groups = [_group(grp=f"tg-{i}", n=10) for i in range(10_000)]
        t0 = time.perf_counter()
        rc, cur, llm, remember, _ = _run_main(groups)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(remember.call_count, 10_000)
        self.assertEqual(len(cur.sql), 10_001)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_ollama_nodes(self):
        ok = _Resp({"message": {"content": "  digest text  "}})
        uo = MagicMock(side_effect=[OSError("down"), OSError("down"), ok])
        with patch.object(sd.urllib.request, "urlopen", uo):
            self.assertEqual(sd.llm("p"), "digest text")
        self.assertEqual(uo.call_count, 3)
        self.assertEqual([c[0][0].full_url for c in uo.call_args_list],
                         [n + "/api/chat" for n in sd.OLLAMA_NODES[:3]])

    def test_llm_returns_empty_when_every_node_fails(self):
        uo = MagicMock(side_effect=OSError("all down"))
        with patch.object(sd.urllib.request, "urlopen", uo):
            self.assertEqual(sd.llm("p"), "")
        self.assertEqual(uo.call_count, len(sd.OLLAMA_NODES))
        uo = MagicMock(return_value=_Resp({"message": {"content": ""}}))      # empty answer counts as a miss
        with patch.object(sd.urllib.request, "urlopen", uo):
            self.assertEqual(sd.llm("p"), "")
        self.assertEqual(uo.call_count, len(sd.OLLAMA_NODES))

    def test_remember_is_one_shot_and_propagates(self):
        # RETRY GAP: remember()/memory-server — a single urlopen; a failure escapes main() (not fail-open), so the
        # remaining groups of that hour are skipped until the next hourly run.
        uo = MagicMock(side_effect=OSError("memsrv down"))
        with patch.object(sd.urllib.request, "urlopen", uo):
            with self.assertRaises(OSError):
                sd.remember("t", {})
        self.assertEqual(uo.call_count, 1)
        cur = _Cur([_group(grp="a"), _group(grp="b")])
        with patch.object(sd.psycopg2, "connect", return_value=_conn(cur)), patch.object(sd, "llm", MagicMock(return_value="x" * 40)), \
             patch.object(sd, "remember", MagicMock(side_effect=OSError("memsrv down"))), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                sd.main()
        self.assertEqual(len(cur.sql), 1)                            # nothing linked; raw rows untouched

    def test_link_insert_failure_is_logged_and_the_run_continues(self):
        # RETRY GAP: memory_links insert — one attempt; failure is logged per group and main() still returns 0
        rc, cur, _, remember, out = _run_main([_group(grp="a"), _group(grp="b")], raise_on=("memory_links",))
        self.assertEqual(rc, 0)
        self.assertEqual(remember.call_count, 2)
        self.assertEqual(out.count("link insert failed"), 2)
        self.assertIn("2 digest(s) from 2 group(s)", out)


class TestUnit(unittest.TestCase):
    def test_log_format(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            sd.log("hello")
        self.assertRegex(buf.getvalue(), r"^\[scanner-digest \d\d:\d\d:\d\d\] hello\n$")

    def test_llm_request_shape(self):
        uo = MagicMock(return_value=_Resp({"message": {"content": "ok"}}))
        with patch.object(sd.urllib.request, "urlopen", uo):
            sd.llm("prompt here", max_tokens=42)
        req = uo.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual((req.get_method(), body["model"], body["stream"], body["think"]), ("POST", sd.LLM_MODEL, False, False))
        self.assertEqual(body["options"]["num_predict"], 42)
        self.assertEqual(body["messages"], [{"role": "user", "content": "prompt here"}])
        self.assertEqual(uo.call_args[1]["timeout"], 90)

    def test_remember_posts_to_memory_server(self):
        uo = MagicMock(return_value=_Resp({"id": "abc"}))
        with patch.object(sd.urllib.request, "urlopen", uo):
            self.assertEqual(sd.remember("text", {"k": "v"}), "abc")
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, sd.MEMSRV + "/remember")
        self.assertEqual(json.loads(req.data), {"text": "text", "source": "scanner_digest", "metadata": {"k": "v"}})
        uo = MagicMock(return_value=_Resp({}))
        with patch.object(sd.urllib.request, "urlopen", uo):
            self.assertIsNone(sd.remember("text", {}))


class TestIntegration(unittest.TestCase):
    def test_query_targets_previous_hour_of_scanner_rows(self):
        rc, cur, _, _, _ = _run_main([])
        sql = cur.sql[0][0]
        self.assertIn("FROM memories WHERE source='scanner'", sql)
        self.assertIn("date_trunc('hour', now() - interval '1 hour')", sql)
        self.assertIn("GROUP BY 1 HAVING count(*) >= %s", sql)
        self.assertIn("coalesce(metadata->>'talkgroup', metadata->>'channel', metadata->>'system', 'unknown')", sql)

    def test_digest_text_links_back_to_constituent_rows(self):
        rc, cur, llm, remember, _ = _run_main([_group(grp="Burbank-Fire", n=9, ids=["r1", "r2", "r3"])])
        text, meta = remember.call_args[0]
        self.assertRegex(text, r"^\[Scanner Burbank-Fire — \d{4}-\d\d-\d\d \d\d:00\] Routine traffic")
        self.assertEqual(meta["talkgroup"], "Burbank-Fire")
        self.assertEqual(meta["n_transmissions"], 9)
        self.assertRegex(meta["source_ref"], r"^scanner://Burbank-Fire/\d{4}-\d\d-\d\dT\d\d:00$")
        self.assertEqual(meta["hour"], text.split("— ")[1].split("]")[0])
        sql, params = cur.sql[1]
        self.assertIn("INSERT INTO memory_links (source_id, target_id, link_type) SELECT %s, unnest(%s::text[]), 'distilled_from'", sql)
        self.assertEqual(params, ("mem-1", ["r1", "r2", "r3"]))
        self.assertIn("CHANNEL: Burbank-Fire\nTRANSMISSIONS (9):", llm.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_one_digest_per_group(self):
        rc, cur, llm, remember, out = _run_main([_group(grp="a"), _group(grp="b")],
                                                 remember_ids=["m-a", "m-b"])
        self.assertEqual(rc, 0)
        self.assertEqual(llm.call_count, 2)
        self.assertEqual([p[1][0] for p in cur.sql[1:]], ["m-a", "m-b"])
        self.assertIn("2 digest(s) from 2 group(s)", out)

    def test_short_or_empty_digests_are_skipped_and_unsaved_ids_not_linked(self):
        rc, cur, llm, remember, out = _run_main([_group(grp="a"), _group(grp="b")], llm_out="too short")
        self.assertEqual((rc, remember.call_count, len(cur.sql)), (0, 0, 1))
        self.assertIn("0 digest(s) from 2 group(s)", out)
        rc, cur, llm, remember, out = _run_main([_group()], llm_out="")
        self.assertEqual(remember.call_count, 0)
        rc, cur, llm, remember, out = _run_main([_group()], remember_ids=[None])
        self.assertEqual(len(cur.sql), 1)
        self.assertIn("0 digest(s) from 1 group(s)", out)

    def test_pg_down_raises_before_any_llm_call(self):
        with patch.object(sd.psycopg2, "connect", side_effect=OSError("pg down")), patch.object(sd, "llm", MagicMock()) as llm:
            with self.assertRaises(OSError):
                sd.main()
        llm.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        r = subprocess.run([sys.executable, "-c", "import nova_scanner_digest"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_module_constants(self):
        self.assertGreaterEqual(sd.MIN_ROWS, 2)
        self.assertTrue(all(n.startswith("http://192.168.1.") for n in sd.OLLAMA_NODES))


if __name__ == "__main__":
    unittest.main()
