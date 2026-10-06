#!/usr/bin/env python3
"""Tests for nova_scanner_backfill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The backfill rewrites memories in place: PG, the corrector LLM and Slack are all replaced at load."""
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sb = _load("nova_scanner_backfill_t", SCRIPTS / "nova_scanner_backfill.py")
import nova_config as _real_cfg  # noqa: E402
sb.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_FEED=_real_cfg.SLACK_FEED)
sb.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")))
sb.correct = MagicMock(side_effect=lambda body, source: (body.upper(), 0.9))
SRC = (SCRIPTS / "nova_scanner_backfill.py").read_text()


class _Conn:
    """Fake connection: count query -> remaining; first SELECT batch -> rows, then empty."""
    def __init__(self, rows):
        self.batches = [list(rows), []]; self.remaining = len(rows); self.writes = []; self.queries = []
        self.autocommit = False; self.closed = False

    def cursor(self):
        conn = self

        class _C:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params=None):
                conn.queries.append((sql, params)); s.sql = sql
                if sql.startswith("UPDATE"):
                    conn.writes.append(params)

            def fetchone(s):
                return (conn.remaining,)

            def fetchall(s):
                return conn.batches.pop(0)
        return _C()

    def close(self):
        self.closed = True


def _run(rows):
    conn = _Conn(rows)
    sb.nova_config.post_both.reset_mock()
    with patch.object(sb.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
        sb.main()
    return conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", sb.DSN)

    def test_update_is_parameterized_and_scoped_by_id(self):
        evil = "x' OR '1'='1"
        conn, _ = _run([(7, f"[Ch 1] {evil}", "scanner", {})])
        upd = [(s, p) for s, p in conn.queries if s.startswith("UPDATE")]
        self.assertEqual(upd[0][0], "UPDATE memories SET text=%s, metadata=%s WHERE id=%s")
        self.assertEqual(upd[0][1][2], 7)
        self.assertNotIn(evil, upd[0][0])

    def test_only_scanner_family_sources_touched(self):
        conn, _ = _run([])
        self.assertEqual(conn.queries[0][1], (["scanner", "fire", "rail"],))


class TestPerformance(unittest.TestCase):
    def test_correct_row_10k(self):
        rows = [(i, f"[Dispatch] unit {i} respond code three", "fire", '{"a": 1}') for i in range(10_000)]
        t0 = time.perf_counter()
        for r in rows:
            sb._correct_row(r)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_row_error_skipped_batch_continues(self):
        # RETRY GAP: _correct_row/correct — one attempt per row; a failed row stays uncorrected
        # (metadata.corrected IS NULL) so a re-run picks it up — the resumable query is the retry.
        def flaky(body, source):
            if "bad" in body:
                raise RuntimeError("router timeout")
            return body, 0.5
        with patch.object(sb, "correct", side_effect=flaky):
            conn, out = _run([(1, "good", "scanner", {}), (2, "bad", "scanner", {})])
        self.assertEqual([w[2] for w in conn.writes], [1])
        self.assertIn("row error: router timeout", out)
        batch_sql = [q for q, _ in conn.queries if q.startswith("SELECT id")]
        self.assertTrue(all("(metadata->>'corrected') IS NULL" in q for q in batch_sql))


class TestUnit(unittest.TestCase):
    def test_prefix_preserved_and_raw_kept(self):
        mid, text, meta = sb._correct_row((3, "[Burbank PD] suspect northbound", "scanner", None))
        self.assertEqual(text, "[Burbank PD] SUSPECT NORTHBOUND")
        m = json.loads(meta)
        self.assertEqual((m["corrected"], m["correction_confidence"], m["raw_transcript"]),
                         (True, 0.9, "suspect northbound"))

    def test_string_metadata_merged(self):
        _, text, meta = sb._correct_row((4, "no prefix here", "rail", '{"talkgroup": 12}'))
        self.assertEqual(text, "NO PREFIX HERE")
        self.assertEqual(json.loads(meta)["talkgroup"], 12)

    def test_empty_text(self):
        _, text, _ = sb._correct_row((5, None, "fire", {}))
        self.assertEqual(text, "")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_corrector_and_feed_channel(self):
        self.assertIn("from nova_scanner_correct import correct", SRC)
        self.assertTrue(hasattr(importlib.import_module("nova_scanner_correct"), "correct"))
        _run([])
        self.assertEqual(sb.nova_config.post_both.call_args[1]["slack_channel"], _real_cfg.SLACK_FEED)


class TestFunctional(unittest.TestCase):
    def test_backfill_golden_path(self):
        rows = [(i, f"[Fire] engine {i} on scene", "fire", {}) for i in range(5)]
        conn, out = _run(rows)
        self.assertEqual(len(conn.writes), 5)
        self.assertTrue(conn.autocommit and conn.closed)
        posts = [c[0][0] for c in sb.nova_config.post_both.call_args_list]
        self.assertIn("5 transcripts to LLM-correct", posts[0])
        self.assertIn("complete — 5 transcripts", posts[-1])
        self.assertIn("DONE — 5 corrected", out)

    def test_nothing_to_do(self):
        conn, out = _run([])
        self.assertEqual(conn.writes, [])
        self.assertIn("DONE — 0 corrected", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_scanner_backfill"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
