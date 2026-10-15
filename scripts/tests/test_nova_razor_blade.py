#!/usr/bin/env python3
"""Tests for nova_razor_blade.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure diff/measure/log-parse on fixtures; DB through fake cursors.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_razor_blade.py"
spec = importlib.util.spec_from_file_location("razor_blade_under_test", SCRIPT)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)
SRC = SCRIPT.read_text()

DRAFT = "## Lead\nThe needle skipped and the room went quiet.\n## Sources\n- one\n- two"
DATELINE = "*Somewhere · Thursday, October 15, 2026 · 9:00 AM · 70F*\n\n"

LOG = """\
[2026-10-14 12:10:00] [journal] [longform] 'A Blood Test for Robots' [repo-scout/operations] 950w — within 800-1600, publishing as written
[2026-10-14 12:11:00] [journal] Published: operations/2026-10-14-a-blood-test-for-robots.md
[2026-10-14 13:00:00] [journal] [longform] 'Short Essay About Tape' (essays) is 400 words — expanding to >=3000
[2026-10-14 13:02:00] [journal] [longform] expanded 400 -> 3100 words
[2026-10-14 13:03:00] [journal] Published: essays/2026-10-14-short-essay-about-tape.md
[2026-10-14 14:00:00] [journal] [longform] 'Failed Expansion Story' (essays) is 500 words — expanding to >=3000
[2026-10-14 14:01:00] [journal] [longform] expanded 500 -> 2900 words
[2026-10-14 14:01:30] [journal] [longform] expansion failed guard (leak) — publishing original
[2026-10-14 14:02:00] [journal] Published: essays/2026-10-14-failed-expansion-story.md
[2026-10-14 15:00:00] [journal] [longform] 'Long Copenhagen Piece' [copenhagen/operations] 2000w — above max 1600, tightening
[2026-10-14 15:01:00] [journal] [longform] 'Long Copenhagen Piece' [copenhagen/operations] 2000w — tightened 2000 -> 1500w via m in 30s — publishing tightened
[2026-10-14 15:02:00] [journal] Published: operations/2026-10-14-long-copenhagen-piece.md
[2026-10-14 16:00:00] [journal] Published: operations/2026-10-14-no-length-policy.md
[2026-10-14 17:00:00] [journal] [longform] 'Mismatched Title' [x/operations] 900w — within 800-1600, publishing as written
[2026-10-14 17:00:05] [journal] Published: operations/2026-10-14-some-other-post.md
"""


class _Cur:
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_emails_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[\w.]+")

    def test_sql_is_parameterized_and_like_is_escaped(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")
        self.assertIn(r'replace("%", r"\%")', SRC)

    def test_read_only_on_the_journal_writes_only_its_table(self):
        for verb in (r"DELETE\s+FROM", r"\bDROP\s", r"TRUNCATE", r"subprocess", r"os\.system", r"write_text", r"unlink"):
            self.assertNotRegex(SRC, verb)
        tables = {t for _, t in re.findall(r"(INSERT INTO|UPDATE)\s+([\w.]+)", SRC)}
        self.assertEqual(tables, {"razor_blade"})

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)

    def test_footer_is_not_enabled_and_hook_only_reads(self):
        fn = (SCRIPTS / "nova_journal.py").read_text().split("def publish_hugo")[1].split("\ndef ")[0].splitlines()
        razor = [i for i, ln in enumerate(fn) if "razor" in ln.lower()]
        write = [i for i, ln in enumerate(fn) if "output.write_text(front_matter + byline + scrub_home_paths(scrub_pii(body)))" in ln]
        self.assertEqual(len(razor), 2)
        self.assertEqual(len(write), 1)
        self.assertTrue(fn[razor[0]].strip().startswith("_razor_draft = body"))
        self.assertGreater(razor[1], write[0])                  # measured after the post is written
        self.assertNotIn("edited", SRC.split("def record_publish")[1].split("def ")[0].lower().replace("edited n%", ""))


class TestPerformance(unittest.TestCase):
    def test_measure_on_a_long_post(self):
        draft = " ".join(f"w{i % 400} x{i}" for i in range(3000))
        pub = draft.replace("x10", "y10") + " " + " ".join(f"added{i}" for i in range(2000))
        t0 = time.monotonic()
        m = rb.measure(draft, pub)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(m["words_added"] - m["words_removed"], 2000)

    def test_parse_log_ten_thousand_lines(self):
        lines = LOG.splitlines() * 700
        t0 = time.monotonic()
        posts = rb.parse_log(lines)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(len(posts), 4 * 700)


class TestRetry(unittest.TestCase):
    # RETRY GAP: record_publish — one best-effort write per publish; it must FAIL OPEN, never retry/stall.
    def test_record_publish_fails_open_when_pg_is_down(self):
        with mock.patch.object(rb, "_under_test", return_value=False), \
             mock.patch.object(rb, "connect", side_effect=OSError("pg down")), redirect_stdout(io.StringIO()):
            self.assertFalse(rb.record_publish("T", "operations", "t", DRAFT, DRAFT))

    def test_record_publish_is_skipped_under_test(self):
        self.assertTrue(rb._under_test())          # this very run, pytest or python3 tests/test_*.py
        with mock.patch.object(rb, "connect") as c:
            self.assertFalse(rb.record_publish("T", "s", "t", DRAFT, DRAFT))
        c.assert_not_called()

    def test_journal_hook_is_exception_safe(self):
        src = (SCRIPTS / "nova_journal.py").read_text()
        hook = [ln for ln in src.splitlines() if "nova_razor_blade" in ln]
        self.assertEqual(len(hook), 1)
        i = src.splitlines().index(hook[0])
        self.assertTrue(hook[0].strip().startswith("try:"))
        self.assertTrue(src.splitlines()[i + 1].strip().startswith("except Exception: pass"))


class TestUnit(unittest.TestCase):
    def test_identical_is_zero(self):
        m = rb.measure(DRAFT, DRAFT)
        self.assertEqual((m["pct_changed"], m["words_added"], m["words_removed"], m["headings"]), (0.0, 0, 0, []))

    def test_dateline_is_not_an_edit(self):
        self.assertEqual(rb.measure(DRAFT, DATELINE + DRAFT)["pct_changed"], 0.0)
        self.assertEqual(rb.strip_dateline("plain text"), "plain text")

    def test_pure_expansion_and_pure_cut(self):
        d = " ".join(f"d{i}" for i in range(100))
        m = rb.measure(d, d + " " + " ".join(f"a{i}" for i in range(100)))
        self.assertEqual((m["words_added"], m["words_removed"]), (100, 0))
        self.assertAlmostEqual(m["pct_changed"], rb.estimate_pct(100, 200, 100, 0), places=0)
        m = rb.measure(d, " ".join(f"d{i}" for i in range(90)))
        self.assertEqual((m["words_added"], m["words_removed"]), (0, 10))

    def test_headings_most_edited(self):
        pub = "## Lead\nThe needle skipped and the room went quiet.\n## New Section\nall new words here\n## Sources\n- one"
        hs = rb.heading_changes(DRAFT, pub)
        self.assertEqual(hs[0]["heading"], "New Section")
        self.assertEqual(hs[0]["pct"], 100.0)
        self.assertEqual({h["heading"] for h in hs}, {"New Section", "Sources"})

    def test_sections_and_slugify(self):
        self.assertEqual(list(rb.sections("intro\n# A\nx\n## B\ny")), ["(lead)", "A", "B"])
        self.assertEqual(list(rb.sections("# A\nx")), ["A"])
        self.assertEqual(rb.slugify("Hold My Beer!"), "hold-my-beer")

    def test_estimate_and_empty(self):
        self.assertEqual(rb.estimate_pct(0, 0, 0, 0), 0.0)
        self.assertEqual(rb.diff_counts([], []), (0, 0, 1.0))

    def test_monthly_stats(self):
        rows = [("2026-10", "operations", 5.0, []), ("2026-10", "essays", 50.0, [{"heading": "H", "added": 9, "removed": 1}]),
                ("2026-10", "operations", 40.0, []), ("2026-09", "operations", 0.0, [])]
        oct_, sep = rb.monthly_stats(rows)
        self.assertEqual((oct_["month"], oct_["posts"], oct_["median_pct"], oct_["heavy"]), ("2026-10", 3, 40.0, 2))
        self.assertEqual(oct_["sections"][0][0], "essays")
        self.assertEqual(oct_["headings"], [("H", 10)])
        self.assertEqual(sep["heavy"], 0)

    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rb.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_parse_log_reconstructs_each_outcome(self):
        posts = {p["file"]: p for p in rb.parse_log(LOG.splitlines())}
        self.assertEqual(set(posts), {"2026-10-14-a-blood-test-for-robots", "2026-10-14-short-essay-about-tape",
                                      "2026-10-14-failed-expansion-story", "2026-10-14-long-copenhagen-piece"})
        self.assertEqual((posts["2026-10-14-short-essay-about-tape"]["draft"],
                          posts["2026-10-14-short-essay-about-tape"]["final"]), (400, 3100))
        self.assertEqual(posts["2026-10-14-failed-expansion-story"]["final"], 500)
        self.assertEqual(posts["2026-10-14-long-copenhagen-piece"]["final"], 1500)
        self.assertEqual(posts["2026-10-14-a-blood-test-for-robots"]["section"], "operations")

    def test_reconstruct_joins_ideal_reader_and_hugo_file(self):
        posts = [p for p in rb.parse_log(LOG.splitlines()) if p["file"].endswith("robots")]
        cur = _Cur([("FROM ideal_reader_log", ("A Blood Test for Robots That Swear", 950, 855, True))])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "operations").mkdir()
            (Path(d) / "operations" / "2026-10-14-a-blood-test-for-robots.md").write_text(
                "---\ntitle: x\n---\n\n*Published Tuesday*\n\n" + DATELINE + " ".join(["w"] * 855))
            (r,) = rb.reconstruct(cur, posts, root=Path(d))
        self.assertEqual((r["draft_words"], r["reader_cut_words"], r["published_words"]), (950, 95, 855))
        self.assertEqual((r["words_added"], r["words_removed"], r["origin"]), (0, 95, "log"))
        self.assertEqual(r["title"], "A Blood Test for Robots That Swear")
        self.assertTrue(cur.params[0][0].startswith("A Blood Test for Robots"))

    def test_shared_helpers(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("FROM ideal_reader_log", SRC)
        self.assertIn('record_publish(title, section, slug, _razor_draft, body)', (SCRIPTS / "nova_journal.py").read_text())


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur):
        notify = mock.Mock(return_value=True)
        out = io.StringIO()
        with ExitStack() as st:
            st.enter_context(mock.patch.object(rb, "connect", return_value=mock.Mock(cursor=mock.Mock(return_value=cur))))
            st.enter_context(mock.patch.object(rb, "ensure_schema"))
            st.enter_context(mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}))
            st.enter_context(redirect_stdout(out))
            rc = rb.main(argv)
        return rc, out.getvalue(), notify

    def test_record_publish_golden_path(self):
        cur = _Cur([("FROM ideal_reader_log", (1200, 1080, True))])
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        with mock.patch.object(rb, "_under_test", return_value=False), \
             mock.patch.object(rb, "connect", return_value=conn), mock.patch.object(rb, "ensure_schema"):
            ok = rb.record_publish("Tape", "essays", "2026-10-15-tape", DRAFT, DATELINE + DRAFT + " plus a crystal ball")
        self.assertTrue(ok)
        ins = [p for q, p in zip(cur.sql, cur.params) if q.startswith("INSERT INTO razor_blade")]
        self.assertEqual(len(ins), 1)
        row = ins[0]
        self.assertEqual(row[1:5], ("2026-10-15-tape", "essays", "Tape", "publish"))
        self.assertEqual((row[6], row[7], row[9], row[10]), (1200, 120, 4, 0))   # expanded, cut, added, removed
        conn.close.assert_called_once()

    def test_dry_run_writes_nothing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write(LOG)
        try:
            cur = _Cur([("to_regclass", (False,))])
            rc, out, notify = self._main(["--dry-run", fh.name], cur)
        finally:
            os.unlink(fh.name)
        self.assertEqual(rc, 0)
        self.assertIn("4 posts reconstructed", out)
        self.assertIn("2026-10  posts   4", out)
        self.assertIn("nothing written", out)
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE)", q) for q in cur.sql))
        notify.assert_not_called()

    def test_backfill_skips_posts_the_hook_already_recorded(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write(LOG)
        try:
            cur = _Cur([("origin = 'publish'", lambda c: (1,) if "short-essay" in c.params[-1][0] else None)])
            rc, out, _ = self._main(["--backfill-log", fh.name], cur)
        finally:
            os.unlink(fh.name)
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for q in cur.sql if q.startswith("INSERT INTO razor_blade")), 3)

    def test_scheduled_notifies_on_the_first_only(self):
        cur = _Cur([("to_regclass", (True,)), ("FROM razor_blade ORDER BY", [("2026-09", "operations", 40.0, [])])])
        notify = mock.Mock(return_value=True)
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            self.assertFalse(rb.scheduled(cur, today=date(2026, 10, 15)))
            self.assertTrue(rb.scheduled(cur, today=date(2026, 10, 1)))
        self.assertEqual(notify.call_args.kwargs["dedup_key"], "razor_blade:2026-09")
        self.assertIn("1 of 1 changed >30%", notify.call_args[0][0])

    def test_missing_log_is_an_error(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            rb.main(["--dry-run", str(Path(tempfile.gettempdir()) / "no-such-razor.log")])


class TestFrame(unittest.TestCase):
    def _sub(self, *args):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30, env=env)

    def test_help_and_selftest(self):
        self.assertEqual(self._sub("--help").returncode, 0)
        r = self._sub("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_house_conventions(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest", "--backfill-log"):
            self.assertIn(flag, SRC)
        for col in ("draft_words integer", "published_words integer", "similarity real", "pct_changed real",
                    "headings jsonb"):
            self.assertIn(col, SRC)


if __name__ == "__main__":
    unittest.main()
