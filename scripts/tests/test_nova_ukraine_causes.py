#!/usr/bin/env python3
"""Tests for nova_ukraine_causes.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401  (real module locked in before any patching)

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ukraine_causes.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


uc = _load("nova_ukraine_causes_t", SCRIPT)
# Never let a test reach Slack: stub the one outbound path for the whole module.
uc.nova_config = MagicMock(wraps=uc.nova_config)
uc.nova_config.post_both = MagicMock(return_value=True)
uc.nova_config.is_private_source = lambda s: s in ("email", "imessage")
uc.nova_config.JORDAN_DM = uc.POST_CHANNEL

ALLOW = [
    {"slug": "razom", "name": "Razom for Ukraine", "aliases": ["Razom"], "official_url": "https://www.razomforukraine.org/donate",
     "domains": ["razomforukraine.org"], "focus": "medical & humanitarian", "funds": "Tactical medicine.",
     "why": "Established NY nonprofit.", "us_501c3": True, "ein": "46-4604398", "tax_note": "", "verified_on": None},
    {"slug": "united24", "name": "UNITED24", "aliases": ["United24", "United 24"], "official_url": "https://u24.gov.ua/",
     "domains": ["u24.gov.ua"], "focus": "defense", "funds": "Drones.", "why": "Official platform.",
     "us_501c3": False, "ein": None, "tax_note": "Government platform.", "verified_on": None},
    {"slug": "come_back_alive", "name": "Come Back Alive", "aliases": ["Povernys Zhyvym"],
     "official_url": "https://savelife.in.ua/en/", "domains": ["savelife.in.ua"], "focus": "drones & defense",
     "funds": "Drones.", "why": "Big.", "us_501c3": False, "ein": None, "tax_note": "", "verified_on": None},
]


def _mem(i, text, title="", channel="Task & Purpose", source="geopolitics", url=""):
    return {"id": str(i), "source": source, "text": text, "channel": channel, "title": title, "url": url}


class FakeCursor:
    def __init__(self, conn): self.conn = conn; self._rows = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "FROM ukraine_cause_allowlist" in sql:
            cols = ("slug", "name", "aliases", "official_url", "domains", "focus", "funds", "why",
                    "us_501c3", "ein", "tax_note", "verified_on")
            self._rows = [tuple(o[c] for c in cols) for o in self.conn.allow]
        elif "FROM ukraine_cause_reports" in sql:
            self._rows = [(k,) for k in self.conn.already]
        elif "FROM memories" in sql:
            self._rows = [(m["id"], m["source"], m["text"], m["channel"], m["title"], m["url"]) for m in self.conn.mems]
        else:
            self._rows = []
    def fetchall(self): return self._rows


class FakeConn:
    def __init__(self, allow=ALLOW, mems=(), already=()):
        self.allow, self.mems, self.already, self.executed = list(allow), list(mems), set(already), []
        self.committed = False
    def cursor(self): return FakeCursor(self)
    def commit(self): self.committed = True
    def close(self): pass


def _connector(ops, mem):
    return lambda db: ops if db == "nova_ops" else mem


GOOD = [_mem(1, "Ukraine's medics need kits. You can donate to Razom for Ukraine, a registered 501(c)(3).",
             title="Frontline medics", url="https://example.test/a"),
        _mem(2, "Ukrainian charity Come Back Alive has handed frontline units 80 new interceptor drones, "
                "funded by donations.", title="Charity buys interceptors", channel="Defence Blog")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(xox[bpa]-|password\s*=|api[_-]?key\s*=\s*['\"]|Bearer\s+[A-Za-z0-9])", SRC))

    def test_sql_is_parameterized(self):
        for m in re.finditer(r"execute\((f?)\"", SRC):
            self.assertEqual(m.group(1), "", "f-string SQL found")
        self.assertNotIn("% (", SRC.replace(" % (", "% ("))  # no %-formatting into SQL strings

    def test_refuses_non_private_channels_and_never_touches_journal(self):
        with self.assertRaises(ValueError):
            uc.send_private("x", channel="C0AMNQ5GX70")      # #nova-chat is not private
        with self.assertRaises(ValueError):
            uc.send_private("x", channel="nova-journal")
        # No journal / Hugo / publishing / memory-write code paths are reachable from this module.
        code = re.sub(r'""".*?"""', "", SRC, flags=re.S)
        for forbidden in ("nova_journal", "nova_publish", "publish_hugo", "HUGO", "remember(", "nova_articles_to_memory",
                          "git push", "post_discord"):
            self.assertNotIn(forbidden, code)

    def test_post_never_mentions_jordans_own_donations(self):
        v, u = uc.find_candidates(GOOD, ALLOW)
        text = uc.format_post(uc.select_items(v, u, set()))
        self.assertIsNone(re.search(r"\b(you (?:gave|donated)|your (?:donation|gift)s?|thousands)\b", text, re.I))
        self.assertNotIn("nova.digitalnoise.net", text)


class TestPerformance(unittest.TestCase):
    def test_find_candidates_fast_on_10k_memories(self):
        import time
        mems = [_mem(i, ("Ukraine war update. " * 20) + ("donate to Razom for Ukraine. " if i % 50 == 0 else ""))
                for i in range(10_000)]
        t = time.perf_counter()
        v, _ = uc.find_candidates(mems, ALLOW)
        self.assertLess(time.perf_counter() - t, 10.0)
        self.assertEqual(v["razom"]["mentions"], 200)


class TestRetry(unittest.TestCase):
    def test_pg_failure_fails_open_and_posts_nothing(self):
        # RETRY GAP: run() — retries live inside nova_config.pg_connect; here we prove failure is safe.
        uc.nova_config.post_both.reset_mock()
        def boom(db): raise psycopg2.OperationalError("down")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=boom), 1)
        uc.nova_config.post_both.assert_not_called()

    def test_memory_scan_failure_posts_nothing(self):
        uc.nova_config.post_both.reset_mock()
        ops = FakeConn()
        def conn(db):
            if db == "nova_ops": return ops
            raise psycopg2.OperationalError("down")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=conn), 1)
        uc.nova_config.post_both.assert_not_called()

    def test_dm_failure_falls_back_to_nova_info_once(self):
        uc.nova_config.post_both.reset_mock()
        uc.nova_config.post_both.side_effect = [False, True]
        try:
            with redirect_stderr(io.StringIO()):
                self.assertTrue(uc.send_private("hello"))
        finally:
            uc.nova_config.post_both.side_effect = None
        chans = [c.kwargs["slack_channel"] for c in uc.nova_config.post_both.call_args_list]
        self.assertEqual(chans, [uc.POST_CHANNEL, uc.INFO_CHANNEL])


class TestUnit(unittest.TestCase):
    def test_allowlist_matching_names_and_aliases(self):
        self.assertEqual(uc.match_allowlist("Give to United 24 today", ALLOW), ["united24"])
        self.assertEqual(uc.match_allowlist("povernys zhyvym and RAZOM", ALLOW), ["razom", "come_back_alive"])
        self.assertEqual(uc.match_allowlist("nothing here, razomania", ALLOW), [])
        self.assertEqual(uc.match_allowlist("", ALLOW), [])

    def test_unverified_flagging_and_red_flags(self):
        mems = [_mem(1, "Ukraine drone appeal: send bitcoin urgently to the Kyiv Drone Freedom Fund, act now!"),
                _mem(2, "Ukrainian soldiers need help, donate to Kyiv Drone Freedom Fund via crypto wallet address.",
                     channel="The Military Show")]
        v, u = uc.find_candidates(mems, ALLOW)
        self.assertEqual(v, {})
        item = u["u:kyiv drone freedom fund"]
        self.assertFalse(item["verified"])
        self.assertIn("urgency pressure", item["flags"])
        self.assertTrue(any(f.startswith("crypto") for f in item["flags"]))
        self.assertIn("no registration info seen", item["flags"])

    def test_impersonation_detected(self):
        self.assertEqual(uc.impersonates("Razom Ukraine Fund", ALLOW), "Razom for Ukraine")
        self.assertEqual(uc.impersonates("Come Back Alive Fund", ALLOW), "Come Back Alive")
        self.assertEqual(uc.impersonates("United24", ALLOW, "united24-donate.com"), "UNITED24")
        self.assertIsNone(uc.impersonates("Hope for Kharkiv Foundation", ALLOW))

    def test_select_requires_a_new_vetted_item(self):
        v, u = uc.find_candidates(GOOD, ALLOW)
        self.assertEqual(uc.select_items({}, u, set()), [])                  # unverified alone never posts
        self.assertEqual(uc.select_items(v, u, {"razom", "come_back_alive"}), [])   # all on cooldown
        self.assertLessEqual(len(uc.select_items(v, u, set(), max_items=1)), 1)

    def test_tax_lines(self):
        self.assertIn("likely — US 501(c)(3) (EIN 46-4604398)", uc.tax_line(ALLOW[0]))
        self.assertIn("unlikely", uc.tax_line(ALLOW[1]))
        self.assertIn("unknown", uc.tax_line(None))

    def test_format_empty_and_selftest(self):
        self.assertEqual(uc.format_post([]), "")
        with redirect_stdout(io.StringIO()):
            uc.demo()


class TestIntegration(unittest.TestCase):
    def test_uses_shared_helpers_and_right_tables(self):
        self.assertIn("nova_config.post_both", SRC)
        self.assertIn("nova_config.pg_connect", SRC)
        self.assertIn("nova_config.is_private_source", SRC)
        self.assertIn("ukraine_cause_allowlist", SRC)
        self.assertNotIn("def post_both", SRC)

    def test_scan_query_shape_and_private_sources_dropped(self):
        conn = FakeConn(mems=[_mem(1, "x"), _mem(2, "y", source="email")])
        rows = uc.fetch_memories(conn, days=7)
        self.assertEqual([r["id"] for r in rows], ["1"])
        sql, params = conn.executed[-1]
        self.assertIn("FROM memories", sql)
        self.assertEqual(params[0], 7)
        self.assertIn("Task & Purpose", params[2])
        self.assertIn("nova_articles", params[3])

    def test_candidates_to_post_chain(self):
        v, u = uc.find_candidates(GOOD, ALLOW)
        text = uc.format_post(uc.select_items(v, u, set()))
        self.assertIn("Razom for Ukraine", text)
        self.assertIn("https://www.razomforukraine.org/donate", text)
        self.assertIn("Seen in: Task & Purpose", text)
        self.assertIn("vetted", text)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        uc.nova_config.post_both.reset_mock()
        uc.nova_config.post_both.return_value = True

    def test_golden_path_posts_privately_and_records(self):
        ops, mem = FakeConn(), FakeConn(mems=GOOD + [_mem(3, "Ukraine needs help, donate to Hope Kharkiv Fund now",
                                                           channel="Ukraine News NowUA"),
                                                      _mem(4, "Ukrainian defenders: please donate to Hope Kharkiv Fund",
                                                           channel="RFU News — Strategic Geopolitics")])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=_connector(ops, mem)), 0)
        uc.nova_config.post_both.assert_called_once()
        args, kwargs = uc.nova_config.post_both.call_args
        self.assertEqual(kwargs["slack_channel"], uc.POST_CHANNEL)
        self.assertEqual(kwargs["discord_channel"], "")          # no public Discord mirror
        self.assertIn("UNVERIFIED — check before giving", args[0])
        self.assertTrue(ops.committed)
        inserts = [p for s, p in ops.executed if s.startswith("INSERT INTO ukraine_cause_reports")]
        self.assertEqual(len(inserts), 3)

    def test_no_post_when_nothing_found(self):
        ops, mem = FakeConn(), FakeConn(mems=[_mem(1, "Ukraine front line update, no appeals at all")])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=_connector(ops, mem)), 0)
        uc.nova_config.post_both.assert_not_called()
        self.assertFalse(ops.committed)

    def test_no_post_when_only_cooldown_items(self):
        ops, mem = FakeConn(already={"razom", "come_back_alive"}), FakeConn(mems=GOOD)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=_connector(ops, mem)), 0)
        uc.nova_config.post_both.assert_not_called()

    def test_dry_run_prints_and_records_nothing(self):
        ops, mem = FakeConn(), FakeConn(mems=GOOD)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=_connector(ops, mem), dry_run=True), 0)
        self.assertIn("Ukraine cause watch", out.getvalue())
        uc.nova_config.post_both.assert_not_called()
        self.assertFalse(ops.committed)

    def test_empty_allowlist_posts_nothing(self):
        ops, mem = FakeConn(allow=[]), FakeConn(mems=GOOD)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(uc.run(connect=_connector(ops, mem)), 1)
        uc.nova_config.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for flag in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(uc.main))


if __name__ == "__main__":
    unittest.main()
