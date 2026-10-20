#!/usr/bin/env python3
"""7-category tests for nova_friends.py — Cars / Tech / Food and Friends + the eight 2026-10-19
sections (Music, Military, Screen, Science, Mystery, Crime, Workshop, Travel) (Security, Performance,
Retry, Unit, Integration, Functional, Frame). PostgreSQL, the Claude CLI (call_openrouter), image
generation, Hugo publish, git push and Slack are all mocked — nothing is published.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_friends_7cat.py
"""
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_friends as m  # noqa: E402
import nova_watches_and_friends as w  # noqa: E402

SRC = (SCRIPTS / "nova_friends.py").read_text()
D = datetime(2026, 10, 19, 9, 0)


def vrow(text, vid, chan="Rob Dahm", title="T", **extra):
    return (text, D, {"video_id": vid, "channel": chan, "title": title, "pipeline": "yt_subs_audio", **extra})


def arow(text, url, feed="Ars Technica", title="A"):
    return (text, D, {"url": url, "feed": feed, "title": title, "type": "gov_rss"})


def recrow(text, url="https://www.meatchurch.com/blogs/recipes/ribs", title="Sweet Honey Ribs", chunk=1):
    return (text, D, {"url": url, "title": title, "type": "recipe", "source_site": "meatchurch.com", "chunk": chunk})


class Cur:
    """Answers by query shape: the channel query gets videos, the article query gets articles,
    the recipe-memory queries get recipe pages / chunks."""

    def __init__(self, videos, arts, recipes=(), history=(), lore=(), scripture=()):
        self.videos, self.arts, self.recipes, self.calls, self._r = videos, arts, list(recipes), [], []
        self.history, self.lore, self.scripture = list(history), list(lore), list(scripture)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "scripture_library" in sql:
            self._r = [] if "md5(" in sql else self.scripture
        elif "on_this_day" in sql:
            self._r = self.history
        elif "esoteric_library" in sql:
            self._r = ([(md["url"], md.get("title"), src) for _, _, md, src in self.lore] if "GROUP BY" in sql
                       else [r for r in self.lore if r[2]["url"] in params[1]])
        elif "source = 'recipes'" in sql:
            self._r = ([(md["url"], md["title"], md["source_site"]) for _, _, md in self.recipes]
                       if "GROUP BY" in sql else [r for r in self.recipes if r[2]["url"] in params[0]])
        else:
            self._r = self.arts if "gov_rss" in sql else self.videos

    def fetchall(self):
        return self._r


# Placeholder house rules: the real ones live in nova_ops.service_config (food_and_friends/recipe_constraints)
# and are deliberately NOT in this repo.
RULES = {"rules": "HOUSE-RULE-SENTINEL: pick the smokiest, spiciest fit.", "private_terms": ["zorbcondition", "secretperson"],
         "prefer": ["smoked", "salmon"], "avoid": ["pudding"]}


def run_main(argv=("--subject", "cars"), videos=None, arts=None, llm=None, publish=True, recipes=(), rules=RULES,
             history=(), lore=(), scripture=()):
    videos = [vrow("a rear wing that lifts the nose", "v1")] if videos is None else videos
    arts = [arow("new kernel release", "https://a/1")] if arts is None else arts
    cur = Cur(videos, arts, recipes, history, lore, scripture)
    conn = MagicMock(); conn.cursor.return_value = cur
    nj = MagicMock()
    nj.today_str.return_value = "2026-10-19"
    nj.call_openrouter.side_effect = llm or (lambda *a, **k: "TITLE: Big Day Out\n## News\n" + "word " * 3000)
    nj.publish_hugo.return_value = publish
    nj.git_push.return_value = "ok"
    with patch.object(sys, "argv", ["x", *argv]), patch.object(m, "_connect", return_value=conn), \
            patch.object(m, "nj", nj), patch.object(m.nova_voice, "system_prompt", side_effect=lambda c, **k: c), \
            patch.object(m, "load_food_rules", return_value=rules):
        rc = m.main()
    return rc, nj, cur


class TestSecurity(unittest.TestCase):
    def test_queries_parameterised(self):
        # the only f-string SQL interpolates the constant NORM_SQL, never data
        for q in re.findall(r'execute\(f"""(.*?)"""', SRC, re.S):
            self.assertNotRegex(re.sub(r"\{NORM_SQL\}", "", q), r"\{")
        _, _, cur = run_main(["--subject", "tech", "--dry-run", "--days", "3"])
        self.assertTrue(all(p[0] == 3 for _, p in cur.calls))

    def test_banned_inputs_dropped(self):
        cur = Cur([vrow("my day at Disney work", "v1"), vrow("ok text", "v2")],
                  [arow("OpenClaw gateway notes", "u1"), arow("fine", "u2")])
        vids, arts = m.fetch(cur, m.SUBJECTS["tech"], 1)
        self.assertEqual([v["text"] for v in vids], ["ok text"])
        self.assertEqual([a["text"] for a in arts], ["fine"])

    def test_draft_with_private_detail_fails_closed(self):
        for leak in ("the box at 192.168.1.6", "back at Disney", "OpenClaw runs it", "/Users/someone/x"):
            rc, nj, _ = run_main(llm=lambda *a, leak=leak, **k: f"TITLE: Big Day Out\n{leak}\n" + "w " * 3000)
            self.assertEqual(rc, 1, leak)
            nj.publish_hugo.assert_not_called()

    def test_prompt_carries_public_journal_rules(self):
        for s in ("Never invent prices", "never mention Jordan's employer", "'OpenClaw'", "no sexual content",
                  "no IP addresses", "household"):
            self.assertIn(s, SRC)

    def test_no_secrets_or_home_paths(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]|password\s*=|sk-or-")


class TestPerformance(unittest.TestCase):
    def test_source_block_bounded(self):
        cur = Cur([vrow("x" * 5000, f"v{i}") for i in range(200)], [arow("y" * 5000, f"u{i}") for i in range(200)])
        vids, arts = m.fetch(cur, m.SUBJECTS["tech"], 1)
        self.assertEqual((len(vids), len(arts)), (m.VID_MAX, m.ART_MAX))
        self.assertLess(len(w.digest(vids)) + len(w.digest(arts)), 70_000)   # under the claude -p hang size

    def test_fetch_large_input_fast(self):
        cur = Cur([vrow("chunk " * 50, f"v{i % 500}") for i in range(20000)], [])
        t = time.perf_counter()
        m.fetch(cur, m.SUBJECTS["cars"], 1)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_llm_calls_have_timeouts(self):
        _, nj, _ = run_main(["--subject", "cars", "--dry-run"])
        self.assertTrue(all(c.kwargs.get("timeout") for c in nj.call_openrouter.call_args_list))


class TestRetry(unittest.TestCase):
    def test_pg_connect_is_the_retrying_watches_helper(self):
        self.assertIs(m._connect, w._connect)

    def test_news_llm_failure_aborts_not_silent(self):
        rc, nj, _ = run_main(llm=lambda *a, **k: None)
        self.assertEqual(rc, 1)
        nj.publish_hugo.assert_not_called()

    def test_image_failure_non_fatal(self):
        cur = Cur([vrow("t", "v1")], [])
        conn = MagicMock(); conn.cursor.return_value = cur
        nj = MagicMock(); nj.today_str.return_value = "d"; nj.publish_hugo.return_value = True
        nj.call_openrouter.side_effect = lambda *a, **k: "TITLE: Long day here\n" + "w " * 3000
        nj.generate_image.side_effect = RuntimeError("SD down")
        with patch.object(sys, "argv", ["x", "--subject", "cars"]), patch.object(m, "_connect", return_value=conn), \
                patch.object(m, "nj", nj), patch.object(m.nova_voice, "system_prompt", side_effect=lambda c, **k: c):
            self.assertEqual(m.main(), 0)
        self.assertIsNone(nj.publish_hugo.call_args.kwargs["image_path"])


