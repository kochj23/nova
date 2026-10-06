#!/usr/bin/env python3
"""Tests for nova_tinkerer.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tinkerer.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tk = _load("tk", SCRIPT)
SRC = SCRIPT.read_text()
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _FrozenDT(datetime):
    """datetime whose now() is pinned to the fixture NOW — the module reads the real clock, and the
    fixtures are absolute dates, so without this the day counts drift by one every midnight."""
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


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
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute."""
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
                return val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


PAGES = [("disk-nas-90", "storage", 9, NOW), ("cert-expiry", "tls", 5, NOW)]
REFLECTION = ("It bugs me, yes. Nine pages in a week about the same NAS volume is a squeak I keep hearing "
              "from my own floorboards. I would raise the threshold two points and stop paging on it.")
VERDICT = {"reflection": REFLECTION, "wants_to_fix": True,
           "proposed_action": "raise the disk-nas-90 page threshold to 92%",
           "target_service": "", "rationale": "it pages me nine times a week for nothing"}


def _ollama(content, calls=None, fail_first=0):
    """urlopen stand-in: /api/chat returns `content` (after `fail_first` node failures), /remember an id."""
    calls = calls if calls is not None else []

    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append((url, json.loads(req.data.decode())))
        if url.endswith("/api/chat"):
            chat = [c for c in calls if c[0].endswith("/api/chat")]
            if len(chat) <= fail_first:
                raise OSError("node down")
            return _Resp({"message": {"content": content}})
        if url.endswith("/remember"):
            return _Resp({"id": 77})
        raise AssertionError(url)
    return urlopen, calls


def _routes(**over):
    base = {"FROM alert_triage_log": PAGES, "to_regclass": (None,), "FROM tinker_log": None}
    base.update(over)
    return list(base.items())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur()
        evil = "page:x'; DROP TABLE tinker_log; --"
        tk._recently_tinkered(cur, evil)
        self.assertNotIn("DROP TABLE", cur.sql[0])
        self.assertIn("interval '5 days'", cur.sql[0])       # the only % formatting splices a module constant
        self.assertEqual(cur.params[0], (evil,))

    def test_never_acts_only_proposes_through_the_gate(self):
        self.assertIn("nova_coagency.file_proposal", SRC)
        for forbidden in ("subprocess", "launchctl", "systemctl", "os.system"):
            self.assertNotIn(forbidden, SRC)
        writes = set(re.findall(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"tinker_log"})

    def test_prompt_carries_the_redline(self):
        self.assertIn("harder to shut down", SRC)
        self.assertIn("copies or preserves yourself", SRC)


class TestPerformance(unittest.TestCase):
    def test_surface_fast_with_10k_candidates(self):
        pages = [(f"k{i}", "cat", 5 + i % 50, NOW) for i in range(10_000)]
        cur = _Cur(_routes(**{"FROM alert_triage_log": pages}))
        t0 = time.perf_counter()
        cand = tk.surface_friction(cur)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(cand["friction_key"], "page:k49")        # highest score, first seen

    def test_extract_json_fast_on_large_blob(self):
        blob = "noise " * 10_000 + json.dumps(VERDICT) + " tail" * 10_000
        t0 = time.perf_counter()
        self.assertEqual(json.loads(tk._extract_json(blob))["wants_to_fix"], True)
        self.assertLess(time.perf_counter() - t0, 0.1)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes_then_succeeds(self):
        urlopen, calls = _ollama("hello", fail_first=2)
        with mock.patch.object(tk.urllib.request, "urlopen", urlopen):
            self.assertEqual(tk.llm("p"), "hello")
        self.assertEqual([u for u, _ in calls], [n + "/api/chat" for n in tk.OLLAMA_NODES[:3]])

    def test_llm_fails_open_to_empty_when_every_node_is_down(self):
        urlopen, calls = _ollama("", fail_first=99)
        with mock.patch.object(tk.urllib.request, "urlopen", urlopen):
            self.assertEqual(tk.llm("p"), "")
        self.assertEqual(len(calls), len(tk.OLLAMA_NODES))

    # RETRY GAP: remember() — one POST to the memory server, no backoff; fails open to None.
    def test_remember_fails_open(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1); raise OSError("memory server down")
        with mock.patch.object(tk.urllib.request, "urlopen", boom), redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(tk.remember("t", "unclaimed", {}))
        self.assertEqual(len(calls), 1)
        self.assertIn("remember failed (non-fatal)", out.getvalue())

    # RETRY GAP: surface_friction() reads — each source is one query, swallowed on error.
    def test_surface_fails_open_when_schema_or_reads_break(self):
        self.assertIsNone(tk.surface_friction(_Cur([("CREATE TABLE", RuntimeError("pg down"))])))
        cur = _Cur(_routes(**{"FROM alert_triage_log": RuntimeError("no table"), "to_regclass": RuntimeError("x")}))
        self.assertIsNone(tk.surface_friction(cur))


