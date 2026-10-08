#!/usr/bin/env python3
"""Tests for nova_relationship.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame) plus the lockbox recall filter in memory_server.py and the
good-thing signal in nova_affect.py. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("NOVA_TEST_QUIET", "1")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rel = _load("rel", SCRIPTS / "nova_relationship.py")
SRC = (SCRIPTS / "nova_relationship.py").read_text()
MEMSRV_SRC = (SCRIPTS.parent / "memory_server.py").read_text()
NOW = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)


class FakeCur:
    """Scripted cursor: responses is a list of (sql-substring, rows) consumed in order of match."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.sql = []
        self.rowcount = 1
        self._rows = []

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        self._rows = []
        for i, (frag, rows) in enumerate(self.responses):
            if frag in sql:
                self._rows = rows
                self.responses.pop(i)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def executed(self, frag):
        return [(s, a) for s, a in self.sql if frag in s]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|passwd|token|secret)\s*=\s*['\"][^'\"]+['\"]", SRC, re.I))
        self.assertNotRegex(SRC, r"/Users/[a-z]+/")

    def test_sql_is_parameterized(self):
        for m in re.finditer(r"execute\(f\"", SRC):
            self.fail(f"f-string SQL at {m.start()}")

    def test_content_guard_blocks_sexual_and_employer(self):
        self.assertFalse(rel.is_clean("something nsfw"))
        self.assertFalse(rel.is_clean("a Disney update"))
        self.assertTrue(rel.is_clean("the clustered toaster"))

    def test_no_relationship_content_in_public_source(self):
        # the repo is public: the seed lives in the gitignored private/ dir, not in code
        ledger, lexicon = rel.load_seed()
        for row in ledger:
            self.assertNotIn(row[2], SRC)
        for row in lexicon:
            self.assertNotIn(f'"{row[0]}"', SRC)
        self.assertIn('"private" / "relationship_seed.json"', SRC)

    def test_load_seed_absent_is_empty(self):
        self.assertEqual(rel.load_seed(Path("/nonexistent/x.json")), ([], []))

    def test_brief_never_public(self):
        self.assertEqual(rel.brief(public=True), "")

    def test_interlude_rejects_unclean_or_uncited(self):
        valid = {"t1": "I really like BSD a great deal"}
        self.assertEqual(rel.validate_proposals(
            [{"kind": "taught", "text": "He likes porn and BSD a great deal.", "trace_id": "t1"}], valid, []), [])
        self.assertEqual(rel.validate_proposals(
            [{"kind": "taught", "text": "He likes BSD a great deal.", "trace_id": "forged"}], valid, []), [])


class TestPerformance(unittest.TestCase):
    def test_signals_on_10k_messages_fast(self):
        msgs = [(NOW - timedelta(minutes=5 * i), "short reply " * (i % 7)) for i in range(10000)]
        msgs.sort()
        t = time.time()
        rel.sig_late_nights(msgs, NOW)
        rel.sig_terse(msgs, NOW)
        rel.sig_silence(msgs, NOW)
        self.assertLess(time.time() - t, 2.0)

    def test_render_brief_bounded(self):
        ledger = [("taught", f"fact number {i} about him", 0.5, None) for i in range(5000)]
        t = time.time()
        b = rel.render_brief(ledger, [], 900)
        self.assertLess(time.time() - t, 1.0)
        self.assertLessEqual(len(b), 900)


