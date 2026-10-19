#!/usr/bin/env python3
"""7-category tests for nova_channel_pools — the news / business / LA-local YouTube pools that feed
Nova and Friends opinion writing (nova_journal "opinion" profile, nova_politics_column) and the daily
Burbank dispatch (nova_local_burbank), plus the INFOWARS RULE: a claims-to-fact-check source only —
never a topic, never grounding, always labelled as an unverified claim (Jordan 2026-10-19).
PG, LLM calls and publishing are mocked. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_channel_pools_7cat.py
"""
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_channel_pools as ncp  # noqa: E402

D = datetime(2026, 10, 19, 9, 0)


def row(text, chan, title, vid=None, **md):
    return (text, D, {"channel": chan, "title": title, "pipeline": "yt_subs_audio",
                      **({"video_id": vid} if vid else {}), **md})


class Cur:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


ROWS = [row("The senate passed the budget bill after a long night of votes in Washington.", "The Bulwark",
            "Senate passes the budget bill overnight", "b1"),
        row("Shipping rates jumped as the canal backlog grew this week.", "What's Going on With Shipping?",
            "Canal backlog sends shipping rates soaring", "s1"),
        row("They are hiding the truth about the budget bill, folks, globalists planned it.", "InfoWars",
            "The budget bill is a globalist plot", "i1")]


class TestSecurity(unittest.TestCase):
    def test_infowars_is_never_a_topic(self):
        items = ncp.fetch_pool(Cur(ROWS), ncp.NOVA_NEWS_CHANNELS)
        topics = ncp.topic_candidates(items)
        self.assertIn("Senate passes the budget bill overnight", topics)
        self.assertFalse(any("globalist" in t for t in topics))
        self.assertEqual([i["channel"] for i in ncp.facts(items)], ["The Bulwark", "What's Going on With Shipping?"])
        self.assertEqual([i["channel"] for i in ncp.claims(items)], ["InfoWars"])

    def test_infowars_lines_always_labelled(self):
        items = ncp.fetch_pool(Cur(ROWS), ncp.NOVA_NEWS_CHANNELS)
        line = ncp.as_line(ncp.claims(items)[0])
        self.assertTrue(line.startswith(ncp.UNVERIFIED_LABEL))
        self.assertFalse(ncp.as_line(ncp.facts(items)[0]).startswith("UNVERIFIED"))
        # even a dict that forgot the flag is labelled by channel name
        self.assertTrue(ncp.as_line({"channel": "infowars", "title": "x", "text": "y"}).startswith("UNVERIFIED"))

    def test_draft_mentions_of_infowars_get_labelled(self):
        draft = "Per InfoWars, the bill is a plot. Meanwhile The Bulwark reported the vote."
        out = ncp.label_claims(draft)
        self.assertTrue(ncp.claims_labelled(out))
        self.assertIn("InfoWars (an unverified claim source)", out)
        ok = "InfoWars made an unverified claim that the bill is a plot."
        self.assertEqual(ncp.label_claims(ok), ok)
        self.assertEqual(ncp.label_claims("no mention here"), "no mention here")
        self.assertFalse(ncp.claims_labelled("Info Wars says it is true."))

    def test_private_and_public_journal_markers_dropped(self):
        rows = [row("server at 10.0.0.5 is down", "CNN", "x1", "c1"),
                row("see /Users/someone/file", "CNN", "x2", "c2"),
                row("a normal story about the senate and the bill", "CNN", "Senate story here", "c3")]
        items = ncp.fetch_pool(Cur(rows), ncp.NOVA_NEWS_CHANNELS)
        self.assertEqual([i["title"] for i in items], ["Senate story here"])

    def test_query_parameterised_and_privacy_filtered(self):
        cur = Cur([])
        ncp.fetch_pool(cur, ncp.LA_LOCAL_CHANNELS, days="2")
        sql, params = cur.calls[0]
        self.assertIn("NOT IN ('private', 'local-only')", sql)
        self.assertEqual(params[0], 2)
        self.assertEqual(set(params[1]), {ncp.norm(c) for c in ncp.LA_LOCAL_CHANNELS})
        with self.assertRaises(ValueError):
            ncp.fetch_pool(Cur([]), ncp.LA_LOCAL_CHANNELS, days="2; drop table memories")


