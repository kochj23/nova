#!/usr/bin/env python3
"""7-category tests for nova_friends.py — Cars / Tech / Food and Friends (Security, Performance,
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

    def __init__(self, videos, arts, recipes=()):
        self.videos, self.arts, self.recipes, self.calls, self._r = videos, arts, list(recipes), [], []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "source = 'recipes'" in sql:
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


def run_main(argv=("--subject", "cars"), videos=None, arts=None, llm=None, publish=True, recipes=(), rules=RULES):
    videos = [vrow("a rear wing that lifts the nose", "v1")] if videos is None else videos
    arts = [arow("new kernel release", "https://a/1")] if arts is None else arts
    cur = Cur(videos, arts, recipes)
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
        self.assertNotIn(m.norm("Parkflyers RC"), cars | tech)

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
        self.assertEqual(set(m.SUBJECTS), {"cars", "tech", "food"})
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

    def test_watches_big_story_is_the_shared_helper(self):
        self.assertIs(m.featured_block, w.featured_block)


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


if __name__ == "__main__":
    unittest.main()