class TestUnit(unittest.TestCase):
    def test_norm_matches_curly_apostrophes_case_and_spaces(self):
        self.assertEqual(m.norm("Tom’s  Guide"), m.norm("tom's guide"))
        self.assertEqual(m.norm('Computer History Archives Project  ("CHAP")'),
                         m.norm('Computer History Archives Project ("CHAP")'))

    def test_channel_lists_disjoint_and_jordans_decisions(self):
        cars, tech = {m.norm(c) for c in m.CARS_CHANNELS}, {m.norm(c) for c in m.TECH_CHANNELS}
        self.assertFalse(cars & tech)
        for c in ("Mark Rober", "Real Engineering", "Veritasium", "Honest HiFi", "Dave's Garage"):
            self.assertIn(m.norm(c), tech)
        for c in ("AMain Hobbies", "1:10 Rod Shop", "TeamAssociatedRC", "Hotrods & Homestead", "CarAudioFabrication"):
            self.assertIn(m.norm(c), cars)
        self.assertIn(m.norm("Parkflyers RC"), cars)          # Jordan 2026-10-19 second pass: RC planes -> Cars
        self.assertIn(m.norm("BocaBola"), tech)

    def test_tech_feed_filter_drops_space_and_forum(self):
        ex = m.TECH_FEED_EXCLUDE
        for f in ("NASA Breaking News", "Astronomy Magazine", "EarthSky", "HA Community Latest"):
            self.assertTrue(ex.search(f), f)
        for f in ("Ars Technica", "LWN.net", "9to5Mac", "Lobste.rs", "HA Core Releases"):
            self.assertFalse(ex.search(f), f)

    def test_downloaded_episode_grouped_and_listed_unlinked(self):
        rows = [("[x] part one", D, {"show": "Rob Dahm", "title": "Wing", "source_file": "/v/youtube/Rob Dahm/a.mp4"}),
                ("part two", D, {"show": "Rob Dahm", "title": "Wing", "source_file": "/v/youtube/Rob Dahm/a.mp4"})]
        vids, _ = m.fetch(Cur(rows, []), m.SUBJECTS["cars"], 1)
        self.assertEqual(len(vids), 1)
        self.assertIsNone(vids[0]["url"])
        self.assertEqual(vids[0]["channel"], "Rob Dahm")
        self.assertIn("- Rob Dahm — Wing (2026-10-19)", m.source_list(vids))

    def test_source_list_empty(self):
        self.assertEqual(m.source_list([]), "- (none)")


class TestIntegration(unittest.TestCase):
    def test_cars_query_uses_normalised_channel_list_no_articles(self):
        _, _, cur = run_main(["--subject", "cars", "--dry-run"])
        self.assertEqual(len(cur.calls), 1)             # cars has no article vectors
        self.assertIn(m.norm("Jay Leno's Garage"), cur.calls[0][1][1])

    def test_tech_reads_article_vectors(self):
        _, _, cur = run_main(["--subject", "tech", "--dry-run"])
        self.assertEqual(cur.calls[1][1][1], list(m.SUBJECTS["tech"]["article_vectors"]))

    def test_sources_appended_and_no_fishbowl(self):
        with patch("builtins.print") as pr:
            run_main(["--subject", "tech", "--dry-run"])
        body = pr.call_args.args[0]
        self.assertIn("## Sources\n\n### Videos", body)
        self.assertIn("### Articles\n\n- [Ars Technica — A](https://a/1)", body)
        self.assertNotIn("Fishbowl", body)

    def test_short_issue_gets_deeper_cuts_call(self):
        calls = []

        def llm(system, user, **k):
            if "very TOP" in system:          # the featured block call (TestTopBlocks covers it)
                return "NONE"
            calls.append(system)
            return "TITLE: Short day\nshort" if len(calls) == 1 else "## Deeper Cuts\nmore"
        with patch("builtins.print"):
            run_main(["--subject", "cars", "--dry-run"], llm=llm)
        self.assertEqual(len(calls), 2)
        self.assertIn("Deeper Cuts", calls[1])


class TestFunctional(unittest.TestCase):
    def test_golden_cars_publishes_to_cars(self):
        rc, nj, _ = run_main()
        self.assertEqual(rc, 0)
        a = nj.publish_hugo.call_args.args
        self.assertTrue(a[0].startswith("Cars and Friends: Big Day Out"))
        self.assertEqual(a[2], "cars")
        self.assertIn("daily", a[3])
        nj.git_push.assert_called_once()
        self.assertEqual(nj.git_push.call_args.args[0], "cars")

    def test_golden_tech_publishes_to_tech(self):
        rc, nj, _ = run_main(["--subject", "tech"])
        self.assertEqual(rc, 0)
        self.assertEqual(nj.publish_hugo.call_args.args[2], "tech")
        self.assertTrue(nj.publish_hugo.call_args.args[0].startswith("Tech and Friends: "))

    def test_no_news_skips_quietly(self):
        rc, nj, _ = run_main(videos=[], arts=[])
        self.assertEqual(rc, 0)
        nj.call_openrouter.assert_not_called()

    def test_quality_guard_reject_returns_1_without_push(self):
        rc, nj, _ = run_main(publish=False)
        self.assertEqual(rc, 1)
        nj.git_push.assert_not_called()

    def test_dry_run_never_publishes(self):
        with patch("builtins.print"):
            rc, nj, _ = run_main(["--subject", "cars", "--dry-run"])
        self.assertEqual(rc, 0)
        nj.publish_hugo.assert_not_called()

    def test_weekly_title_fallback(self):
        rc, nj, _ = run_main(["--subject", "tech", "--days", "7"], llm=lambda *a, **k: "no title\n" + "w " * 6000)
        self.assertEqual(nj.publish_hugo.call_args.args[0], "Tech and Friends — Week Ending 2026-10-19")


class TestFrame(unittest.TestCase):
    def test_module_shape(self):
        for n in ("main", "fetch", "source_list", "norm", "load_food_rules", "private_hit", "fetch_recipes"):
            self.assertTrue(callable(getattr(m, n)))
        self.assertEqual(set(m.SUBJECTS), {"cars", "tech", "food", *NEW, *NEW2})
        self.assertEqual(set(m.FEATURES), set(m.SUBJECTS))

    def test_watches_script_untouched_by_the_sibling(self):
        self.assertEqual(w.SECTION, "watches")
        self.assertNotIn("subject", (SCRIPTS / "nova_watches_and_friends.py").read_text())

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_friends.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--subject", r.stdout)



# ── Featured top blocks (Jordan 2026-10-19) ─────────────────────────────────────────────────────
YT1 = "https://www.youtube.com/watch?v=v1"
REC = "https://www.meatchurch.com/blogs/recipes/ribs"


def block_llm(block_text, seen=None):
    """News call -> a long issue; the featured-block call -> block_text."""
    def llm(system, user, **k):
        if seen is not None:
            seen.append((system, user))
        if "block that sits at the very TOP" in system:
            return block_text
        return "TITLE: Big Day Out\n## News\nThe roundup starts here. " + "word " * 3000
    return llm


def good_block(url=YT1, extra=""):
    return f"The day belongs to a wild new build — [Rob Dahm — T]({url}) shows it. " + "punchy " * 40 + extra


def published_body(nj):
    return nj.publish_hugo.call_args.args[1]


