#!/usr/bin/env python3
"""Tests for nova_monthly_wrap.py — the 7 house categories (Unit, Security, Performance,
Retry, Integration, Functional, Frame). Every model / image / git / PG / notify call is
mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
from json import loads as json_loads
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mw = _load("nova_monthly_wrap_t", SCRIPTS / "nova_monthly_wrap.py")
SRC = (SCRIPTS / "nova_monthly_wrap.py").read_text()
_TMP = Path(tempfile.mkdtemp())
mw.CONTENT_ROOT = _TMP / "content"
mw.IMAGES_ROOT = _TMP / "static/images"
mw.LOG_FILE = _TMP / "wrap.log"
mw._TAP["installed"] = True            # never wrap the real nova_journal.log in tests
mw.system_prompt = lambda ctx="", **k: "VOICE\n" + ctx   # real one reads PG

DRAFT = "SUBTITLE: Five Hundred Posts and a Grudge\n\n## The month\n\n" + ("word " * 3000)


class FakePG:
    """service_config stand-in: {(service, key): json}."""
    rows: dict = {}
    fail = False

    def __call__(self):
        if FakePG.fail:
            raise OSError("pg down")
        return self

    def cursor(self):
        pg = self

        class Cur:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params):
                s.sql = sql
                if sql.lstrip().upper().startswith("SELECT"):
                    svc, like = params
                    pre = like.rstrip("%")
                    s.res = [(k,) for (sv, k) in FakePG.rows if sv == svc and k.startswith(pre)]
                else:
                    svc, key, val = params
                    FakePG.rows[(svc, key)] = val

            def fetchall(s):
                return s.res
        return Cur()

    def commit(self):
        pass

    def close(self):
        pass


def _reset():
    shutil.rmtree(mw.CONTENT_ROOT, ignore_errors=True)
    FakePG.rows, FakePG.fail = {}, False
    mw._pg = FakePG()
    mw.call_openrouter = MagicMock(return_value=DRAFT)
    mw.generate_image = MagicMock(return_value=None)
    mw.git_push = MagicMock(return_value="pushed")
    mw.nova_notify = MagicMock()

    def fake_publish(title, body, section, tags, description, image_path=None, emoji="",
                     stable_slug=None, sources=None, profile=None, min_words=None, **k):
        p = mw.CONTENT_ROOT / section / f"{stable_slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\ntitle: "{title}"\ndate: 2026-10-01T13:30:00-07:00\n'
                     f'tags: {tags}\n---\n\n{body}\n')
        return True
    mw.publish_hugo = MagicMock(side_effect=fake_publish)
    mw.RUN_BUDGET_S = 3000
    mw.WORKERS = 3


def _art(section, name, d, title="A piece", tags='["x"]', body="Body text here.", desc=""):
    p = mw.CONTENT_ROOT / section / name
    p.parent.mkdir(parents=True, exist_ok=True)
    extra = f'description: "{desc}"\n' if desc else ""
    p.write_text(f'---\ntitle: "{title}"\ndate: {d.isoformat()}T09:00:00-07:00\n'
                 f'tags: {tags}\n{extra}---\n\n*Published Monday*\n\n{body}\n')
    return p


def _q():
    return redirect_stdout(io.StringIO())


# ── Unit ─────────────────────────────────────────────────────────────────────

class TestUnit(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_parse_month(self):
        self.assertEqual(mw.parse_month("2026-09"), (2026, 9))
        for bad in ("2026-13", "2026-9", "26-09", "", "2026-00", "2026-09-01"):
            with self.assertRaises(ValueError):
                mw.parse_month(bad)

    def test_default_month_is_previous_calendar_month(self):
        self.assertEqual(mw.default_month(date(2026, 10, 1)), "2026-09")
        self.assertEqual(mw.default_month(date(2026, 10, 6)), "2026-09")
        self.assertEqual(mw.default_month(date(2027, 1, 1)), "2026-12")
        self.assertEqual(mw.default_month(date(2026, 3, 31)), "2026-02")

    def test_labels_and_slug(self):
        self.assertEqual(mw.month_label("2026-09"), "September 2026")
        self.assertEqual(mw.section_label("tech-today"), "Tech Today")
        self.assertEqual(mw.wrap_slug("2026-09", "dreams"), "2026-09-dreams-monthly-wrap")
        self.assertEqual(mw.wrap_url("2026-09", "local"),
                         "https://nova.digitalnoise.net/local/2026-09-local-monthly-wrap/")

    def test_list_sections_skips_retired_and_meta(self):
        for s in ("operations", "dreams", "art", "after-dark", "pilot", "meta", "about",
                  "start-here", "rando", "_hidden"):
            (mw.CONTENT_ROOT / s).mkdir(parents=True, exist_ok=True)
        self.assertEqual(mw.list_sections(), ["dreams", "operations"])

    def test_collect_filters_month_and_roundups(self):
        _art("operations", "2026-09-02-a.md", date(2026, 9, 2), "Real one")
        _art("operations", "2026-08-31-b.md", date(2026, 8, 31), "August")
        _art("operations", "2026-10-01-c.md", date(2026, 10, 1), "October")
        _art("operations", "2026-09-07-w.md", date(2026, 9, 7), "This Week in Operations: Sep",
             tags='["operations", "weekly-summary"]')
        _art("operations", "2026-08-monthly.md", date(2026, 9, 1), "Monthly Wrap: Ops",
             tags='["monthly-wrap"]')
        _art("synthesis", "2026-09-06-twimh.md", date(2026, 9, 6), "🧵 This Week in My Head: Aug 30")
        arts = mw.collect_month_articles("operations", "2026-09")
        self.assertEqual([a["title"] for a in arts], ["Real one"])
        self.assertEqual(arts[0]["url"],
                         "https://nova.digitalnoise.net/operations/2026-09-02-a/")
        self.assertNotIn("*Published", arts[0]["body"])
        # synthesis' own weekly column is real content, not a recap
        self.assertEqual(len(mw.collect_month_articles("synthesis", "2026-09")), 1)

    def test_build_sources_lists_every_post_with_url(self):
        for i in range(1, 6):
            _art("essays", f"2026-09-0{i}-e{i}.md", date(2026, 9, i), f"Essay {i}",
                 body="deep " * 5000, desc=f"about {i}")
        arts = mw.collect_month_articles("essays", "2026-09")
        src = mw.build_sources("essays", "2026-09", arts)
        self.assertLessEqual(len(src), mw.SOURCES_BUDGET)
        for a in arts:
            self.assertIn(a["url"], src)
            self.assertIn(a["title"], src)
        self.assertIn("POSTS PUBLISHED IN THIS SECTION THIS MONTH: 5", src)


# ── Security ─────────────────────────────────────────────────────────────────

class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_home_paths(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("/Users/" + "kochj/", SRC)

    def test_month_and_section_injection_rejected(self):
        for bad in ("2026-09/../../etc", "2026-09; rm -rf /", "../2026-09"):
            with self.assertRaises(ValueError):
                mw.wrap_slug(bad, "ops")
        for bad in ("../etc", "a/b", "ops;ls", "", "A"):
            with self.assertRaises(ValueError):
                mw.wrap_slug("2026-09", bad)

    def test_unknown_section_is_ignored(self):
        _reset()
        _art("dreams", "2026-09-03-d.md", date(2026, 9, 3), "Dream")
        with _q():
            rep = mw.run("2026-09", ["../../etc", "dreams"])
        self.assertEqual([r["section"] for r in rep["published"]], ["dreams"])

    def test_sql_is_parameterized(self):
        for m in re.finditer(r"cur\.execute\((.{0,400}?)\)\n", SRC, re.S):
            self.assertNotIn("f\"", m.group(1)[:3])
            self.assertIn("%s", m.group(1))


# ── Performance ──────────────────────────────────────────────────────────────

class TestPerformance(unittest.TestCase):
    def test_big_month_sources_fast_and_bounded(self):
        _reset()
        arts = [{"title": f"Post {i}", "date": date(2026, 9, 1 + i % 28), "name": f"{i}.md",
                 "url": f"https://x/{i}/", "description": "d" * 300, "body": "w " * (i % 900 + 50),
                 "words": i % 900 + 50, "tags": []} for i in range(2000)]
        t0 = time.monotonic()
        src = mw.build_sources("operations", "2026-09", arts)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertLessEqual(len(src), mw.SOURCES_BUDGET)
        self.assertLessEqual(src.count("Excerpt:"), mw.MAX_SOURCE_ARTICLES)
        self.assertIn("2000", src)

    def test_sections_run_concurrently_bounded_by_workers(self):
        _reset()
        for s in ("digests", "dreams", "essays", "local", "opinions"):
            _art(s, "2026-09-03-x.md", date(2026, 9, 3), f"{s} post")
        live, peak, lock = [0], [0], threading.Lock()

        def slow(*a, **k):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.15)
            with lock:
                live[0] -= 1
            return DRAFT
        mw.call_openrouter = MagicMock(side_effect=slow)
        mw.WORKERS = 2
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(len(rep["published"]), 5)
        self.assertEqual(peak[0], 2)


# ── Retry ────────────────────────────────────────────────────────────────────

class TestRetry(unittest.TestCase):
    def setUp(self):
        _reset()
        _art("dreams", "2026-09-03-d.md", date(2026, 9, 3), "Dream")

    def test_draft_falls_back_to_haiku(self):
        mw.call_openrouter = MagicMock(side_effect=[None, DRAFT])
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(len(rep["published"]), 1)
        models = [c.kwargs["model"] for c in mw.call_openrouter.call_args_list]
        self.assertEqual(models, [mw.DRAFT_MODEL, mw.DRAFT_FALLBACK_MODEL])

    def test_short_drafts_fail_the_section(self):
        mw.call_openrouter = MagicMock(return_value="too short")
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(rep["failed"], ["dreams"])
        mw.publish_hugo.assert_not_called()
        mw.git_push.assert_not_called()

    def test_image_failure_non_fatal(self):
        mw.generate_image = MagicMock(side_effect=RuntimeError("comfy down"))
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(len(rep["published"]), 1)
        self.assertFalse(rep["published"][0]["image"])

    def test_cover_retries_balanced_tier_once(self):
        mw.generate_image = MagicMock(side_effect=[None, "/tmp/x.png"])
        with _q():
            rep = mw.run("2026-09")
        self.assertTrue(rep["published"][0]["image"])
        self.assertEqual([c.kwargs["section"] for c in mw.generate_image.call_args_list],
                         ["dreams", "default"])

    def test_publish_refusal_not_marked_done(self):
        mw.publish_hugo = MagicMock(return_value=False)
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(rep["failed"], ["dreams"])
        self.assertEqual(FakePG.rows, {})
        mw.git_push.assert_not_called()

    def test_pg_down_still_dedups_on_wrap_file(self):
        with _q():
            mw.run("2026-09")
        FakePG.fail = True
        mw.publish_hugo.reset_mock()
        with _q():
            rep = mw.run("2026-09")
        mw.publish_hugo.assert_not_called()
        self.assertEqual(rep["published"], [])

    def test_image_generation_is_capped(self):
        with _q():
            mw.run("2026-09")
        self.assertEqual(mw.nova_image_utils.TIMEOUT, mw.IMAGE_TIMEOUT_S)
        self.assertEqual(mw.nova_image_utils.MAX_RETRIES, mw.IMAGE_MAX_RETRIES)


# ── Integration ──────────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    def test_reuses_journal_pipeline(self):
        for name in ("publish_hugo", "git_push", "call_openrouter", "generate_image"):
            self.assertIn(name, SRC)
        self.assertIn("from nova_journal_weekly_summary import _parse_article", SRC)

    def test_publish_hugo_gets_grounding_args(self):
        _reset()
        _art("essays", "2026-09-15-road.md", date(2026, 9, 15), "The Road to Sentience",
             body="cron jobs in a trenchcoat " * 50)
        with _q():
            mw.run("2026-09")
        kw = mw.publish_hugo.call_args.kwargs
        self.assertEqual(kw["profile"], "monthly-wrap")
        self.assertEqual(kw["min_words"], 5000)
        self.assertEqual(kw["stable_slug"], "2026-09-essays-monthly-wrap")
        self.assertIn("The Road to Sentience", kw["sources"])
        self.assertIn("https://nova.digitalnoise.net/essays/2026-09-15-road/", kw["sources"])
        self.assertIn("trenchcoat", kw["sources"])
        title = mw.publish_hugo.call_args.args[0]
        self.assertEqual(title, "Essays — September 2026: Five Hundred Posts and a Grudge")
        self.assertNotIn("SUBTITLE", mw.publish_hugo.call_args.args[1])
        # the draft prompt carried the same sources the expander will see
        self.assertIn(kw["sources"], mw.call_openrouter.call_args.args[1])

    def test_profile_row_exists_in_journal(self):
        import nova_journal
        self.assertEqual(nova_journal.ARTICLE_LENGTH["monthly-wrap"][2], nova_journal.EXPAND_GROUNDED)
        self.assertLessEqual(mw.SOURCES_BUDGET, nova_journal.LONGFORM_SOURCES_MAX_CHARS)


# ── Functional ───────────────────────────────────────────────────────────────

class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset()
        _art("operations", "2026-09-01-o.md", date(2026, 9, 1), "Ops 1")
        _art("operations", "2026-09-20-o2.md", date(2026, 9, 20), "Ops 2")
        _art("local", "2026-09-04-l.md", date(2026, 9, 4), "Burbank")
        _art("dreams", "2026-08-30-d.md", date(2026, 8, 30), "August dream only")
        _art("art", "2026-09-05-a.md", date(2026, 9, 5), "Retired art")

    def test_one_wrap_per_live_section_with_posts_and_one_push(self):
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual([r["section"] for r in rep["published"]], ["local", "operations"])
        self.assertEqual(mw.git_push.call_count, 1)
        self.assertEqual(set(k for _, k in FakePG.rows), {"2026-09:local", "2026-09:operations"})
        self.assertTrue((mw.CONTENT_ROOT / "local/2026-09-local-monthly-wrap.md").exists())
        self.assertGreater(rep["published"][0]["final_words"], 2000)

    def test_rerun_is_deduped_via_state(self):
        with _q():
            mw.run("2026-09")
        # even if the file vanished, the PG row stops a second publish
        for p in mw.CONTENT_ROOT.glob("*/*-monthly-wrap.md"):
            p.unlink()
        mw.publish_hugo.reset_mock()
        mw.git_push.reset_mock()
        with _q():
            rep = mw.run("2026-09")
        mw.publish_hugo.assert_not_called()
        mw.git_push.assert_not_called()
        self.assertEqual(rep["published"], [])

    def test_partial_failure_publishes_the_rest(self):
        def flaky(system, user, **k):
            if "Burbank" in user:
                raise RuntimeError("boom")
            return DRAFT
        mw.call_openrouter = MagicMock(side_effect=flaky)
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(rep["failed"], ["local"])
        self.assertEqual([r["section"] for r in rep["published"]], ["operations"])
        self.assertEqual(mw.git_push.call_count, 1)
        self.assertTrue(any("incomplete" in c.args[0] for c in mw.nova_notify.call_args_list))

    def test_budget_defers_but_still_pushes(self):
        mw.WORKERS = 1
        mw.RUN_BUDGET_S = -1
        with _q():
            rep = mw.run("2026-09")
        self.assertEqual(sorted(rep["deferred"]), ["local", "operations"])
        mw.git_push.assert_not_called()

    def test_force_republishes_in_place_and_keeps_existing_cover(self):
        with _q():
            mw.run("2026-09", ["local"])
        wrap = mw.CONTENT_ROOT / "local/2026-09-local-monthly-wrap.md"
        wrap.write_text('---\ntitle: "old"\n---\n\n' + "short " * 100)
        cover = mw.IMAGES_ROOT / "local/2026-09-local-monthly-wrap.webp"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"RIFF-old-cover")
        mw.publish_hugo.reset_mock(); mw.generate_image.reset_mock(); mw.git_push.reset_mock()
        seen = {}
        orig = mw.publish_hugo.side_effect

        def capture(*a, **k):
            seen["img"] = Path(k["image_path"]).read_bytes()
            return orig(*a, **k)
        mw.publish_hugo.side_effect = capture
        with _q():
            rep = mw.run("2026-09", ["local"])                  # without --force: deduped
        mw.publish_hugo.assert_not_called()
        with _q():
            rep = mw.run("2026-09", ["local"], force=True)
        self.assertEqual([r["section"] for r in rep["published"]], ["local"])
        r = rep["published"][0]
        self.assertEqual(r["before_words"], 100)
        self.assertEqual(mw.publish_hugo.call_args.kwargs["stable_slug"], "2026-09-local-monthly-wrap")
        mw.generate_image.assert_not_called()                     # existing cover kept
        self.assertEqual(seen["img"], b"RIFF-old-cover")
        self.assertNotEqual(Path(mw.publish_hugo.call_args.kwargs["image_path"]), cover)  # temp copy, not itself
        self.assertFalse(Path(mw.publish_hugo.call_args.kwargs["image_path"]).exists())   # temp cleaned up
        self.assertEqual(len(list(wrap.parent.glob("*-monthly-wrap.md"))), 1)            # same file overwritten
        self.assertGreater(mw._written_words("local", "2026-09"), 2000)
        self.assertTrue(json_loads(FakePG.rows[("nova_monthly_wrap", "2026-09:local")])["republished"])
        self.assertEqual(mw.git_push.call_count, 1)
        # cover missing -> generated as usual
        cover.unlink(); mw.generate_image.reset_mock()
        with _q():
            mw.run("2026-09", ["local"], force=True)
        mw.generate_image.assert_called()

    def test_dry_run_never_writes(self):
        with _q():
            rep = mw.run("2026-09", dry_run=True)
        self.assertEqual(sorted(rep["would_wrap"]), ["local", "operations"])
        mw.call_openrouter.assert_not_called()
        mw.publish_hugo.assert_not_called()
        mw.git_push.assert_not_called()
        self.assertEqual(FakePG.rows, {})


# ── Frame ────────────────────────────────────────────────────────────────────

class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_cli_dry_run_exits_zero_with_default_month(self):
        _reset()
        with _q(), patch.object(mw, "default_month", return_value="2026-09") as dm:
            self.assertEqual(mw.main(["--dry-run"]), 0)
        dm.assert_called_once()
        mw.publish_hugo.assert_not_called()

    def test_cli_rejects_bad_month(self):
        with _q(), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                mw.main(["--month", "September"])
        self.assertEqual(cm.exception.code, 2)

    def test_cli_force_requires_section(self):
        with _q(), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                mw.main(["--month", "2026-09", "--force"])
        self.assertEqual(cm.exception.code, 2)
        _reset()
        with _q(), patch.object(mw, "run", return_value={"failed": [], "published": [1]}) as run:
            self.assertEqual(mw.main(["--month", "2026-09", "--section", "local", "--force"]), 0)
        self.assertTrue(run.call_args.kwargs["force"])

    def test_cli_generate_implies_dry_run(self):
        _reset()
        _art("dreams", "2026-09-03-d.md", date(2026, 9, 3), "Dream")
        with _q(), patch.object(mw.nova_journal, "longform_expand", side_effect=lambda t, b, *a, **k: b):
            self.assertEqual(mw.main(["--month", "2026-09", "--generate"]), 0)
        mw.publish_hugo.assert_not_called()
        mw.git_push.assert_not_called()
        self.assertEqual(FakePG.rows, {})


if __name__ == "__main__":
    unittest.main()
