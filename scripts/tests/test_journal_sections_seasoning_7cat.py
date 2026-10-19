#!/usr/bin/env python3
"""7-category tests for the 2026-10-19 journal section consolidation + seasoning audit:
nova_sections (the one remap), dreams refusal (publish_hugo + nova_publish_journal), the section
maps (Big Brother staleness, stats poller, cross linker, tag seeds, article watchdog, monthly wrap,
weekly summary, article history), nova_lexicon live sections / epigraph_block / safe_seasoning,
nova_voice section inference, and every article generator being wired for sci-fi + faith seasoning
(emergencies stay unseasoned). PG is mocked. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_journal_sections_seasoning_7cat.py
"""
import ast
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_sections as ns  # noqa: E402
import nova_lexicon as lx  # noqa: E402

RETIRED = {"dreams", "operations", "local", "essays", "synthesis", "tech-today", "rando", "research", "opinions"}
LIVE = {"burbank-ops", "nova", "watches", "cars", "tech", "food", "digests",
        "security", "meta", "after-dark", "pilot", "start-here",
        "music", "military", "screen", "science", "mystery", "crime", "workshop", "travel"}


def _src(name):
    return (SCRIPTS / name).read_text()


def _season(section, topic="x"):
    with patch.object(lx, "ferengi_rule", return_value=(48, "The bigger the smile, the sharper the knife.")):
        return lx.seasoning(section, topic)


# Generators that build their OWN raw prompt: each must append nova_lexicon seasoning.
RAW_SEASONED = {
    "nova_after_dark.py": "after-dark", "nova_tv_pilot.py": "pilot", "nova_meta_analysis.py": "meta",
    "nova_postmortem.py": "burbank-ops", "nova_pg_postmortem.py": "burbank-ops",
    "nova_security_weekly.py": "security", "nova_weekly_ops_report.py": "burbank-ops",
    "nova_weekly_ops_wrap.py": "burbank-ops", "nova_politics_column.py": "opinions",
}
# Generators that call nova_voice.system_prompt and now pass section= explicitly.
SECTION_PASSED = {
    "nova_capabilities_inventory_article.py": "burbank-ops", "nova_fishbowl_daily.py": "watches",
    "nova_iot_scout.py": "tech", "nova_ledger_of_changed_minds.py": "burbank-ops",
    "nova_local_airwaves.py": "burbank-ops", "nova_new_organs_article.py": "burbank-ops",
    "nova_opinion_fishbowl.py": "watches", "nova_opinion_fishbowl_roster.py": "watches",
    "nova_unclaimed_digest.py": "burbank-ops", "nova_weekly_media_wrap.py": "burbank-ops",
    "nova_rando_top10_weird.py": "burbank-ops",
}


class TestSecurity(unittest.TestCase):
    def test_emergency_generators_stay_unseasoned(self):
        src = _src("nova_journal_emergency.py")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", "")) == "system_prompt"]
        self.assertTrue(calls)
        for c in calls:
            kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
            self.assertEqual(kw.get("flavor"), "False", "an evacuation notice must never be seasoned")

    def test_unknown_or_empty_section_never_seasoned(self):
        for bad in ("", "breaking", "wat", "dreams"):
            self.assertEqual(_season(bad, "brush fire evacuation"), "", bad)

    def test_research_epigraph_requires_citation(self):
        import nova_research_paper as rp
        self.assertEqual(rp.render_epigraph({"epigraph": {"text": "made up", "attribution": ""}}), "")
        self.assertEqual(rp.render_epigraph({"epigraph": "not a dict"}), "")
        self.assertEqual(rp.render_epigraph(None), "")


class TestPerformance(unittest.TestCase):
    def test_canon_fast(self):
        t = time.perf_counter()
        for _ in range(100000):
            ns.canon("operations")
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_big_brother_scan_bounded(self):
        self.assertIn("[:200]", _src("nova_big_brother.py"))   # burbank-ops holds 2,500+ posts