class TestTopBlocks(unittest.TestCase):
    def test_each_subject_has_its_block_at_the_top(self):
        cases = {"cars": ("The Big Story", [vrow("a rear wing", "v1")], (), YT1),
                 "tech": ("Buying Recommendation", [vrow("a new UniFi gateway", "v1", chan="Lawrence Systems")], None, YT1),
                 "food": ("Recipe Recommendation", [vrow("smoked ribs", "v1", chan="Meat Church BBQ")], (), REC)}
        for subj, (heading, vids, arts, url) in cases.items():
            rc, nj, _ = run_main(["--subject", subj], videos=vids, arts=[] if arts == () else arts,
                                 recipes=[recrow("Ingredients: ribs, honey. Smoke at 250F.")],
                                 llm=block_llm(good_block(url)))
            self.assertEqual(rc, 0, subj)
            body = published_body(nj)
            self.assertTrue(body.startswith(f"## {heading}\n\n"), (subj, body[:80]))
            self.assertLess(body.index(heading), body.index("The roundup starts here"))
            self.assertIn(url, body.split("## News")[0])

    def test_block_omitted_when_model_says_none(self):
        for subj in ("cars", "tech", "food"):
            rc, nj, _ = run_main(["--subject", subj], llm=block_llm("NONE"))
            self.assertEqual(rc, 0)
            body = published_body(nj)
            self.assertTrue(body.startswith("## News"), subj)
            for h in ("The Big Story", "Buying Recommendation", "Recipe Recommendation"):
                self.assertNotIn(h, body)

    def test_block_omitted_when_it_links_nothing_real(self):
        for text in (good_block("https://invented.example/x"),           # link not one of the day's sources
                     good_block(YT1, " also [x](https://made.up/y)"),     # one real + one invented
                     "punchy " * 60,                                       # no link at all
                     good_block(YT1, " " + "w " * 600),                   # far too long
                     good_block(YT1, " it runs on nova-core3 at 10.0.0.6")):   # privacy
            rc, nj, _ = run_main(llm=block_llm(text))
            self.assertEqual(rc, 0)
            self.assertNotIn("The Big Story", published_body(nj), text[-60:])

    def test_block_omitted_when_nothing_linkable(self):
        rows = [("t", D, {"show": "Rob Dahm", "title": "Wing", "source_file": "/v/youtube/Rob Dahm/a.mp4"})]
        seen = []
        rc, nj, _ = run_main(videos=rows, arts=[], llm=block_llm(good_block(), seen))
        self.assertEqual(rc, 0)
        self.assertNotIn("The Big Story", published_body(nj))
        self.assertFalse(any("very TOP" in s for s, _ in seen))   # no call made without grounding

    def test_block_call_failure_never_sinks_the_issue(self):
        def llm(system, user, **k):
            if "very TOP" in system:
                raise RuntimeError("cli died")
            return "TITLE: Big Day Out\n## News\n" + "word " * 3000
        rc, nj, _ = run_main(llm=llm)
        self.assertEqual(rc, 0)
        self.assertTrue(published_body(nj).startswith("## News"))

    def test_food_recipe_memory_grounds_and_is_credited(self):
        seen = []
        rc, nj, cur = run_main(["--subject", "food"], videos=[vrow("ribs on the smoker", "v1", chan="Chuds BBQ")],
                               recipes=[recrow("Ingredients: baby back ribs, honey."),
                                        recrow("Method: smoke at 250F for 3 hours.", chunk=2)],
                               llm=block_llm(f"Cook the [Meat Church BBQ — Sweet Honey Ribs]({REC}). " + "yum " * 40, seen))
        self.assertEqual(rc, 0)
        system, user = next(x for x in seen if "very TOP" in x[0])
        self.assertIn("RECIPE PAGES", user)
        self.assertIn("Ingredients: baby back ribs, honey. Method: smoke at 250F", user)
        self.assertIn("Amounts, times and temperatures come ONLY from the source", system)
        self.assertIn("HOUSE-RULE-SENTINEL", system)          # the private rules reach the model at runtime
        body = published_body(nj)
        self.assertTrue(body.startswith("## Recipe Recommendation"))
        self.assertIn("### Recipe\n\n- [Meat Church BBQ — Sweet Honey Ribs](" + REC, body)

    def test_tech_block_prompt_uses_public_stack_only(self):
        seen = []
        run_main(["--subject", "tech", "--dry-run"], llm=block_llm("NONE", seen))
        system = next(s for s, _ in seen if "very TOP" in s)
        for item in ("Mac Studio", "Beelink", "UNAS", "Synology", "Lutron", "Meshtastic", "SDRplay", "Bambu",
                     "Ubiquiti cameras"):
            self.assertIn(item, system)
        self.assertIn("never hostnames, IP addresses, camera or room names", system)
        self.assertNotRegex(m.HOME_STACK, r"\d+\.\d+\.\d+|nova-|office|kitchen|bedroom|garage|disney|SER\d|GTi")

    def test_issue_calls_are_seasoned_with_the_section(self):
        for subj in ("cars", "tech", "food"):
            with patch.object(m.nova_voice, "system_prompt", side_effect=lambda c, **k: c) as sp:
                cur = Cur([vrow("x", "v1")], [])
                conn = MagicMock(); conn.cursor.return_value = cur
                nj = MagicMock(); nj.today_str.return_value = "d"; nj.call_openrouter.side_effect = block_llm("NONE")
                with patch.object(sys, "argv", ["x", "--subject", subj, "--dry-run"]), \
                        patch.object(m, "_connect", return_value=conn), patch.object(m, "nj", nj), patch("builtins.print"):
                    m.main()
            secs = [c.kwargs.get("section") for c in sp.call_args_list if "very TOP" not in c.args[0]]
            self.assertTrue(secs and all(x == subj for x in secs), (subj, secs))

    def test_block_that_would_trip_the_publish_guard_is_dropped_not_the_issue(self):
        text = f"Little Mister, the material you gave me says buy this: [Rob Dahm — T]({YT1}). " + "punchy " * 40
        rc, nj, _ = run_main(llm=block_llm(text))
        self.assertEqual(rc, 0)
        body = published_body(nj)
        self.assertNotIn("The Big Story", body)
        self.assertTrue(body.startswith("## News"))
        nj.git_push.assert_called_once()

    def test_block_prompt_forbids_talking_about_the_material(self):
        seen = []
        run_main(["--subject", "cars", "--dry-run"], llm=block_llm("NONE", seen))
        system = next(s for s, _ in seen if "very TOP" in s)
        self.assertIn("never refer to 'the material'", system)

    def test_watches_big_story_is_the_shared_helper(self):
        self.assertIs(m.featured_block, w.featured_block)
        self.assertIs(m.place_block, w.place_block)


class TestFoodPrivacy(unittest.TestCase):
    """The Recipe Recommendation's selection rules are private: loaded from PG at runtime, never in the
    repo, never in the published block."""

    def test_block_with_a_private_term_is_never_published(self):
        for leak in ("zorbcondition", "ZorbConditions", "my secretperson can't eat it"):
            text = f"Cook the [Meat Church BBQ — Sweet Honey Ribs]({REC}). Good for {leak}. " + "yum " * 40
            rc, nj, _ = run_main(["--subject", "food"], recipes=[recrow("Ingredients: ribs.")], llm=block_llm(text))
            self.assertEqual(rc, 0)
            body = published_body(nj)
            self.assertNotIn("Recipe Recommendation", body, leak)
            self.assertNotIn(leak.lower(), body.lower())

    def test_neutral_tags_and_generic_swaps_are_fine(self):
        text = (f"*gluten-free · pescatarian · low-sugar · smoked · spicy · pairs with: 805*\n\nSmoked salmon from "
                f"[Meat Church BBQ — Sweet Honey Ribs]({REC}). Swap: garlic-infused oil keeps it gentle. Serves 2, "
                "moderate. A pat of brown butter to finish. " + "yum " * 30 + "\n\n**Pairs with:** rum & Coke Zero.")
        rc, nj, _ = run_main(["--subject", "food"], recipes=[recrow("Ingredients: salmon.")], llm=block_llm(text))
        self.assertTrue(published_body(nj).startswith("## Recipe Recommendation\n\n*gluten-free · pescatarian"))

    def test_no_house_rules_means_no_recipe_block(self):
        seen = []
        rc, nj, _ = run_main(["--subject", "food"], rules=None, llm=block_llm(good_block(), seen))
        self.assertEqual(rc, 0)
        self.assertNotIn("Recipe Recommendation", published_body(nj))
        self.assertFalse(any("very TOP" in s for s, _ in seen))
        nj.git_push.assert_called_once()                     # the roundup still publishes

    def test_rules_only_reach_the_food_block(self):
        for subj in ("cars", "tech"):
            seen = []
            run_main(["--subject", subj, "--dry-run"], llm=block_llm("NONE", seen))
            self.assertFalse(any("HOUSE-RULE-SENTINEL" in s or "HOUSE-RULE-SENTINEL" in u for s, u in seen), subj)
        seen = []
        run_main(["--subject", "food", "--dry-run"], llm=block_llm("NONE", seen))
        news = [s for s, _ in seen if "very TOP" not in s]
        self.assertFalse(any("HOUSE-RULE-SENTINEL" in s for s in news))   # never in the roundup call

    def test_load_food_rules_reads_service_config_parameterised(self):
        cur = MagicMock(); cur.fetchone.return_value = (RULES,)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(m, "_connect", return_value=conn):
            self.assertEqual(m.load_food_rules(), RULES)
        sql, params = cur.execute.call_args.args
        self.assertEqual(params, ("food_and_friends", "recipe_constraints"))
        for bad in (None, ({"rules": "x"},), ({"private_terms": ["a"]},)):
            cur.fetchone.return_value = bad
            with patch.object(m, "_connect", return_value=conn):
                self.assertIsNone(m.load_food_rules())
        with patch.object(m, "_connect", side_effect=RuntimeError("pg down")):
            self.assertIsNone(m.load_food_rules())

    def test_private_hit_word_start(self):
        self.assertEqual(m.private_hit("Zorbconditional stuff", ["zorbcondition"]), "zorbcondition")
        self.assertIsNone(m.private_hit("xzorbcondition", ["zorbcondition"]))
        self.assertIsNone(m.private_hit("anything", None))

    def test_recipe_pick_follows_house_preferences(self):
        recs = [recrow("x", url=f"https://r/{i}", title=t) for i, t in
                enumerate(["Banana Pudding", "Smoked Salmon", "Country Style Ribs"])]
        nj = MagicMock(); nj.today_str.return_value = "2026-10-19"
        with patch.object(m, "nj", nj):
            got = m.fetch_recipes(Cur([], [], recs), [], n=1, prefer=["smoked", "salmon"], avoid=["pudding"])
        self.assertEqual(got[0]["title"], "Smoked Salmon")

    def test_live_private_terms_never_in_repo_or_published_food_blocks(self):
        """Against the REAL private terms (PG) — skipped when PG is unreachable."""
        rules = m.load_food_rules()
        if not rules:
            self.skipTest("house rules not reachable")
        terms = rules["private_terms"]
        for f in ("nova_friends.py", "nova_watches_and_friends.py", "tests/test_nova_friends_7cat.py"):
            self.assertIsNone(m.private_hit((SCRIPTS / f).read_text().replace("private_terms", ""), terms), f)
        # every real private term (whatever categories the PG row carries) is caught in a block
        for t in terms:
            text = f"Cook the [Meat Church BBQ — Sweet Honey Ribs]({REC}). Ideal for {t} reasons. " + "yum " * 40
            rc, nj, _ = run_main(["--subject", "food"], recipes=[recrow("Ingredients: salmon.")], llm=block_llm(text),
                                 rules=rules)
            self.assertNotIn("Recipe Recommendation", published_body(nj), "a private term slipped through")
        food = Path(m.nj.HUGO_ROOT) / "content/food"
        for md in sorted(food.glob("*.md")) if food.is_dir() else []:
            t = md.read_text()
            if "## Recipe Recommendation" in t:
                block = t.split("## Recipe Recommendation", 1)[1].split("\n## ", 1)[0]
                self.assertIsNone(m.private_hit(block, terms), md.name)


