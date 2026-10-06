#!/usr/bin/env python3
"""Tests for nova_block_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_block_report.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    nj = types.ModuleType("nova_journal"); nj.call_openrouter = MagicMock(return_value="")
    voice = types.ModuleType("nova_voice"); voice.system_prompt = MagicMock(side_effect=lambda ctx: f"SYS {ctx}")
    ref = types.ModuleType("nova_code_reference"); ref.code_reference_block = MagicMock(return_value="\nREF")
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    return {"nova_journal": nj, "nova_voice": voice, "nova_code_reference": ref, "nova_notify": nn}


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _stubbed(_stub_modules()):
        spec.loader.exec_module(mod)
    return mod


br = _load("br", SCRIPT)
# Offline guard: replace the module's own psycopg2 binding (never the real module in sys.modules).
br.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")),
                                    extras=types.SimpleNamespace(RealDictCursor=object))


class _Conn:
    def __init__(self, rows):
        self.rows = rows; self.sql = []; self.closed = False; self.autocommit = False

    def cursor(self, cursor_factory=None):
        return self

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


def _row(mi=0.4, dir_="NE", source="scanner", text="211 in progress at 123 Main St", anchor=None, hour=14):
    return {"source": source, "text": text, "created_at": datetime(2026, 10, 5, hour, 30), "mi": mi, "dir": dir_,
            "anchor": anchor}


def _run(rows, body="x" * 400, connect_exc=None):
    conn = _Conn(rows)
    connect = MagicMock(side_effect=connect_exc) if connect_exc else MagicMock(return_value=conn)
    br.psycopg2 = types.SimpleNamespace(connect=connect, extras=types.SimpleNamespace(RealDictCursor=object))
    br.notify = MagicMock(return_value=True)
    br.nj.call_openrouter = MagicMock(return_value=body)
    br.code_reference_block = MagicMock(return_value="\nREF")
    br.nova_voice.system_prompt = MagicMock(side_effect=lambda ctx: f"SYS {ctx}")
    with redirect_stdout(io.StringIO()) as out:
        br.main()
    return conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", br.MEM_DSN)

    def test_sql_is_read_only_and_parameterized(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP)\b", SRC))
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        conn, _ = _run([])
        sql, params = conn.sql[0]
        self.assertTrue(sql.startswith("SELECT"))
        self.assertEqual(params, (br.RADIUS_MI,))
        self.assertIn("<= %s", sql)
        self.assertNotIn(str(br.RADIUS_MI), sql)

    def test_private_report_goes_to_the_notify_bus_never_the_public_journal(self):
        self.assertNotIn("publish_hugo", SRC)
        self.assertNotIn("git_push", SRC)
        _run([_row()])
        self.assertEqual(br.notify.call_args[1]["category"], "block_report")

    def test_scanner_text_and_llm_body_are_bounded(self):
        _run([_row(text="Z" * 1000)], body="B" * 10_000)
        block = br.nj.call_openrouter.call_args[0][1]
        self.assertEqual(block.count("Z"), 220)
        self.assertEqual(len(br.notify.call_args[1]["body"]), 2800)


class TestPerformance(unittest.TestCase):
    def test_10k_rows_render_fast(self):
        rows = [_row(mi=round(i / 5000, 3), text=f"call {i}", source="fire" if i % 2 else "scanner") for i in range(10_000)]
        t0 = time.perf_counter()
        _run(rows)
        self.assertLess(time.perf_counter() - t0, 2.0)
        block = br.nj.call_openrouter.call_args[0][1]
        self.assertEqual(block.count("\n"), 9_999)
        self.assertIn("10000 calls within", br.notify.call_args[0][0])


class TestRetry(unittest.TestCase):
    def test_pg_connect_is_one_shot_and_the_error_escapes(self):
        # RETRY GAP: main()/psycopg2.connect — a single attempt; a connect failure propagates to the scheduler
        # (nothing is posted, so no false "quiet 24h" report is ever sent on a DB outage)
        with self.assertRaises(RuntimeError):
            _run([], connect_exc=RuntimeError("pg down"))
        self.assertEqual(br.psycopg2.connect.call_count, 1)
        br.notify.assert_not_called()

    def test_short_llm_output_fails_open_without_posting(self):
        # RETRY GAP: main()/nj.call_openrouter — one call; an empty or <80 char answer skips the post
        for body in ("", None, "too short"):
            _, out = _run([_row()], body=body)
            self.assertEqual(br.nj.call_openrouter.call_count, 1)
            br.notify.assert_not_called()
            self.assertIn("LLM produced too little", out)


class TestUnit(unittest.TestCase):
    def test_line_format_kind_time_and_distance(self):
        _run([_row(source="fire", mi=0.9, dir_="SW", text="structure fire", hour=9),
              _row(source="scanner", mi=2.1, dir_=None, text="traffic stop", hour=23)])
        block = br.nj.call_openrouter.call_args[0][1]
        lines = block.split("\n")
        self.assertEqual(lines[0], "[Mon 09:30] FIRE — ~0.9 mi SW of home: structure fire")
        self.assertEqual(lines[1], "[Mon 23:30] LAPD — ~2.1 mi  of home: traffic stop")

    def test_secondary_anchor_is_named_but_home_is_not_repeated(self):
        _run([_row(anchor={"name": "school", "mi": 0.2, "dir": "N"}),
              _row(anchor={"name": "home", "mi": 0.4, "dir": "NE"}),
              _row(anchor={"mi": 1.0})])
        block = br.nj.call_openrouter.call_args[0][1]
        self.assertIn("of home (~0.2 mi N of school)", block)
        self.assertEqual(block.count("(~"), 1)

    def test_radius_constant(self):
        self.assertEqual(br.RADIUS_MI, 2.5)


class TestIntegration(unittest.TestCase):
    def test_reads_memories_table_from_the_memories_db(self):
        conn, _ = _run([])
        self.assertIn("FROM memories WHERE source IN ('scanner','fire')", conn.sql[0][0])
        self.assertIn("dbname=nova_memories", br.MEM_DSN)
        self.assertTrue(conn.closed and conn.autocommit)

    def test_code_reference_and_voice_helpers_are_shared_not_reimplemented(self):
        _run([_row(text="459 silent at 5 Elm")])
        br.code_reference_block.assert_called_once()
        block, domains = br.code_reference_block.call_args[0]
        self.assertEqual(domains, ["police", "fire"])
        self.assertIn("459 silent", block)
        system = br.nj.call_openrouter.call_args[0][0]
        self.assertTrue(system.startswith("SYS "))
        self.assertTrue(system.endswith("REF"))
        self.assertNotIn("def code_reference_block", SRC)

    def test_llm_call_shape(self):
        _run([_row()])
        self.assertEqual(br.nj.call_openrouter.call_args[1], {"max_tokens": 1200, "temperature": 0.6})


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_the_narrated_report(self):
        conn, out = _run([_row(mi=0.3), _row(mi=1.9, source="fire")], body="The night was quiet except " * 10)
        title, kw = br.notify.call_args[0][0], br.notify.call_args[1]
        self.assertEqual(title, "\U0001F3D8️ Block report — 2 calls within 2.5 mi")
        self.assertTrue(kw["body"].startswith("The night was quiet"))
        self.assertEqual((kw["category"], kw["dedup_key"]), ("block_report", "block-report"))
        self.assertIn("reported 2 nearby incidents", out)

    def test_quiet_night_posts_the_quiet_notice_without_calling_the_llm(self):
        _, out = _run([])
        br.nj.call_openrouter.assert_not_called()
        title, kw = br.notify.call_args[0][0], br.notify.call_args[1]
        self.assertEqual(title, "\U0001F3D8️ Block report")
        self.assertEqual(kw["body"], "Quiet 24h — nothing within 2.5 mi of home on the scanners.")
        self.assertIn("nothing close", out)

    def test_error_path_llm_failure_posts_nothing(self):
        _, out = _run([_row()], body="")
        br.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types\n"
                "for m in ('nova_journal','nova_voice','nova_code_reference','nova_notify'):\n"
                "    sys.modules[m] = types.ModuleType(m)\n"
                "sys.modules['nova_code_reference'].code_reference_block = lambda *a, **k: ''\n"
                "sys.modules['nova_notify'].notify = lambda *a, **k: True\n"
                "import nova_block_report\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
