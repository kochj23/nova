#!/usr/bin/env python3
"""Tests for nova_skipping_needle.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure loop detection on fixtures; DB through fake cursors; the memory
server through a mocked urlopen. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_skipping_needle.py"
spec = importlib.util.spec_from_file_location("skipping_needle_under_test", SCRIPT)
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)
SRC = SCRIPT.read_text()

SCRATCH = "so the crate was " + "that was right here " * 200 + "and then the record ended"
PAGE_OF_O = "he shouted " + "O " * 400 + "and walked off the stage"
REAL_WORDS = " ".join(f"word{i}" for i in range(40))


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
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


def _writes(cur):
    return [q for q in cur.sql if re.match(r"\s*(INSERT|UPDATE|CREATE|ALTER|DELETE)", q)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_emails_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[\w.]+")
        self.assertNotRegex(SRC, r"(?i)\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")

    def test_never_deletes_memories(self):
        for verb in (r"DELETE\s+FROM", r"\bDROP\s", r"TRUNCATE", r"subprocess", r"os\.system"):
            self.assertNotRegex(SRC, verb)
        # the only write to memories is the supersession pointer
        mem_updates = re.findall(r"UPDATE memories SET ([^\"]+)", SRC)
        self.assertEqual(mem_updates, ["superseded_by = %s, valid_to = now() WHERE id = %s AND superseded_by IS NULL"])
        tables = {t for _, t in re.findall(r"(INSERT INTO|UPDATE)\s+([\w.]+)", SRC)} - {"SET"}   # DO UPDATE SET
        self.assertEqual(tables, {"skipping_needle", "skipping_needle_ingest", "skipping_needle_scan", "memories"})

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)

    def test_statement_timeout_on_every_connection(self):
        self.assertIn('options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"', SRC)   # scan / repair
        self.assertIn('options="-c statement_timeout=5000"', SRC)                       # ingest hook


class TestPerformance(unittest.TestCase):
    def test_long_transcript_is_linear(self):
        text = " ".join(f"w{i % 997} said thing{i}" for i in range(20000)) + " " + "that was right here " * 500
        t0 = time.monotonic()
        loops = sn.find_loops(text)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["repeats"], 500)

    def test_ten_thousand_short_chunks(self):
        chunks = [f"chunk {i} " + REAL_WORDS for i in range(10000)]
        t0 = time.monotonic()
        self.assertEqual(sum(1 for c in chunks if sn.find_loops(c)), 0)
        self.assertLess(time.monotonic() - t0, 10.0)

    def test_scan_stops_at_the_deadline(self):
        cur = _Cur([("ORDER BY pass_no DESC", (1, 0, None))])
        mcur = _Cur()
        st = sn.scan(cur, mcur, deadline=time.monotonic() - 1)
        self.assertEqual(st["slices"], 0)
        self.assertFalse(any("FROM memories" in q for q in mcur.sql))


class TestRetry(unittest.TestCase):
    def test_remember_retries_twice_then_succeeds(self):
        calls = []
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps({"id": "new", "status": "stored"}).encode()

        def urlopen(req, timeout=0):
            calls.append(req)
            if len(calls) < 3:
                raise OSError("memory server restarting")
            return resp
        sleeps = []
        with mock.patch("urllib.request.urlopen", side_effect=urlopen), \
             mock.patch.object(sn, "remember_url", return_value="http://memory.invalid/remember"):
            out = sn.post_remember("text", "tv", {}, _sleep=sleeps.append)
        self.assertEqual(out["id"], "new")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1, 2])

    def test_unreachable_memory_server_leaves_row_flagged_and_stops(self):
        cur = _Cur([("FROM skipping_needle WHERE status", [(1, "m1"), (2, "m2")])])
        mcur = _Cur([("SELECT text, source, metadata", ("intro " + SCRATCH + " " + REAL_WORDS, "tv", {}))])
        out = sn.repair(cur, mcur, 10, time.monotonic() + 30, post=lambda *a: None)
        self.assertEqual(out, {"flagged": 1})
        self.assertFalse(any("UPDATE memories" in q for q in mcur.sql))

    def test_ingest_record_is_skipped_under_test(self):
        self.assertTrue(sn._under_test())
        with mock.patch("psycopg2.connect") as c:
            sn.record_ingest("tv_ingest", sn.find_loops(SCRATCH), 900, 100)
        c.assert_not_called()

    def test_ingest_hook_fails_open(self):
        with mock.patch.object(sn, "record_ingest", side_effect=RuntimeError("pg down")):
            self.assertIn("[loop x200]", sn.ingest_clean(SCRATCH, "tv_ingest"))
        with mock.patch.object(sn, "collapse_loops", side_effect=RuntimeError("bug")):
            self.assertEqual(sn.ingest_clean(SCRATCH, "tv_ingest"), SCRATCH)
        self.assertIsNone(sn.ingest_clean(None, "nova_ingest"))


class TestUnit(unittest.TestCase):
    def test_scratch_that_was_right_here(self):
        out, loops = sn.collapse_loops(SCRATCH)
        self.assertEqual(out, "so the crate was that was right here [loop x200] and then the record ended")
        self.assertEqual((loops[0]["phrase"], loops[0]["repeats"], loops[0]["n"]), ("that was right here", 200, 4))

    def test_page_of_o(self):
        out, loops = sn.collapse_loops(PAGE_OF_O)
        self.assertEqual(out, "he shouted O [loop x400] and walked off the stage")
        self.assertAlmostEqual(sn.share_of_text(PAGE_OF_O, loops), 399 / 407, places=3)

    def test_single_token_run(self):
        self.assertEqual(sn.collapse_loops("OOOOOOOOOOOOOOOO yes")[0], "O [loop x16] yes")
        self.assertEqual(sn.collapse_loops("O-O-O-O-O-O-O-O-O-O-O-O then")[0], "O- [loop x11] then")
        self.assertEqual(sn.find_loops("abcabcabcabcabcabcabcabcabcabcXY"), [])     # tail is not the unit

    def test_real_speech_is_left_alone(self):
        for t in ("no no no no, I said", "very very good", "it was what it was", REAL_WORDS,
                  ". . . . . . . . . . . .", "— — — — — — — — —", ""):
            self.assertEqual(sn.find_loops(t), [], t)
        self.assertEqual(sn.collapse_loops("no no no no")[0], "no no no no")

    def test_threshold_edges(self):
        self.assertEqual(sn.find_loops("go and " * 3), [])           # 3 repeats
        self.assertEqual(len(sn.find_loops("go and " * 4)), 1)       # 4 repeats, 8 words
        self.assertEqual(sn.find_loops("no " * 7), [])               # 7 words < MIN_SPAN
        self.assertEqual(sn.find_loops("no " * 8)[0]["repeats"], 8)

    def test_punctuation_and_case_are_folded_and_idempotent(self):
        t = "Thank you. thank you, THANK YOU! thank you. Thank you."
        out, loops = sn.collapse_loops(t)
        self.assertEqual(loops[0]["repeats"], 5)
        self.assertEqual(out, "Thank you. [loop x5]")
        self.assertEqual(sn.collapse_loops(out)[1], [])

    def test_two_separate_loops_and_smallest_period(self):
        t = "a " + "la la " * 6 + "middle words here " + "we go " * 5 + "end"
        loops = sn.find_loops(t)
        self.assertEqual([(lp["n"], lp["repeats"]) for lp in loops], [(1, 12), (2, 5)])

    def test_scrub_for_her_own_text(self):
        self.assertEqual(sn.scrub("I keep thinking " * 5 + "about it."), "I keep thinking about it.")
        self.assertEqual(sn.scrub(None), None)

    def test_summarize_slices_and_real_words(self):
        loops = sn.find_loops("x " + "a b " * 10 + "y " + "c " * 9)
        self.assertEqual(sn.summarize(loops)["loop_phrase"], "a b")
        self.assertEqual(sn.summarize(loops)["loops"], 2)
        self.assertEqual(sn.slice_bounds(0), ("000", "001"))
        self.assertEqual(sn.slice_bounds(4095), ("fff", "g"))
        self.assertEqual(sn.real_words("a [loop x9] b"), 2)

    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sn.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_and_tables(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn('connect("nova_memories")', SRC)
        self.assertIn("superseded_by IS NULL", SRC)
        for t in ("tv_transcript", "video_transcript", "fishbowl_stream", "youtube_transcript"):
            self.assertIn(t, sn.SCAN_TYPES)

    def test_every_transcript_ingester_calls_the_hook(self):
        for f, origin in (("nova_tv_ingest.py", "tv_ingest"), ("nova_nightly_media.py", "nightly_media"),
                          ("nova_tv_retry_failed.py", "tv_retry_failed"),
                          ("nova_plex_auto_ingest.py", "plex_auto_ingest"), ("nova_ingest.py", "nova_ingest")):
            src = (SCRIPTS / f).read_text()
            self.assertIn("from nova_skipping_needle import ingest_clean as _needle_clean", src, f)
            self.assertIn(f'_needle_clean(', src, f)
            self.assertIn(f'"{origin}"', src, f)
            self.assertIn('def _needle_clean(text, origin=""): return text', src, f)   # fallback when missing

    def test_tv_ingest_chunking_keeps_the_words_around_a_loop(self):
        import nova_tv_ingest as tv
        before = " ".join(f"real{i}" for i in range(60))
        after = " ".join(f"more{i}" for i in range(60))
        with mock.patch("nova_skipping_needle.record_ingest") as rec:     # never touch the real DB
            chunks = tv.chunk_text(before + " " + "that was right here " * 200 + after)
        rec.assert_called_once()
        self.assertEqual(rec.call_args[0][0], "tv_ingest")
        joined = " ".join(chunks)
        self.assertIn("real59", joined)
        self.assertIn("more0", joined)
        self.assertLess(joined.count("that was right here"), 2)

    def test_scan_slice_then_record_hits(self):
        rows = [("m1", "tv", "tv_transcript", SCRATCH), ("m2", "tv", "tv_transcript", REAL_WORDS)]
        mcur = _Cur([("FROM memories", rows)])
        n, hits = sn.scan_slice(mcur, 5)
        self.assertEqual((n, len(hits)), (2, 1))
        self.assertEqual(mcur.params[0][:2], ("005", "006"))
        cur = _Cur()
        self.assertEqual(sn.record_hits(cur, hits), 1)
        self.assertIn("ON CONFLICT (memory_id)", cur.sql[0])
        self.assertEqual(cur.params[0][0], "m1")


class TestFunctional(unittest.TestCase):
    def _run(self, argv, cur, mcur, post=None):
        from contextlib import ExitStack
        conns = {"nova_ops": mock.Mock(cursor=mock.Mock(return_value=cur)),
                 "nova_memories": mock.Mock(cursor=mock.Mock(return_value=mcur))}
        notify = mock.Mock(return_value=True)
        out = io.StringIO()
        with ExitStack() as st:
            st.enter_context(mock.patch.object(sn, "connect", side_effect=lambda db: conns[db]))
            st.enter_context(mock.patch.object(sn, "ensure_schema"))
            st.enter_context(mock.patch.object(sn, "SLICES", ["000", "800"]))
            st.enter_context(mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}))
            if post:
                st.enter_context(mock.patch.object(sn, "post_remember", post))
            st.enter_context(redirect_stdout(out))
            rc = sn.main(argv)
        return rc, out.getvalue(), notify

    def test_scheduled_scans_flags_repairs_by_supersession_and_reports_the_pass(self):
        text = "intro " + SCRATCH + " " + REAL_WORDS
        mcur = _Cur([("FROM memories WHERE id >=", [("m1", "tv", "tv_transcript", text)]),
                     ("SELECT text, source, metadata", (text, "tv", {"show": "Scratch"}))])
        cur = _Cur([("ORDER BY pass_no DESC", (1, 0, None)),
                    ("FROM skipping_needle WHERE status", [(7, "m1")]),
                    ("SELECT rows_scanned, rows_looped", (2, 2))])
        posted = []

        def post(text, source, meta):
            posted.append((text, source, meta))
            return {"id": "m1-clean", "status": "stored"}
        rc, out, notify = self._run(["--scheduled"], cur, mcur, post)
        self.assertEqual(rc, 0)
        self.assertIn("[loop x200]", posted[0][0])
        self.assertEqual(posted[0][2]["supersedes"], "m1")
        self.assertEqual(posted[0][2]["show"], "Scratch")
        sup = [p for q, p in zip(mcur.sql, mcur.params) if q.startswith("UPDATE memories")]
        self.assertEqual(sup, [("m1-clean", "m1")])
        self.assertTrue(any("status = 'repaired'" in q for q in cur.sql))
        self.assertEqual(sum(1 for q in cur.sql if q.startswith("INSERT INTO skipping_needle ")), 2)
        notify.assert_called_once()
        self.assertIn("pass #1 done", notify.call_args[0][0])

    def test_hollow_memory_is_flagged_only(self):
        hollow = "O " * 300
        mcur = _Cur([("SELECT text, source, metadata", (hollow, "tv", {}))])
        cur = _Cur([("FROM skipping_needle WHERE status", [(3, "m9")])])
        post = mock.Mock()
        self.assertEqual(sn.repair(cur, mcur, 5, time.monotonic() + 30, post=post), {"hollow": 1})
        post.assert_not_called()
        self.assertFalse(any("UPDATE memories" in q for q in mcur.sql))

    def test_rejected_copy_is_not_superseded(self):
        text = "intro " + SCRATCH + " " + REAL_WORDS
        mcur = _Cur([("SELECT text, source, metadata", (text, "tv", {}))])
        cur = _Cur()
        st = sn.repair_one(cur, mcur, 1, "m1", post=lambda *a: {"id": None, "status": "rejected", "reason": "x"})
        self.assertEqual(st, "hollow")
        self.assertFalse(any("UPDATE memories" in q for q in mcur.sql))

    def test_dry_run_writes_nothing(self):
        mcur = _Cur([("FROM memories", [("m1", "tv", "tv_transcript", SCRATCH)])])
        cur = _Cur()
        rc, out, notify = self._run(["--dry-run", "2"], cur, mcur)
        self.assertEqual(rc, 0)
        self.assertIn("nothing written", out)
        self.assertIn("'that was right here'", out)
        self.assertEqual(_writes(cur) + _writes(mcur), [])
        notify.assert_not_called()

    def test_report_without_tables(self):
        cur = _Cur([("to_regclass", (False,))])
        rc, out, _ = self._run(["--report"], cur, _Cur())
        self.assertEqual(rc, 0)
        self.assertIn("No skipping-needle data yet", out)

    def test_monthly_metrics_shape(self):
        cur = _Cur([("WHERE finished_at IS NOT NULL", (2, 1000, 13, datetime(2026, 10, 15, tzinfo=timezone.utc))),
                    ("FROM skipping_needle_ingest", [("2026-10", 4, 6, 900)]),
                    ("GROUP BY 1, 2", [("2026-10", "repaired", 25), ("2026-10", "flagged", 3)])])
        m = sn.monthly_metrics(cur)
        self.assertEqual(m["last_pass"]["pct_with_loops"], 1.3)
        self.assertEqual(m["ingest_by_month"][0]["loops"], 6)
        self.assertEqual(m["status_by_month"]["2026-10"]["repaired"], 25)


class TestFrame(unittest.TestCase):
    def _sub(self, *args):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              timeout=30, env=env)

    def test_help_and_selftest(self):
        self.assertEqual(self._sub("--help").returncode, 0)
        r = self._sub("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_bad_args(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            sn.main(["--dry-run", "0"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            sn.main(["--repair", "-1"])

    def test_import_does_not_run_main_and_house_conventions(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(flag, SRC)
        for col in ("memory_id text", "loop_phrase text", "repeats integer", "share_of_text real",
                    "found_at timestamptz", "status text"):
            self.assertIn(col, SRC)


if __name__ == "__main__":
    unittest.main()