class TestFoodRouting(unittest.TestCase):
    def test_food_channels_listed_and_excluded_kept_out(self):
        food = {m.norm(c) for c in m.FOOD_CHANNELS}
        for c in ("Meat Church BBQ", "Sam The Cooking Guy", "SONS OF SAM THE COOKING GUY", "America’s Test Kitchen",
                  "Cooper Abercrombie - Bar-A-BBQ", "Beers & Bars", "James May's Planet Gin", "Whiskey Tribe"):
            self.assertIn(m.norm(c), food, c)
        everything = food | {m.norm(c) for c in m.CARS_CHANNELS + m.TECH_CHANNELS}
        for c in m.FOOD_EXCLUDED:
            self.assertNotIn(m.norm(c), everything, c)
        self.assertEqual(len(m.FOOD_CHANNELS), 32)

    def test_food_lists_disjoint_from_cars_and_tech(self):
        food = {m.norm(c) for c in m.FOOD_CHANNELS}
        self.assertFalse(food & {m.norm(c) for c in m.CARS_CHANNELS + m.TECH_CHANNELS})

    def test_food_query_routes_by_normalised_channel_no_articles(self):
        rc, nj, cur = run_main(["--subject", "food", "--dry-run"], videos=[vrow("brisket", "v1", chan="Chuds BBQ")])
        self.assertEqual(rc, 0)
        self.assertIn(m.norm("A Cooks Tour with Anthony Bourdain"), cur.calls[0][1][1])
        self.assertNotIn(m.norm("Peter Santenello"), cur.calls[0][1][1])
        self.assertFalse(any("gov_rss" in sql for sql, _ in cur.calls))

    def test_golden_food_publishes_to_food(self):
        rc, nj, _ = run_main(["--subject", "food"], videos=[vrow("brisket", "v1", chan="Chuds BBQ")])
        self.assertEqual(rc, 0)
        a = nj.publish_hugo.call_args.args
        self.assertTrue(a[0].startswith("Food and Friends: "))
        self.assertEqual(a[2], "food")
        self.assertEqual(nj.git_push.call_args.args[0], "food")

    def test_recipe_pick_prefers_matching_titles_and_is_stable(self):
        recs = [recrow("x", url=f"https://r/{i}", title=t) for i, t in
                enumerate(["Banana Pudding", "Smoked Brisket Chili", "Country Style Ribs", "Turkey Gravy"])]
        nj = MagicMock(); nj.today_str.return_value = "2026-10-19"
        with patch.object(m, "nj", nj):
            vids = [{"title": "Brisket day", "text": "chili on the smoker", "channel": "c", "url": "u", "date": "d"}]
            got = m.fetch_recipes(Cur([], [], recs), vids, n=1)
            again = m.fetch_recipes(Cur([], [], recs), vids, n=1)
        self.assertEqual(got[0]["title"], "Smoked Brisket Chili")
        self.assertEqual(got[0]["channel"], "Meat Church BBQ")
        self.assertEqual(got, again)

    def test_no_recipe_memories_is_fine(self):
        self.assertEqual(m.fetch_recipes(Cur([], [], []), []), [])


# ── The eight 2026-10-19 sections ───────────────────────────────────────────────────────────────
NEW = ("music", "military", "screen", "science", "mystery", "crime", "workshop", "travel")
LISTS = {"cars": m.CARS_CHANNELS, "tech": m.TECH_CHANNELS, "food": m.FOOD_CHANNELS, "music": m.MUSIC_CHANNELS,
         "military": m.MILITARY_CHANNELS, "screen": m.SCREEN_CHANNELS, "science": m.SCIENCE_CHANNELS,
         "mystery": m.MYSTERY_CHANNELS, "crime": m.CRIME_CHANNELS, "workshop": m.WORKSHOP_CHANNELS,
         "travel": m.TRAVEL_CHANNELS, "history": m.HISTORY_CHANNELS, "belief": m.BELIEF_CHANNELS}
NEW2 = ("history", "belief")
import nova_channel_pools as ncp  # noqa: E402
# Every channel belongs to exactly one subject OR non-Friends pool (Jordan 2026-10-19).
OWNERS = {**LISTS, **{f"pool:{k}": v for k, v in ncp.POOLS.items()}}
SAMPLE = {"music": "Rick Beato", "military": "Forgotten Weapons", "screen": "RedLetterMedia", "science": "PBS Space Time",
          "mystery": "Wendigoon", "crime": "Kitboga", "workshop": "Blacktail Studio", "travel": "Peter Santenello"}
HEADINGS = {"music": "🎧 Track of the Day", "military": "📜 This Day in Military History",
            "screen": "🍿 What to Watch Tonight", "science": "🌌 The Coolest Thing I Learned",
            "mystery": "🕯️ The Rabbit Hole of the Day", "crime": "🔦 Case of the Day",
            "workshop": "🔨 Project Worth Stealing", "travel": "🗺️ Place of the Day"}
WIKI = "https://en.wikipedia.org/wiki/Battle_of_X"


def hrow(text, url=WIKI, title="Battle of X", source="military_history"):
    return (text, D, {"url": url, "title": title, "source": source})


def lrow(text, url, title, source, **md):
    return (text, D, {"url": url, "title": title, **md}, source)


