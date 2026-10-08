#!/usr/bin/env python3
"""Tests for nova_ivory_head.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_ivory_head as I  # noqa: E402
import nova_watch_common as W  # noqa: E402

SRC = (SCRIPTS / "nova_ivory_head.py").read_text()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = []
        for k, v in self.routes.items():
            if k in sql:
                self._last = list(v)
                break

    def fetchall(self):
        return self._last


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _fakes():
    oc = FakeCur({"FROM beliefs": [(1, "🐠 The Fishbowl Ate My Week"), (2, "Quiet Post")],
                  "FROM article_citations": [("the-fishbowl", "m1"), ("the-fishbowl", "m2"),
                                             ("quiet-post", "m3")]})
    mc = FakeCur({"source='nova_articles'": [("🐠 The Fishbowl Ate My Week", "2026-10-06-the-fishbowl")],
                  "WHERE id = ANY": [("m1", "fishbowl", None, "fishbowl_stream", None),
                                     ("m2", "fishbowl", None, "fishbowl_stream", None),
                                     ("m3", "conversation", None, "chat_turn", None)]})
    return oc, mc


def _connect_for(oc, mc):
    return lambda dsn=W.DSN, **k: FakeConn(mc if dsn == W.MEM_DSN else oc)


class TestSecurity(unittest.TestCase):
    def test_no_fstring_sql_or_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn(str(Path.home()), SRC)

    def test_never_deletes_beliefs_or_posts(self):
        self.assertNotRegex(SRC, r"(?i)(DELETE FROM|UPDATE)\s+beliefs")
        for banned in ("post_both", "slack", "notify(", "/remember"):
            self.assertNotIn(banned, SRC.lower().replace("not posted", ""))

    def test_memory_ids_are_parameters(self):
        mc = FakeCur()
        I.memory_families(mc, ["x'; DROP TABLE memories; --"])
        sql, params = mc.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, (["x'; DROP TABLE memories; --"],))


class TestPerformance(unittest.TestCase):
    def test_analyse_10k_beliefs(self):
        fams = ["youtube", "tv", "news", "conversation", "gutenberg"]
        fam = {f"m{i}": fams[i % 5] for i in range(50000)}
        cites = {b: [f"m{(b * 7 + j) % 50000}" for j in range(5)] for b in range(10000)}
        t = time.monotonic()
        weekly, flags, cov = I.analyse(cites, fam, 10000)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(cov["beliefs_resolved"], 10000)

    def test_lookup_is_batched(self):
        mc = FakeCur()
        I.memory_families(mc, [str(i) for i in range(2500)])
        self.assertEqual(len(mc.sql), 3)                 # 1000 + 1000 + 500


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            W.connect(attempts=3, delay=0, _sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_memory_lookup_fails_open(self):
        # RETRY GAP: memory_families — the query itself is not retried (W.connect retries the
        # connection); a failed batch leaves those citations unresolved, never raises.
        self.assertEqual(I.memory_families(FakeCur(boom=True), ["a", "b"]), {})
        self.assertEqual(I.article_slugs(FakeCur(boom=True), ["t"]), {})
        self.assertEqual(I.load_beliefs(FakeCur(boom=True)), [])


class TestUnit(unittest.TestCase):
    def test_family_of(self):
        self.assertEqual(I.family_of("literature", None, "local_file", "/staging/pg84.txt"), "gutenberg")
        self.assertEqual(I.family_of("literature", None, "local_file", "/staging/book.txt"), "local_files")
        self.assertEqual(I.family_of("news", "youtube", "video_transcript"), "youtube")
        self.assertEqual(I.family_of("reddit", None, "gov_rss"), "news")
        self.assertEqual(I.family_of("scanner"), "scanner")
        self.assertEqual(I.family_of("homekit"), "sensor")
        self.assertEqual(I.family_of(None), "other")

    def test_slugs(self):
        self.assertEqual(I.slug_of("🗣️ Russell Crowe Is the Fishbowl Now"), "russell-crowe-is-the-fishbowl-now")
        self.assertEqual(len(I.slug_of("a" * 200)), 60)
        self.assertEqual(I.slug_of(None), "")
        self.assertEqual(I.strip_date("2026-09-opinions-monthly-wrap"), "2026-09-opinions-monthly-wrap")

    def test_flag_rules(self):
        fam = {f"y{i}": "youtube" for i in range(5)} | {"t": "tv", "c": "conversation", "s": "self"}
        _w, flags, _c = I.analyse({1: ["y0", "y1", "y2", "y3", "t"],     # 80% youtube -> flag
                                   2: ["y0", "y1", "y2", "y3", "c"],     # grounded -> no flag
                                   3: ["y0", "t"],                       # 50% -> no flag
                                   4: ["s", "s"]}, fam, 4)                # self is not a corpus
        self.assertEqual([f["belief_id"] for f in flags], [1])
        self.assertEqual(flags[0]["share"], 0.8)

    def test_empty(self):
        self.assertEqual(I.analyse({}, {}, 0), ([], [], {"beliefs_total": 0, "beliefs_cited": 0, "beliefs_uncited": 0,
                                                          "beliefs_resolved": 0, "citations_unresolved": 0,
                                                          "coverage": 0.0}))

    def test_week_is_monday(self):
        self.assertEqual(I.week_of(date(2026, 10, 11)), date(2026, 10, 5))

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(I.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_connect_and_mem_dsn(self):
        self.assertIn("W.connect(W.MEM_DSN)", SRC)
        self.assertNotIn("pg-primary", SRC)

    def test_reads_house_tables(self):
        oc, mc = _fakes()
        beliefs = I.load_beliefs(oc)
        self.assertIn("FROM beliefs WHERE active", oc.sql[0][0])
        cites = I.belief_citations(beliefs, I.load_citations(oc), I.article_slugs(mc, [t for _b, t in beliefs]))
        self.assertEqual(cites, {1: ["m1", "m2"], 2: ["m3"]})   # stable slug via metadata; title slug rule

    def test_chain_shape(self):
        oc, mc = _fakes()
        cites = {1: ["m1", "m2"], 2: ["m3"]}
        weekly, flags, cov = I.analyse(cites, I.memory_families(mc, ["m1", "m2", "m3"]), 2)
        self.assertEqual({r["family"] for r in weekly}, {"fishbowl", "conversation"})
        self.assertEqual(set(weekly[0]), {"family", "beliefs_supported", "single_corpus_beliefs", "share",
                                          "beliefs_total", "beliefs_cited"})


class TestFunctional(unittest.TestCase):
    def test_run_writes_rows(self):
        oc, mc = _fakes()
        with mock.patch.object(W, "connect", side_effect=_connect_for(oc, mc)), mock.patch.object(I, "log"):
            res = I.run(dry=False)
        self.assertEqual([f["belief_id"] for f in res["flags"]], [1])
        sqls = " ".join(s for s, _ in oc.sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS ivory_head_weekly", sqls)
        self.assertIn("ON CONFLICT (week, family) DO UPDATE", sqls)
        flag_rows = [p for s, p in oc.sql if s.startswith("INSERT INTO ivory_head_flags")]
        self.assertEqual(flag_rows[0][1:], (1, "fishbowl", 1.0, 2))
        self.assertFalse(any("reflection_questions" in s for s, _ in oc.sql))

    def test_dry_run_writes_nothing(self):
        oc, mc = _fakes()
        with mock.patch.object(W, "connect", side_effect=_connect_for(oc, mc)), mock.patch.object(I, "log"):
            res = I.run(dry=True)
        self.assertEqual(res["coverage"]["beliefs_cited"], 2)
        self.assertFalse(any(s.lstrip().split()[0] in ("CREATE", "INSERT", "DELETE", "UPDATE")
                             for s, _ in oc.sql + mc.sql))

    def test_memory_db_down_writes_no_family_rows(self):
        oc, _ = _fakes()
        with mock.patch.object(W, "connect", side_effect=_connect_for(oc, FakeCur(boom=True))), \
                mock.patch.object(I, "log"):
            res = I.run(dry=False)
        self.assertEqual(res["weekly"], [])
        # metadata slug lookup failed too, so only the title-slug route (quiet-post -> m3) resolves
        self.assertEqual(res["coverage"]["citations_unresolved"], 1)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ivory_head.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ivory_head.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        r = subprocess.run([sys.executable, "-c", "import nova_ivory_head"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
