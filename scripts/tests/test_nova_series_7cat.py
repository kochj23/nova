#!/usr/bin/env python3
"""7-category tests for nova_series.py — running-story series + the *and Friends* top_block front matter
(Security, Performance, Retry, Unit, Integration, Functional, Frame). Works on temp Hugo trees and a
temp git repo; nothing touches ~/nova-journal and nothing is pushed.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_series_7cat.py
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_series as m  # noqa: E402

SRC = (SCRIPTS / "nova_series.py").read_text()


def art(title, tags, body="Body text.\n", extra=""):
    return (f'---\ntitle: "{title}"\ndate: 2026-10-19T09:00:00-07:00\ndraft: false\n'
            f'tags: {json.dumps(tags)}\n{extra}---\n\n*Published Monday*\n\n{body}')


BIG = ("## The Big Story\n\n**Rolex pulls the GMT** — [Watch Reporter](https://youtu.be/x) says the 1.2.3.4 "
       "dealer on nova-core2 confirmed it. More text follows here for the summary.\n\n## The Day in Watches\n\nRest.\n")


def tree(files):
    d = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        p = d / "content" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return d


class TestSecurity(unittest.TestCase):
    def test_top_block_scrubs_ips_hosts_and_paths(self):
        tb = m.extract_top_block("watches", BIG)
        self.assertNotIn("1.2.3.4", tb["summary"])
        self.assertNotIn("nova-core", tb["summary"].lower())
        self.assertIn("[redacted]", tb["summary"])
        self.assertEqual(m.md_plain("see /Users/someone/x and openclaw"), "see and Nova")

    def test_yaml_injection_is_quoted(self):
        new, _ = m.enrich_text(art("Watches and Friends: x", ["watches"], body=BIG.replace(
            "Rolex pulls the GMT", 'evil"\nmalicious: true')), "watches")
        fm, _ = m.split_front_matter(new)
        self.assertNotIn("\nmalicious: true", fm)

    def test_no_shell_and_explicit_paths_only(self):
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn('"add", "-A"', SRC)
        self.assertNotIn("stash", SRC.replace("--autostash", ""))


class TestPerformance(unittest.TestCase):
    def test_thousand_articles_under_two_seconds(self):
        d = tree({f"burbank-ops/2026-10-{i % 28 + 1:02d}-x{i}.md": art(f"LA County Emergency Recap — {i}",
                  ["emergency"]) for i in range(1000)})
        t = time.time()
        changed, series, _ = m.run(d, write=False)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(series["LA County Emergency Recap"], 1000)


class TestRetry(unittest.TestCase):
    def test_idempotent_second_run_changes_nothing(self):
        d = tree({"watches/2026-10-19-w.md": art("⌚ Watches and Friends: Hands", ["watches"], body=BIG)})
        c1, _, b1 = m.run(d, write=True)
        c2, _, b2 = m.run(d, write=True)
        self.assertEqual((len(c1), b1), (1, 1))
        self.assertEqual((c2, b2), ([], 0))

    def test_unreadable_file_is_skipped(self):
        d = tree({"opinions/2026-10-19-a.md": "x"})
        (d / "content/opinions/2026-10-19-b.md").write_bytes(b"\xff\xfe\x00bad")
        changed, _, _ = m.run(d, write=False)
        self.assertEqual(changed, [])

    def test_push_lock_unavailable_is_noop(self):
        with patch.dict(sys.modules, {"nova_journal": None}):
            acq, rel = m._push_lock()
            self.assertIsNone(acq())
            rel(None)


class TestUnit(unittest.TestCase):
    def test_rules(self):
        cases = [
            ("burbank-ops", "🛡️ NOVA PDB: SECURITY INTELLIGENCE BRIEFING, 19 OCT 2026", ["daily-briefing"],
             "Daily Security Briefing"),
            ("watches", "⌚ Watches and Friends: Gold Bridges", ["watches", "fishbowl"], "Watches and Friends"),
            ("cars", "🚗 Cars and Friends: Fires", [], "Cars and Friends"),
            ("tech", "💻 Tech and Friends: Cancel", [], "Tech and Friends"),
            ("food", "🍳 Food and Friends: Ribs", [], "Food and Friends"),
            ("watches", "🗣️ The Fishbowl Imported a Riot", ["opinion", "fishbowl"], "The Fishbowl"),
            ("burbank-ops", "🚒 LA County Emergency Recap — Saturday", ["emergency"], "LA County Emergency Recap"),
            ("burbank-ops", "What Flew Over Burbank This Week — October 19", ["burbank"], "What Flew Over Burbank"),
            ("burbank-ops", "📊 WEEK IN INTELLIGENCE — October 3–9", [], "Week in Intelligence"),
            ("opinions", "🎮 Impeached in 54 Turns", ["games", "gtnw"], "Game Reports"),
            ("tech", "📅 This Week in Tech Today: Oct 4–11", ["weekly-summary"], "This Week in Tech"),
            ("burbank-ops", "📅 This Week in Operations: June", ["weekly-summary"], "This Week in Operations"),
        ]
        for sec, title, tags, want in cases:
            self.assertEqual(m.assign_series(sec, title, tags), want, title)

    def test_conservative_misses(self):
        self.assertIsNone(m.assign_series("burbank-ops", "🛡️ BREAKING: something", ["breaking-alert"]))
        self.assertIsNone(m.assign_series("opinions", "Watches and Friends: wrong section", []))
        self.assertIsNone(m.assign_series("watches", "Watches are nice", ["fishbowl"]))   # not the column
        self.assertIsNone(m.assign_series("tech", "This Week in Tech", []))               # no weekly tag

    def test_top_block_fields(self):
        tb = m.extract_top_block("watches", BIG)
        self.assertEqual(tb["kind"], "The Big Story")
        self.assertEqual(tb["title"], "Rolex pulls the GMT")
        self.assertLessEqual(len(tb["summary"]), 301)
        self.assertIsNone(m.extract_top_block("tech", BIG))       # tech's block is Buying Recommendation
        self.assertIsNone(m.extract_top_block("opinions", BIG))

    def test_existing_series_never_overwritten(self):
        t = art("LA County Emergency Recap", ["emergency"], extra='series: ["Hand Set"]\n')
        self.assertEqual(m.enrich_text(t, "burbank-ops"), (t, []))


class TestIntegration(unittest.TestCase):
    def test_commit_only_clean_files_explicit_paths(self):
        d = tree({"burbank-ops/2026-10-18-a.md": art("LA County Emergency Recap — A", ["emergency"]),
                  "burbank-ops/2026-10-19-b.md": art("LA County Emergency Recap — B", ["emergency"])})
        g = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)  # noqa: E731
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        g("add", "-A"); g("commit", "-qm", "init")
        (d / "content/burbank-ops/2026-10-19-b.md").write_text(art("LA County Emergency Recap — B", ["emergency"],
                                                                     body="still being written"))
        (d / "content/other.txt").write_text("someone else's WIP")
        changed, _, _ = m.run(d, write=True, only_clean=True)
        self.assertEqual(changed, ["content/burbank-ops/2026-10-18-a.md"])
        m._git(d, "commit", "-q", "-m", "s", "--", *changed)
        st = g("status", "--porcelain").stdout
        self.assertIn("2026-10-19-b.md", st)      # the in-progress file is untouched and uncommitted
        self.assertIn("other.txt", st)
        self.assertIn("series:", g("show", "HEAD:content/burbank-ops/2026-10-18-a.md").stdout)


class TestFunctional(unittest.TestCase):
    def test_backfill_dry_run_writes_nothing_then_write(self):
        d = tree({"watches/2026-10-19-w.md": art("⌚ Watches and Friends: x", ["watches"], body=BIG),
                  "opinions/2026-10-19-o.md": art("A plain opinion", ["opinion"])})
        before = (d / "content/watches/2026-10-19-w.md").read_text()
        m.main(["--backfill", "--root", str(d)])
        self.assertEqual((d / "content/watches/2026-10-19-w.md").read_text(), before)
        m.main(["--backfill", "--root", str(d), "--write"])
        after = (d / "content/watches/2026-10-19-w.md").read_text()
        fm, body = m.split_front_matter(after)
        self.assertIn('series: ["Watches and Friends"]', fm)
        self.assertIn('kind: "The Big Story"', fm)
        self.assertTrue(body.startswith("\n*Published"))
        self.assertEqual((d / "content/opinions/2026-10-19-o.md").read_text(), art("A plain opinion", ["opinion"]))


class TestFrame(unittest.TestCase):
    def test_cli_contract(self):
        for flag in ("--backfill", "--sweep", "--stats", "--check", "--write", "--root", "--no-push"):
            self.assertIn(flag, SRC)
        self.assertIn("Written by Jordan Koch", SRC)

    def test_skips_special_dirs_and_index(self):
        d = tree({"about/_index.md": "x", "search.md": "x",
                  "tech/_index.md": art("Tech and Friends", []), "tech/2026-10-19-t.md": art("Tech and Friends: a", [])})
        self.assertEqual([p.name for _, p in m.content_files(d)], ["2026-10-19-t.md"])

    def test_no_front_matter_untouched(self):
        self.assertEqual(m.enrich_text("no front matter", "tech"), ("no front matter", []))


if __name__ == "__main__":
    unittest.main()
