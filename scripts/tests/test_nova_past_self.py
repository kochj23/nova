#!/usr/bin/env python3
"""Tests for nova_past_self.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_past_self.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nps", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load()


def _row(text, source="email_archive", meta=None, created=None, dist=0.2):
    return {"text": text, "source": source, "metadata": meta, "created_at": created, "distance": dist}


def _db(rows):
    conn = MagicMock(); conn.fetch = AsyncMock(return_value=rows); conn.close = AsyncMock()
    return patch.object(ps.asyncpg, "connect", AsyncMock(return_value=conn)), conn


def _embed(vec=(0.1, 0.2)):
    return patch.object(ps, "get_embedding", AsyncMock(return_value=list(vec)))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(ps.DB_DSN, r"//[^/@]+:[^/@]+@")

    def test_query_text_never_enters_sql_and_sources_are_bound(self):
        p, conn = _db([])
        with p, _embed():
            asyncio.run(ps.query_past_self("x'; select 1 --", year=2003))
        sql, *params = conn.fetch.call_args[0]
        self.assertNotIn("select 1", sql)
        self.assertEqual(tuple(params[1:]), ps.PERSONAL_SOURCES)
        self.assertIn("source IN ($2, $3, $4, $5, $6)", sql)
        conn.close.assert_awaited_once()

    def test_requires_a_time_window(self):
        with self.assertRaises(ValueError):
            asyncio.run(ps.query_past_self("anything"))


class TestPerformance(unittest.TestCase):
    def test_extract_year_10k_long_texts(self):
        text = "Date: Tue, 4 Mar 2003 10:00\n" + ("blah " * 5000)
        t0 = time.perf_counter()
        for _ in range(10_000):
            ps.extract_year(text)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_embed_server_down_exits_cleanly_without_db(self):
        # RETRY GAP: get_embedding() — one POST, no retry; CLI prints an error and exits 1, never touches PG
        client = MagicMock(); client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client.__aenter__ = AsyncMock(return_value=client); client.__aexit__ = AsyncMock(return_value=False)
        p, conn = _db([])
        err = io.StringIO()
        with patch.object(ps.httpx, "AsyncClient", return_value=client), p as connect, \
             patch.object(sys, "argv", ["x", "raves", "--year", "2001"]), redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                ps.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(client.post.await_count, 1)
        connect.assert_not_called()
        self.assertIn("Cannot reach embedding server", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_extract_year_patterns(self):
        self.assertEqual(ps.extract_year("Date: Mon, 1 Jan 2003 x"), 2003)
        self.assertEqual(ps.extract_year("on 2011-07 we"), 2011)
        self.assertEqual(ps.extract_year("sent 07/04/1999 ok"), 1999)
        self.assertEqual(ps.extract_year("March 3, 2005 it rained"), 2005)
        self.assertIsNone(ps.extract_year("no date here 12345"))
        self.assertIsNone(ps.extract_year(""))

    def test_year_in_range(self):
        self.assertFalse(ps.year_in_range(None, 2000, 2005))
        self.assertTrue(ps.year_in_range(2000, 2000, 2005))
        self.assertFalse(ps.year_in_range(2006, 2000, 2005))

    def test_format_excerpt(self):
        self.assertEqual(ps.format_excerpt("From: a\nTo: b\nSubject: c\n\nhello   world"), "hello world")
        self.assertTrue(ps.format_excerpt("x" * 500).endswith("..."))
        self.assertEqual(ps.format_excerpt("short"), "short")

    def test_parse_range(self):
        self.assertEqual(ps.parse_range("2000-2005"), (2000, 2005))
        for bad in ("2005-2000", "abc", "2000-x", "1-2-3"):
            with self.assertRaises(ps.argparse.ArgumentTypeError):
                ps.parse_range(bad)

    def test_format_narrative(self):
        self.assertIn('No personal communications found about "x" from 2003.', ps.format_narrative([], "x", 2003, 2003))
        out = ps.format_narrative([{"source": "email_archive", "date_raw": "d", "excerpt": "e"}], "q", 2000, 2002)
        self.assertIn("(2000-2002)", out); self.assertIn("(Email Archive)", out); self.assertIn("(1 result found)", out)


class TestIntegration(unittest.TestCase):
    def test_year_resolution_chain_metadata_then_text_then_created_at(self):
        rows = [_row("no date", meta={"date": "2003-02-01"}, dist=0.3),
                _row("Date: Fri, 7 Feb 2003 09:00\n\nbody", dist=0.1),
                _row("plain", created=datetime(2003, 5, 5), dist=0.2),
                _row("plain", created=datetime(2025, 5, 5)),            # recent ingest date is not trusted
                _row("Date: 1999-01-01", dist=0.05)]                     # wrong year
        p, _ = _db(rows)
        with p, _embed():
            res = asyncio.run(ps.query_past_self("q", year_start=2003, year_end=2003))
        self.assertEqual([r["distance"] for r in res], [0.1, 0.2, 0.3])
        self.assertEqual(res[0]["date_raw"], "Fri, 7 Feb 2003 09:00")
        self.assertEqual(res[1]["date_raw"], "~2003")
        self.assertEqual(res[2]["date_raw"], "2003-02-01")

    def test_uses_memory_server_embed_and_memories_table(self):
        self.assertTrue(ps.EMBED_URL.endswith("/embed"))
        self.assertEqual(ps.TABLE, "memories")


class TestFunctional(unittest.TestCase):
    def test_cli_json_golden_path(self):
        p, _ = _db([_row("Date: 2001-06-01\n\n\n\nraving all night", source="livejournal")])
        out = io.StringIO()
        with p, _embed(), patch.object(sys, "argv", ["x", "raves", "--range", "2000-2005", "--json", "--limit", "3"]), \
             redirect_stdout(out):
            ps.main()
        data = json.loads(out.getvalue())
        self.assertEqual(data[0]["year"], 2001)
        self.assertEqual(data[0]["source"], "livejournal")

    def test_cli_without_window_errors(self):
        with patch.object(sys, "argv", ["x", "raves"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ps.main()
        self.assertEqual(cm.exception.code, 2)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--range", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_past_self"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
