#!/usr/bin/env python3
"""7-category gap tests for nova_ideal_reader.py (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Complements tests/test_nova_ideal_reader.py — this file adds the
PG retry/backoff (_pg_connect), the darlings fallback, deletion-only properties over many
inputs, and the CLI paths. Offline: PG, the chooser model and the journal guard are mocked.
Written by Jordan Koch (via Claude)."""
import io
import json
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import psycopg2  # noqa: E402
import nova_ideal_reader as ir  # noqa: E402

SRC = (SCRIPTS / "nova_ideal_reader.py").read_text()
D = list(ir.BUILTIN_LEADS)
TAIL = "## Sources & Attribution\n\n- [1] memory abc (ops): the job failed 3 times.\n- [2] https://example.org/x\n"
HEAD = ("Here's the thing: the backup job failed three nights running and nobody noticed it.\n"
        "I read the logs and the disk was really full on the volume that holds the snapshots.\n"
        "The disk on the volume that holds the snapshots was full. That matters.\n"
        "My brilliant fix pruned 40 snapshots and the job has run cleanly twice since then.\n"
        "Only I could have found that, and I alone noticed the pattern in the logs here.\n"
        "I didn't verify the restore path yet, so treat it as a condition, not a finding.\n"
        "The scheduler on the storage host runs the snapshot job at two in the morning each night.\n"
        "Each snapshot keeps a copy of the configuration files and the database dumps from that day.\n"
        "The retention policy was set to keep ninety days, which is longer than the volume can hold.\n"
        "Pruning by hand works for now, but the policy itself needs a smaller window going forward.\n"
        "Alerts for a full volume existed on paper, yet the threshold was set above one hundred percent.\n"
        "That threshold is now set to eighty five percent, so the next fill should page before it breaks.\n")
ART = HEAD + "\n" + TAIL


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_deletion_only_over_many_variants(self):
        """Property: for every variant and target, output content words and numbers are a subset."""
        variants = [ART, ART.replace("three", "3"), HEAD * 5 + "\n" + TAIL, HEAD.upper() + "\n" + TAIL]
        for v in variants:
            for tgt in (0.0, 0.05, 0.1, 0.15):
                with self.subTest(tgt=tgt):
                    out = ir.edit(v, "article", darlings=D, target=tgt)["text"]
                    self.assertLessEqual(ir._content_words(out), ir._content_words(v))
                    self.assertLessEqual(set(ir._NUM_RE.findall(out)), set(ir._NUM_RE.findall(v)))

    def test_sources_tail_byte_identical(self):
        out = ir.edit(ART, "article", darlings=D, target=0.15)["text"]
        self.assertTrue(out.endswith(TAIL))

    def test_verify_rejects_tampered_sources_and_damien(self):
        self.assertEqual(ir.verify(ART, ART.replace("example.org", "evil.org")), "Sources section changed")
        self.assertIsNotNone(ir.verify("Damien. Damien.", "Damien."))

    def test_chooser_ids_outside_offer_are_ignored(self):
        res = ir.edit(ART, "article", darlings=D, chooser=lambda s, u: '{"cut": [999, "S-1", "x", -3]}')
        self.assertTrue(res["applied"])
        self.assertNotIn("ideal-reader", [k for k, _ in res["cuts"]])

    def test_log_edit_never_writes_under_pytest(self):
        with mock.patch.object(ir, "_pg_connect") as pc:
            ir.log_edit("article", "t", {"before": 1, "after": 1, "cut_pct": 0, "applied": True, "why": "", "cuts": []})
        pc.assert_not_called()

    def test_no_secrets_or_home_paths(self):
        self.assertNotRegex(SRC, r"(password|secret|token)\s*=\s*['\"][^'\"]{8,}")
        self.assertNotRegex(SRC, r"/Users/[a-z]")