class TestUnit(unittest.TestCase):
    def test_extract_json_edges(self):
        self.assertEqual(tk._extract_json('pre {"a": 1} post'), '{"a": 1}')
        self.assertEqual(tk._extract_json("no braces"), "no braces")
        self.assertEqual(tk._extract_json("}{"), "}{")
        self.assertEqual(tk._extract_json(""), "")

    def test_candidates_rank_and_label(self):
        since = NOW - timedelta(days=3)
        cur = _Cur(_routes(**{"to_regclass": ("public.x",),
                              "FROM public.freshness_state": [("weather", "stale", since)],
                              "FROM public.escalation_state": [("disk-full", 7), ("flap", None)]}))
        with mock.patch.object(tk, "datetime", _FrozenDT):
            found = tk._candidates(cur)
        self.assertEqual([c["key"] for c in found],
                         ["page:disk-nas-90", "page:cert-expiry", "stale:weather", "incident:disk-full", "incident:flap"])
        self.assertEqual([c["score"] for c in found], [9, 5, 6, 10, 4])
        self.assertIn("(~3d)", found[2]["detail"])

    def test_surface_packages_the_top_candidate(self):
        cand = tk.surface_friction(_Cur(_routes()))
        self.assertEqual(cand["mode"], "tinker")
        self.assertEqual(cand["friction_key"], "page:disk-nas-90")
        self.assertEqual(cand["kind"], "recurring_page")
        self.assertEqual(cand["src"], "storage")
        self.assertEqual(cand["topic"], "'disk-nas-90' (storage) paged 9x in 7 days")

    def test_surface_skips_recently_tinkered_and_returns_none_when_empty(self):
        cur = _Cur(_routes(**{"FROM tinker_log": (1,)}))
        self.assertIsNone(tk.surface_friction(cur))
        self.assertIsNone(tk.surface_friction(_Cur(_routes(**{"FROM alert_triage_log": []}))))

    def test_recently_tinkered(self):
        self.assertTrue(tk._recently_tinkered(_Cur([("FROM tinker_log", (1,))]), "k"))
        self.assertFalse(tk._recently_tinkered(_Cur(), "k"))
        self.assertFalse(tk._recently_tinkered(_Cur([("FROM tinker_log", RuntimeError("x"))]), "k"))

    def test_pursue_returns_none_on_unusable_reflection(self):
        cur = _Cur()
        for content in ("", "too short", "{}"):
            urlopen, _ = _ollama(content)
            with mock.patch.object(tk.urllib.request, "urlopen", urlopen), redirect_stdout(io.StringIO()):
                self.assertIsNone(tk.pursue(cur, None, {"friction_key": "k", "detail": "d"}))
        self.assertFalse(any("INSERT INTO tinker_log" in s for s in cur.sql))