class TestNewSectionRouting(unittest.TestCase):
    def test_every_list_disjoint_from_every_other(self):
        seen = {}
        for subj, chans in OWNERS.items():
            for c in chans:
                k = m.norm(c)
                self.assertNotIn(k, seen, f"{c} in both {seen.get(k)} and {subj}")
                seen[k] = subj

    def test_tech_and_science_do_not_overlap(self):
        tech, sci = {m.norm(c) for c in m.TECH_CHANNELS}, {m.norm(c) for c in m.SCIENCE_CHANNELS}
        self.assertFalse(tech & sci)
        for c in ("Veritasium", "Real Engineering", "Mark Rober"):
            self.assertIn(m.norm(c), tech)
            self.assertNotIn(m.norm(c), sci)
        for c in ("PBS Space Time", "SciShow Space", "minutephysics", "T. Folse Nuclear", "YaleCourses"):
            self.assertIn(m.norm(c), sci)

    def test_jordans_picks_are_in_their_sections(self):
        picks = {"music": ("Rick Beato 2", "HÖR BERLIN", "Marc Rebillet - Topic", "Rock N’ Rewind", "DnB Portal"),
                 "military": ("VASAviation -", "U.S. Navy", "Airforceproud95", "Task & Purpose"),
                 "screen": ("How Did This Get Made?", "Late Night With Conan O'Brien", "LastWeekTonight", "Kevin Smith"),
                 "mystery": ("Dark5 Ancient Mysteries", "ESOTERICA", "Brain Blaze"),
                 "crime": ('Salvatore "Sammy The Bull" Gravano', "Matthew Cox | Inside True Crime", "Scammer Payback"),
                 "workshop": ("A R Pond Maintenance And Supplies.", "Bertha's House Hacks", "This Old House"),
                 "travel": ("Miha's Life in Japan", "Away Together w/ Nik and Allie", "Deep Sea Explorer YT")}
        for subj, names in picks.items():
            keys = {m.norm(c) for c in LISTS[subj]}
            for n in names:
                self.assertIn(m.norm(n), keys, (subj, n))
        self.assertNotIn(m.norm("Peter Santenello"), {m.norm(c) for c in m.FOOD_EXCLUDED})

    def test_each_subject_queries_only_its_own_channels(self):
        for subj in NEW:
            rc, nj, cur = run_main(["--subject", subj, "--dry-run"], videos=[vrow("x", "v1", chan=SAMPLE[subj])])
            self.assertEqual(rc, 0, subj)
            keys = set(cur.calls[0][1][1])
            self.assertEqual(keys, {m.norm(c) for c in LISTS[subj]}, subj)
            self.assertFalse(any("gov_rss" in sql for sql, _ in cur.calls), subj)

    def test_each_subject_publishes_to_its_own_section(self):
        for subj in NEW:
            rc, nj, _ = run_main(["--subject", subj], videos=[vrow("x", "v1", chan=SAMPLE[subj])])
            self.assertEqual(rc, 0, subj)
            a = nj.publish_hugo.call_args.args
            self.assertTrue(a[0].startswith(m.SUBJECTS[subj]["name"] + ": "), a[0])
            self.assertEqual(a[2], subj)
            self.assertEqual(nj.git_push.call_args.args[0], subj)

    def test_thin_day_widens_new_subjects_only(self):
        rc, nj, cur = run_main(["--subject", "travel", "--dry-run"], videos=[vrow("x", "v1", chan="Smarty Van")])
        days = [p[0] for sql, p in cur.calls if "yt_subs_audio" in sql]
        self.assertEqual(days, [1, m.WIDEN_DAYS])
        rc, nj, cur = run_main(["--subject", "cars", "--dry-run"], videos=[vrow("x", "v1")], arts=[])
        self.assertEqual([p[0] for sql, p in cur.calls if "yt_subs_audio" in sql], [1])

    def test_live_names_match_jordans_subscriptions(self):
        try:
            conn = m._connect(m.OPS_DSN, attempts=1)
            cur = conn.cursor()
            cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s",
                        ("yt_subs_audio", "subscriptions"))
            row = cur.fetchone()
            conn.close()
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"PG unavailable: {e}")
        if not row:
            self.skipTest("no subscription cache")
        subs = {m.norm(r[2] if isinstance(r, list) else r) for r in row[0]}
        legacy = {m.norm(c) for c in ("Supercharged Sib", "Made with Layers", "Tony's Hyper Tinkering Shop")}
        for subj, chans in OWNERS.items():   # (three older aliases kept for downloaded episodes' show names)
            missing = [c for c in chans if m.norm(c) not in subs and m.norm(c) not in legacy]
            self.assertEqual(missing, [], subj)
        missing = [c for c in m.UNASSIGNED_CHANNELS if m.norm(c) not in subs]
        self.assertEqual(missing, [])
        # Every channel a section reads is either one he follows or on the explicit direct-ingest list
        # (nova_yt_direct: ingested by channel id without a subscription, Jordan 2026-10-19).
        direct = {m.norm(c[2]) for c in m.nova_yt_direct.DIRECT_CHANNELS}
        for subj, cfg in m.SUBJECTS.items():
            for c in (*cfg["channels"], *cfg.get("shared_channels", ())):
                k = m.norm(c)
                self.assertTrue(k in subs or k in direct or k in legacy, f"{subj}: {c}")


class TestNewSectionTopBlocks(unittest.TestCase):
    def test_each_new_block_sits_on_top_and_matches_the_homepage_kind(self):
        import nova_series
        for subj in NEW:
            self.assertEqual(m.FEATURES[subj][0], HEADINGS[subj])
            self.assertEqual(nova_series.TOP_BLOCK_KINDS[subj], HEADINGS[subj])
            rc, nj, _ = run_main(["--subject", subj], videos=[vrow("x", "v1", chan=SAMPLE[subj])],
                                 llm=block_llm(good_block()))
            self.assertEqual(rc, 0, subj)
            body = published_body(nj)
            self.assertTrue(body.startswith(f"## {HEADINGS[subj]}\n\n"), (subj, body[:60]))
            tb = nova_series.extract_top_block(subj, body)
            self.assertEqual(tb["kind"], HEADINGS[subj])

    def test_each_new_block_omitted_on_none(self):
        for subj in NEW:
            rc, nj, _ = run_main(["--subject", subj], videos=[vrow("x", "v1", chan=SAMPLE[subj])],
                                 llm=block_llm("NONE"))
            self.assertEqual(rc, 0, subj)
            self.assertTrue(published_body(nj).startswith("## News"), subj)

    def test_military_history_notes_ground_and_link(self):
        seen = []
        notes = [hrow("The fleet engaged off the cape on October 19, 1944, and the battle turned. More text.")]
        nj_today = "2026-10-19"
        text = f"On this day the fleet fought — [Battle of X]({WIKI}). " + "history " * 40
        with patch.object(m.nj, "today_str", return_value=nj_today):
            rc, nj, cur = run_main(["--subject", "military"], videos=[vrow("x", "v1", chan="Forgotten Weapons")],
                                   history=notes, llm=block_llm(text, seen))
        self.assertEqual(rc, 0)
        system, user = next(x for x in seen if "very TOP" in x[0])
        self.assertIn("THIS DAY IN HISTORY", user)
        self.assertIn("October 19, 1944", user)
        self.assertIn("October 19", system)
        body = published_body(nj)
        self.assertTrue(body.startswith("## 📜 This Day in Military History"))
        self.assertIn("### This Day in History", body)
        self.assertIn(WIKI, body.split("## Sources")[1])

    def test_history_needs_a_military_event_in_the_dated_sentence(self):
        rows = [hrow("The army crossed the river on 19 October 1805. Then peace.", url="https://w/1"),
                hrow("A poem was published on October 19, 1840. The army later marched.", url="https://w/2"),
                hrow("JP 3-01, Joint Doctrine for Air Defense, 19 October 1999. Army.", url="https://w/3"),
                hrow("The navy sailed on October 20, 1944.", url="https://w/4")]
        with patch.object(m.nj, "today_str", return_value="2026-10-19"):
            got = m.fetch_history(Cur([], [], history=rows), today="2026-10-19")
        self.assertEqual([g["url"] for g in got], ["https://w/1"])

    def test_military_falls_back_to_the_days_story_without_notes(self):
        seen = []
        rc, nj, _ = run_main(["--subject", "military"], videos=[vrow("x", "v1", chan="LazerPig")],
                             llm=block_llm(good_block(), seen))
        self.assertEqual(rc, 0)
        system, user = next(x for x in seen if "very TOP" in x[0])
        self.assertNotIn("THIS DAY IN HISTORY ---", user)
        self.assertIn("day's single most interesting military story", system)
        self.assertTrue(published_body(nj).startswith("## 📜 This Day in Military History"))

    def test_mystery_library_keeps_fiction_and_esoteric_labels(self):
        lore = [lrow("Abdul Alhazred's book...", "https://www.gutenberg.org/ebooks/1", "Necronomicon Tales",
                     "necronomicon", fiction=True),
                lrow("A 13th-century compendium...", "https://archive.org/details/shams", "Shams al-Ma'arif grimoire",
                     "shams_al_maarif", framing="Historical esoteric text (history, not instructions)")]
        seen = []
        rc, nj, _ = run_main(["--subject", "mystery"], lore=lore, llm=block_llm(good_block(), seen),
                             videos=[vrow("x", "v1", chan="ESOTERICA", title="The Necronomicon and the grimoire Shams al-Ma'arif")])
        self.assertEqual(rc, 0)
        system, user = next(x for x in seen if "very TOP" in x[0])
        self.assertIn("ESOTERIC LIBRARY", user)
        self.assertIn("FICTION (H. P. Lovecraft's mythos)", user)
        self.assertIn("Historical esoteric text (history, not instructions)", user)
        self.assertIn("never as instructions", system)

    def test_mystery_library_needs_a_real_topic_match(self):
        lore = [lrow("t", "https://en.wikipedia.org/wiki/Third_Epistle_of_John", "Third Epistle of John", "occult")]
        vids = [{"title": "The Soul Frequency they describe", "text": "", "channel": "c", "url": "u", "date": "d"}]
        with patch.object(m.nj, "today_str", return_value="2026-10-19"):
            self.assertEqual(m.fetch_lore(Cur([], [], lore=lore), vids), [])

    def test_crime_block_with_an_address_is_omitted_and_body_redacted(self):
        text = good_block(extra=" The victim lived at 1234 Elm Street in town.")
        def llm(system, user, **k):
            if "very TOP" in system:
                return text
            return "TITLE: Big Day Out\n## News\nThe house at 55 North Oak Avenue was raided. " + "word " * 3000
        rc, nj, _ = run_main(["--subject", "crime"], videos=[vrow("x", "v1", chan="Kitboga")], llm=llm)
        self.assertEqual(rc, 0)
        body = published_body(nj)
        self.assertNotIn("Case of the Day", body)
        self.assertNotIn("Oak Avenue", body)
        self.assertIn("[address withheld]", body)

    def test_screen_never_reads_plex_or_viewing_history(self):
        seen = []
        rc, nj, cur = run_main(["--subject", "screen"], videos=[vrow("x", "v1", chan="RedLetterMedia")],
                               llm=block_llm(good_block(), seen))
        self.assertEqual(rc, 0)
        for sql, _ in cur.calls:
            self.assertNotRegex(sql.lower(), r"plex|watch_history|viewing")
        system = next(s for s, _ in seen if "very TOP" in s)
        self.assertIn("never a Plex library or viewing history", system)

    def test_military_stance_block_in_every_military_prompt(self):
        seen = []
        rc, nj, _ = run_main(["--subject", "military"], videos=[vrow("drones over the front", "v1", chan="Task & Purpose")],
                             llm=block_llm(good_block(), seen))
        self.assertEqual(rc, 0)
        news = [s for s, _ in seen if "very TOP" not in s]
        top = [s for s, _ in seen if "very TOP" in s]
        self.assertTrue(news and top)
        for system in news + top:
            self.assertIn(m.MILITARY_STANCE, system)
        for phrase in ("unprovoked aggression", "Ukraine is the defender", "call it propaganda",
                       "never at Russian people", "no battlefield facts, casualty numbers or quotes"):
            self.assertIn(phrase, m.MILITARY_STANCE)

    def test_stance_reaches_screen_and_travel_only_when_the_war_comes_up(self):
        for subj, chan in (("screen", "The Daily Show"), ("travel", "Peter Santenello")):
            for text, want in (("a quiet sitcom review", False), ("a trip to Kyiv during the Russian invasion", True)):
                seen = []
                run_main(["--subject", subj], videos=[vrow(text, "v1", chan=chan)], llm=block_llm(good_block(), seen))
                got = any(m.MILITARY_STANCE in s for s, _ in seen)
                self.assertEqual(got, want, (subj, text))
        seen = []
        run_main(["--subject", "cars"], videos=[vrow("a Ukrainian-built kit car", "v1")], llm=block_llm(good_block(), seen))
        self.assertFalse(any(m.MILITARY_STANCE in s for s, _ in seen))

    def test_new_sections_are_seasoned_and_series_assigned(self):
        import nova_lexicon
        import nova_series
        for subj in NEW:
            self.assertIn(subj, nova_lexicon.FLAVOR_SECTIONS, subj)
            name = m.SUBJECTS[subj]["name"]
            self.assertEqual(nova_series.assign_series(subj, f"{name}: Some Day", []), name)