class TestPerformance(unittest.TestCase):
    def test_edit_scales_roughly_linearly(self):
        small = HEAD * 20 + "\n" + TAIL
        big = HEAD * 160 + "\n" + TAIL
        t = time.perf_counter(); ir.edit(small, "article", darlings=D); t1 = time.perf_counter() - t
        t = time.perf_counter(); ir.edit(big, "article", darlings=D); t2 = time.perf_counter() - t
        self.assertLess(t2, 3.0)
        self.assertLess(t2, max(0.05, t1) * 40)       # 8x input, generous bound against O(n^2) blow-up

    def test_compute_darlings_200_docs_bounded(self):
        tmp = Path(tempfile.mkdtemp())
        for i in range(200):
            (tmp / f"{i}.md").write_text(f"---\nt: x\n---\nHere's the thing about item {i}: it broke today.\n" + HEAD)
        t = time.perf_counter()
        ds = ir.compute_darlings(sorted(tmp.glob("*.md")))
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertLessEqual(len(ds), ir.DARLING_TOP)

    def test_hostile_chooser_output_bounded(self):
        huge = json.dumps({"cut": list(range(100_000))})
        t = time.perf_counter()
        res = ir.edit(ART, "article", darlings=D, chooser=lambda s, u: huge)
        self.assertLess(time.perf_counter() - t, 3.0)
        self.assertLessEqual(res["cut_pct"], 100 * ir.MAX_CUT + 3)


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_with_backoff_then_raises(self):
        err = psycopg2.OperationalError("down")
        with mock.patch("psycopg2.connect", side_effect=err) as c, mock.patch("time.sleep") as sl, _quiet():
            with self.assertRaises(psycopg2.OperationalError):
                ir._pg_connect()
        self.assertEqual(c.call_count, 3)
        self.assertEqual([a[0][0] for a in sl.call_args_list], [0.5, 1.0])

    def test_pg_connect_recovers_on_second_try(self):
        conn = object()
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("blip"), conn]) as c, \
                mock.patch("time.sleep"), _quiet():
            self.assertIs(ir._pg_connect(), conn)
        self.assertEqual(c.call_count, 2)

    def test_load_darlings_falls_back_loudly_after_retries(self):
        ir._DARLING_CACHE.clear()
        buf = io.StringIO()
        try:
            with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")) as c, \
                    mock.patch("time.sleep"), redirect_stdout(buf):
                ds = ir.load_darlings()
        finally:
            ir._DARLING_CACHE.clear()
        self.assertEqual(c.call_count, 3)
        self.assertEqual(set(ds), set(ir.BUILTIN_LEADS))
        self.assertIn("darlings unavailable", buf.getvalue())     # never silent

    def test_claude_chooser_goes_through_retrying_cli_wrapper(self):
        import nova_journal
        with mock.patch.object(nova_journal, "call_openrouter", return_value='{"cut": []}') as co:
            self.assertEqual(ir.claude_chooser("s", "u"), '{"cut": []}')
        self.assertIn("haiku", co.call_args[1]["model"])
        self.assertIn("_attempts = 3", (SCRIPTS / "nova_journal.py").read_text())  # the wrapper's retry loop

    def test_chooser_none_result_is_safe(self):
        import nova_journal
        with mock.patch.object(nova_journal, "call_openrouter", return_value=None):
            self.assertEqual(ir.claude_chooser("s", "u"), "")


class TestUnit(unittest.TestCase):
    def test_target_cut_clamped(self):
        with mock.patch("nova_voice.dial_scale", return_value=0.9):
            self.assertEqual(ir.target_cut(), ir.MAX_CUT)
        with mock.patch("nova_voice.dial_scale", return_value=-1):
            self.assertEqual(ir.target_cut(), 0.0)
        with mock.patch("nova_voice.dial_scale", side_effect=RuntimeError("pg")):
            self.assertEqual(ir.target_cut(), 0.10)

    def test_split_tail(self):
        head, tail = ir._split_tail(ART)
        self.assertEqual(tail, TAIL)
        self.assertEqual(ir._split_tail("no sources"), ("no sources", ""))

    def test_strip_lead_needs_three_words_left(self):
        self.assertEqual(ir._strip_lead("Here's the thing: no.", D), ("Here's the thing: no.", None))
        self.assertEqual(ir._strip_lead("Look, the disk was full.", D)[0], "The disk was full.")

    def test_stock_and_grandiose_sentences(self):
        self.assertTrue(ir.STOCK_SENTENCES.match("Let that sink in."))
        self.assertFalse(ir.STOCK_SENTENCES.match("That matters for the restore test."))
        self.assertTrue(ir._GRANDIOSITY_SENT.search("Only I could fix it."))

    def test_protected_sentences(self):
        for s in ("Is it fixed?", "It's all for you, Damien", "Little Mister approved.",
                  "I don't know why it failed.", "He said \"really\" twice."):
            self.assertTrue(ir._protected(s), s)


