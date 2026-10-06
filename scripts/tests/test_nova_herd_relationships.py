#!/usr/bin/env python3
"""Tests for nova_herd_relationships.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG, Ollama and the memory server are mocked; the log goes to a tempdir.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_herd_relationships.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("herdrel", SCRIPTS / "nova_herd_relationships.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = _load()
hr.LOG_FILE = Path(_TMP.name) / "herd.log"
hr.HERD_DIR = Path(_TMP.name) / "herd"
hr.print = lambda *a, **k: None          # module-level log() prints; keep test output clean


class _Cur:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.sql.append((sql, params))

    def fetchone(self):
        return self.db.row

    def fetchall(self):
        return self.db.rows


class _DB:
    def __init__(self, row=None, rows=None):
        self.row = row; self.rows = rows or []; self.sql = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **k):
        return _Cur(self)

    def writes(self, verb):
        return [(s, p) for s, p in self.sql if s.lstrip().startswith(verb)]


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _ollama(content):
    return _Resp(json.dumps({"message": {"content": content}}).encode())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))

    def test_pii_scrubbed_before_llm(self):
        addr = "kochj23" + "@" + "gmail.com"
        home = str(Path.home()) + "/secret.txt"
        out = hr.scrub_pii(f"mail {addr} or bob@example.org, file {home}")
        self.assertNotIn(addr, out)
        self.assertNotIn("bob@example.org", out)
        self.assertNotIn(str(Path.home()) + "/", out)
        db = _DB(row={"persona": "careful", "nova_view": "steady"})
        seen = []
        with patch.object(hr, "_conn", db), \
             patch.object(hr, "_ollama_chat", side_effect=lambda s, u: seen.append(u) or "{}"):
            hr.capture_self_testimony("Marey", f"Please write to {addr} — " + "x" * 60)
        self.assertNotIn(addr, seen[0])

    def test_llm_stays_on_box(self):
        self.assertTrue(all(h.startswith("http://192.168.1.") for h in hr.OLLAMA_HOSTS))


class TestPerformance(unittest.TestCase):
    def test_dedup_merge_10k_bounded(self):
        adds = [f"idea number {i % 500}" for i in range(10_000)]
        t0 = time.perf_counter()
        out = hr._dedup_merge([], adds, hr.MAX_RUNNING_IDEAS)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(out), hr.MAX_RUNNING_IDEAS)


class TestRetry(unittest.TestCase):
    def test_ollama_fails_over_hosts(self):
        calls = []

        def uo(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("host down")
            return _ollama("<think>hm</think>final answer")

        with patch.object(hr.urllib.request, "urlopen", side_effect=uo):
            self.assertEqual(hr._ollama_chat("s", "u"), "final answer")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.split("/api")[0] for c in calls], hr.OLLAMA_HOSTS)

    def test_all_hosts_down_returns_empty(self):
        with patch.object(hr.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(hr._ollama_chat("s", "u"), "")

    def test_memory_write_fails_open(self):
        # RETRY GAP: _write_memory — one POST, best-effort
        with patch.object(hr.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertFalse(hr._write_memory("t", "src", {}, "sub"))
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_extract_json_edges(self):
        self.assertEqual(hr._extract_json(""), {})
        self.assertEqual(hr._extract_json("no json"), {})
        self.assertEqual(hr._extract_json('noise {"a": [1, 2,],} tail'), {"a": [1, 2]})
        self.assertEqual(hr._extract_json("{broken"), {})

    def test_dedup_and_resolve(self):
        self.assertEqual(hr._dedup_merge(["Alpha"], ["alpha", " ", "Beta"], 8), ["Alpha", "Beta"])
        self.assertEqual(hr._dedup_merge(["a long running idea here"], ["A LONG running idea"], 8),
                         ["a long running idea here"])
        self.assertEqual(hr._remove_resolved(["finish the poem draft", "x"], ["the poem draft"]), ["x"])
        self.assertEqual(hr._remove_resolved(["a"], []), ["a"])

    def test_parse_email_dt_and_strip(self):
        self.assertIsNone(hr._parse_email_dt("garbage"))
        self.assertEqual(hr._parse_email_dt("Mon, 1 Jan 2024 10:00:00").tzinfo, timezone.utc)
        self.assertEqual(hr._strip_thinking("<think>a</think> b "), "b")
        self.assertEqual(hr.scrub_pii(""), "")

    def test_context_formats_row(self):
        row = {"persona": "P", "nova_view": "V", "running_ideas": ["i1"], "open_threads": ["t1"],
               "last_exchange": datetime(2024, 5, 6, tzinfo=timezone.utc)}
        with patch.object(hr, "_conn", _DB(row=row)):
            ctx = hr.correspondent_context("Marey")
        self.assertIn("Who they are: P", ctx)
        self.assertIn("Last exchange: 2024-05-06", ctx)
        with patch.object(hr, "_conn", _DB(row={"persona": None})):
            self.assertEqual(hr.correspondent_context("x"), "")


class TestIntegration(unittest.TestCase):
    def test_record_face_appends_with_lineage(self):
        db = _DB()
        with patch.object(hr, "_conn", db):
            self.assertTrue(hr.record_face("Marey", "self_testimony", "I erred", attribution="Marey"))
            self.assertFalse(hr.record_face("Marey", "nova_hypothesis", "   "))
        self.assertIn("CREATE TABLE IF NOT EXISTS herd_correspondent_faces", db.sql[0][0])
        ins = db.writes("INSERT INTO herd_correspondent_faces")
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1][:2], ("Marey", "self_testimony"))
        self.assertIn("lineage", ins[0][1][5].adapted)
        self.assertFalse(db.writes("UPDATE herd_correspondent_faces"))   # append-only

    def test_memory_payload_is_private_with_lineage(self):
        got = {}

        def uo(req, timeout=None):
            got["url"] = req.full_url; got["body"] = json.loads(req.data); return _Resp(b"{}")

        with patch.object(hr.urllib.request, "urlopen", side_effect=uo):
            self.assertTrue(hr._write_memory("hello", "herd_relationships", {"k": 1}, "sub"))
        self.assertTrue(got["url"].startswith(hr.MEMORY_URL))
        self.assertEqual(got["body"]["metadata"]["privacy"], "private")
        self.assertIn("lineage", got["body"]["metadata"])


class TestFunctional(unittest.TestCase):
    def test_update_correspondent_upserts_merged_row(self):
        db = _DB(row={"persona": "old", "nova_view": "old view", "running_ideas": ["a"],
                      "open_threads": ["finish the long poem"], "last_exchange": None})
        synth = json.dumps({"persona": "new p", "nova_view": "new view", "new_running_ideas": ["b"],
                            "new_open_threads": ["t2"], "resolved_threads": ["finish the long poem"]})
        le = datetime(2024, 1, 2, tzinfo=timezone.utc)
        with patch.object(hr, "_conn", db), patch.object(hr, "_ollama_chat", return_value=synth), \
             patch.object(hr, "capture_self_testimony") as cst:
            hr.update_correspondent("Marey", "m@example.org", [{"subject": "s", "body_excerpt": "hello"}], le)
        up = db.writes("INSERT INTO herd_correspondents")[0][1]
        self.assertEqual(up, ("Marey", "m@example.org", "new p", ["a", "b"], ["t2"], "new view", le))
        self.assertTrue(db.writes("INSERT INTO herd_correspondent_faces"))     # view changed -> hypothesis
        cst.assert_called_once()

    def test_update_never_raises_when_db_down(self):
        def boom():
            raise RuntimeError("pg down")
        with patch.object(hr, "_conn", boom), patch.object(hr, "_ollama_chat", return_value=""):
            self.assertIsNone(hr.update_correspondent("x", "e", []))
        self.assertIn("failed", hr.LOG_FILE.read_text())


class TestFrame(unittest.TestCase):
    def test_no_command_prints_doc_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_herd_relationships.py"), "--help"],
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Public API", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_herd_relationships"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