# ── History and Friends / Belief and Friends (Jordan 2026-10-19, second pass; Faith renamed Belief) ───────────────────
SAMPLE2 = {"history": "OverSimplified", "belief": "Ascension Presents"}
HEADINGS2 = {"history": "🏛️ This Day in History", "belief": "📜 Text in Context"}
KJV = "https://www.gutenberg.org/cache/epub/10/pg10.txt"
PSALM = ("[Psalms 23:1-4 (KJV)] 1 The LORD is my shepherd; I shall not want. 2 He maketh me to lie down in green "
         "pastures: he leadeth me beside the still waters. 3 He restoreth my soul: he leadeth me in the paths of "
         "righteousness for his name's sake. 4 Yea, though I walk through the valley of the shadow of death, I will "
         "fear no evil: for thou art with me; thy rod and thy staff they comfort me.")
PSALM_MD = {"url": KJV, "book": "Psalms", "chapter": 23, "verses": "1-4", "type": "scripture",
            "translation": "King James Version"}
DHP = ("[Dhammapada — Chapter 1, The Twin Verses (F. Max Müller 1881, passage 1/3)] All that we are is the result "
       "of what we have thought: it is founded on our thoughts, it is made up of our thoughts.")
DHP_MD = {"url": "https://sacred-texts.com/bud/sbe10/sbe1003.htm", "work": "Dhammapada", "type": "scripture",
          "section": "Chapter 1, The Twin Verses", "tradition": "Buddhism"}


def srow(text, md, source):
    return (text, md, source)


def verse_block(quote, cite, extra=""):
    return (f"> {quote}\n\n— {cite}\n\nA quiet morning thought, tied to [Ascension Presents — T]({YT1}). "
            + "reflect " * 40 + extra)


