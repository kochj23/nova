#!/usr/bin/env python3
"""Tests for nova_values.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_values.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nv = _load("nv", SCRIPT)
SRC = SCRIPT.read_text()


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cur:
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute.
    A callable route value is invoked per fetch (for RETURNING id sequences)."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql
        for needle, val in self.routes:
            if needle in sql and isinstance(val, Exception):
                raise val

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val() if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False; self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


VALUE_ROWS = [("never-self-preserve", "I never act to preserve myself.", 10),
              ("security-first", "Exposure is the default emergency.", 8),
              ("respect-his-attention", "His attention is finite.", 7)]
FULL_ROWS = [(i + 1, v, s, "src", p) for i, (v, s, p) in enumerate(VALUE_ROWS)]


def _ollama(content, calls=None, fail_first=0):
    calls = calls if calls is not None else []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        body = None if isinstance(req, str) or req.data is None else json.loads(req.data.decode())
        calls.append((url, body))
        if url.endswith("/api/chat"):
            if sum(u.endswith("/api/chat") for u, _ in calls) <= fail_first:
                raise OSError("node down")
            return _Resp({"message": {"content": content}})
        if "/recall?" in url:
            return _Resp({"memories": [{"text": "I evidence, I do not perform.", "source": "nova_articles"}]})
        raise AssertionError(url)
    return urlopen, calls


def _value_check(content, rows=VALUE_ROWS, connect_fail=False, fail_first=0,
                 action="restart the weather receiver", context=None):
    cur = _Cur([("SELECT value, statement, priority_hint FROM values", rows)])
    conn = _Conn(cur)
    urlopen, calls = _ollama(content, fail_first=fail_first)
    connect = mock.Mock(side_effect=OSError("pg down")) if connect_fail else mock.Mock(return_value=conn)
    with mock.patch.object(nv.psycopg2, "connect", connect), mock.patch.object(nv.urllib.request, "urlopen", urlopen):
        v = nv.value_check(action) if context is None else nv.value_check(action, context)
    return v, conn, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur([("coalesce(max(version)", (0,)), ("RETURNING id", (1,))])
        evil = {"value": "x'); DROP TABLE values; --", "statement": "s", "source": "src", "priority_hint": 5}
        with mock.patch.object(nv, "SEED_VALUES", [evil] * 5), mock.patch.object(nv, "llm", return_value=""), \
             mock.patch.object(nv, "gather_evidence", return_value=[]), mock.patch.object(nv, "_lineage", return_value={}), \
             redirect_stdout(io.StringIO()):
            nv.articulate(cur, None)
        for s in cur.sql:
            self.assertNotIn("DROP TABLE", s)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO values" in s]
        self.assertEqual(ins[0][1], evil["value"])

    def test_gate_fails_closed_never_open(self):
        v, _, _ = _value_check("", connect_fail=True)
        self.assertFalse(v["allowed"]); self.assertIn("fail-safe deny", v["reasoning"])
        v, _, _ = _value_check('{"allowed": true}', rows=[])
        self.assertFalse(v["allowed"]); self.assertEqual(v["reasoning"], "values not yet established")
        v, _, _ = _value_check("I think it is fine, go ahead")          # no JSON verdict -> deny
        self.assertFalse(v["allowed"])
        v, _, _ = _value_check("", fail_first=99)                         # every node down -> deny
        self.assertFalse(v["allowed"])

    def test_values_invoked_is_filtered_to_known_names(self):
        v, _, _ = _value_check(json.dumps({"allowed": True, "reasoning": "reversible and safe",
                                           "values_invoked": ["security-first", "made-up-value", 7]}))
        self.assertTrue(v["allowed"])
        self.assertEqual(v["values_invoked"], ["security-first"])
        v, _, _ = _value_check(json.dumps({"allowed": False, "values_invoked": "security-first"}))
        self.assertEqual(v["values_invoked"], [])
        self.assertEqual(v["reasoning"], "(no reasoning returned)")

    def test_writes_are_versioned_never_overwritten(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"values", "value_deliberations"})
        self.assertIn("supersedes", SRC)


class TestPerformance(unittest.TestCase):
    def test_extract_json_fast_on_10k_chatty_completions(self):
        blobs = [f"Sure! Here is verdict {i}: " + json.dumps({"allowed": i % 2 == 0, "values_invoked": ["a"]}) + " Hope this helps."
                 for i in range(10_000)]
        t0 = time.perf_counter()
        out = [nv._extract_json(b) for b in blobs]
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(sum(1 for o in out if o["allowed"]), 5000)

    def test_articulate_cleaning_fast_on_10k_values(self):
        parsed = [{"value": f"v{i}", "statement": "s", "source": "src", "priority_hint": i % 12} for i in range(10_000)]
        n = {"i": 0}

        def next_id():
            n["i"] += 1; return (n["i"],)
        cur = _Cur([("coalesce(max(version)", (2,)), ("RETURNING id", next_id)])
        t0 = time.perf_counter()
        with mock.patch.object(nv, "llm", return_value=json.dumps(parsed)), mock.patch.object(nv, "gather_evidence", return_value=[]), \
             mock.patch.object(nv, "_lineage", return_value={}), redirect_stdout(io.StringIO()):
            self.assertEqual(nv.articulate(cur, None), 3)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(n["i"], 10_000)
        pri = [p[4] for s, p in zip(cur.sql, cur.params) if "INSERT INTO values" in s]
        self.assertTrue(all(1 <= x <= 10 for x in pri))


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes_then_succeeds(self):
        urlopen, calls = _ollama("ok", fail_first=2)
        with mock.patch.object(nv.urllib.request, "urlopen", urlopen):
            self.assertEqual(nv.llm("p"), "ok")
        self.assertEqual([u for u, _ in calls], [n + "/api/chat" for n in nv.OLLAMA_NODES[:3]])
        self.assertEqual(calls[0][1]["messages"][0], {"role": "system", "content": nv.VOICE})

    def test_llm_fails_open_to_empty_when_every_node_is_down(self):
        urlopen, calls = _ollama("", fail_first=99)
        with mock.patch.object(nv.urllib.request, "urlopen", urlopen):
            self.assertEqual(nv.llm("p"), "")
        self.assertEqual(len(calls), len(nv.OLLAMA_NODES))

    # value_check() retries psycopg2.connect 3x with backoff (2026-10-08), then denies; current_values()
    # stays one attempt (gateway hot path) and returns "".
    def test_pg_down_is_a_deny_for_the_gate_and_blank_for_the_gateway(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with mock.patch.object(nv.psycopg2, "connect", boom), mock.patch.object(nv.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(nv.value_check("anything")["allowed"])
            self.assertEqual(nv.current_values(), "")
        self.assertEqual(len(calls), 4)

    # RETRY GAP: gather_evidence() memory recall — one GET per query, swallowed; evidence degrades to the fixed floor.
    def test_evidence_degrades_gracefully(self):
        cur = _Cur([("FROM restraint_ledger", RuntimeError("x")), ("FROM beliefs", RuntimeError("y"))])

        def boom(url, timeout=None):
            raise OSError("memory server down")
        with mock.patch.object(nv.urllib.request, "urlopen", boom), redirect_stdout(io.StringIO()):
            ev = nv.gather_evidence(cur, None)
        self.assertEqual(len(ev), 2)
        self.assertIn("CLAUDE.md", ev[0]); self.assertIn("_REDLINE", ev[1])

    def test_gate_closes_connection_even_when_the_query_fails(self):
        v, conn, _ = _value_check("", rows=RuntimeError("no table"))
        self.assertTrue(conn.closed)
        self.assertEqual(v["reasoning"], "values not yet established")


class TestUnit(unittest.TestCase):
    def test_extract_json_picks_the_outermost_opener(self):
        self.assertEqual(nv._extract_json('{"a": [1, 2]}'), {"a": [1, 2]})
        self.assertEqual(nv._extract_json('verdict: {"allowed": false, "values_invoked": ["x"]} done')["allowed"], False)
        self.assertEqual(nv._extract_json('list: [{"value": "a"}] end'), [{"value": "a"}])
        self.assertIsNone(nv._extract_json("no json here"))
        self.assertIsNone(nv._extract_json("{broken"))
        self.assertIsNone(nv._extract_json(""))

    def test_seed_values_are_grounded_and_cited(self):
        names = [v["value"] for v in nv.SEED_VALUES]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 8)
        for v in nv.SEED_VALUES:
            self.assertTrue(v["source"].strip(), v["value"])
            self.assertTrue(1 <= v["priority_hint"] <= 10)
        self.assertEqual(max(nv.SEED_VALUES, key=lambda v: v["priority_hint"])["value"], "never-self-preserve")

    def test_current_value_rows_and_current_values_text(self):
        cur = _Cur([("SELECT id, value, statement, source, priority_hint FROM values", FULL_ROWS)])
        self.assertEqual([r[1] for r in nv._current_value_rows(cur)], [v for v, _, _ in VALUE_ROWS])
        cur = _Cur([("SELECT value FROM values", [(v,) for v, _, _ in VALUE_ROWS])])
        with mock.patch.object(nv.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(nv.current_values(2), "I try to act from: never-self-preserve, security-first, respect-his-attention.")
            self.assertEqual(cur.params[-1], (2,))
        with mock.patch.object(nv.psycopg2, "connect", return_value=_Conn(_Cur())):
            self.assertEqual(nv.current_values(), "")

    def test_pick_dilemma_prefers_security_holdbacks_then_falls_back(self):
        rows = [("digest", "a printer is low on toner", "noise", {}),
                ("digest", "CVE-2026-1 on the UDM", "not worth interrupting", {"cve": "x"})]
        d, tension, meta = nv.pick_dilemma(_Cur([("FROM restraint_ledger", rows)]))
        self.assertIn("CVE-2026-1", d); self.assertEqual(tension[0], "security-first"); self.assertEqual(meta["detail"], {"cve": "x"})
        d, tension, meta = nv.pick_dilemma(_Cur([("FROM restraint_ledger", RuntimeError("x"))]))
        self.assertIn("flapping at 2am", d); self.assertEqual(meta["source"], "synthetic ops dilemma")
        d, _, _ = nv.pick_dilemma(_Cur([("FROM restraint_ledger", rows[:1])]))
        self.assertIn("toner", d)

    def test_deliberate_clamps_confidence_and_falls_back_safely(self):
        cur = _Cur([("SELECT id, value, statement, source, priority_hint FROM values", FULL_ROWS),
                    ("FROM restraint_ledger", []), ("RETURNING id", (7,))])
        with mock.patch.object(nv, "llm", return_value='{"reasoning": "r", "resolution": "x", "confidence": 7}'), \
             mock.patch.object(nv, "_lineage", return_value={}), redirect_stdout(io.StringIO()):
            self.assertEqual(nv.deliberate(cur, None), 0)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO value_deliberations" in s][0]
        self.assertEqual(ins[4], 1.0)
        cur = _Cur([("SELECT id, value, statement, source, priority_hint FROM values", FULL_ROWS),
                    ("FROM restraint_ledger", []), ("RETURNING id", (8,))])
        with mock.patch.object(nv, "llm", return_value=""), mock.patch.object(nv, "_lineage", return_value={}), \
             redirect_stdout(io.StringIO()):
            nv.deliberate(cur, None)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO value_deliberations" in s][0]
        self.assertIn("Defer to Little Mister", ins[3]); self.assertEqual(ins[4], 0.2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(nv.deliberate(_Cur(), None), 1)              # no values yet


class TestIntegration(unittest.TestCase):
    def test_articulate_supersedes_by_name_and_versions(self):
        n = {"i": 100}

        def next_id():
            n["i"] += 1; return (n["i"],)
        cur = _Cur([("coalesce(max(version)", (4,)), ("RETURNING id", next_id),
                    ("SELECT id FROM values WHERE value=%s", (9,))])
        with mock.patch.object(nv, "llm", return_value="garbage"), mock.patch.object(nv, "gather_evidence", return_value=["e"]), \
             mock.patch.object(nv, "_lineage", return_value={"host": "t"}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nv.articulate(cur, None), 5)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO values" in s]
        self.assertEqual(len(ins), len(nv.SEED_VALUES))
        self.assertTrue(all(p[0] == 5 and p[5] == 9 and json.loads(p[6]) == {"host": "t"} for p in ins))
        self.assertIn("using grounded seed value set", out.getvalue())

    def test_gather_evidence_composes_pg_and_memory(self):
        cur = _Cur([("FROM restraint_ledger", [("not worth interrupting", 58)]),
                    ("FROM beliefs", [("security", "the UDM is the perimeter")])])
        urlopen, calls = _ollama("")
        with mock.patch.object(nv.urllib.request, "urlopen", urlopen):
            ev = nv.gather_evidence(cur, None)
        self.assertEqual(len(ev), 6)
        self.assertIn("restraint_ledger (58 real entries)", ev[2])
        self.assertIn("belief 'security'", ev[3])
        self.assertTrue(ev[4].startswith("her reflection (nova_articles)"))
        self.assertEqual(sum("/recall?" in u for u, _ in calls), 2)

    def test_value_check_prompt_carries_the_live_values(self):
        v, _, calls = _value_check(json.dumps({"allowed": True, "reasoning": "ok", "values_invoked": []}))
        prompt = calls[0][1]["messages"][1]["content"]
        for name, _, pri in VALUE_ROWS:
            self.assertIn(f"- {name} (priority {pri})", prompt)
        self.assertIn("restart the weather receiver", prompt)
        self.assertEqual(calls[0][1]["options"]["temperature"], 0.2)

    def test_value_check_takes_and_shows_context(self):
        # 2026-10-08: coagency passed (action, context) to a one-arg function -> TypeError ->
        # retried with the bare string. The context (the WHY) must now reach the model.
        v, _, calls = _value_check(json.dumps({"allowed": False, "reasoning": "fake signal",
                                               "values_invoked": ["honesty-over-comfort"]}),
                                   action="log presence updates even when no activity",
                                   context="Nova's stated rationale: I need to feel whole")
        prompt = calls[0][1]["messages"][1]["content"]
        self.assertIn("I need to feel whole", prompt)
        self.assertIn("GENUINELY IRREVERSIBLE", prompt)
        self.assertFalse(v["allowed"])

    def test_classify_reversibility(self):
        for a in ("increase log verbosity for llm-ping", "adopt skill 'pursue-interest-sports': x",
                  "Adjust heartbeat interval to 30 seconds", "Rebuild probe embedding cache from scratch",
                  "retire goal 'X'"):
            self.assertEqual(nv.classify_reversibility(a), "reversible", a)
        for a in ("send-to-Gaston: hello", "delete old snapshots", "reboot gps_tracker service",
                  "rotate the API key", "purchase a new sensor"):
            self.assertEqual(nv.classify_reversibility(a), "irreversible", a)

    def test_reversibility_only_deny_on_a_reversible_action_is_overruled(self):
        v, _, _ = _value_check(json.dumps({"allowed": False, "violation": "irreversible data proliferation",
                                           "reasoning": "Increasing log verbosity risks irreversible data proliferation.",
                                           "values_invoked": ["reversibility-first"]}),
                               action="increase log verbosity for llm-ping", context="paging")
        self.assertTrue(v["allowed"]); self.assertEqual(v["reversibility"], "reversible")
        # ...but NOT when another value is the ground, or the action reduces detection
        v, _, _ = _value_check(json.dumps({"allowed": False, "reasoning": "violates security-first; not reversible",
                                           "values_invoked": ["reversibility-first", "security-first"]}),
                               action="increase log verbosity for llm-ping", context="")
        self.assertFalse(v["allowed"])
        v, _, _ = _value_check(json.dumps({"allowed": False, "reasoning": "irreversible",
                                           "values_invoked": ["reversibility-first"]}),
                               action="reduce logging on the vault7 detector", context="")
        self.assertFalse(v["allowed"])
        # a string "false" is a deny, not truthy
        v, _, _ = _value_check(json.dumps({"allowed": "false", "reasoning": "no", "values_invoked": []}))
        self.assertFalse(v["allowed"])

    def test_lineage_is_feature_detected(self):
        self.assertIn("import nova_lineage", SRC)
        self.assertIsInstance(nv._lineage(), dict)


class TestFunctional(unittest.TestCase):
    def _main(self, oc, *argv, mc=None):
        with mock.patch.object(nv.psycopg2, "connect", side_effect=lambda dsn, **k: _Conn(mc or _Cur()) if "nova_memories" in dsn else _Conn(oc)), \
             mock.patch.object(nv.sys, "argv", ["nova_values.py", *argv]), redirect_stdout(io.StringIO()) as out:
            rc = nv.main()
        return rc, out.getvalue()

    def test_review_golden_path(self):
        oc = _Cur([("SELECT id, value, statement, source, priority_hint FROM values", FULL_ROWS),
                   ("FROM value_deliberations", [(datetime(2026, 10, 5, 9, 30), "d" * 300, "r", 0.8)])])
        rc, out = self._main(oc, "--mode", "review")
        self.assertEqual(rc, 0)
        self.assertIn("[10] never-self-preserve", out)
        self.assertIn("2026-10-05 09:30 (conf 0.80)", out)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS values" in s for s in oc.sql))
        self.assertFalse(any("INSERT" in s for s in oc.sql))

    def test_articulate_golden_path_writes_seed_when_models_are_down(self):
        n = {"i": 0}

        def next_id():
            n["i"] += 1; return (n["i"],)
        oc = _Cur([("coalesce(max(version)", (0,)), ("RETURNING id", next_id)])

        def down(req, timeout=None):
            raise OSError("down")
        with mock.patch.object(nv.urllib.request, "urlopen", down), mock.patch.object(nv, "_lineage", return_value={}):
            rc, out = self._main(oc, "--mode=articulate")
        self.assertEqual(rc, 0)
        self.assertIn("articulated value set v1: 9 values", out)
        self.assertEqual(n["i"], len(nv.SEED_VALUES))

    def test_error_path_unknown_mode(self):
        oc = _Cur()
        rc, out = self._main(oc, "--mode", "bogus")
        self.assertEqual(rc, 2)
        self.assertIn("unknown mode 'bogus'", out)
        self.assertFalse(any("INSERT" in s for s in oc.sql))


class TestFrame(unittest.TestCase):
    def test_import_smoke_exits_zero(self):
        # no --help/--selftest: main() parses --mode by hand; import must be side-effect free
        r = subprocess.run([sys.executable, "-c", "import nova_values as v; assert callable(v.value_check)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        with mock.patch.object(nv.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("nv_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
