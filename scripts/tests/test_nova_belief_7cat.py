#!/usr/bin/env python3
"""Belief and Friends (2026-10-19) — the PG source list (belief_sources / belief_families), theme days
(belief_themes), the family round-robin, the topic filter and nova_yt_direct.py's subscription-free channels,
in the 7 house categories (Security, Performance, Retry, Unit, Integration, Functional, Frame). PG, YouTube and
the writer are always fakes. Written by Jordan Koch (via Claude)."""
import re
import subprocess
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_friends as m  # noqa: E402
import nova_sections as s  # noqa: E402
import nova_yt_direct as yd  # noqa: E402

FAMILIES = [("Abrahamic", ("bible", "quran")), ("Dharmic", ("hinduism", "buddhism")), ("East Asian", ("taoism",)),
            ("Non-theist and ancient", ("nontheism",)), ("New religious movements and sects", ())]
ROWS = [  # name, tradition, family, role, fetch_mode, owned_by_section, topic_only, nrm
    ("ReligionForBreakfast", "Religious studies", "Non-theist and ancient", "scholar", "direct", None, False, False),
    ("Yaqeen Institute", "Islam", "Abrahamic", "insider", "direct", None, False, False),
    ("Chabad.org", "Judaism", "Abrahamic", "insider", "direct", None, False, False),
    ("Doug's Dharma", "Buddhism", "Dharmic", "scholar", "direct", None, False, False),
    ("Benebell Wen", "Taoism", "East Asian", "insider", "direct", None, False, False),
    ("ESOTERICA", "Religious history", "Non-theist and ancient", "scholar", "subscription", "mystery", True, False),
    ("Mormon Stories Podcast", "Latter-day Saints", "New religious movements and sects", "critic", "direct", None,
     False, True),
]
THEMES = [("afterlife", "What happens when we die?", ["afterlife", "heaven", "rebirth"], None, 0),
          ("suffering", "Why is there suffering?", ["suffering", "evil", "dukkha"], None, 0),
          ("fasting", "Why fast?", ["fasting", "ramadan", "lent"], date(2026, 10, 15), 1)]


class Cur:
    """Answers by the first matching SQL fragment; records every statement."""
    def __init__(self, answers, fail=False):
        self.answers, self.fail, self.sql, self._rows = answers, fail, [], []

    def execute(self, sql, params=None):
        if self.fail:
            raise ConnectionError("pg down")
        self.sql.append((sql, params))
        self._rows = next((list(v) for k, v in self.answers if k in sql), [])

    def fetchall(self):
        return self._rows


def belief_cur(fail=False):
    return Cur([("FROM belief_families", [(n, i, list(src)) for i, (n, src) in enumerate(FAMILIES)]),
                ("FROM belief_sources", ROWS)], fail=fail)


def fresh_sources(cur):
    m._BELIEF = None
    with patch.object(m, "log"), patch.object(m.nj, "log"):
        return m.belief_sources(cur)


def vid(channel, i=0):
    return {"channel": channel, "url": f"https://www.youtube.com/watch?v={channel[:4]}{i:07d}", "title": f"t{i}"}


class TestSecurity(unittest.TestCase):
    def test_theme_keywords_are_escaped_in_the_pg_regex(self):
        rx = m.theme_regex(["c++ (rite)", "a|b", "end.times"])
        self.assertIn(re.escape("c++ (rite)"), rx)
        self.assertNotIn("|b)", rx.replace(re.escape("a|b"), ""))   # the literal '|' can't open an alternative
        self.assertTrue(rx.startswith(r"\m(") and rx.endswith(r")\M"))

    def test_text_in_context_fails_closed(self):
        self.assertEqual(m.verse_ok("No quote at all, just prose.", []), (False, "no quoted passage"))
        ok, _ = m.verse_ok("> In the beginning was a quote nobody can verify.\n— Made-up 1:1", [])
        self.assertFalse(ok)

    def test_tone_keeps_the_historian_stance(self):
        tone = m.BELIEF_TONE.lower()
        for must in ("historian", "anthropologist", "not a believer", "not a debunker"):
            self.assertIn(must, tone)

    def test_direct_list_query_reads_only_active_direct_rows(self):
        cur = Cur([("FROM belief_sources", [("UC" + "x" * 22, "@x", "X")])])
        yd.direct_channels(cur)
        sql = cur.sql[0][0]
        self.assertIn("fetch_mode = 'direct'", sql)
        self.assertIn("AND active", sql)
        self.assertIsNone(cur.sql[0][1])              # no user input reaches this query


class TestPerformance(unittest.TestCase):
    def test_theme_day_replay_three_years_out_is_fast(self):
        t = time.perf_counter()
        m.is_theme_day((date(2029, 10, 19)).isoformat())
        self.assertLess(time.perf_counter() - t, 0.5)

    def test_round_robin_10k_videos_fast(self):
        fam = {m.norm(r[0]): r[2] for r in ROWS}
        vids = [vid(ROWS[i % len(ROWS)][0], i) for i in range(10000)]
        t = time.perf_counter()
        out = m.round_robin(vids, fam)
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertEqual(len(out), 10000)


