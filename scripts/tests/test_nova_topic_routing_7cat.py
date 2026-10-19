#!/usr/bin/env python3
"""7-category tests for nova_sections.topic_section / route_topic — clearly-music and clearly-military
pieces from Nova's own generators (essay / research / opinion profiles, nova_topic_study,
nova_research_paper) publish to Music and Friends / Military and Friends (Jordan 2026-10-19).
Written by Jordan Koch (via Claude).

Run: python3 -m pytest -q tests/test_nova_topic_routing_7cat.py
"""
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_sections as ns  # noqa: E402


def src(name):
    return (SCRIPTS / name).read_text()


class TestUnit(unittest.TestCase):
    def test_clear_music(self):
        self.assertEqual(ns.topic_section("Wrong Wave, Right Corpse: No Wave and the death of New Wave"), "music")
        self.assertEqual(ns.topic_section("The Paradox of Influence Without Coherence", ["newwave"] * 6), "music")
        self.assertEqual(ns.topic_section("Hardcore punk and the DIY record label", []), "music")

    def test_clear_military(self):
        self.assertEqual(ns.topic_section("The Battle of Tangier and the Portuguese army"), "military")
        self.assertEqual(ns.topic_section("Why Ukraine's drone warfare rewrote doctrine"), "military")
        self.assertEqual(ns.topic_section("Logistics lessons", ["military_doctrine_navy"] * 5), "military")

    def test_conservative_one_keyword_or_figurative_never_moves(self):
        for t in ("A band of brothers", "The chip wars", "Price wars and margins", "Culture war on campus",
                  "Entropy and Gardens", "The war on drugs failed", "Star Wars and myth"):
            self.assertIsNone(ns.topic_section(t), t)

    def test_conditional_sources_need_a_war_topic(self):
        self.assertIsNone(ns.topic_section("Trade routes of the Hanseatic League", ["world_history"] * 5))
        self.assertEqual(ns.topic_section("The siege that ended a war", ["world_history"] * 5), "military")
        self.assertIsNone(ns.topic_section("Phishing kits", ["intelligence"] * 5))

    def test_overlap_picks_the_primary_subject(self):
        self.assertEqual(ns.topic_section("Wartime songs: the music of WWII", ["music"] * 4), "music")
        self.assertEqual(ns.topic_section("Military bands at the Battle of the Somme", ["military_history"] * 4),
                         "military")


class TestIntegration(unittest.TestCase):
    def test_route_only_touches_nova_and_friends(self):
        t = "No Wave, punk and the downtown band scene"
        for sec in ("nova", "research", "opinions", "essays", "synthesis"):
            self.assertEqual(ns.route_topic(sec, t), "music", sec)
        for sec in ("tech", "burbank-ops", "watches", "security"):
            self.assertEqual(ns.route_topic(sec, t), sec, sec)
        self.assertEqual(ns.route_topic("research", "Entropy and Gardens"), "research")

    def test_generators_call_the_shared_router(self):
        self.assertIn("route_topic(section", src("nova_journal.py"))
        self.assertIn('profile_name in ("essay", "opinion", "research")', src("nova_journal.py"))
        self.assertIn("route_topic(section", src("nova_topic_study.py"))
        rp = src("nova_research_paper.py")
        self.assertIn('route_topic("nova"', rp)
        self.assertIn("else section, title)", rp)
        self.assertIn('categories: ["{section}"]', rp)


class TestSecurity(unittest.TestCase):
    def test_no_eval_or_io(self):
        body = src("nova_sections.py")
        for bad in ("eval(", "exec(", "subprocess", "open("):
            self.assertNotIn(bad, body)

    def test_garbage_inputs_are_safe(self):
        for t, s in ((None, None), ("", ()), ("x" * 100000, ["music", None, 3])):
            ns.topic_scores(t, s or ())
            ns.topic_section(t, s or ())


class TestPerformance(unittest.TestCase):
    def test_fast(self):
        t0 = time.time()
        for _ in range(2000):
            ns.topic_section("The Battle of the Bands: a punk album at war with its label", ["music"] * 30)
        self.assertLess(time.time() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_router_failure_never_sinks_a_generator(self):
        for name in ("nova_journal.py", "nova_topic_study.py", "nova_research_paper.py"):
            self.assertIn("topic routing unavailable", src(name), name)


class TestFunctional(unittest.TestCase):
    def test_scores_shape(self):
        s = ns.topic_scores("jazz and the army", ["jazz", "jazz", "jazz"])
        self.assertEqual(set(s), {"music", "military"})
        self.assertGreater(s["music"], s["military"])


class TestFrame(unittest.TestCase):
    def test_exports_and_live_targets(self):
        for n in ("topic_section", "route_topic", "topic_scores", "MUSIC_SOURCES", "MILITARY_SOURCES"):
            self.assertTrue(hasattr(ns, n), n)
        self.assertIn("music", ns.LIVE_SECTIONS)
        self.assertIn("military", ns.LIVE_SECTIONS)
        self.assertTrue({"nowave", "hardcore_punk", "edm", "idm", "jazz", "music"} <= ns.MUSIC_SOURCES)


if __name__ == "__main__":
    unittest.main()