class TestRetry(unittest.TestCase):
    def test_safe_seasoning_never_raises(self):
        with patch.object(lx, "seasoning", side_effect=RuntimeError("pg down")):
            self.assertEqual(lx.safe_seasoning("burbank-ops", "x"), "")

    def test_publish_journal_dream_refuses_with_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "2026-10-19.md"
            f.write_text('Theme: "x"\n')
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_publish_journal.py"), "dream", str(f)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("retired", r.stderr.lower())


class TestUnit(unittest.TestCase):
    def test_sections_shape(self):
        friends8 = {"music", "military", "screen", "science", "mystery", "crime", "workshop", "travel"}  # 2026-10-19
        self.assertTrue(friends8 <= set(ns.LIVE_SECTIONS))
        self.assertEqual(set(ns.LIVE_SECTIONS) | friends8, LIVE | friends8)
        self.assertEqual(set(ns.SECTION_REDIRECTS) | set(ns.RETIRED_SECTIONS), RETIRED)
        self.assertTrue(set(ns.SECTION_REDIRECTS.values()) <= LIVE)
        self.assertEqual(ns.canon("Operations"), "burbank-ops")
        self.assertEqual(ns.canon("essays"), "nova")
        self.assertEqual(ns.canon("research"), "nova")
        self.assertEqual(ns.canon("opinions"), "nova")
        self.assertEqual(ns.canon("tech-today"), "tech")
        self.assertEqual(ns.canon("cars"), "cars")
        self.assertTrue(ns.is_retired("dreams"))
        self.assertFalse(ns.is_retired("local"))

    def test_lexicon_live_sections(self):
        self.assertNotIn("dreams", lx.FLAVOR_SECTIONS)
        for live in LIVE - {"start-here"}:
            self.assertIn(live, lx.FLAVOR_SECTIONS, live)
        for old in ("operations", "local", "essays", "synthesis", "tech-today", "rando", "research", "opinions"):
            self.assertNotIn(old, lx.FLAVOR_SECTIONS)
            self.assertIn("FAITHS", _season(old))             # still seasoned via canon

    def test_seasoning_has_faiths_and_ferengi(self):
        out = _season("pilot")
        self.assertIn("RULE OF ACQUISITION #48", out)
        self.assertIn("THE WORLD'S FAITHS", out)

    def test_epigraph_block_is_restrained(self):
        b = lx.epigraph_block("memory")
        self.assertIn("THE WORLD'S FAITHS", b)
        self.assertIn("OUT of the body", b)
        self.assertNotIn("LIBERALLY", b)

    def test_voice_infers_new_sections(self):
        import nova_voice as nv
        self.assertEqual(nv._infer_section(nv.CONTEXT_JOURNAL_OPS), "burbank-ops")
        self.assertEqual(nv._infer_section(nv.CONTEXT_JOURNAL_LOCAL), "burbank-ops")
        self.assertEqual(nv._infer_section(nv.CONTEXT_JOURNAL_ESSAY), "research")
        self.assertEqual(nv._infer_section("no context"), "")