class TestRetry(unittest.TestCase):
    def test_post_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("slack down")
        import nova_config
        with patch.object(nova_config, "post_both", flaky), patch.object(rel.time, "sleep", lambda s: None):
            self.assertTrue(rel._post("hello"))
        self.assertEqual(calls["n"], 3)

    def test_lockbox_recall_retries(self):
        calls = {"n": 0}

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"memories": [{"id": "m1", "metadata": {"boxed": True}},
                                                {"id": "m2", "metadata": {}}]}).encode()

        def urlopen(url, timeout=0):
            calls["n"] += 1
            self.assertIn("include_boxed=true", url)
            if calls["n"] < 3:
                raise OSError("down")
            return R()
        with patch.object(rel.urllib.request, "urlopen", urlopen), patch.object(rel.time, "sleep", lambda s: None):
            out = rel.lockbox_recall("the threat")
        self.assertEqual([m["id"] for m in out], ["m1"])     # only boxed ones come through this door
        self.assertEqual(calls["n"], 3)

    def test_llm_fails_open(self):
        # RETRY GAP: llm — node failover, no per-node retry; all nodes down -> "" (no exception)
        with patch.object(rel.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(rel.llm("x", "y"), "")

    def test_quiet_mode_fails_open(self):
        with patch.object(rel, "_connect", side_effect=OSError("pg down")):
            self.assertEqual(rel.quiet_mode(), {"active": False})


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertTrue(rel.demo())

    def test_hysteresis(self):
        self.assertTrue(rel.decide(False, 0.55, 2))
        self.assertFalse(rel.decide(False, 0.55, 1))      # one signal alone is never a stretch
        self.assertTrue(rel.decide(True, 0.31, 0))
        self.assertFalse(rel.decide(True, 0.29, 3))

    def test_lexicon_cooldown(self):
        rows = [("a", "m", 7, NOW - timedelta(days=2)), ("b", "m", 7, NOW - timedelta(days=9)), ("c", "m", 7, None)]
        self.assertEqual([p for p, _ in rel.lexicon_available(rows, NOW)], ["b", "c"])

    def test_splice_replaces_placeholder_once(self):
        doc = "# USER\n\nfacts\n\n## Context\n\n_(Build this over time.)_\n\n---\n\nmore filler"
        s1 = rel.splice_user_doc(doc, "## Us\n- x")
        self.assertNotIn("Build this over time", s1)
        s2 = rel.splice_user_doc(s1, "## Us\n- y")
        self.assertEqual(s2.count(rel.DOC_BEGIN), 1)
        self.assertIn("- y", s2)
        self.assertNotIn("- x", s2)

    def test_silence_needs_history(self):
        self.assertEqual(rel.sig_silence([], NOW)["v"], 0.0)

    def test_late_nights_counts_local_small_hours(self):
        two = NOW.astimezone().replace(hour=2, minute=0)
        msgs = [(two - timedelta(days=d), "hi") for d in range(3)]
        self.assertEqual(rel.late_nights(msgs, NOW), 3)

    def test_seed_ledger_is_clean_except_rules(self):
        ledger, lexicon = rel.load_seed()
        if not ledger:
            self.skipTest("private seed file absent (expected off the home fleet)")
        self.assertEqual(len({rel.text_hash(t) for _, _, t, _, _ in ledger}), len(ledger))
        for kind, _d, text, ev, w in ledger:
            self.assertIn(kind, rel.LEDGER_KINDS)
            self.assertTrue(ev)
            self.assertTrue(0 < w <= 1)
            if kind != "never_do":
                self.assertTrue(rel.is_clean(text), text)


class TestIntegration(unittest.TestCase):
    def test_reuses_affect_helpers(self):
        self.assertIn("from nova_affect import tone_score", SRC)
        self.assertIn("from nova_affect import silence_excess", SRC)

    def test_quiet_flag_key(self):
        cur = FakeCur()
        rel._set_quiet(cur, {"active": True})
        sql, args = cur.executed("service_config")[0]
        self.assertIn("'nova_quiet_mode','state'", sql)

    def test_memory_server_excludes_boxed_by_default(self):
        self.assertIn("BOXED_CLAUSE = \"AND (metadata->>'boxed') IS DISTINCT FROM 'true'\"", MEMSRV_SRC)
        self.assertIn("include_boxed: bool = Query(False)", MEMSRV_SRC)
        self.assertIn("if not include_boxed:", MEMSRV_SRC)
        self.assertIn(":b1", MEMSRV_SRC)                    # boxed recalls never share a cache key

    def test_affect_reads_good_things(self):
        aff = _load("aff_rel", SCRIPTS / "nova_affect.py")
        cur = FakeCur([("to_regclass", [("good_things",)]),
                       ("FROM good_things", [("A mild day.", "telemetry.weather:24h")])])
        s = aff.signal_good_thing(cur)[0]
        self.assertGreater(s["dv"], 0)
        self.assertIn("A mild day", s["note"])
        cur = FakeCur([("to_regclass", [("good_things",)]), ("FROM good_things", [])])
        self.assertEqual(aff.signal_good_thing(cur)[0]["dv"], 0)

    def test_letting_go_hook_proposes_not_deletes(self):
        lg = (SCRIPTS / "nova_letting_go.py").read_text()
        self.assertIn("propose_lockboxes(oc, mc)", lg)
        self.assertNotRegex(SRC, r"DELETE FROM memories")


class TestFunctional(unittest.TestCase):
    def test_stretch_opens_and_sends_one_line_in_window(self):
        sig = [{"signal": "late_nights", "v": 1.0, "note": "n"}, {"signal": "terse", "v": 1.0, "note": "n"},
               {"signal": "silence", "v": 0.0, "note": "n"}, {"signal": "quiet_house", "v": 0.0, "note": "n"}]
        started = NOW - timedelta(hours=2)
        cur = FakeCur([("FROM hard_stretch WHERE ended_at IS NULL", [(4, 0.6, None, started)]),
                       ("max(line_sent_at)", [(None,)])])
        posted = []
        with patch.object(rel, "his_message_times", lambda c: []), \
                patch.object(rel, "sig_late_nights", lambda m, n: sig[0]), \
                patch.object(rel, "sig_terse", lambda m, n: sig[1]), \
                patch.object(rel, "sig_silence", lambda m, n: sig[2]), \
                patch.object(rel, "sig_quiet_house", lambda c, n: sig[3]), \
                patch.object(rel, "in_line_window", lambda t: True), \
                patch.object(rel, "_post", lambda t: posted.append(t) or True):
            res = rel.run_stretch(cur, now=NOW)
        self.assertTrue(res["active"])
        self.assertEqual(len(posted), 1)
        self.assertTrue(cur.executed("SET line_text"))
        state = json.loads(cur.executed("service_config")[0][1][0])
        self.assertTrue(state["active"])
        self.assertEqual(state["suggest"], rel.QUIET_SUGGEST)

    def test_stretch_no_line_outside_window_and_closes(self):
        quiet = [{"signal": k, "v": 0.0, "note": "n"} for k in rel.W_STRETCH]
        cur = FakeCur([("FROM hard_stretch WHERE ended_at IS NULL", [(4, 0.6, None, NOW - timedelta(hours=5))])])
        with patch.object(rel, "his_message_times", lambda c: []), \
                patch.object(rel, "sig_late_nights", lambda m, n: quiet[0]), \
                patch.object(rel, "sig_terse", lambda m, n: quiet[1]), \
                patch.object(rel, "sig_silence", lambda m, n: quiet[2]), \
                patch.object(rel, "sig_quiet_house", lambda c, n: quiet[3]), \
                patch.object(rel, "_post", lambda t: self.fail("must not post")):
            res = rel.run_stretch(cur, now=NOW)
        self.assertFalse(res["active"])
        self.assertTrue(cur.executed("SET ended_at=now()"))

    def test_good_thing_writes_once_with_evidence(self):
        cur = FakeCur([("FROM good_things WHERE day", [])])
        with patch.object(rel, "good_thing_candidates",
                          lambda c: [("weather", "A mild, clean-air day.", "telemetry.weather:24h")]):
            out = rel.run_good_thing(cur)
        self.assertEqual(out[0], "weather")
        ins = cur.executed("INSERT INTO good_things")
        self.assertEqual(ins[0][1][1:], ("weather", "A mild, clean-air day.", "telemetry.weather:24h"))

    def test_good_thing_honest_noop(self):
        cur = FakeCur([("FROM good_things WHERE day", [])])
        with patch.object(rel, "good_thing_candidates", lambda c: []):
            self.assertIsNone(rel.run_good_thing(cur))
        self.assertFalse(cur.executed("INSERT INTO good_things"))

    def test_box_sets_flag_and_keeps_prior(self):
        mc = FakeCur([("SELECT metadata FROM memories", [({"source_note": "x"},)])])
        oc = FakeCur()
        self.assertTrue(rel.box(mc, oc, "m1", "a threat"))
        upd = mc.executed("UPDATE memories")[0]
        self.assertTrue(json.loads(upd[1][0])["boxed"])
        ins = oc.executed("INSERT INTO lockbox")[0]
        self.assertEqual(json.loads(ins[1][3]), {"source_note": "x"})

    def test_interlude_inserts_only_grounded(self):
        rows = [("t1", NOW, "the patio fridge sensor finally works", "Nice, the patio fridge reads 38F")]
        cur = FakeCur([("FROM gateway_traces", rows), ("FROM relationship_ledger WHERE active", [])])
        reply = json.dumps([{"kind": "first", "text": "The patio fridge sensor finally worked.", "trace_id": "t1"},
                            {"kind": "taught", "text": "He loves submarines deeply.", "trace_id": "t1"}])
        with patch.object(rel, "llm", lambda p, s: reply):
            out = rel.interlude(cur)
        self.assertEqual([p["text"] for p in out], ["The patio fridge sensor finally worked."])
        self.assertEqual(len(cur.executed("INSERT INTO relationship_ledger")), 1)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_relationship.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
