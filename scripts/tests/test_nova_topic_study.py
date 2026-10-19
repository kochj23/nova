#!/usr/bin/env python3
"""Tests for nova_topic_study.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_topic_study.py"
SRC = SCRIPT.read_text()
os.environ.setdefault("NOVA_TEST_QUIET", "1")

import nova_topic_study as ts  # noqa: E402


def _row(i, ep, chunk, text, source="comedy"):
    return {"id": f"m{i}", "source": source, "text": text,
            "metadata": {"type": "tv_transcript", "show": "Cheers (1982)", "chunk": chunk,
                         "title": f"Cheers (1982) - S0{ep}E01 - Episode {ep}"}}


ROWS = [_row(e * 10 + c, e, c, f"Sam says the bigger the lie the bigger the fool in episode {e} chunk {c}. " * 3)
        for e in range(1, 6) for c in range(1, 5)]
ROWS.append({"id": "f1", "source": "frame_vision", "text": "[Cheers (1982) — frame @ 00:01:00] A man pours a beer.",
             "metadata": {"kind": "frame", "show": "Cheers (1982)", "video": "Cheers (1982) - S01E01 - Episode 1.ts",
                          "t_seconds": 60}})


def fake_llm_factory(passages_holder):
    def llm(system, user):
        if "JSON only" in system:
            ps = passages_holder["p"]
            return json.dumps([{"lesson": f"Lesson {i}", "moment": "Sam tells the truth.",
                                "quote": "the bigger the lie the bigger the fool", "passage": i}
                               for i in range(1, min(len(ps), 6) + 1)] +
                              [{"lesson": "Invented", "moment": "x", "quote": "never said this line at all", "passage": 1}])
        return "SUBTITLE: Lessons from the bar\n\n" + ("Body paragraph about honesty and the bar. " * 40)
    return llm


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        self.assertIsNone(re.search(r"(password|api_key|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))

    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_topic_sanitized(self):
        self.assertIsNone(ts.clean_topic("x'; DROP TABLE memories; --"))
        self.assertIsNone(ts.clean_topic("a"))
        self.assertEqual(ts.clean_topic("  Cheers  "), "Cheers")

    def test_private_and_own_outputs_dropped(self):
        rows = [{"id": "1", "source": "nova_articles", "text": "x", "metadata": {}},
                {"id": "2", "source": "quarantine:comedy", "text": "x", "metadata": {}},
                {"id": "3", "source": "comedy", "text": "x", "metadata": {}}]
        fake_cfg = MagicMock()
        fake_cfg.filter_private_memories = lambda r: r
        with patch.dict(sys.modules, {"nova_config": fake_cfg}):
            self.assertEqual([r["id"] for r in ts._keep(rows)], ["3"])


class TestPerformance(unittest.TestCase):
    def test_sample_10k_rows_fast(self):
        rows = [_row(i, (i % 9) + 1, i % 7, "text " * 40) for i in range(10000)]
        for i, r in enumerate(rows):
            r["metadata"]["title"] = f"Show - S0{(i % 9) + 1}E{i % 50:02d} - Ep"
        t = time.time()
        p = ts.sample_series(rows)
        self.assertLess(time.time() - t, 3.0)
        self.assertLessEqual(len(p), ts.MAX_PASSAGES)


class TestRetry(unittest.TestCase):
    def test_remember_retries_then_succeeds(self):
        ok = MagicMock()
        ok.__enter__ = lambda s: s
        ok.__exit__ = lambda s, *a: False
        ok.read.return_value = b'{"id": "abc"}'
        with patch("urllib.request.urlopen", side_effect=[OSError("x"), OSError("y"), ok]) as u:
            self.assertEqual(ts.remember("t", {}), "abc")
        self.assertEqual(u.call_count, 3)

    def test_remember_fails_open(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(ts.remember("t", {}))


class TestUnit(unittest.TestCase):
    def test_episode_label_and_key(self):
        self.assertEqual(ts.episode_label("Cheers (1982) - S05E03 - Money Dearest.ts"), "S05E03 “Money Dearest”")
        self.assertLess(ts.episode_key("X - S01E02 - a"), ts.episode_key("X - S01E10 - b"))

    def test_spread(self):
        self.assertEqual(ts.spread(list(range(10)), 5), [0, 2, 4, 6, 8])
        self.assertEqual(ts.spread([1, 2], 5), [1, 2])

    def test_quote_check(self):
        self.assertTrue(ts.quote_in("The bigger the LIE, the bigger", "oh the bigger the lie the bigger the fool"))
        self.assertFalse(ts.quote_in("too short", "too short"))

    def test_verify_drops_unsupported(self):
        p = [{"id": "a", "label": "S01E01", "text": "Sam said I was not a great drunk I was a good drunk."}]
        raw = [{"lesson": "L", "quote": "I was not a great drunk", "passage": 1},
               {"lesson": "Bad", "quote": "something invented entirely here", "passage": 1},
               {"lesson": "OOR", "quote": "I was not a great drunk", "passage": 9}, "junk"]
        out = ts.verify_lessons(raw, p)
        self.assertEqual([o["lesson"] for o in out], ["L"])
        self.assertEqual(out[0]["memory_id"], "a")

    def test_parse_json_fenced(self):
        self.assertEqual(ts.parse_json('x ```json\n[{"a":1}]\n``` y'), [{"a": 1}])
        self.assertIsNone(ts.parse_json("no json"))

    def test_article_url_keeps_date_prefix(self):
        self.assertEqual(ts.article_url("nova", "What Nova Learned From Cheers", "2026-10-19"),
                         "https://nova.digitalnoise.net/nova/2026-10-19-what-nova-learned-from-cheers/")
        # old section names go through the one remap (Research + Opinions -> Nova and Friends, 2026-10-19)
        self.assertEqual(ts.article_url("research", "What Nova Learned From Cheers", "2026-10-19"),
                         "https://nova.digitalnoise.net/nova/2026-10-19-what-nova-learned-from-cheers/")

    def test_display_name(self):
        self.assertEqual(ts.display_name("cheers", "Cheers (1982)"), "Cheers")


class TestIntegration(unittest.TestCase):
    def test_memory_shape_matches_work_lessons(self):
        self.assertIn('"source": "work_lessons"', SRC)
        self.assertIn('"type": "work_lessons"', SRC)
        self.assertIn('"privacy": "local-only"', SRC)

    def test_uses_voice_and_publish_path(self):
        self.assertIn("nova_voice.system_prompt(ctx, section=section, topic=", SRC)
        self.assertIn("cited_memory_ids=cited", SRC)

    def test_sources_block_is_grounding_parseable(self):
        import nova_grounding
        p = [{"id": "a", "source": "comedy", "label": "S01E01", "text": "t"}]
        self.assertEqual(len(nova_grounding.parse_source_items(ts.sources_block(p, "Cheers"))), 1)

    def test_sample_series_chain(self):
        p = ts.sample_series(ROWS)
        self.assertTrue(any(x["label"].endswith("(on screen)") for x in p))
        self.assertEqual(p[0]["label"], "S01E01 “Episode 1”")


class TestFunctional(unittest.TestCase):
    def setUp(self):
        self.prev = Path(tempfile.mkdtemp()) / "p.md"
        os.environ["NOVA_STUDY_PREVIEW"] = str(self.prev)

    def test_golden_dry_run(self):
        holder = {}
        passages = ts.sample_series(ROWS)
        holder["p"] = passages
        fake_voice = MagicMock()
        fake_voice.system_prompt = lambda ctx, section="", topic="": "SYS " + ctx
        with patch.object(ts, "gather", return_value=("Cheers (1982)", passages, 1810)), \
             patch.dict(sys.modules, {"nova_voice": fake_voice}):
            res = ts.study("Cheers", dry_run=True, llm=fake_llm_factory(holder))
        self.assertEqual(res["status"], "dry-run")
        self.assertTrue(res["title"].startswith("What Nova Learned From Cheers: Lessons from the bar"))
        expect = sum(1 for x in passages[:6] if "bigger the lie" in x["text"])   # the frame passage has no quote
        self.assertEqual(len(res["lessons"]), expect)
        self.assertNotIn("Invented", [l["lesson"] for l in res["lessons"]])
        self.assertIn("## Episodes cited", self.prev.read_text())

    def test_thin_topic_skips(self):
        with patch.object(ts, "gather", return_value=(None, [], 0)):
            res = ts.study("Nothing", llm=lambda s, u: self.fail("no LLM call on a thin topic"))
        self.assertEqual(res["status"], "skipped")

    def test_pending_marks_done(self):
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = lambda s, *a: False
        cur.fetchone.side_effect = [(7, "topic study: Frasier"), None]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(ts, "_ops_pg", return_value=conn), \
             patch.object(ts, "study", return_value={"status": "published", "url": "https://x/y/"}):
            self.assertEqual(ts.pending(limit=2), 1)
        last = cur.execute.call_args_list[-2][0]
        self.assertIn("UPDATE claude_queue SET status=%s", last[0])
        self.assertEqual(last[1][:2], ("done", "https://x/y/"))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