class TestRetry(unittest.TestCase):
    def test_pg_down_falls_back_loudly_never_empty(self):
        logs = []
        m._BELIEF = None
        with patch.object(m, "log", side_effect=logs.append), patch.object(m.nj, "log") as alert:
            b = m.belief_sources(belief_cur(fail=True))
        self.assertEqual(set(b["channels"]), set(m.BELIEF_FALLBACK))
        self.assertTrue(any(x.startswith("ERROR") for x in logs))
        self.assertIn("ALERT", alert.call_args[0][0])

    def test_sources_cached_for_the_run(self):
        cur = belief_cur()
        fresh_sources(cur)
        n = len(cur.sql)
        m.belief_sources(cur)
        self.assertEqual(len(cur.sql), n)            # second call: no new queries

    def test_direct_list_unreadable_still_ingests_subscriptions(self):
        logs = []
        self.assertEqual(yd.as_subscriptions([("UCa",)], cur=Cur([], fail=True), log=logs.append), [])
        self.assertTrue(logs and logs[0].startswith("ERROR"))

    def test_recently_cited_unreadable_repo_is_empty_not_fatal(self):
        with patch.object(m.nj, "HUGO_ROOT", "/nonexistent/repo"):
            self.assertEqual(m.recently_cited("belief"), set())


class TestUnit(unittest.TestCase):
    def test_theme_days_never_three_in_a_row_and_about_half(self):
        days = [m.is_theme_day((date(2026, 10, 1) + timedelta(d)).isoformat()) for d in range(365)]
        self.assertFalse(any(days[i] == days[i + 1] == days[i + 2] for i in range(len(days) - 2)))
        self.assertTrue(0.4 <= sum(days) / len(days) <= 0.6)
        self.assertEqual(m.is_theme_day("2026-11-03"), m.is_theme_day("2026-11-03"))   # deterministic

    def test_round_robin_deals_one_family_at_a_time(self):
        fam = {m.norm(r[0]): r[2] for r in ROWS}
        vids = [vid("Mormon Stories Podcast", i) for i in range(5)] + [vid("Doug's Dharma"), vid("Benebell Wen")]
        first3 = {fam[m.norm(v["channel"])] for v in m.round_robin(vids, fam)[:3]}
        self.assertEqual(len(first3), 3)

    def test_focus_family_rotates_daily(self):
        fresh_sources(belief_cur())
        names = {m.focus_family(f"2026-10-{d:02d}")[0] for d in range(1, 6)}
        self.assertEqual(names, {f[0] for f in FAMILIES})

    def test_topic_filter_needs_real_religious_context(self):
        self.assertTrue(s.is_belief_topic("How the Quran and the hadith shaped early Sunni and Shia law"))
        for noise in ("Best cocktail shaker for home bars", "Orthodox economics explained",
                      "Friends reunion: the one with the witness"):
            self.assertFalse(s.is_belief_topic(noise), noise)


class TestIntegration(unittest.TestCase):
    def test_table_rows_become_section_config(self):
        b = fresh_sources(belief_cur())
        self.assertIn("ESOTERICA", b["shared"])                      # owned by Mystery: topic videos only
        self.assertNotIn("ESOTERICA", b["channels"])
        self.assertEqual(b["capped"][0], ("Mormon Stories Podcast",))
        cfg = m.apply_belief_sources({})
        self.assertEqual(cfg["family_of"][m.norm("Yaqeen Institute")], "Abrahamic")

    def test_direct_channels_merge_with_subscriptions_without_duplicates(self):
        cur = Cur([("FROM belief_sources", [("UCa", "@a", "A"), ("UCb", "@b", "B")])])
        self.assertEqual(yd.as_subscriptions([("UCa", "@a", "A")], cur=cur, log=lambda x: None), [("UCb", "@b", "B")])


class TestFunctional(unittest.TestCase):
    def setUp(self):
        fresh_sources(belief_cur())
        self.cfg = m.apply_belief_sources({})
        self.oc = Cur([("FROM belief_themes", THEMES)])

    def test_theme_needs_three_traditions_and_prefers_four_families(self):
        rich = [vid(r[0]) for r in ROWS[1:5]] + [vid("ReligionForBreakfast")]
        with patch.object(m, "theme_material", return_value=rich):
            t, vids, trads, fams = m.choose_theme(self.oc, None, self.cfg, "2026-10-20")
        self.assertIsNotNone(t)
        self.assertGreaterEqual(len(fams), m.THEME_MIN_FAMILIES)
        self.assertNotIn("Religious studies", trads)                 # generic buckets don't count as traditions

    def test_too_little_material_falls_back_to_roundup(self):
        thin = [vid("Chabad.org"), vid("ReligionForBreakfast")]
        with patch.object(m, "theme_material", return_value=thin):
            self.assertIsNone(m.choose_theme(self.oc, None, self.cfg, "2026-10-20")[0])

    def test_recently_used_theme_not_picked_inside_repeat_window(self):
        picks = {m.pick_theme(self.oc, f"2026-10-{d:02d}")["theme"] for d in range(16, 31)}
        self.assertNotIn("fasting", picks)


class TestFrame(unittest.TestCase):
    def test_friends_help_exits_zero(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_friends.py"), "--help"], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertIn("belief", p.stdout)

    def test_yt_direct_import_does_not_touch_pg(self):
        with patch("psycopg2.connect", side_effect=AssertionError("connected at import")):
            import importlib
            importlib.reload(yd)


_SAVED = {}


def setUpModule():
    _SAVED["belief"] = m._BELIEF


def tearDownModule():
    m._BELIEF = _SAVED.get("belief")   # restore whatever another suite in the same run had cached


if __name__ == "__main__":
    unittest.main()