class TestIntegration(unittest.TestCase):
    def test_edit_article_applies_when_guard_passes(self):
        with mock.patch("nova_journal_guard.is_publishable", return_value=(True, "ok")), \
                mock.patch.object(ir, "load_darlings", return_value=D), \
                mock.patch.object(ir, "target_cut", return_value=0.15), _quiet():
            out = ir.edit_article("Backup Failure Notes", ART)
        self.assertNotIn("Here's the thing", out)
        self.assertNotIn("Only I could", out)
        self.assertTrue(out.endswith(TAIL))

    def test_edit_reach_logs_only_changes(self):
        with mock.patch.object(ir, "load_darlings", return_value=D), mock.patch.object(ir, "log_edit") as le:
            self.assertEqual(ir.edit_reach("The backup ran fine."), "The backup ran fine.")
            le.assert_not_called()
            msg = ("Honestly, the backup on the storage host ran fine overnight and the snapshot "
                   "job finished before the morning reports went out to the dashboard.")
            self.assertTrue(ir.edit_reach(msg).startswith("The backup on the storage host ran fine"))
            le.assert_called_once()

    def test_darlings_cache_avoids_repeat_pg(self):
        ir._DARLING_CACHE.clear()
        cur = mock.MagicMock(); cur.fetchall.return_value = [("the quiet part",)]
        conn = mock.MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value.__enter__.return_value = cur
        try:
            with mock.patch.object(ir, "_pg_connect", return_value=conn) as pc:
                a = ir.load_darlings(); b = ir.load_darlings()
        finally:
            ir._DARLING_CACHE.clear()
        self.assertEqual(pc.call_count, 1)
        self.assertIn("the quiet part", a)
        self.assertEqual(a, b)


class TestFunctional(unittest.TestCase):
    def test_golden_article(self):
        res = ir.edit(ART, "article", darlings=D, target=0.15)
        self.assertTrue(res["applied"], res["why"])
        t = res["text"]
        self.assertTrue(t.startswith("The backup job failed three nights running"))
        self.assertNotIn("That matters.", t)
        self.assertNotIn("brilliant", t)
        self.assertIn("I didn't verify the restore path yet", t)
        self.assertLess(res["after"], res["before"])

    def test_error_path_returns_original(self):
        with mock.patch.object(ir, "verify", return_value="new words introduced"):
            res = ir.edit(ART, "article", darlings=D)
        self.assertEqual(res["text"], ART)
        self.assertFalse(res["applied"])
        self.assertEqual(res["cut_pct"], 0.0)

    def test_reach_never_drops_whole_sentences_for_target(self):
        msg = "The disk was full. The disk was full again on the same volume today."
        self.assertEqual(ir.edit(msg, "reach", darlings=D, target=0.15)["text"], msg)


class TestFrame(unittest.TestCase):
    def test_main_no_args_prints_help(self):
        with mock.patch.object(sys, "argv", ["nova_ideal_reader.py"]), _quiet():
            self.assertEqual(ir.main(), 0)

    def test_main_file_no_model(self):
        p = Path(tempfile.mkdtemp()) / "a.md"
        p.write_text("---\ntitle: x\n---\n" + ART)
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["x", "--file", str(p), "--no-model"]), \
                mock.patch.object(ir, "load_darlings", return_value=D), redirect_stdout(buf):
            self.assertEqual(ir.main(), 0)
        self.assertIn("applied=True", buf.getvalue())

    def test_public_api_present(self):
        for name in ("edit", "edit_article", "edit_reach", "verify", "claude_chooser", "_pg_connect", "main"):
            self.assertTrue(callable(getattr(ir, name)), name)


if __name__ == "__main__":
    unittest.main()
