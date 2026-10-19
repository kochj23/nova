#!/usr/bin/env python3
"""Tests for nova_corrections.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
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
SCRIPT = SCRIPTS / "nova_corrections.py"
SRC = SCRIPT.read_text()
os.environ.setdefault("NOVA_TEST_QUIET", "1")

import nova_corrections as nc  # noqa: E402

EARLIER = "The fix held overnight and the gateway has been stable ever since, with zero restarts recorded."
LATER = "I said the fix held. It did not: the gateway restarted four times overnight and the fix never loaded."


def _art(title, url, date, text, mid="m"):
    return {"id": mid, "title": title, "url": url, "path": "x.md", "date": date, "text": text}


def fake_llm(system, user):
    if "judge whether" in system:
        return "[0]"
    if "possible self-correction" in system:
        return json.dumps({"genuine": True, "topic": "the gateway fix",
                           "earlier_quote": "The fix held overnight and the gateway has been stable ever since",
                           "later_quote": "the gateway restarted four times overnight and the fix never loaded",
                           "what_changed": "The fix had not loaded.", "which_stands": "later — the logs"})
    return "TITLE: I Was Wrong About the Gateway\n\n" + "Body of the column about being wrong. " * 30


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        self.assertIsNone(re.search(r"(password|api_key|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))

    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_quotes_with_ip_rejected(self):
        j = {"genuine": True, "earlier_quote": "blocked an event on 192.168.1.1 early this morning ok",
             "later_quote": "the gateway restarted four times overnight and the fix never loaded"}
        self.assertIsNone(nc.verify(j, "blocked an event on 192.168.1.1 early this morning ok", LATER))


class TestPerformance(unittest.TestCase):
    def test_cosine_pairs_scale(self):
        import numpy as np
        rng = np.random.default_rng(1)
        rec = [{"stance": f"r{i}", "article": f"A{i}"} for i in range(200)]
        old = [{"stance": f"o{i}", "article": f"B{i}"} for i in range(2000)]
        t = time.time()
        out = nc.cosine_pairs(rec, old, rng.normal(size=(200, 64)), rng.normal(size=(2000, 64)), lo=-1.0)
        self.assertLess(time.time() - t, 5.0)
        self.assertLessEqual(len(out), nc.MAX_SCREEN)


class TestRetry(unittest.TestCase):
    # RETRY GAP: article_pairs / ledger reads — one PG attempt; the article pass fails open inside run()
    def test_article_pass_fails_open(self):
        with patch.object(nc, "candidates", return_value=[]), patch.object(nc, "article_index", return_value={}), \
             patch.object(nc, "article_pairs", side_effect=RuntimeError("pg down")):
            res = nc.run(llm=lambda s, u: "[]")
        self.assertEqual(res["status"], "skipped")

    def test_embedding_failure_degrades(self):
        fake = MagicMock()
        fake.embed.return_value = None
        rec = [{"id": 1, "stance": "a", "article": "A", "recent": True}]
        old = [{"id": 2, "stance": "b", "article": "B", "recent": False}]
        with patch.object(nc, "load_beliefs", return_value=(rec, old, [(2, 1)])), \
             patch.dict(sys.modules, {"nova_beliefs": fake}):
            out = nc.candidates(7)
        self.assertEqual(len(out), 1)   # the explicit revision survives without embeddings


class TestUnit(unittest.TestCase):
    def test_quote_in(self):
        self.assertTrue(nc.quote_in("the fix held overnight and the gateway", EARLIER))
        self.assertFalse(nc.quote_in("the fix", EARLIER))

    def test_verify_requires_genuine_and_both_quotes(self):
        good = fake_llm("possible self-correction", "")
        self.assertIsNotNone(nc.verify(json.loads(good), EARLIER, LATER))
        self.assertIsNone(nc.verify(dict(json.loads(good), genuine=False), EARLIER, LATER))
        self.assertIsNone(nc.verify(json.loads(good), LATER, EARLIER))

    def test_cosine_pairs_skip_same_article_and_restatements(self):
        rec = [{"stance": "x", "article": "Same"}, {"stance": "y", "article": "R"}]
        old = [{"stance": "x2", "article": "Same"}, {"stance": "y", "article": "O"}]
        out = nc.cosine_pairs(rec, old, [[1, 0], [0, 1]], [[1, 0], [0.8, 0.6]], lo=0.5)
        arts = [(a["article"], b["article"]) for _s, a, b in out]
        self.assertNotIn(("Same", "Same"), arts)
        self.assertIn(("R", "O"), arts)

    def test_strip_front_matter_and_footer(self):
        t = '---\ntitle: "x"\n---\n\nBody <div class="nova-grounding">g</div>'
        self.assertEqual(nc.strip_front_matter(t), "Body")

    def test_article_url(self):
        self.assertEqual(nc.article_url(Path("/c/opinions/2026-10-01-a-b.md")), "https://nova.digitalnoise.net/opinions/2026-10-01-a-b/")

    def test_render_record_links_both(self):
        it = {"topic": "t", "earlier_quote": "e", "later_quote": "l", "which_stands": "later",
              "earlier": {"title": "E", "url": "https://n/e/", "date": "2026-10-01"},
              "later": {"title": "L", "url": "https://n/l/", "date": "2026-10-15"}}
        out = nc.render_record([it])
        self.assertIn("[E](https://n/e/)", out)
        self.assertIn("[L](https://n/l/)", out)


class TestIntegration(unittest.TestCase):
    def test_uses_ledger_and_voice(self):
        self.assertIn("superseded_by", SRC)
        self.assertIn("nova_voice.system_prompt(ctx, section=SECTION, topic=", SRC)
        self.assertEqual(nc.SECTION, "nova")

    def test_ground_orders_by_article_date(self):
        idx = {}
        e = _art("Earlier", "https://n/e/", "2026-10-01", EARLIER, "m1")
        l_ = _art("Later", "https://n/l/", "2026-10-15", LATER, "m2")
        with patch.object(nc, "resolve", side_effect=lambda i, a: {"Later": l_, "Earlier": e}[a]):
            g = nc.ground({"article": "Earlier", "stance": "s1"}, {"article": "Later", "stance": "s2"}, idx, fake_llm)
        self.assertEqual(g["earlier"]["url"], "https://n/e/")
        self.assertEqual(g["later"]["url"], "https://n/l/")

    def test_article_index_from_files(self):
        root = Path(tempfile.mkdtemp())
        (root / "content/nova").mkdir(parents=True)
        (root / "content/nova/2026-10-01-hello-world.md").write_text('---\ntitle: "💬 Hello World"\n---\n\nBody')
        with patch.object(nc, "_pg", side_effect=RuntimeError("offline")):
            idx = nc.article_index(root)
        self.assertEqual(idx["hello world"]["url"], "https://nova.digitalnoise.net/nova/2026-10-01-hello-world/")
        self.assertEqual(idx["hello world"]["date"], "2026-10-01")


class TestFunctional(unittest.TestCase):
    def _patches(self, published):
        e = _art("Earlier", "https://n/e/", "2026-10-01", EARLIER, "m1")
        l_ = _art("Later", "https://n/l/", "2026-10-15", LATER, "m2")
        nj = MagicMock()
        nj.publish_hugo.side_effect = lambda *a, **k: published.append((a, k)) or True
        nj.git_push.return_value = "pushed"
        voice = MagicMock()
        voice.system_prompt = lambda ctx, section="", topic="": ctx
        return [patch.object(nc, "candidates", return_value=[(0.9, {"article": "Later", "stance": "s2"},
                                                               {"article": "Earlier", "stance": "s1"})]),
                patch.object(nc, "article_index", return_value={}),
                patch.object(nc, "article_pairs", return_value=[]),
                patch.object(nc, "resolve", side_effect=lambda i, a: {"Later": l_, "Earlier": e}[a]),
                patch.object(nc, "already", return_value=set()),
                patch.object(nc, "record"),
                patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": voice})]

    def test_golden_publish(self):
        published = []
        ps = self._patches(published)
        for p in ps:
            p.start()
        try:
            res = nc.run(llm=fake_llm)
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertEqual(res["status"], "published")
        (title, body, section, *_), kw = published[0]
        self.assertTrue(title.startswith("Nova's Corrections"))
        self.assertEqual(section, "nova")
        self.assertIn("## The record", body)
        self.assertEqual(kw["cited_memory_ids"], ["m1", "m2"])

    def test_no_genuine_correction_skips(self):
        published = []
        ps = self._patches(published)
        for p in ps:
            p.start()
        try:
            res = nc.run(llm=lambda s, u: "[]" if "judge whether" in s else "{}")
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(published, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
