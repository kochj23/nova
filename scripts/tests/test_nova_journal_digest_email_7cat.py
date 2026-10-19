#!/usr/bin/env python3
"""7-category tests for nova_journal_digest_email.py — the daily (all sections) and weekly (per section)
journal digest emails (Security, Performance, Retry, Unit, Integration, Functional, Frame). The site
fetch and the mail path (nova_send_mail.send_mail) are mocked — nothing is sent.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_journal_digest_email_7cat.py
"""
import sys
import tempfile
import time
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_journal_digest_email as m  # noqa: E402

SRC = (SCRIPTS / "nova_journal_digest_email.py").read_text()
D = date(2026, 10, 19)


def item(title, section, day="2026-10-19", summary="A summary."):
    return {"title": title, "url": f"/{section}/{title.lower().replace(' ', '-')}/", "section": section,
            "date": day, "summary": summary}


IDX = [item("BLUF today", "burbank-ops"), item("Big Story", "watches"), item("Buy this", "tech"),
       item("Old one", "tech", day="2026-10-14"), item("Ancient", "opinions", day="2026-09-01"),
       item("About", "about")]


def mail_mocks():
    sent = MagicMock(return_value=True)
    return {"nova_send_mail": types.SimpleNamespace(send_mail=sent),
            "nova_config": types.SimpleNamespace(JORDAN_EMAIL="jordan@example.test"),
            "nova_notify": types.SimpleNamespace(notify=MagicMock())}, sent


class TestSecurity(unittest.TestCase):
    def test_jordan_only_never_the_herd(self):
        self.assertNotIn("HERD_EMAILS", SRC)
        self.assertNotIn("smtplib", SRC)          # all mail goes through nova_send_mail / herd-mail
        mods, sent = mail_mocks()
        with patch.dict(sys.modules, mods), patch.object(m, "fetch_index", return_value=IDX):
            m.main(["--date", "2026-10-19"])
        self.assertEqual(sent.call_args.args[0], "jordan@example.test")
        self.assertEqual(sent.call_count, 1)

    def test_no_secrets_in_source(self):
        self.assertNotIn("app-password", SRC)
        self.assertNotIn("find-generic-password", SRC)


class TestPerformance(unittest.TestCase):
    def test_large_index_fast_and_capped(self):
        big = [item(f"Post {i}", "burbank-ops") for i in range(5000)]
        t = time.time()
        posts = m.collect(D, D, index=big)
        subject, body = m.build_daily(D, posts)
        self.assertLess(time.time() - t, 1.0)
        self.assertIn("…and 4975 more", body)


class TestRetry(unittest.TestCase):
    def test_site_down_falls_back_to_local_clone(self):
        d = Path(tempfile.mkdtemp())
        (d / "tech").mkdir()
        (d / "tech/2026-10-19-tech-and-friends.md").write_text('---\ntitle: "Tech and Friends: x"\n'
                                                               'description: "desc"\n---\nbody')
        posts = m.local_posts(D, D, content=d)
        self.assertEqual([p["title"] for p in posts], ["Tech and Friends: x"])
        with patch.object(m, "fetch_index", side_effect=OSError("down")), \
                patch.object(m, "local_posts", return_value=posts):
            self.assertEqual(len(m.collect(D, D)), 1)

    def test_old_index_shape_uses_local(self):
        old = [{"title": "x", "permalink": "https://n/x/", "summary": "", "content": "", "tags": []}]
        with patch.object(m, "fetch_index", return_value=old), \
                patch.object(m, "local_posts", return_value=[item("Local", "tech")]) as lp:
            self.assertEqual(m.collect(D, D)[0]["title"], "Local")
            lp.assert_called_once()

    def test_send_failure_exit_1(self):
        mods, sent = mail_mocks()
        sent.return_value = False
        with patch.dict(sys.modules, mods), patch.object(m, "fetch_index", return_value=IDX):
            self.assertEqual(m.main(["--date", "2026-10-19"]), 1)


class TestUnit(unittest.TestCase):
    def test_collect_date_window_and_skips(self):
        self.assertEqual({p["title"] for p in m.collect(D, D, index=IDX)}, {"BLUF today", "Big Story", "Buy this"})
        week = m.collect(date(2026, 10, 13), D, index=IDX)
        self.assertIn("Old one", {p["title"] for p in week})
        self.assertNotIn("About", {p["title"] for p in week})

    def test_group_order(self):
        order = [s for s, _ in m.group(m.collect(D, D, index=IDX + [item("Z", "zeta")]))]
        self.assertEqual(order, ["burbank-ops", "watches", "tech", "zeta"])

    def test_section_block_links_and_rss(self):
        b = m.section_block("tech", [item("Buy this", "tech")])
        self.assertIn("## Tech and Friends (1)", b)
        self.assertIn("(https://nova.digitalnoise.net/tech/buy-this/)", b)
        self.assertIn("https://nova.digitalnoise.net/tech/index.xml", b)


class TestIntegration(unittest.TestCase):
    def test_weekly_one_email_per_section(self):
        mods, sent = mail_mocks()
        with patch.dict(sys.modules, mods), patch.object(m, "fetch_index", return_value=IDX):
            self.assertEqual(m.main(["--weekly", "--date", "2026-10-19"]), 0)
        subjects = [c.args[1] for c in sent.call_args_list]
        self.assertEqual(len(subjects), 3)                     # burbank-ops, watches, tech
        self.assertTrue(all(c.kwargs.get("rich") for c in sent.call_args_list))
        self.assertTrue(any("Tech and Friends" in s and "(2)" in s for s in subjects))

    def test_weekly_single_section(self):
        mods, sent = mail_mocks()
        with patch.dict(sys.modules, mods), patch.object(m, "fetch_index", return_value=IDX):
            m.main(["--weekly", "--section", "tech", "--date", "2026-10-19"])
        self.assertEqual(sent.call_count, 1)


class TestFunctional(unittest.TestCase):
    def test_daily_dry_run_prints_all_sections(self):
        mods, sent = mail_mocks()
        with patch.dict(sys.modules, mods), patch.object(m, "fetch_index", return_value=IDX), \
                patch("builtins.print") as pr:
            self.assertEqual(m.main(["--date", "2026-10-19", "--dry-run"]), 0)
        out = "\n".join(str(c.args[0]) for c in pr.call_args_list if c.args)
        sent.assert_not_called()
        for s in ("Burbank and Ops", "Watches and Friends", "Tech and Friends", "3 posts"):
            self.assertIn(s, out)

    def test_quiet_day_sends_nothing(self):
        mods, sent = mail_mocks()
        with patch.dict(sys.modules, mods), patch.object(m, "fetch_index", return_value=[]):
            self.assertEqual(m.main(["--date", "2026-10-19"]), 0)
        sent.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_cli_and_docs(self):
        for s in ("--weekly", "--section", "--date", "--dry-run", "Written by Jordan Koch", "18:30"):
            self.assertIn(s, SRC)

    def test_today_is_pacific(self):
        self.assertIn("America/Los_Angeles", SRC)
        self.assertIsInstance(m.today_pt(), date)


if __name__ == "__main__":
    unittest.main()