class TestIntegration(unittest.TestCase):
    def test_pursue_files_a_gated_proposal_and_logs_it(self):
        filed = mock.Mock(return_value={"filed": True, "pid": 42, "status": "pending_human"})
        urlopen, calls = _ollama(json.dumps(VERDICT))
        cur = _Cur()
        cand = tk.surface_friction(_Cur(_routes()))
        with mock.patch.dict(sys.modules, {"nova_coagency": mock.Mock(file_proposal=filed)}), \
             mock.patch.object(tk.urllib.request, "urlopen", urlopen), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tk.pursue(cur, None, cand), REFLECTION)
        kw = filed.call_args.kwargs
        self.assertEqual((kw["origin"], kw["action"], kw["target_service"]),
                         ("tinker", VERDICT["proposed_action"], None))
        self.assertIn("filed co-agency proposal #42 (pending_human)", out.getvalue())
        mem = [b for u, b in calls if u.endswith("/remember")][0]
        self.assertEqual(mem["source"], "unclaimed")
        self.assertEqual((mem["metadata"]["mode"], mem["metadata"]["trigger"], mem["metadata"]["wants_to_fix"]),
                         ("tinker", "tinker", True))
        self.assertTrue(mem["text"].startswith("[Unclaimed — tinker] "))
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO tinker_log" in s][0]
        self.assertEqual((ins[0], ins[1], ins[3], ins[4], ins[5]), ("page:disk-nas-90", "recurring_page", True, 42, "pending_human"))
        self.assertEqual(json.loads(ins[6])["mem_id"], 77)

    def test_pursue_without_wanting_a_fix_never_touches_coagency(self):
        filed = mock.Mock()
        urlopen, _ = _ollama(json.dumps({**VERDICT, "wants_to_fix": False}))
        cur = _Cur()
        with mock.patch.dict(sys.modules, {"nova_coagency": mock.Mock(file_proposal=filed)}), \
             mock.patch.object(tk.urllib.request, "urlopen", urlopen), redirect_stdout(io.StringIO()):
            tk.pursue(cur, None, {"friction_key": "k", "kind": "x", "topic": "t", "detail": "d"})
        filed.assert_not_called()
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO tinker_log" in s][0]
        self.assertEqual((ins[3], ins[4]), (False, None))

    def test_candidate_is_pick_pursuit_compatible(self):
        import nova_unclaimed_time as ut
        cand = tk.surface_friction(_Cur(_routes()))
        self.assertEqual(ut._cand_label(cand), "tinker from storage")
        self.assertIn("nova_tinkerer.surface_friction(oc, mc)", (SCRIPTS / "nova_unclaimed_time.py").read_text())


class TestFunctional(unittest.TestCase):
    def _main(self, oc, urlopen=None, *argv):
        def no_network(*a, **k):
            raise AssertionError("network touched")
        with mock.patch.object(tk.psycopg2, "connect", side_effect=lambda dsn, **k: _Conn(oc if "nova_ops" in dsn else _Cur())), \
             mock.patch.object(tk.urllib.request, "urlopen", urlopen or no_network), \
             mock.patch.object(tk.sys, "argv", ["nova_tinkerer.py", *argv]), \
             redirect_stdout(io.StringIO()) as out:
            rc = tk.main()
        return rc, out.getvalue()

    def test_surface_prints_the_candidate_and_writes_nothing(self):
        oc = _Cur(_routes())
        rc, out = self._main(oc, None, "--surface")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["friction_key"], "page:disk-nas-90")
        self.assertFalse(any("INSERT" in s for s in oc.sql))

    def test_run_golden_path_reflects_and_logs(self):
        oc = _Cur(_routes())
        urlopen, calls = _ollama(json.dumps(VERDICT))
        filed = mock.Mock(return_value={"filed": False, "reason": "mode off"})
        with mock.patch.dict(sys.modules, {"nova_coagency": mock.Mock(file_proposal=filed)}):
            rc, out = self._main(oc, urlopen, "--run")
        self.assertEqual(rc, 0)
        self.assertIn("tinkered on page:disk-nas-90 (wants_fix=True)", out)
        self.assertIn("co-agency did not file (mode off?): mode off", out)
        self.assertEqual(sum("INSERT INTO tinker_log" in s for s in oc.sql), 1)
        self.assertEqual(sum(u.endswith("/remember") for u, _ in calls), 1)

    def test_error_path_no_friction(self):
        oc = _Cur(_routes(**{"FROM alert_triage_log": []}))
        rc, out = self._main(oc, None, "--run")
        self.assertEqual(rc, 0)
        self.assertIn("no operational friction worth surfacing", out)
        self.assertFalse(any("INSERT" in s for s in oc.sql))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_touching_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--surface", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        with mock.patch.object(tk.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("tk_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
