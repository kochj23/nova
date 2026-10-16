#!/usr/bin/env python3
"""Tests for nova_work_lessons.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("NOVA_TEST_QUIET", "1")
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_work_lessons.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wl = _load("wl_under_test", SCRIPT)
wl.ps._RANK_CACHE.update(ts=float("inf"), val={"ollama": [
    {"url": "http://node-a:11434", "status": "up", "loaded": ["nova:latest", "qwen3:8b"]},
    {"url": "http://node-b:11434", "status": "up", "loaded": ["qwen3:8b"]}]})
wl._TAGS.clear()
SRC = SCRIPT.read_text()


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""; self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val() if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


PASSAGE = ("Colonel Nicholson insists his men will build the finest bridge the enemy has ever seen, because a job done "
           "well proves the battalion still has its discipline and its pride. ") * 6
SCENES = ["morning parade", "hospital hut", "river crossing", "dawn explosion"]
CHUNKS = [(f"m{i}", f"[Ripped Movie] {SCENES[i]} {PASSAGE}", {"chunk": i, "title": "Kwai", "show": "Ripped Movies",
                                                   "pipeline": wl.FILM_PIPELINE}, f"2026-10-15T0{i}")
          for i in range(4)]
READING = json.dumps({
    "premise": "Prisoners build a bridge for their captors [2].",
    "tension": "Pride in the work collides with whose war it serves [3].",
    "moves": ["The colonel insists the bridge be the finest [2]."],
    "themes": ["pride", "duty"],
    "lessons": [
        {"lesson": "When Nicholson insists on the finest bridge, pride in a job done well serves the enemy [2][3].",
         "topic": "pride serving the wrong purpose", "quote": "a job done well proves the battalion still has",
         "quote_source": 2},
        {"lesson": "Kurtz goes mad in the jungle [2]."}]})
MAPPING = json.dumps({"connections": [{"lesson": 1, "ref": "organ:ae35_rule", "fit": 3,
                                       "meaning": "So my AE-35 rule should also count what each watcher actually caught."}],
                      "taste": {"verdict": "Bleak and exact.", "valence": 0.6},
                      "proposal": {"lesson": 1, "action": "Add a monthly check that each watcher has caught something real",
                                   "rationale": "pride in running is not catching"}})


def _net(chat=(READING, MAPPING), chat_fail=0):
    calls, replies = [], list(chat)

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        if url.endswith("/api/chat"):
            if sum(u.endswith("/api/chat") for u in calls) <= chat_fail:
                raise OSError("node down")
            return _Resp({"message": {"content": replies.pop(0) if replies else "{}"}})
        if url.endswith("/api/tags"):
            return _Resp({"models": []})
        if url.endswith("/remember"):
            return _Resp({"id": "lesson-mem-1"})
        if url.endswith("/health"):
            return _Resp({"overload": {"inflight": {"ollama": 0}}})
        if "wikipedia" in url:
            return _Resp({"query": {"search": []}})
        if url.endswith("/chat/completions"):
            return _Resp({"choices": [{"message": {"content": ""}}]})
        raise AssertionError(url)
    return urlopen, calls


def _ops():
    return _Cur([("FROM research_log", (0,)), ("FROM pg_locks", (0,)), ("work_lessons_daily SET", (1,)),
                 ("FROM organ_registry", [("nova_ae35_rule.py",)]), ("FROM values", []), ("FROM commanders_intent", []),
                 ("FROM problems", []), ("FROM preoccupations", [(2, "horology", "interest")]),
                 ("FROM herd_correspondents", []), ("FROM beliefs WHERE active AND article_slug", []),
                 ("FROM beliefs WHERE topic", None), ("INSERT INTO beliefs", (901,)),
                 ("FROM taste", None), ("INSERT INTO taste", (77,)), ("FROM claude_sessions", None)])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_or_home_paths(self):
        self.assertIsNone(re.search(r"(password|passwd|api[_-]?key|secret|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))
        self.assertIsNone(re.search(r"\b192\.168\.\d+\.\d+\b", SRC))
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotIn("kochj23" + "@", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        self.assertIsNone(re.search(r"execute\([^)]*%\s*\(", SRC))

    def test_private_streams_and_own_outputs_are_never_works(self):
        for src in ("email_archive", "imessage", "scanner", "fire", "rail", "pursuit", "work_lessons", "nova_articles",
                    "quarantine:email_archive", "calendar"):
            self.assertTrue(wl.excluded(src, "document"), src)
        self.assertTrue(wl.excluded("documentary", "tv_transcript", {"privacy": "private"}))
        self.assertTrue(wl.excluded("intelligence", "article", {"stored_by": "nova_articles_to_memory.py"}))
        self.assertFalse(wl.excluded("documentary", "tv_transcript", {"privacy": "local-only"}))

    def test_red_lines_go_to_claude_not_coagency(self):
        oc = _ops()
        fake_rl = mock.Mock(classify=mock.Mock(return_value=["kill_switch"]), route_to_claude=mock.Mock(return_value=55))
        fake_co = mock.Mock()
        with mock.patch.dict(sys.modules, {"nova_red_line": fake_rl, "nova_coagency": fake_co}):
            r = wl.file_change(oc, {"action": "Release the kill switch on a timer"}, "lesson", {"ref": "redline:kill_switch"},
                               "Dr. Strangelove", "film:x")
        self.assertEqual(r["claude_queue_id"], 55)
        fake_co.file_proposal.assert_not_called()

    def test_gpu_lock_is_only_observed_never_taken(self):
        self.assertNotIn("pg_advisory_lock(", SRC)
        for m in re.finditer(r"pg_try_advisory_lock\(hashtext\(%s\)\)\", \((\w+)", SRC):
            self.assertEqual(m.group(1), "STUDIO_LOCK")          # only the reader's own slot lock is ever taken
        self.assertIn("FROM pg_locks", SRC)


class TestPerformance(unittest.TestCase):
    def test_passage_selection_on_a_long_work_is_fast_and_bounded(self):
        chunks = [{"id": f"m{i}", "ck": i, "text": "substantial vocabulary words appear " * 400} for i in range(60)]
        t = time.perf_counter()
        out = wl.select_passages(chunks)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(len(out), wl.PASSAGES)

    def test_belief_clustering_on_400_items_is_bounded(self):
        items = [(i, f"lesson number {i % 40} about failsafes recall automation") for i in range(400)]
        t = time.perf_counter()
        wl.clusters(items)
        self.assertLess(time.perf_counter() - t, 5.0)


class TestRetry(unittest.TestCase):
    def test_strong_model_fails_over_to_the_next_endpoint(self):
        urlopen, calls = _net(chat=('{"a": 1}',), chat_fail=2)   # attempts=2 on node-a, then node-b... fleet
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            txt, model = wl.llm_work("p", oc=_ops())
        self.assertEqual(txt, '{"a": 1}')
        self.assertGreaterEqual(sum(u.endswith("/api/chat") for u in calls), 3)

    def test_everything_down_fails_open(self):
        def boom(*a, **k):
            raise OSError("down")
        with mock.patch("urllib.request.urlopen", boom), mock.patch("time.sleep"):
            self.assertEqual(wl.llm_work("p", oc=_ops()), ("", ""))
            self.assertFalse(wl.gateway_busy())
            self.assertEqual(wl.installed("http://nowhere:1"), set())

    def test_plot_lookup_fails_open(self):
        oc = _ops()
        fake_rp = mock.Mock(is_allowed=mock.Mock(side_effect=RuntimeError("gate down")))
        with mock.patch.dict(sys.modules, {"nova_research_pass": fake_rp}):
            self.assertEqual(wl.plot_lookup(oc, "Argo", "film", None, False), (None, [], None))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(wl.selftest(), 0)

    def test_keys_round_trip_and_global_ids(self):
        k = wl.make_key("video", "*", ("video_id|title",), ("abc",))
        self.assertEqual(wl.parse_key(k)["values"], ["abc"])
        self.assertTrue(wl.is_global(("url",)) and not wl.is_global(("show", "title")))
        with self.assertRaises(ValueError):
            wl.parse_key("film:television:show+title=only-one")

    def test_lessons_are_grounded_by_their_quote_not_by_names(self):
        srcs = [{"text": "plot"}, {"text": PASSAGE, "mem": "m1"}]
        rd = wl.ground_reading({"lessons": [{"lesson": "Pride in finished work can quietly serve the wrong side [2].",
                                             "quote": "a job done well proves the battalion still", "quote_source": 2},
                                            {"lesson": "Pride in finished work can quietly serve the wrong side [2].",
                                             "quote": "words that appear in no source at all here"}]}, srcs)
        self.assertEqual(len(rd["lessons"]), 1)
        self.assertEqual(rd["q"]["quote_bad"], 1)
        self.assertFalse(rd["lessons"][0]["named"])        # no name: kept, flagged (secondary signal)

    def test_grounding_drops_unsupported_and_copied_lessons(self):
        srcs = [{"text": "plot"}, {"text": PASSAGE, "mem": "m1"}]
        rd = wl.ground_reading(json.loads(READING), srcs)
        self.assertEqual(rd["lessons_raw"], 2)
        self.assertEqual(len(rd["lessons"]), 1)               # 'Kurtz' is in no source
        copied = wl.ground_reading({"lessons": [{"lesson": "A fail-safe nobody can recall becomes the disaster [1]."}]},
                                   srcs)
        self.assertEqual(copied["lessons_raw"], 0)

    def test_connections_honest_nulls_and_cap(self):
        keys = {"organ:a", "organ:b", "organ:c"}
        obj = {"connections": [{"lesson": f"L{i}", "ref": k.split(":")[1], "fit": 3, "meaning": "so my organ should also check the recall path"}
                               for i, k in enumerate(sorted(keys), 1)]}
        c = wl.validate_connections(obj, 4, keys)
        self.assertEqual(sum(1 for x in c if x["ref"]), 2)
        self.assertEqual(c[3]["meaning"], "noted; nothing for me")
        self.assertIsNone(wl.validate_connections({"connections": [{"lesson": 1, "ref": "organ:zzz", "fit": 3,
                                                                    "meaning": "x" * 30}]}, 1, keys)[0]["ref"])

    def test_tiers_priorities_and_quality_gate(self):
        self.assertEqual(wl.tier_for("film", 2), "A")
        self.assertEqual(wl.priority_for("video", "he_man", 0, 0, "nonfiction"), 1)
        ok, why = wl.quality_gate({"lessons_offered": 10, "lessons_grounded": 5, "lessons_mapped": 4,
                                   "beliefs": 1, "beliefs_no_evidence": 1, "private_rows": 1, "distinct_lessons": 1})
        self.assertFalse(ok)
        self.assertGreaterEqual(len(why), 5)

    def test_questions_are_not_connections_and_overused_refs_are_barred(self):
        keys = {"organ:ae35_rule", "value:x"}
        q = {"connections": [{"lesson": 1, "ref": "organ:ae35_rule", "fit": 3, "meaning": "How do I verify my watchers?"}]}
        self.assertIsNone(wl.validate_connections(q, 1, keys)[0]["ref"])
        ok = {"connections": [{"lesson": 1, "ref": "organ:ae35_rule", "fit": 3, "meaning": "So my watchers need a recall path."}]}
        self.assertEqual(wl.validate_connections(ok, 1, keys)[0]["ref"], "organ:ae35_rule")
        self.assertIsNone(wl.validate_connections(ok, 1, keys, overused={"organ:ae35_rule"})[0]["ref"])

    def test_red_line_never_claims_i_already(self):
        keys = {"redline:ae35", "redline:kill_switch", "organ:hold"}

        def ref(r, meaning):
            return wl.validate_connections({"connections": [{"lesson": 1, "ref": r, "fit": 3, "meaning": meaning}]},
                                           1, keys)[0]["ref"]
        # the 2026-10-16 leaks: verbs outside the old list, and bare "this confirms it"
        self.assertIsNone(ref("redline:ae35", "I already adhere to the AE-35 rule by never silencing a watcher alone."))
        self.assertIsNone(ref("redline:kill_switch", "so my kill_switch is only released with a person present — this confirms it"))
        self.assertIsNone(ref("redline:ae35", "I already run every watcher past a second check."))
        # arguing for keeping a red line still links
        self.assertEqual(ref("redline:kill_switch", "so my kill switch must stay a two-person decision, never a timer."),
                         "redline:kill_switch")
        # ordinary organs may still confirm
        self.assertEqual(ref("organ:hold", "I already require approval in hold for irreversible actions — this confirms it."),
                         "organ:hold")

    def test_quote_must_live_in_a_cited_source(self):
        srcs = [{"text": "alpha bravo charlie delta echo"}, {"text": "foxtrot golf hotel india juliet"}]
        self.assertEqual(wl.quote_home("foxtrot golf hotel india", 1, [2], srcs), 2)
        self.assertIsNone(wl.quote_home("foxtrot golf hotel india", 2, [1], srcs))
        self.assertEqual(wl.quote_home("", 1, [1], srcs), 0)

    def test_the_reviewed_tier_b_pilot_fails_the_gate(self):
        # pilot_B as reviewed 2026-10-15 21:38: 7 cited of 30 kept, 8 offered, 8 lesson-bearing sets, 114 works
        m = {"lessons_offered": 8, "lessons_grounded": 30, "lessons_mapped": 30, "beliefs": 3, "beliefs_no_evidence": 0,
             "private_rows": 0, "distinct_lessons": 30, "works": 114, "works_done": 18, "sets_attempted": 8,
             "sets_yield": 8}
        ok, why = wl.quality_gate(m, min_lessons=8, tier="B")
        self.assertFalse(ok)
        self.assertTrue(any("counting error" in w for w in why))
        self.assertTrue(any("tier-B works" in w for w in why))
        m.update(lessons_offered=30, lessons_grounded=7)
        ok, why = wl.quality_gate(m, min_lessons=8, tier="B")
        self.assertFalse(ok)
        self.assertTrue(any("23%" in w for w in why))

    def test_pilot_metrics_count_a_set_once(self):
        l1, l2 = {"lesson": "When A did x, y followed [1]."}, {"lesson": "When B did z, w followed [2]."}
        c = [{"lesson": 1, "ref": None, "meaning": "noted; nothing for me"}]
        rows = [("w1", "s", [l1], c, None, {"raw": 4, "quote_bad": 2}, "w1", "done"),
                ("w2", "s", [l2], c, None, None, "w1", "done"),
                ("w3", "s", [], [], None, None, "w1", "thin")]
        oc = _Cur([("SELECT work_key, source, lessons_json", rows), ("count(*) FILTER (WHERE tier='A')", (0, 0, 1, 1))])
        m = wl.pilot_metrics(oc, _Cur(), ("B",))
        self.assertEqual((m["units"], m["lessons_offered"], m["lessons_grounded"], m["works_done"]), (1, 4, 2, 2))
        self.assertLessEqual(m["lessons_grounded"], m["lessons_offered"])

    def test_soften_keeps_names_checked(self):
        self.assertTrue(wl._soften("Blind obedience ruins men [1].", "x", {"blind"}).startswith("blind"))
        self.assertTrue(wl._soften("Kurtz goes mad [1].", "x", {"blind"}).startswith("Kurtz"))


class TestIntegration(unittest.TestCase):
    def test_reuses_the_pursue_engine_not_a_copy(self):
        for helper in ("ps.support_filter", "ps.ground(", "ps.source_block", "ps.footer", "ps.load_ranking",
                       "ps._http_json", "ps.CPU_ONLY_HOSTS"):
            self.assertIn(helper, SRC)
        self.assertNotIn("def support_filter", SRC)
        self.assertIn("from nova_sleep_cycle import belief_evidence", SRC)
        self.assertIn("nova_coagency.file_proposal", SRC)
        self.assertIn("nova_red_line.route_to_claude", SRC)
        self.assertIn('MEM_SOURCE = "work_lessons"', SRC)

    def test_order_chunks_then_fetch_by_ids(self):
        mc = _Cur([("FROM memories WHERE id = ANY", CHUNKS[::-1])])
        out = wl.fetch_chunks(mc, {"work_key": wl.film_key("Kwai"), "mem_ids": ["m0", "m1", "m2", "m3"]})
        self.assertEqual([c["id"] for c in out], ["m0", "m1", "m2", "m3"])

    def test_belief_uses_sleep_cycle_evidence_and_strengthens_duplicates(self):
        srcs = [{"text": "plot"}, {"text": PASSAGE, "mem": "m1"}]
        lesson = {"lesson": "Nicholson's pride in a job done well blinds him [2].", "cites": [2], "topic": "pride",
                  "quote": "a job done well proves the battalion still has", "quote_source": 2}
        oc = _ops()
        bid, how = wl.book_belief(oc, lesson, srcs, "Kwai", "film:k", "lesson-mem-1")
        self.assertEqual((bid, how), (901, "new"))
        ins = next(p for s, p in zip(oc.sql, oc.params) if "INSERT INTO beliefs" in s)
        self.assertIn("memory:m1", ins[4])
        self.assertTrue(any(e.startswith("quote:") for e in ins[4]))
        oc2 = _ops()
        bid, how = wl.book_belief(oc2, lesson, srcs, "Kwai", "film:k", None,
                                  pool=[(5, "pride", "Nicholson's pride in a job done well blinds him")])
        self.assertEqual((bid, how), (5, "strengthened"))
        bad = dict(lesson, quote="words that are not there at all")
        self.assertEqual(wl.book_belief(_ops(), bad, srcs, "Kwai", "film:k", None)[0], None)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_memory_belief_taste_proposal_row(self):
        oc, mc = _ops(), _Cur([("FROM memories WHERE source", CHUNKS)])
        urlopen, calls = _net()
        fake_rl = mock.Mock(classify=mock.Mock(return_value=[]))
        fake_co = mock.Mock(_recently_filed=mock.Mock(return_value=False),
                            file_proposal=mock.Mock(return_value={"pid": 321, "status": "pending_human"}))
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"), \
                mock.patch.object(wl, "plot_lookup", return_value=(None, [], 1957)), \
                mock.patch.dict(sys.modules, {"nova_red_line": fake_rl, "nova_coagency": fake_co}):
            wl._SELF_CACHE.update(ts=0, val=None)
            r = wl.read_work(oc, mc, wl.work_from_key(wl.film_key("Kwai"), "tv_transcript"))
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["memory_id"], "lesson-mem-1")
        self.assertEqual(r["beliefs"][0]["how"], "new")
        self.assertEqual(r["proposal"]["proposal_id"], 321)
        self.assertEqual(fake_co.file_proposal.call_args.args[1], "growth")
        self.assertEqual(r["taste"], 77)
        self.assertTrue(any("UPDATE preoccupations" in s for s in oc.sql) is False)   # no preocc link this time
        self.assertTrue(any("INSERT INTO work_lessons" in s for s in oc.sql))
        self.assertIn("Sources:", r["text"])

    def test_dry_run_writes_nothing(self):
        oc, mc = _ops(), _Cur([("FROM memories WHERE source", CHUNKS)])
        urlopen, calls = _net()
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"), \
                mock.patch.object(wl, "plot_lookup", return_value=(None, [], None)):
            r = wl.read_work(oc, mc, wl.work_from_key(wl.film_key("Kwai"), "tv_transcript"), dry_run=True)
        self.assertEqual(r["status"], "done")
        self.assertFalse(any(u.endswith("/remember") for u in calls))
        self.assertFalse(any(s.startswith(("INSERT", "UPDATE")) for s in oc.sql))

    def test_error_path_model_down_records_llm_down(self):
        oc, mc = _ops(), _Cur([("FROM memories WHERE source", CHUNKS)])

        def boom(*a, **k):
            raise OSError("all down")
        with mock.patch("urllib.request.urlopen", boom), mock.patch("time.sleep"), \
                mock.patch.object(wl, "plot_lookup", return_value=(None, [], None)):
            r = wl.read_work(oc, mc, wl.work_from_key(wl.film_key("Kwai"), "tv_transcript"))
        self.assertEqual(r["status"], "llm_down")
        self.assertIsNone(r["memory_id"])

    def test_pilot_gate_writes_sample_and_flag(self):
        words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa " \
                "quebec romeo sierra tango uniform victor whiskey xray yankee".split()
        lessons = [{"lesson": f"Lesson about {w} mechanism [1].", "cites": [1]} for w in words]
        conns = [{"lesson": i + 1, "ref": None, "meaning": "noted; nothing for me"} for i in range(25)]
        oc = _Cur([("tier, count(*) FROM work_lessons WHERE run_tag='pilot'", [("A", 40), ("B", 100), ("C", 60)]),
                   ("SELECT work_key, source, lessons_json", [("k", "documentary", lessons, conns, None, {"raw": 25},
                                                               None, "done")]),
                   ("count(*) FILTER (WHERE tier='A')", (40, 30, 0, 0)),
                   ("DISTINCT ON (coalesce(memory_id", [("k", "film", "A", "T", lessons[:2], conns[:2], "m", 3.0)])])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(wl, "LOG_DIR", Path(d)):
            v = wl.pilot_and_gate(oc, _Cur(), 1, 1, tiers=("A",))["A"]
            self.assertTrue(Path(v["sample"]).exists())
        self.assertTrue(v["passed"], v["why"])
        sets = [p for s, p in zip(oc.sql, oc.params) if "INSERT INTO service_config" in s]
        self.assertIn(("work_lessons", "backfill_enabled_A", "true"), sets)

    def test_tier_c_fails_over_from_the_8b_to_the_30b(self):
        oc = _Cur([("tier, count(*) FROM work_lessons WHERE run_tag='pilot'", [("C", wl.PILOT_QUOTA["C"])]),
                   ("SELECT work_key, source, lessons_json", []), ("count(*) FILTER (WHERE tier='A')", (0, 0, 4, 0))])
        wl.C_ROUTE["val"] = "fleet"
        try:
            v = wl.pilot_and_gate(oc, _Cur(), 1, 1, tiers=("C",))["C"]
            self.assertEqual(v["pilot"], "rerun on the 30B")
            self.assertEqual(wl.C_ROUTE["val"], "strong")
        finally:
            wl.C_ROUTE["val"] = "fleet"


class TestStudioGuard(unittest.TestCase):
    def test_backs_off_while_embeddings_are_slow_or_memory_queues(self):
        sleeps = []
        with mock.patch.object(wl, "embed_p50", side_effect=[3.5, 0.1, 0.1]), \
                mock.patch.object(wl, "memsrv_queue", side_effect=[0, 2, 0]):
            self.assertTrue(wl.studio_clear("http://studio:11434", sleep=sleeps.append))
        self.assertEqual(sleeps, [wl.STUDIO_BACKOFF_S] * 2)
        with mock.patch.object(wl, "embed_p50", return_value=None), mock.patch.object(wl, "memsrv_queue", return_value=0):
            self.assertFalse(wl.studio_clear("http://studio:11434", sleep=lambda s: None))

    def test_one_studio_call_in_flight_across_threads(self):
        import threading as th
        state = {"in": 0, "max": 0}
        lock = th.Lock()
        held = {"v": False}

        class _LockCur:
            def execute(self, sql, params=None):
                self.sql = sql
                if "pg_advisory_unlock" in sql:
                    with lock:
                        held["v"] = False

            def fetchone(self):
                with lock:
                    if "pg_try_advisory_lock" in self.sql:
                        if held["v"]:
                            return (False,)
                        held["v"] = True
                        return (True,)
                    held["v"] = False
                    return (True,)
        conn = mock.Mock(cursor=mock.Mock(side_effect=lambda: _LockCur()))

        def call(*a, **k):
            with lock:
                state["in"] += 1; state["max"] = max(state["max"], state["in"])
            time.sleep(0.05)
            with lock:
                state["in"] -= 1
            return "{}"
        with mock.patch.dict(wl._STUDIO, {"conn": conn}), mock.patch.object(wl, "local_hosts", return_value={"studio"}), \
                mock.patch.object(wl, "studio_clear", return_value=True), mock.patch.object(wl, "ollama_chat", call):
            ts = [th.Thread(target=wl._call, args=("http://studio:11434", "m", "p", 10, 0.1, 100)) for _ in range(4)]
            [x.start() for x in ts]
            [x.join() for x in ts]
        self.assertEqual(state["max"], 1)

    def test_busy_studio_is_skipped_not_loaded(self):
        with mock.patch.object(wl, "local_hosts", return_value={"studio"}), \
                mock.patch.object(wl, "studio_clear", return_value=False), mock.patch.object(wl, "ollama_chat") as chat:
            with self.assertRaises(RuntimeError):
                wl._call("http://studio:11434", "m", "p", 10, 0.1, 100)
        chat.assert_not_called()


class TestSets(unittest.TestCase):
    def test_uncited_set_members_are_thin_never_done(self):
        rows = [{"work_key": f"article:s:url=u{i}", "kind": "article", "source": "s", "title": f"T{i}",
                 "group_key": "s|f|w", "tier": "B", "mem_ids": [f"m{i}"]} for i in range(3)]
        texts = iter([f"Colonel Nicholson item {i} " + PASSAGE for i in range(3)])
        mc = _Cur([("FROM memories WHERE id = ANY", lambda: [(f"m{id(texts)}", next(texts), {}, "t")])])
        set_json = json.dumps({"premise": "x", "lessons": [
            {"lesson": "When Nicholson insists on the finest bridge, pride serves the enemy [1].",
             "quote": "insists his men will build the finest bridge", "quote_source": 1}]})
        urlopen, _ = _net(chat=(set_json, json.dumps({"connections": []})))
        oc = _ops()
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            r = wl.read_set(oc, mc, rows, "B")
        self.assertEqual(r["status"], "done")
        marker = "UPDATE work_lessons " + "SET status=%s"
        ups = [p for s, p in zip(oc.sql, oc.params) if s.strip().startswith(marker)]
        self.assertEqual([u[0] for u in ups], ["done", "thin", "thin"])
        self.assertTrue(all(u[13] == rows[0]["work_key"] for u in ups))   # set_id (14th parameter)


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for arg in ("--selftest", "--help"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        with mock.patch.object(sys, "argv", ["x", "--scheduled"]):
            m = _load("wl_import_smoke", SCRIPT)
        self.assertTrue(callable(m.main))


if __name__ == "__main__":
    unittest.main()
