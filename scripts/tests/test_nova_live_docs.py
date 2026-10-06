#!/usr/bin/env python3
"""Tests for nova_live_docs.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_live_docs.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="livedocs-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ld = _load("livedocs_under_test", SCRIPT)
ld.WORKSPACE = TMP / "workspace"          # never touch ~/.openclaw/workspace
ld.WORKSPACE.mkdir()


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Cur:
    def __init__(self, nodes=8, docs=None):
        self.nodes, self.docs, self.sql = nodes, docs or {}, []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql.append((sql, params)); self._last = sql
    def fetchone(self): return (self.nodes,)
    def fetchall(self): return list(self.docs.items())


def _pg(cur):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return cur
    return _C()


def _fresh():
    ld._cache["at"] = 0.0; ld._cache["vals"] = {}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ld.OPS_DSN)

    def test_sql_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        cur = _Cur(docs={"identity": "x"})
        with patch("psycopg2.connect", lambda *a, **k: _pg(cur)):
            ld.load_docs(("identity", "'; DROP TABLE agent_docs; --"))
        sql, params = cur.sql[0]
        self.assertIn("doc_type = ANY(%s)", sql)
        self.assertEqual(params, (["identity", "'; DROP TABLE agent_docs; --"],))

    def test_render_substitutes_only_the_known_placeholders(self):
        # an attacker-controlled doc cannot pull arbitrary names out of the probe dict
        self.assertEqual(ld.render("{{OPS_DSN}} {{__class__}}", {"OPS_DSN": "leak"}), "{{OPS_DSN}} {{__class__}}")


class TestPerformance(unittest.TestCase):
    def test_render_10k_placeholders_fast(self):
        text = " ".join("{{memory_count}} and {{ as_of }}" for _ in range(5_000))
        vals = {"memory_count": "1", "as_of": "d"}
        t0 = time.perf_counter()
        out = ld.render(text, vals)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out.count("1 and d"), 5_000)


class TestRetry(unittest.TestCase):
    def test_probe_fails_open_per_source(self):
        # RETRY GAP: _probe()/memory-server and _probe()/psycopg2 — one attempt each; a failed probe simply
        # leaves its key unset so the placeholder renders "(unmeasured)" instead of a stale number
        attempts = []

        def dead(*a, **k):
            attempts.append(a); raise OSError("down")
        with patch("urllib.request.urlopen", dead), patch("psycopg2.connect", dead):
            v = ld._probe()
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("memory_count", v)
        self.assertNotIn("node_count", v)
        self.assertEqual(ld.render("{{memory_count}}/{{node_count}}/{{script_count}}", v).split("/")[:2], ["(unmeasured)", "(unmeasured)"])
        self.assertTrue(v["script_count"].isdigit())

    def test_write_workspace_fails_loud_when_pg_is_down(self):
        # RETRY GAP: load_docs()/psycopg2 — no retry; the exception propagates so the job is marked failed
        # rather than silently writing empty identity files
        ws = TMP / "ws-pg-down"; ws.mkdir()
        with patch("psycopg2.connect", lambda *a, **k: (_ for _ in ()).throw(OSError("pg down"))), patch.object(ld, "WORKSPACE", ws):
            with self.assertRaises(OSError):
                ld.write_workspace()
        self.assertEqual(list(ws.iterdir()), [])


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        with redirect_stdout(io.StringIO()) as out:
            ld.demo()
        self.assertIn("all live-docs assertions passed", out.getvalue())

    def test_render_edges(self):
        self.assertEqual(ld.render(None, {}), "")
        self.assertEqual(ld.render("", {"as_of": "x"}), "")
        self.assertEqual(ld.render("{{ memory_count }}", {"memory_count": "2,241,191"}), "2,241,191")
        self.assertEqual(ld.render("{{memory_count}}", {}), "(unmeasured)")
        self.assertEqual(ld.render("{{bogus}}", {}), "{{bogus}}")

    def test_values_caches_for_cache_s_and_force_refreshes(self):
        _fresh()
        probes = []
        with patch.object(ld, "_probe", lambda: probes.append(1) or {"as_of": "d"}):
            ld.values(); ld.values()
            self.assertEqual(len(probes), 1)
            ld.values(force=True)
            self.assertEqual(len(probes), 2)
            with patch.object(ld.time, "time", lambda: ld._cache["at"] + ld.CACHE_S + 1):
                ld.values()
            self.assertEqual(len(probes), 3)
        _fresh()

    def test_probe_formats_counts(self):
        cur = _Cur(nodes=8)
        with patch("urllib.request.urlopen", lambda u, timeout=None: _Resp({"count": 2241191})), \
             patch("psycopg2.connect", lambda *a, **k: _pg(cur)):
            v = ld._probe()
        self.assertEqual(v["memory_count"], "2,241,191")
        self.assertEqual(v["node_count"], "8")
        self.assertEqual(v["as_of"], ld.date.today().isoformat())
        self.assertIn("service_registry", cur.sql[0][0])


class TestIntegration(unittest.TestCase):
    def test_load_docs_reads_agent_docs_for_agent_all(self):
        cur = _Cur(docs={"identity": "I have {{memory_count}} memories.", "soul": "s"})
        with patch("psycopg2.connect", lambda *a, **k: _pg(cur)):
            docs = ld.load_docs()
        self.assertEqual(set(docs), {"identity", "soul"})
        sql, params = cur.sql[0]
        self.assertIn("FROM agent_docs WHERE agent_id='all'", sql)
        self.assertEqual(params, (["identity", "soul", "user"],))

    def test_probe_values_flow_into_rendered_docs(self):
        cur = _Cur(nodes=3, docs={"identity": "{{node_count}} nodes as of {{as_of}}"})
        with patch("urllib.request.urlopen", lambda u, timeout=None: _Resp({"count": 10})), \
             patch("psycopg2.connect", lambda *a, **k: _pg(cur)):
            text = ld.render(ld.load_docs()["identity"], ld._probe())
        self.assertEqual(text, f"3 nodes as of {ld.date.today().isoformat()}")


class TestFunctional(unittest.TestCase):
    def test_write_workspace_golden_path(self):
        _fresh()
        for f in ld.WORKSPACE.iterdir():
            f.unlink()
        cur = _Cur(nodes=8, docs={"identity": "Nova: {{memory_count}} memories, {{script_count}} scripts",
                                  "soul": "{{node_count}} nodes", "user": "Jordan"})
        with patch("urllib.request.urlopen", lambda u, timeout=None: _Resp({"count": 2241191})), \
             patch("psycopg2.connect", lambda *a, **k: _pg(cur)), redirect_stdout(io.StringIO()) as out:
            ld.write_workspace()
        self.assertEqual(sorted(p.name for p in ld.WORKSPACE.iterdir()), ["IDENTITY.md", "SOUL.md", "USER.md"])
        ident = (ld.WORKSPACE / "IDENTITY.md").read_text()
        self.assertTrue(ident.startswith("Nova: 2,241,191 memories, "))
        self.assertNotIn("{{", ident)
        self.assertEqual((ld.WORKSPACE / "SOUL.md").read_text(), "8 nodes")
        self.assertIn("wrote IDENTITY.md", out.getvalue())
        self.assertIn("memory_count=2,241,191", out.getvalue())
        _fresh()

    def test_memory_server_outage_renders_unmeasured_not_stale(self):
        _fresh()
        cur = _Cur(nodes=8, docs={"identity": "{{memory_count}} memories"})
        with patch("urllib.request.urlopen", lambda u, timeout=None: (_ for _ in ()).throw(OSError("down"))), \
             patch("psycopg2.connect", lambda *a, **k: _pg(cur)), redirect_stdout(io.StringIO()):
            ld.write_workspace()
        self.assertEqual((ld.WORKSPACE / "IDENTITY.md").read_text(), "(unmeasured) memories")
        _fresh()


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all live-docs assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_live_docs"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