class TestPerformance(unittest.TestCase):
    def test_big_pool_fast_and_bounded(self):
        rows = [row("word " * 400, "CNN", f"Story number {i % 50}", f"v{i % 50}") for i in range(20000)]
        t0 = time.time()
        items = ncp.fetch_pool(Cur(rows), ncp.NOVA_NEWS_CHANNELS, per_video=500, max_videos=30)
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(len(items), 30)
        self.assertTrue(all(len(i["text"]) <= 500 for i in items))
        t0 = time.time()
        for _ in range(2000):
            ncp.label_claims("InfoWars " * 20 + "text " * 200)
        self.assertLess(time.time() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_opinion_pool_failure_never_sinks_the_piece(self):
        import nova_journal as nj
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")):
            self.assertEqual(nj._opinion_pool(), [])
        self.assertEqual(nj._pool_grounding(None, "x"), [])

    def test_burbank_pool_failure_fails_open(self):
        import nova_local_burbank as lb
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")):
            self.assertEqual(lb.get_la_channel_news(), [])

    def test_politics_pool_failure_keeps_the_shelves(self):
        import nova_politics_column as pc
        with patch.object(pc, "_pool_items", side_effect=RuntimeError("boom")):
            conn = MagicMock()
            ctx = MagicMock()
            ctx.__enter__.return_value.fetchall.return_value = []
            conn.cursor.return_value = ctx
            with patch.object(pc, "_mem_conn", return_value=conn):
                self.assertEqual(pc.gather_brief(), [])


class TestUnit(unittest.TestCase):
    def test_norm_matches_nova_friends(self):
        import nova_friends as nf
        for n in ("What's Going on With Shipping?", "Arvin Haddad ", "LIVING IN LOS ANGELES AND THE SOUTH BAY",
                  "Lovett or Leave It"):
            self.assertEqual(ncp.norm(n), nf.norm(n))
        self.assertEqual(ncp.NORM_SQL, nf.NORM_SQL)

    def test_related_needs_two_shared_words_or_exact_title(self):
        items = ncp.fetch_pool(Cur(ROWS), ncp.NOVA_NEWS_CHANNELS)
        self.assertEqual([i["channel"] for i in ncp.related(items, "Senate passes the budget bill overnight")],
                         ["The Bulwark", "InfoWars"])
        self.assertEqual(ncp.related(items, "Canal"), [])

    def test_videos_grouped_and_linked(self):
        rows = [row("part one", "CNN", "Big story", "v9"), row("part two", "CNN", "Big story", "v9")]
        items = ncp.fetch_pool(Cur(rows), ncp.NOVA_NEWS_CHANNELS)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://www.youtube.com/watch?v=v9")
        self.assertEqual(items[0]["text"], "part one part two")


class TestIntegration(unittest.TestCase):
    def test_opinion_topic_mixes_pool_and_grounds_with_labels(self):
        import nova_journal as nj
        pool = ncp.fetch_pool(Cur(ROWS), ncp.NOVA_NEWS_CHANNELS + ncp.NOVA_BUSINESS_CHANNELS)
        with patch.object(nj, "_fetch_google_news", return_value=[]), \
                patch.object(nj, "_opinion_pool", return_value=pool), \
                patch.object(nj, "get_recent", return_value=[]), \
                patch.object(nj, "recall_memories", return_value=[{"text": "old memory", "source": "x"}]), \
                patch.object(nj.random, "choice", side_effect=lambda c: c[0]):
            topic, mems = nj.topic_opinion({})
        self.assertEqual(topic, "Senate passes the budget bill overnight")
        self.assertEqual([m["source"] for m in mems], ["yt_channel_pool", "claims_only", "x"])
        self.assertTrue(mems[1]["text"].startswith(ncp.UNVERIFIED_LABEL))

    def test_opinion_prompt_carries_claims_rule_and_output_is_labelled(self):
        import nova_journal as nj
        seen = []

        def llm(system, user, **k):
            seen.append(system + user)
            return "# A Title\n\nInfoWars says the bill is a plot. " + "words " * 200
        mems = [{"text": ncp.as_line({"channel": "InfoWars", "title": "t", "text": "x"}), "source": "claims_only"}]
        with patch.object(nj, "call_openrouter", side_effect=llm), \
                patch.object(nj, "system_prompt", side_effect=lambda c, **k: c), \
                patch.object(nj, "_get_weekly_theme", return_value=""), \
                patch.object(nj, "_with_self_inventory", side_effect=lambda s, t: s):
            title, body = nj.generate_opinion("the bill", mems)
        self.assertIn(ncp.CLAIMS_RULE, seen[0])
        self.assertIn(ncp.UNVERIFIED_LABEL, seen[0])
        self.assertTrue(ncp.claims_labelled(body))

    def test_journal_never_grounds_expansion_on_claims_and_labels_the_final_body(self):
        src = (SCRIPTS / "nova_journal.py").read_text()
        run = src[src.index("def run_profile"):]
        drop = run.index('m.get("source") == "claims_only"')
        self.assertLess(drop, run.index("_append_attribution(body, memories"))
        self.assertLess(drop, run.index("sources=sources_from_memories(memories, topic)"))
        pub = src[src.index("def publish_hugo"):src.index("def publish_hugo") + 9000]
        self.assertLess(pub.index("longform_expand(title, body"), pub.index('"nova_channel_pools").label_claims(body)'))

    def test_politics_claims_labelled_never_grounding_never_counted(self):
        import nova_politics_column as pc
        items = [{"outlet": "The Bulwark", "headline": "senate vote"}, {"outlet": "CNN", "headline": "court ruling"},
                 {"outlet": "InfoWars", "headline": "a plot", "claim": True}]
        block = pc.build_source_block(items)
        self.assertIn(f"- {ncp.UNVERIFIED_LABEL}: [InfoWars] a plot", block)
        self.assertEqual(len(pc.fact_items(items)), 2)
        self.assertFalse(pc.verify_grounded("InfoWars and nothing else, CNN", items))
        self.assertTrue(pc.verify_grounded("The Bulwark and CNN", items))

    def test_politics_thin_week_counts_facts_only(self):
        import nova_politics_column as pc
        items = [{"outlet": "InfoWars", "headline": f"claim {i}", "claim": True} for i in range(20)]
        with patch.object(pc, "gather_brief", return_value=items), patch.object(pc, "_log_action"), \
                patch.object(pc, "generate_article") as gen:
            self.assertEqual(pc.main([]), 0)
        gen.assert_not_called()

    def test_politics_pool_rows_parsed_capped_and_claims_flagged(self):
        import nova_politics_column as pc
        rows = [(f"[Pod Save America] the senate vote on bill number {i} was close tonight",) for i in range(10)] + \
               [("[InfoWars] the senate is secretly run by globalists, a congress plot",)]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        got = pc._pool_items(conn, 7, 12, set())
        self.assertEqual(sum(g["outlet"] == "Pod Save America" for g in got), pc.POOL_PER_CHANNEL)
        self.assertEqual([g for g in got if g.get("claim")][0]["outlet"], "InfoWars")
        sql, params = cur.execute.call_args.args
        self.assertEqual(params[0], "yt_subs_audio")
        self.assertIn("InfoWars".lower(), params[1])

    def test_burbank_items_flow_into_the_california_filtered_feed(self):
        import nova_local_burbank as lb
        conn = MagicMock()
        conn.cursor.return_value = Cur([row("A fire in Los Angeles closed the 5 freeway.", "KTLA 5", "LA fire", "k1")])
        with patch("psycopg2.connect", return_value=conn):
            items = lb.get_la_channel_news()
        self.assertEqual(items[0]["source"], "KTLA 5 (YouTube)")
        self.assertIn("Los Angeles", items[0]["text"])
        self.assertEqual(items[0]["metadata"], {"url": "https://www.youtube.com/watch?v=k1"})
        self.assertIn("get_la_channel_news()", (SCRIPTS / "nova_local_burbank.py").read_text())


class TestFunctional(unittest.TestCase):
    def test_pools_are_jordans_lists(self):
        self.assertEqual(len(ncp.NOVA_NEWS_CHANNELS), 18)
        self.assertEqual(len(ncp.NOVA_BUSINESS_CHANNELS), 10)
        self.assertEqual(len(ncp.LA_LOCAL_CHANNELS), 10)
        self.assertIn("InfoWars", ncp.NOVA_NEWS_CHANNELS)
        self.assertEqual(ncp.CLAIMS_ONLY_CHANNELS, ("InfoWars",))

    def test_pools_disjoint_from_each_other(self):
        seen = {}
        for k, chans in ncp.POOLS.items():
            for c in chans:
                self.assertNotIn(ncp.norm(c), seen, (c, k, seen.get(ncp.norm(c))))
                seen[ncp.norm(c)] = k


class TestFrame(unittest.TestCase):
    def test_stdlib_only_no_io(self):
        src = (SCRIPTS / "nova_channel_pools.py").read_text()
        for bad in ("import psycopg2", "subprocess", "open(", "eval(", "exec(", "requests"):
            self.assertNotIn(bad, src)

    def test_exports(self):
        for n in ("fetch_pool", "facts", "claims", "topic_candidates", "related", "as_line", "label_claims",
                  "claims_labelled", "is_claims_only", "UNVERIFIED_LABEL", "CLAIMS_RULE", "POOLS"):
            self.assertTrue(hasattr(ncp, n), n)


if __name__ == "__main__":
    unittest.main()