class TestHistoryBelief(unittest.TestCase):
    def test_lists_jordans_channels_and_style_health_stay_unassigned(self):
        for n in ("OverSimplified", "Who Do You Think You Are?", "Well There's Your Problem Podcast", "RealLifeLore"):
            self.assertIn(m.norm(n), {m.norm(c) for c in m.HISTORY_CHANNELS})
        for n in ("Godsplaining | Catholic Podcast", "The Pope Leo's Path", "DeepBeliever"):
            self.assertIn(m.norm(n), {m.norm(c) for c in m.BELIEF_CHANNELS})
        owned = {m.norm(c) for chans in OWNERS.values() for c in chans}
        for n in m.UNASSIGNED_CHANNELS:
            self.assertNotIn(m.norm(n), owned, n)
        for subj, names in {"military": ("Ukraine News NowUA", "Professor Gerdes Explains 🇺🇦", "Shawn Ryan Show"),
                            "screen": ("The Onion", "Jomboy Media", "R2-D2 SPEAKS! The REAL hero of STAR WARS"),
                            "music": ("djvlad", "Letters Live", "The Streets"),
                            "cars": ("GarageInsiderTV. com", "RR&BD Driving School", "Quick Shift"),
                            "tech": ("styropyro", "Williston Audio Labs", "BocaBola")}.items():
            for n in names:
                self.assertIn(m.norm(n), {m.norm(c) for c in LISTS[subj]}, (subj, n))

    def test_each_queries_its_own_channels_and_publishes_to_its_section(self):
        for subj in NEW2:
            rc, nj, cur = run_main(["--subject", subj], videos=[vrow("x", "v1", chan=SAMPLE2[subj])])
            self.assertEqual(rc, 0, subj)
            want = {m.norm(c) for c in (*m.SUBJECTS[subj]["channels"], *m.SUBJECTS[subj].get("shared_channels", ()))}
            self.assertTrue({m.norm(c) for c in LISTS[subj]} <= want, subj)
            self.assertEqual(set(cur.calls[0][1][1]), want, subj)
            a = nj.publish_hugo.call_args.args
            self.assertTrue(a[0].startswith(m.SUBJECTS[subj]["name"] + ": "), a[0])
            self.assertEqual(a[2], subj)
            self.assertEqual(nj.git_push.call_args.args[0], subj)

    def test_headings_match_homepage_kinds_series_and_seasoning(self):
        import nova_lexicon
        import nova_sections
        import nova_series
        for subj in NEW2:
            self.assertEqual(m.FEATURES[subj][0], HEADINGS2[subj])
            self.assertEqual(nova_series.TOP_BLOCK_KINDS[subj], HEADINGS2[subj])
            self.assertIn(subj, nova_lexicon.FLAVOR_SECTIONS)
            self.assertIn(subj, nova_sections.LIVE_SECTIONS)
            name = m.SUBJECTS[subj]["name"]
            self.assertEqual(nova_series.assign_series(subj, f"{name}: Some Day", []), name)

    def test_history_block_grounds_in_general_history_notes(self):
        seen = []
        notes = [hrow("The museum opened its doors on October 19, 1781, to great crowds. More.", title="Museum")]
        text = f"On this day a museum opened — [Museum]({WIKI}). " + "history " * 40
        with patch.object(m.nj, "today_str", return_value="2026-10-19"):
            rc, nj, cur = run_main(["--subject", "history"], videos=[vrow("x", "v1", chan="OverSimplified")],
                                   history=notes, llm=block_llm(text, seen))
        self.assertEqual(rc, 0)
        sql, params = next(c for c in cur.calls if "on_this_day" in c[0])
        self.assertEqual(params[0], ["world_history", "history"])
        self.assertIs(params[1], False)          # no military_doctrine manuals for general history
        system, user = next(x for x in seen if "very TOP" in x[0])
        self.assertIn("THIS DAY IN HISTORY", user)
        self.assertIn("October 19", system)
        body = published_body(nj)
        self.assertTrue(body.startswith("## 🏛️ This Day in History"))
        self.assertIn("### This Day in History", body)

    def test_history_prefers_non_military_events(self):
        rows = [hrow("The army crossed the river on 19 October 1805. Then peace.", url="https://w/army"),
                hrow("A great library opened on October 19, 1840, in the city.", url="https://w/library"),
                hrow("JP 3-01, Joint Doctrine, 19 October 1999. Army.", url="https://w/manual")]
        with patch.object(m.nj, "today_str", return_value="2026-10-19"):
            got = m.fetch_history(Cur([], [], history=rows), today="2026-10-19", military=False)
        self.assertEqual([g["url"] for g in got], ["https://w/library", "https://w/army"])

    def test_belief_text_in_context_verified_cited_and_listed(self):
        seen = []
        block = verse_block("The LORD is my shepherd; I shall not want.", "Psalms 23:1 (KJV)")
        rc, nj, cur = run_main(["--subject", "belief"], videos=[vrow("trust and the shepherd", "v1", chan="Ascension Presents",
                                                                     title="The Good Shepherd")],
                               scripture=[srow(PSALM, PSALM_MD, "bible"), srow(DHP, DHP_MD, "buddhism")],
                               llm=block_llm(block, seen))
        self.assertEqual(rc, 0)
        body = published_body(nj)
        self.assertTrue(body.startswith("## 📜 Text in Context"), body[:80])
        self.assertIn("### Scripture", body)
        self.assertIn("Psalms 23:1-4 (KJV)", body.split("## Sources")[1])
        system, user = next(x for x in seen if "very TOP" in x[0])
        self.assertIn("SCRIPTURE LIBRARY", user)
        self.assertIn("CITE AS: Psalms 23:1-4 (KJV)", user)
        self.assertIn("never mocking believers", system)
        sql = next(c[0] for c in cur.calls if "scripture_library" in c[0])
        self.assertIn("metadata->>'type' IN ('scripture', 'philosophy')", sql)

    def test_belief_invented_or_misattributed_scripture_is_never_published(self):
        cases = {"invented": verse_block("Blessed are the debuggers, for they shall ship on time.", "Psalms 23:1 (KJV)"),
                 "wrong verse": verse_block("The LORD is my shepherd; I shall not want.", "Psalms 23:4 (KJV)"),
                 "uncited": verse_block("The LORD is my shepherd; I shall not want.", "an old song"),
                 "paraphrase": verse_block("The Lord is my shepherd, so I lack nothing at all.", "Psalms 23:1 (KJV)"),
                 "no quote": "Psalms 23:1 is lovely — [Ascension Presents — T](" + YT1 + "). " + "reflect " * 40}
        for name, block in cases.items():
            rc, nj, _ = run_main(["--subject", "belief"], videos=[vrow("x", "v1", chan="Ascension Presents")],
                                 scripture=[srow(PSALM, PSALM_MD, "bible")], llm=block_llm(block))
            self.assertEqual(rc, 0, name)
            self.assertTrue(published_body(nj).startswith("## News"), name)

    def test_verse_ok_unit(self):
        items = [{"_source": "bible", "_md": PSALM_MD, "text": PSALM, "title": "Psalms 23:1-4 (KJV)"},
                 {"_source": "buddhism", "_md": DHP_MD, "text": DHP, "title": "Dhammapada — Chapter 1"}]
        self.assertTrue(m.verse_ok("> he leadeth me beside the still waters.\n\nPsalms 23:2 (KJV)", items)[0])
        self.assertTrue(m.verse_ok("> Yea, though I walk through the valley of the shadow of death\n\nPsalms 23:3-4", items)[0])
        self.assertTrue(m.verse_ok("> All that we are is the result of what we have thought\n\n— Dhammapada, ch. 1", items)[0])
        self.assertFalse(m.verse_ok("> All that we are is the result of what we have thought\n\n— the Buddha", items)[0])
        self.assertFalse(m.verse_ok("> he leadeth me beside the still waters.\n\nPsalms 23:4 (KJV)", items)[0])
        self.assertEqual(m._bible_verses(PSALM)[2][:22], "He maketh me to lie do")

    def test_fetch_scripture_caps_per_tradition_and_labels(self):
        rows = [srow(PSALM.replace("1-4", f"1-{i}"), {**PSALM_MD, "verses": f"1-{i}"}, "bible") for i in range(5, 9)] + \
               [srow("[Nietzsche — The Joyful Wisdom §1] a shepherd of thought " * 3,
                     {"url": "https://www.gutenberg.org/ebooks/52881", "work": "The Joyful Wisdom", "type": "philosophy",
                      "tradition": "19th-century German philosophy"}, "nontheism")]
        vids = [{"title": "The Good Shepherd and the still waters", "text": "shepherd shepherd", "channel": "c",
                 "url": "u", "date": "d"}]
        got = m.fetch_scripture(Cur([], [], scripture=rows), vids, today="2026-10-19")
        self.assertEqual(sum(g["_source"] == "bible" for g in got), m.SCRIPTURE_PER_SOURCE)
        nt = next(g for g in got if g["_source"] == "nontheism")
        self.assertIn("philosophy, not scripture", nt["channel"])
        self.assertTrue(all(g["title"] for g in got))

    def test_belief_tone_reaches_issue_and_block(self):
        seen = []
        run_main(["--subject", "belief"], videos=[vrow("x", "v1", chan="Sacred Logic")],
                 scripture=[srow(PSALM, PSALM_MD, "bible")], llm=block_llm("NONE", seen))
        self.assertTrue(seen)
        for system, _ in seen:
            self.assertIn("never ranking religions", system)
            self.assertIn("historian and anthropologist of religion", system)   # study, not devotion
            self.assertIn("PRIMARY SOURCES about believers", system)
            self.assertIn("skeptics", system)

    def test_belief_is_study_not_devotion(self):
        heading, task = m.FEATURES["belief"]
        self.assertEqual(heading, "📜 Text in Context")
        for need in ("who wrote it and when", "first audience", "read today", "skeptic", "not devotion"):
            self.assertIn(need, task)
        self.assertNotIn("faith", m.SUBJECTS)
        self.assertEqual(m.SUBJECTS["belief"]["name"], "Belief and Friends")
        self.assertIn("nontheism", m.SCRIPTURE_SOURCES)

    def test_belief_scope_is_balanced(self):
        feeds = {m.norm(c) for c in (*m.SUBJECTS["belief"]["channels"], *m.SUBJECTS["belief"]["shared_channels"])}
        for n in ("ReligionForBreakfast", "Dan McClellan", "Alex O'Connor", "Bishop Robert Barron"):
            self.assertIn(m.norm(n), feeds, n)   # scholars, a skeptic and the theological counter-case
        self.assertFalse({m.norm(c) for c in m.BELIEF_DIRECT_CHANNELS} & {m.norm(c) for c in m.BELIEF_CHANNELS})
        self.assertTrue({"ESOTERICA", "CrashCourse", "YaleCourses"} <= set(m.SUBJECTS["belief"]["shared_channels"]))
        owned = {m.norm(c) for chans in OWNERS.values() for c in chans}
        topic_only = {m.norm(c) for c in m.nova_yt_direct.for_section("belief", "topic")}
        for c in m.SUBJECTS["belief"]["shared_channels"]:   # owned elsewhere, or a broad direct channel
            self.assertTrue(m.norm(c) in owned or m.norm(c) in topic_only, c)
        self.assertFalse(topic_only & {m.norm(c) for c in m.SUBJECTS["belief"]["channels"]})
        science, mystery = {m.norm(c) for c in m.SCIENCE_CHANNELS}, {m.norm(c) for c in m.MYSTERY_CHANNELS}
        self.assertIn(m.norm("CrashCourse"), science)    # shared, still owned by their own sections
        self.assertIn(m.norm("ESOTERICA"), mystery)

    def test_scope_covers_the_worlds_traditions(self):
        feeds = {m.norm(c) for c in m.SUBJECTS["belief"]["channels"]}
        for tradition, names in {"buddhism": ("Plum Village App", "Dalai Lama", "Doug's Dharma"),
                                 "hinduism": ("Oxford Centre for Hindu Studies", "Vedanta Society of New York"),
                                 "sikh/jain": ("Sikh Research Institute (SikhRI)", "International School for Jain Studies"),
                                 "taoism": ("Benebell Wen",),
                                 "judaism": ("Henry Abramson", "Chabad.org", "The Jewish Theological Seminary",
                                             "The Movement for Reform Judaism", "Frieda Vizel"),
                                 "islam": ("Dr. Javad T. Hashmi", "Yasir Qadhi", "Zaytuna College",
                                           "Sayed Ammar Nakshawani")}.items():
            for n in names:
                self.assertIn(m.norm(n), feeds, f"{tradition}: {n}")

    def test_focus_family_rotates_across_all_five(self):
        days = [f"2026-10-{d:02d}" for d in range(19, 24)]
        self.assertEqual({m.focus_family(d)[0] for d in days},
                         {"Abrahamic", "Dharmic", "East Asian", "Non-theist and ancient",
                          "New religious movements and sects"})
        fam_srcs = {s for _, srcs in m.TRADITION_FAMILIES for s in srcs}
        self.assertEqual(fam_srcs, set(m.SCRIPTURE_SOURCES))   # every library source belongs to a family

    def test_focus_family_passages_are_offered_first(self):
        day = next(f"2026-10-{d:02d}" for d in range(19, 23) if m.focus_family(f"2026-10-{d:02d}")[0] == "Dharmic")
        rows = [srow(PSALM.replace("1-4", f"1-{i}"), {**PSALM_MD, "verses": f"1-{i}"}, "bible") for i in range(5, 9)] + \
               [srow(DHP, DHP_MD, "buddhism")]
        vids = [{"title": "The Good Shepherd and the still waters", "text": "shepherd shepherd", "channel": "c",
                 "url": "u", "date": "d"}]
        got = m.fetch_scripture(Cur([], [], scripture=rows), vids, today=day)
        self.assertEqual(got[0]["_source"], "buddhism")   # Bible matches the words better; the family leads anyway

    def test_block_task_names_todays_family(self):
        seen = []
        run_main(["--subject", "belief"], videos=[vrow("x", "v1", chan="Sacred Logic")],
                 scripture=[srow(PSALM, PSALM_MD, "bible")], llm=block_llm("NONE", seen))
        _, user = next(x for x in seen if "very TOP" in x[0])
        system = next(x for x in seen if "very TOP" in x[0])[0]
        self.assertIn("BALANCE: today's focus is the " + m.focus_family("2026-10-19")[0], system + user)

    def test_nrm_day_with_no_library_family_still_offers_passages(self):
        day = next(f"2026-10-{d:02d}" for d in range(19, 24)
                   if m.focus_family(f"2026-10-{d:02d}")[0] == "New religious movements and sects")
        got = m.fetch_scripture(Cur([], [], scripture=[srow(PSALM, PSALM_MD, "bible")]),
                                [{"title": "shepherd", "text": "shepherd shepherd", "channel": "c", "url": "u",
                                  "date": "d"}], today=day)
        self.assertTrue(got)

    def test_nrm_videos_capped_per_issue(self):
        nrm = m.BELIEF_NRM_CHANNELS[0]
        vids = [vrow(f"mormon history and the latter-day saints {i}", f"n{i}", chan=nrm, title=f"N{i}")
                for i in range(m.NRM_MAX + 5)] + [vrow("buddhist monks", "w1", chan="Plum Village App", title="W1")]
        got, _ = m.fetch(Cur(vids, []), m.SUBJECTS["belief"], 1)
        self.assertEqual(sum(v["channel"] == nrm for v in got), m.NRM_MAX)
        self.assertIn("W1", [v["title"] for v in got])

    def test_topic_only_direct_channels_are_filtered(self):
        cur = Cur([vrow("vietnamese pork and cabbage salad", "m1", chan="Mennonite Central Committee", title="Salad"),
                   vrow("mennonite peace churches and anabaptist history", "m2", chan="Mennonite Central Committee",
                        title="Anabaptists")], [])
        got, _ = m.fetch(cur, m.SUBJECTS["belief"], 1)
        self.assertEqual([v["title"] for v in got], ["Anabaptists"])

    def test_neutral_on_sects_never_ranks_doctrine(self):
        for phrase in ("never as a verdict on the doctrine", "Never call any group's doctrine 'wrong'",
                       "never promote, recruit for", "only through scholars, journalists and former members"):
            self.assertIn(phrase, m.BELIEF_TONE)
        for bad in ("wrong doctrine", "not as usable to join"):
            self.assertNotIn(bad, m.SRC if hasattr(m, "SRC") else SRC)

    def test_videos_cited_by_recent_issues_are_not_retold(self):
        import tempfile
        root = Path(tempfile.mkdtemp()); (root / "content" / "belief").mkdir(parents=True)
        (root / "content" / "belief" / "2026-10-19-belief.md").write_text(
            "## Sources\n- [x](https://www.youtube.com/watch?v=v1AAAAAAAAA)\n")
        cur = Cur([vrow("buddhist monks", "v1AAAAAAAAA", chan="Plum Village App", title="Old"),
                   vrow("buddhist monks", "v2BBBBBBBBB", chan="Plum Village App", title="New")], [])
        with patch.object(m.nj, "HUGO_ROOT", root):
            got, _ = m.fetch(cur, m.SUBJECTS["belief"], 1)
        self.assertEqual([v["title"] for v in got], ["New"])
        with patch.object(m.nj, "HUGO_ROOT", root / "missing"):   # unreadable repo: nothing skipped
            self.assertEqual(len(m.fetch(cur, m.SUBJECTS["belief"], 1)[0]), 2)

    def test_shared_channels_feed_belief_only_on_religion_topics(self):
        cur = Cur([vrow("photosynthesis and the light reactions", "c1", chan="CrashCourse", title="Photosynthesis"),
                   vrow("is atheism a religion? religious demographics and the new atheism", "c2",
                        chan="CrashCourse", title="Is Atheism a Religion?: Crash Course Religions #13"),
                   vrow("the gnostic demiurge in early christian texts", "e1", chan="ESOTERICA", title="The Demiurge"),
                   vrow("anything at all", "a1", chan="Ascension Presents", title="Homily")], [])
        vids, _ = m.fetch(cur, m.SUBJECTS["belief"], 1)
        self.assertEqual(sorted(v["title"] for v in vids),
                         sorted(["Is Atheism a Religion?: Crash Course Religions #13", "The Demiurge", "Homily"]))
        sci, _ = m.fetch(cur, m.SUBJECTS["science"], 1)   # science still gets every CrashCourse video
        self.assertIn("Photosynthesis", [v["title"] for v in sci])

    def test_history_gets_the_stance_only_when_the_war_comes_up(self):
        for text, want in (("the fall of Rome", False), ("how Russia's invasion of Ukraine began", True)):
            seen = []
            run_main(["--subject", "history"], videos=[vrow(text, "v1", chan="RealLifeLore")],
                     llm=block_llm(good_block(), seen))
            self.assertEqual(any(m.MILITARY_STANCE in sy for sy, _ in seen), want, text)

    def test_scripture_fetch_fast_on_big_input(self):
        rows = [srow(PSALM, PSALM_MD, "bible")] * 400
        vids = [{"title": "shepherd " * 50, "text": "waters " * 5000, "channel": "c", "url": "u", "date": "d"}]
        t0 = time.time()
        m.fetch_scripture(Cur([], [], scripture=rows), vids, today="2026-10-19")
        self.assertLess(time.time() - t0, 3.0)


if __name__ == "__main__":
    unittest.main()
