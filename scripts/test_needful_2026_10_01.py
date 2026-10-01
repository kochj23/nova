#!/usr/bin/env python3
"""Seven-category tests for the 2026-09-30 → 10-01 changes: memory anchors (wish #38), the
recall boost (wish #39), temporal intuition (wish #40), affect resonance/silence (wish #41),
always-on unclaimed time with yield, the horror shelf in the lexicon, the journal psql fix,
the letting-go anchor guard, and the Slack-only mail summary. No DB, no network: every
external edge is stubbed. Run: python3 test_needful_2026_10_01.py"""
import io, os, re, sys, time, types, unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))

def _src(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()

sys.path.insert(0, HERE)
import nova_memory_anchor as anchor
import nova_temporal_intuition as temporal
import nova_affect as affect
import nova_unclaimed_time as unclaimed
import nova_lexicon as lexicon
import nova_letting_go as letting_go

sys.path.insert(0, os.path.dirname(HERE))          # memory_server.py lives one level up
import memory_server as ms


# ── 1. SECURITY ────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_anchor_subjects_are_not_regex(self):
        # a subject with regex metacharacters must match literally, never be compiled
        self.assertTrue(ms._anchor_hit("notes on c++ (and .net)", ["c++ (and .net)"]))
        self.assertFalse(ms._anchor_hit("notes on cxx", ["c++ (and .net)"]))

    def test_resonance_evidence_never_carries_message_text(self):
        secret = "my password is hunter2 and I love you"
        sc, pos, neg = affect.tone_score(secret)
        note_words = set(pos) | set(neg)
        self.assertNotIn("hunter2", note_words)
        self.assertNotIn("password", note_words)       # only lexicon words ever surface

    def test_temporal_notice_contains_no_raw_memory_text(self):
        t = temporal.notice_text([("preocc:9", "since I last developed 'x'", 31, 30)], date(2026, 10, 1))
        self.assertNotIn("preocc:9", t)                 # keys stay internal; prose only

    def test_journal_fetch_uses_explicit_host_and_no_local_socket(self):
        src = _src("nova_journal.py")
        self.assertIn('"psql", "-h", "pg-primary.digitalnoise.net", "-U", "kochj", "-d", "nova_memories"', src)
        self.assertNotIn('["psql", "-U", "kochj", "-d", "nova_memories"', src)


# ── 2. PERFORMANCE ─────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_tone_score_is_linear_and_fast(self):
        msgs = ["thanks that was great but broken again? ugh " * 5] * 5000
        t0 = time.perf_counter()
        for m in msgs:
            affect.tone_score(m)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_anchor_decide_scales(self):
        anchors = {f"k{i}": {"anchored_at": date(2026, 1, 1), "zero_since": None} for i in range(5000)}
        t0 = time.perf_counter()
        anchor.decide(anchors, [f"k{i}" for i in range(3)], {f"k{i}": 1.0 for i in range(5000)}, date(2026, 10, 1))
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_should_yield_scans_once(self):
        tasks = {f"t{i}": {"group": "llm", "running": False, "next_run": 10**9, "enabled": True} for i in range(5000)}
        t0 = time.perf_counter(); unclaimed.should_yield(tasks, 0.0)
        self.assertLess(time.perf_counter() - t0, 0.5)


# ── 3. RETRY ───────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_anchor_remember_retries_three_times_then_raises(self):
        calls = []
        def boom(req, timeout=0):
            calls.append(1); raise OSError("down")
        with mock.patch("urllib.request.urlopen", boom), mock.patch("time.sleep", lambda s: None):
            with self.assertRaises(OSError):
                anchor.remember("x", {})
        self.assertEqual(len(calls), 3)

    def test_temporal_remember_retries_three_times(self):
        calls = []
        def boom(req, timeout=0):
            calls.append(1); raise OSError("down")
        with mock.patch("urllib.request.urlopen", boom), mock.patch("time.sleep", lambda s: None):
            with self.assertRaises(OSError):
                temporal.remember("x", {})
        self.assertEqual(len(calls), 3)

    def test_anchor_refresh_keeps_last_good_list_on_failure(self):
        import asyncio
        ms._anchors_cache.update({"at": 0.0, "subjects": ["horology"]})
        async def failing_connect(*a, **k): raise ConnectionError("pg away")
        with mock.patch.object(ms.asyncpg, "connect", failing_connect):
            got = asyncio.run(ms._anchored_subjects())
        self.assertEqual(got, ["horology"])              # fail-open: last good set survives


# ── 4. UNIT ────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_anchor_set_hold_release(self):
        d = date(2026, 9, 30)
        s, r, after = anchor.decide({}, ["p1"], {"p1": 9.0}, d)
        self.assertEqual((s, r), (["p1"], []))
        s, r, after = anchor.decide(after, [], {"p1": 0.2}, d)      # still has gravity -> HOLD
        self.assertIn("p1", after); self.assertIsNone(after["p1"]["zero_since"])
        s, r, after = anchor.decide(after, [], {}, d)               # zero gravity -> clock starts
        self.assertEqual(after["p1"]["zero_since"], d)
        s, r, after = anchor.decide(after, [], {}, d + timedelta(days=anchor.RELEASE_AFTER_DAYS))
        self.assertEqual(r, ["p1"])

    def test_temporal_crossings(self):
        self.assertIsNone(temporal.crossed(6, None)); self.assertEqual(temporal.crossed(7, None), 7)
        self.assertEqual(temporal.crossed(400, None), 365); self.assertIsNone(temporal.crossed(31, 30))
        self.assertEqual(temporal.word(90), "a season")

    def test_tone_and_silence_math(self):
        self.assertEqual(affect.tone_score("thanks, great")[0], 1.0)
        self.assertEqual(affect.tone_score("broken, ugh")[0], -1.0)
        self.assertEqual(affect.tone_score("great but broken")[0], 0.0)
        self.assertEqual(affect.silence_excess(5, 10), 0.0)
        self.assertEqual(affect.silence_excess(30, 10), 1.0)
        self.assertEqual(affect.silence_excess(50, None), 0.0)

    def test_should_yield_rules(self):
        now = 1000.0
        T = {"unclaimed_time": {"group": "llm", "running": True, "next_run": now, "enabled": True},
             "essay": {"group": "llm", "running": False, "next_run": now + 60, "enabled": True}}
        self.assertEqual(unclaimed.should_yield(T, now), "essay")
        T["essay"]["next_run"] = now + 10_000
        self.assertIsNone(unclaimed.should_yield(T, now))
        T["essay"]["enabled"] = False; T["essay"]["running"] = True
        self.assertIsNone(unclaimed.should_yield(T, now))           # disabled tasks never win

    def test_recall_boost_hit(self):
        self.assertTrue(ms._anchor_hit("the Watch Fishbowl churned", ["the watch fishbowl"]))
        self.assertFalse(ms._anchor_hit("", ["x"])); self.assertFalse(ms._anchor_hit("x", []))


# ── 5. INTEGRATION ─────────────────────────────────────────────────────────────
class _Cur:
    """Minimal cursor double: canned rows per SQL fragment, or raises on demand."""
    def __init__(self, rows=None, raise_on=None): self.rows, self.raise_on, self.sql = rows or {}, raise_on, []
    def execute(self, sql, params=None):
        self.sql.append(sql)
        if self.raise_on and self.raise_on in sql: raise RuntimeError("boom")
        for frag, rows in self.rows.items():
            if frag in sql: self._out = rows; return
        self._out = []
    def fetchall(self): return self._out
    def fetchone(self): return self._out[0] if self._out else None


class TestIntegration(unittest.TestCase):
    def test_letting_go_skips_anchored_preoccupation(self):
        cur = _Cur(rows={"FROM memory_anchors": [("preocc:7",)],
                         "FROM preoccupations": [(7, "horology", "k", "s", 2, 999, 999), (8, "rail", "k", "s", 2, 999, 999)]})
        with mock.patch.object(letting_go, "_fizzle_stats", lambda mc, t: (0, 0)):
            out = letting_go.nominate_preoccupations(cur, None)
        self.assertEqual([c["id"] for c in out], [8])             # 7 is held, 8 is nominated

    def test_letting_go_guard_fails_open_without_table(self):
        cur = _Cur(rows={"FROM preoccupations": [(7, "horology", "k", "s", 2, 999, 999)]}, raise_on="memory_anchors")
        with mock.patch.object(letting_go, "_fizzle_stats", lambda mc, t: (0, 0)):
            out = letting_go.nominate_preoccupations(cur, None)
        self.assertEqual([c["id"] for c in out], [7])             # no anchors table -> nothing held

    def test_affect_resonance_signal_from_traces(self):
        now = datetime.now(timezone.utc)
        cur = _Cur(rows={"SELECT user_message": [("thanks Nova, great work",), ("restart the poller",)],
                         "SELECT created_at": [(now - timedelta(hours=h),) for h in (50, 30, 20, 10, 2)]})
        with mock.patch.object(affect, "_table_exists", lambda oc, n: True):
            sigs = affect.signal_resonance(cur)
        by = {s["signal"]: s for s in sigs}
        self.assertGreater(by["resonance"]["dv"], 0); self.assertIn("warm:", by["resonance"]["note"])
        self.assertLessEqual(by["silence"]["da"], 0)

    def test_horror_shelf_is_in_the_pool_and_sampled(self):
        self.assertEqual(len(lexicon.HORROR_POOL), 9)
        self.assertTrue(all(t in lexicon.POOL for t in lexicon.HORROR_POOL))
        self.assertEqual(lexicon.seasoning("", "x"), "")              # emergencies stay unseasoned
        self.assertEqual(lexicon.seasoning("breaking", "x"), "")


# ── 6. REGRESSION ──────────────────────────────────────────────────────────────
class TestRegression(unittest.TestCase):
    def test_silence_baseline_ignores_intra_conversation_gaps(self):
        now = datetime.now(timezone.utc)
        burst = [(now - timedelta(hours=49) - timedelta(seconds=s),) for s in (0, 5, 10, 15)]   # one sitting
        spaced = [(now - timedelta(hours=h),) for h in (200, 150, 100)]
        cur = _Cur(rows={"SELECT user_message": [], "SELECT created_at": sorted(spaced + burst)})
        with mock.patch.object(affect, "_table_exists", lambda oc, n: True):
            sil = [s for s in affect.signal_resonance(cur) if s["signal"] == "silence"][0]
        self.assertGreater(sil["baseline"], 1.0)        # was 0h before the fix

    def test_journal_fetch_splits_on_record_separator(self):
        src = _src("nova_journal.py")
        self.assertIn('"-R", "\\x1e"', src); self.assertIn('split("\\x1e")', src)

    def test_mail_summary_no_longer_emails(self):
        src = _src("nova_mail_deliver.py")
        self.assertNotIn("    send_email(\n", src)
        self.assertNotIn('send_email(f"Nova Morning Mail Summary', src)

    def test_unclaimed_time_has_no_waking_window_or_budget_veto(self):
        src = _src("nova_unclaimed_time.py")
        self.assertNotIn("outside waking window", src)
        self.assertNotIn('return {"mode": "depleted"}', src)


# ── 7. EDGE CASES / DOCUMENTATION ──────────────────────────────────────────────
class TestEdgesAndDocs(unittest.TestCase):
    def test_boosts_stay_modest(self):
        self.assertLessEqual(ms.W_ANCHOR, ms.W_RECENCY)
        self.assertLessEqual(affect.WEIGHTS["resonance_v"], 0.25)

    def test_empty_inputs_are_honest_not_negative(self):
        self.assertIn("holding nothing", anchor.anchor_text([], [], date(2026, 10, 1)))
        self.assertEqual(temporal.notice_text([], date(2026, 10, 1)), "")
        self.assertEqual(affect.tone_score("")[0], 0.0)

    def test_every_new_organ_documents_its_flags(self):
        for mod, flags in ((anchor, ("--dry-run", "--report", "--selftest")), (temporal, ("--dry-run", "--report", "--selftest"))):
            for f in flags:
                self.assertIn(f, mod.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=1)
