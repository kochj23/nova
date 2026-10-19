#!/usr/bin/env python3
"""Tests for nova_grounding.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_grounding.py"
SRC = SCRIPT.read_text()
os.environ.setdefault("NOVA_TEST_QUIET", "1")

import nova_grounding as g  # noqa: E402  (import-clean: no side effects at import)

SOURCES = ("TOPIC: x\n\n[1] (news, https://apnews.com/article/1) text one\n\n"
           "[2] (television) Cheers transcript\n\n[3] (camera_events) Exterior cam saw a person\n\n"
           "[4] (news, http://192.168.1.10/admin) internal page")


def _fake_cursor(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda s, *a: False
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        self.assertIsNone(re.search(r"(password|api_key|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))

    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_private_and_ops_sources_never_itemised(self):
        self.assertIsNone(g.memory_label("imessage", {"type": ""}))
        self.assertIsNone(g.memory_label("frame_vision", {"camera": "Exterior - Side"}))
        self.assertIsNone(g.source_label({"source": "camera_events", "url": ""}))

    def test_ip_and_internal_urls_hidden(self):
        self.assertIsNone(g.public_url("http://192.168.1.10/admin"))
        self.assertIsNone(g.public_url("http://grafana.digitalnoise.net/d/x"))
        self.assertIsNone(g.public_url("http://nas.local/share"))
        self.assertEqual(g.public_url("https://nova.digitalnoise.net/research/x/"), "https://nova.digitalnoise.net/research/x/")

    def test_labels_scrubbed(self):
        home = str(Path.home())
        lab = g._scrub(f"OpenClaw run at {home}/secret.txt from 10.0.0.5 [dQw4w9WgXcQ]")
        self.assertNotIn("OpenClaw", lab)
        self.assertNotIn(home, lab)
        self.assertNotIn("10.0.0.5", lab)
        self.assertNotIn("dQw4w9WgXcQ", lab)

    def test_household_name_scrubbed(self):
        self.assertNotIn("Dylan", g._scrub("Dylan's room recap"))


class TestPerformance(unittest.TestCase):
    def test_parse_10k_items_fast(self):
        big = "\n\n".join(f"[{i}] (news, https://example.com/{i}) t" for i in range(10000))
        t = time.time()
        items = g.parse_source_items(big)
        self.assertEqual(len(items), 10000)
        self.assertLess(time.time() - t, 2.0)

    def test_render_caps_list(self):
        items = [{"source": "news", "url": f"https://example.com/{i}"} for i in range(500)]
        with patch.object(g, "_blocked", return_value=False):
            out = g.render(0, [], items)
        self.assertLessEqual(out.count("\n- "), g.MAX_ITEMS + 2)


class TestRetry(unittest.TestCase):
    # RETRY GAP: _fetch_memories — one PG attempt; it fails open (no exception, [] returned)
    def test_fetch_fails_open(self):
        with patch("psycopg2.connect", side_effect=RuntimeError("down")):
            self.assertEqual(g._fetch_memories(["a"]), [])

    def test_footer_never_raises(self):
        with patch.object(g, "parse_source_items", side_effect=RuntimeError("boom")):
            self.assertEqual(g.footer(["a"], "x"), "")


class TestUnit(unittest.TestCase):
    def test_nothing_means_nothing(self):
        self.assertEqual(g.footer(None, None), "")
        self.assertEqual(g.footer([], "no items here"), "")
        self.assertEqual(g.render(0, [], []), "")

    def test_parse_numbered_items(self):
        items = g.parse_source_items(SOURCES)
        self.assertEqual([i["source"] for i in items], ["news", "television", "camera_events", "news"])

    def test_parse_free_text_urls(self):
        items = g.parse_source_items("see https://a.org/x and https://a.org/x, also https://b.com/y.")
        self.assertEqual([i["url"] for i in items], ["https://a.org/x", "https://b.com/y"])

    def test_summary_counts_and_plurals(self):
        with patch.object(g, "_blocked", return_value=False):
            out = g.render(1, [("television", {"type": "tv_transcript", "show": "Cheers (1982)",
                                                "title": "Cheers (1982) - S05E03 - Money Dearest"})],
                           [{"source": "news", "url": "https://apnews.com/a"}])
        self.assertIn("Grounded in 1 memory · 1 source", out)
        self.assertIn("Cheers (1982) - S05E03 - Money Dearest", out)
        self.assertIn(g.MARKER, out)

    def test_add_to_body_idempotent(self):
        body = "x " + g.MARKER
        self.assertEqual(g.add_to_body(body, ["1"], SOURCES), body)

    def test_friendly_source(self):
        self.assertEqual(g.friendly_source("quarantine:film_criticism"), "Film criticism")


class TestIntegration(unittest.TestCase):
    def test_uses_journal_scrubbers_and_privacy_gate(self):
        self.assertIn("nj.scrub_pii", SRC)
        self.assertIn("is_private_source", SRC)
        self.assertIn("_contains_blocked_content", SRC)

    def test_reads_article_citations_and_memories(self):
        self.assertIn("FROM article_citations", SRC)
        self.assertIn("FROM memories WHERE id = ANY(%s)", SRC)

    def test_publish_hugo_calls_hook(self):
        self.assertIn('__import__("nova_grounding").add_to_body(body, cited_memory_ids, sources)',
                      (SCRIPTS / "nova_journal.py").read_text())

    def test_footer_chains_parse_fetch_render(self):
        conn, _ = _fake_cursor([("comedy", {"type": "tv_transcript", "show": "Cheers (1982)"})])
        with patch("psycopg2.connect", return_value=conn), patch.object(g, "_blocked", return_value=False):
            out = g.footer(["m1", "m1", "m2"], SOURCES)
        self.assertIn("Grounded in 2 memories · 4 sources", out)
        self.assertIn("[apnews.com](https://apnews.com/article/1)", out)
        self.assertIn("not itemised", out)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "content/research").mkdir(parents=True)
        self.art = self.root / "content/research/2026-10-10-some-study.md"
        self.art.write_text("---\ntitle: x\n---\n\nBody.")
        (self.root / "content/opinions").mkdir()
        (self.root / "content/opinions/the-fishbowl.md").write_text("rolling")

    def _conn(self):
        conn, cur = _fake_cursor([("some-study", ["m1", "m2"]), ("the-fishbowl", ["m3"]), ("missing", ["m4"])])
        return conn

    def test_backfill_golden_path(self):
        with patch("psycopg2.connect", return_value=self._conn()), \
             patch.object(g, "_fetch_memories", return_value=[("comedy", {"type": "tv_transcript", "show": "Cheers"})]), \
             patch.object(g, "_blocked", return_value=False), patch("builtins.print"):
            changed = g.backfill(30, root=self.root)
        self.assertEqual(changed, [str(self.art)])
        self.assertIn("Grounded in 2 memories", self.art.read_text())
        self.assertEqual((self.root / "content/opinions/the-fishbowl.md").read_text(), "rolling")

    def test_backfill_dry_run_writes_nothing(self):
        with patch("psycopg2.connect", return_value=self._conn()), patch.object(g, "_fetch_memories", return_value=[]), \
             patch("builtins.print"):
            changed = g.backfill(30, dry_run=True, root=self.root)
        self.assertEqual(len(changed), 1)
        self.assertNotIn(g.MARKER, self.art.read_text())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