class TestIntegration(unittest.TestCase):
    def test_journal_remap_is_nova_sections(self):
        import nova_journal as nj
        self.assertIs(nj.SECTION_REDIRECTS, ns.SECTION_REDIRECTS)
        self.assertIs(nj.SECTION_REMAP, ns.SECTION_REDIRECTS)
        self.assertEqual(nj._canon_section("local"), "burbank-ops")

    def test_publish_hugo_refuses_dreams(self):
        import nova_journal as nj
        with patch.object(nj, "scrub_faces", side_effect=AssertionError("must refuse before any work")):
            self.assertFalse(nj.publish_hugo("A Perfectly Fine Title", "body " * 50, "dreams", [], "d"))

    def test_maps_know_live_sections_not_retired(self):
        import nova_big_brother as bb
        import nova_cross_linker as cl
        import nova_journal_stats_poller as sp
        import nova_tag_extractor as te
        self.assertFalse(set(bb.JOURNAL_SECTIONS) & RETIRED)
        self.assertIn("burbank-ops", bb.JOURNAL_SECTIONS)
        self.assertFalse(set(sp.SECTIONS) & RETIRED)
        self.assertTrue({"burbank-ops", "nova", "cars", "tech", "food"} <= set(sp.SECTIONS))
        self.assertEqual(set(sp.SECTION_SCHEDULES), set(sp.SECTIONS))
        for m in (cl.CATEGORY_URLS, cl.CATEGORY_EMOJI):
            self.assertTrue({"burbank-ops", "nova", "cars", "tech", "food"} <= set(m))
            self.assertFalse(set(m) & (RETIRED - {"dreams"}))   # dreams = archive links only
        self.assertTrue({"burbank-ops", "nova", "cars", "tech", "food"} <= set(te.CATEGORY_SEEDS))
        self.assertFalse(set(te.CATEGORY_SEEDS) & RETIRED)

    def test_watchdog_and_recaps_skip_retired_dirs(self):
        import nova_article_watchdog as aw
        import nova_journal_weekly_summary as ws
        import nova_monthly_wrap as mw
        self.assertFalse({sec for sec, _ in aw.ARTICLE_SCRIPTS.values()} & RETIRED)
        self.assertEqual(aw._journal_section("essay"), "nova")
        self.assertEqual(aw._journal_section("synthesis"), "nova")
        self.assertEqual(aw._journal_section("opinion"), "nova")
        self.assertEqual(aw._journal_section("research"), "nova")
        self.assertTrue(RETIRED <= ws.SKIP_DIRS)
        self.assertTrue(RETIRED <= mw.SKIP_DIRS)
        self.assertFalse(set(mw.MOTIF) & RETIRED)


class TestFunctional(unittest.TestCase):
    def test_no_writer_targets_a_retired_folder(self):
        import re
        pat = re.compile(r'(["\']content["\']\s*[/,]\s*["\']|["\']content/)(dreams|essays|synthesis|tech-today|operations|local)\b')
        bad = []
        for f in SCRIPTS.glob("*.py"):
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if pat.search(line) and not line.lstrip().startswith("#"):
                    bad.append(f"{f.name}:{i}")
        self.assertEqual(bad, [], "write to content/<retired>")

    def test_no_writer_emits_retired_category(self):
        import re
        pat = re.compile(r'categories:\s*\[\s*"(dreams|essays|synthesis|tech-today|operations|local|rando)"')
        bad = [f.name for f in SCRIPTS.glob("*.py") if pat.search(f.read_text(errors="ignore"))]
        self.assertEqual(bad, [])

    def test_article_history_follows_remap(self):
        import nova_article_history as ah
        with tempfile.TemporaryDirectory() as d, patch.object(Path, "home", return_value=Path(d)):
            (Path(d) / "nova-journal" / "content" / "burbank-ops").mkdir(parents=True)
            self.assertEqual(ah._content_dir("operations").name, "burbank-ops")


class TestFrame(unittest.TestCase):
    def test_raw_prompt_generators_append_seasoning(self):
        for f, sec in RAW_SEASONED.items():
            src = _src(f)
            self.assertRegex(src, r'_season\("%s"' % sec, f)

    def test_system_prompt_generators_pass_section(self):
        for f, sec in SECTION_PASSED.items():
            self.assertIn(f'section="{sec}"', _src(f), f)

    def test_nova_journal_profiles_all_seasoned(self):
        src = _src("nova_journal.py")
        for sec in ("opinions", "research", "tech", "art"):
            self.assertIn(f'section="{sec}", topic=topic', src)
        self.assertIn('seasoning("pilot"', src)

    def test_research_paper_uses_restrained_epigraph_only(self):
        src = _src("nova_research_paper.py")
        self.assertIn("epigraph_block", src)
        self.assertNotIn("safe_seasoning", src)


if __name__ == "__main__":
    unittest.main()
