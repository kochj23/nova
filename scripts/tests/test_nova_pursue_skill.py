#!/usr/bin/env python3
"""Tests for nova_pursue_skill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("NOVA_TEST_QUIET", "1")
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pursue_skill.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load("ps_under_test", SCRIPT)
SRC = SCRIPT.read_text()
SLUGS = ["pursue-fascination-the-watch-fishbowl", "pursue-local-news-interest", "pursue-interest-geopolitics",
         "pursue-fascination-he-man-and-80s-cartoons", "pursue-interest-infrastructure", "pursue-interest-email",
         "pursue-interest-nightly", "pursue-interest-sports", "pursue-interest-nova-articles"]


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
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


CARD = ("pursue-interest-geopolitics", "Pursue Interest: Geopolitics", "implemented",
        json.dumps(["Identify actors", "Collect news", "Summarize"]), "User confirms understanding", "retire")
GEO_MEM = [("m1", "[NV] Russia launches strikes on Odesa Oblast highway, injuring five.", "geopolitics",
            {"url": "https://example.org/odesa", "title": "Strikes on Odesa"}),
           ("m2", "[NV] Lithuania moves to lift nuclear weapons ban as Kremlin threatens measures.", "geopolitics",
            {"url": "https://example.org/lt", "title": "Lithuania nuclear ban"})]
NOTE = ("Russia struck the Odesa Oblast highway and five people were hurt [1]. Lithuania is moving to lift its "
        "nuclear weapons ban while the Kremlin threatens measures [2]. TikTok is driving all of it [1]. "
        "Grim, and very ordinary.\nNEXT: read what the Kremlin's measures are.")


def _ops(card=CARD, thread=None, wakes_days=(0,)):
    return _Cur([("FROM nova_skills", card), ("FROM pursuit_threads WHERE topic=", thread),
                 ("count(DISTINCT", wakes_days), ("FROM research_log", (0,)),
                 ("FROM incidents", []), ("FROM email_threat_scan", [])])


def _net(chat=NOTE, chat_fail=0, remember_fail=0):
    calls = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        body = None if isinstance(req, str) or req.data is None else json.loads(req.data.decode())
        calls.append((url, body))
        if url.endswith("/chat/completions"):
            if sum(u.endswith("/chat/completions") for u, _ in calls) <= chat_fail:
                raise OSError("node down")
            return _Resp({"choices": [{"message": {"content": chat}}]})
        if url.endswith("/remember"):
            if sum(u.endswith("/remember") for u, _ in calls) <= remember_fail:
                raise OSError("memsrv blip")
            return _Resp({"id": "mem-42"})
        if "/recall?" in url:
            return _Resp({"memories": []})
        raise AssertionError(url)
    return urlopen, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|passwd|api[_-]?key|secret|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))
        self.assertNotIn("sk-", SRC)

    def test_sql_is_parameterized(self):
        for m in re.finditer(r"execute\(\s*f[\"']", SRC):
            self.fail(f"f-string SQL at {m.start()}")
        self.assertNotIn("% (", SRC.split("def gather")[1].split("def _web")[0])

    def test_never_acts_on_the_world(self):
        # read + note only: no mail, Slack, posting, coagency filing, or alert channels from here
        for bad in ("nova_send_mail", "smtplib", "post_both", "notify(", "file_proposal", "slack", "chat.postMessage"):
            self.assertNotIn(bad, SRC)
        self.assertIn("do NOT draft replies", SRC)

    def test_outputs_are_private_and_sourced_pursuit(self):
        self.assertIn('"privacy": "private"', SRC)
        self.assertIn('"source": "pursuit"', SRC)

    def test_web_goes_through_the_safety_gate(self):
        web = SRC.split("def _web")[1].split("def load_card")[0]
        self.assertLess(web.index("rp.is_allowed"), web.index("rp.searx"))


class TestPerformance(unittest.TestCase):
    def test_grounding_on_10k_sentences_is_fast(self):
        body = " ".join(f"Kyiv item {i} happened [1]." for i in range(10_000))
        t = time.perf_counter()
        kept, dropped = ps.support_filter(body, ["kyiv item happened " + " ".join(str(i) for i in range(10_000))])
        ps.ground(kept, 3)
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertEqual(dropped, [])

    def test_dedupe_is_bounded(self):
        items = [{"id": str(i), "text": f"distinct source text number {i} with words"} for i in range(10_000)]
        t = time.perf_counter()
        out = ps.dedupe_sources(items)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(len(out), ps.MAX_SOURCES)


class TestRetry(unittest.TestCase):
    def test_http_json_retries_with_backoff_then_succeeds(self):
        n = {"c": 0}

        def flaky(req, timeout=None):
            n["c"] += 1
            if n["c"] < 3:
                raise OSError("blip")
            return _Resp({"ok": 1})
        with mock.patch.object(ps.urllib.request, "urlopen", flaky), mock.patch.object(ps.time, "sleep") as sl:
            self.assertEqual(ps._http_json("http://x/y"), {"ok": 1})
        self.assertEqual(n["c"], 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1.5, 3.0])

    def test_llm_fails_over_across_endpoints(self):
        urlopen, calls = _net(chat="hello", chat_fail=2)
        with mock.patch.object(ps.urllib.request, "urlopen", urlopen):
            self.assertEqual(ps.llm("x"), "hello")
        self.assertEqual(sum(u.endswith("/chat/completions") for u, _ in calls), 3)

    def test_llm_all_down_returns_empty_and_recall_fails_open(self):
        with mock.patch.object(ps.urllib.request, "urlopen", side_effect=OSError("down")), \
             mock.patch.object(ps.time, "sleep"):
            self.assertEqual(ps.llm("x"), "")
            self.assertEqual(ps.recall("q", source="geopolitics"), [])

    def test_remember_retries(self):
        urlopen, calls = _net(remember_fail=2)
        with mock.patch.object(ps.urllib.request, "urlopen", urlopen), mock.patch.object(ps.time, "sleep"):
            self.assertEqual(ps.remember("t", {}), "mem-42")
        self.assertEqual(sum(u.endswith("/remember") for u, _ in calls), 3)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ps.selftest(), 0)
        self.assertIn("selftest OK", out.getvalue())

    def test_all_nine_skills_mapped_to_their_thread_topics(self):
        self.assertEqual(sorted(ps.SKILLS), sorted(SLUGS))
        for slug, cfg in ps.SKILLS.items():
            self.assertEqual(ps.slug_for_topic(cfg["topic"]), slug)
            if not cfg.get("meta"):
                self.assertTrue(cfg["sources"])
        self.assertIsNone(ps.slug_for_topic(""))
        self.assertEqual(ps.slug_for_source("local_news"), "pursue-local-news-interest")
        self.assertIsNone(ps.slug_for_source("reddit"))

    def test_grounding_rules(self):
        clean, cites = ps.ground("x [1] y [9] z [2]", 2)
        self.assertEqual(cites, [1, 2]); self.assertNotIn("[9]", clean)
        kept, dropped = ps.support_filter("Odesa was hit [1]. Odesa was hit. Kyiv was hit [1]. Bleak.", ["odesa strike"])
        self.assertEqual(kept, "Odesa was hit [1]. Bleak.")
        self.assertEqual(len(dropped), 2)
        self.assertFalse(ps.is_grounded([], 5)); self.assertFalse(ps.is_grounded([1], 4))
        self.assertTrue(ps.is_grounded([1, 3], 4))

    def test_query_ignores_ungrounded_riff_steps(self):
        riff = {"last_note": "a poem about absence", "next_step": "map TikTok valence"}
        self.assertEqual(ps.build_query({"hint": "geopolitics"}, riff), "geopolitics")
        grounded = {"last_note": "fact [1].\nSources:\n[1] x", "next_step": "read the Kremlin measures"}
        self.assertEqual(ps.build_query({"hint": "geopolitics"}, grounded), "read the Kremlin measures")

    def test_night_window_and_fishbowl_log(self):
        self.assertTrue(ps.is_night(23)); self.assertTrue(ps.is_night(7)); self.assertFalse(ps.is_night(8))
        self.assertFalse(ps.is_night(20))
        txt = ps.fishbowl_log([("a", datetime(2026, 1, 1, 8, 0), "C", "live", "t"),
                               ("v", datetime(2026, 1, 1, 9, 0), "C", "vod", "t"),
                               ("b", datetime(2026, 1, 2, 8, 5), "C", "live", "t")])
        self.assertIn("24h05m after the previous live stream", txt)
        self.assertEqual(ps.fishbowl_log([]), "")

    def test_success_evaluation(self):
        cfg = ps.SKILLS["pursue-fascination-the-watch-fishbowl"]
        self.assertTrue(ps.evaluate_success(cfg, {}, "noted", [1], log_days=3)[0])
        self.assertFalse(ps.evaluate_success(cfg, {}, "noted", [1], log_days=2)[0])
        ok, detail = ps.evaluate_success(ps.SKILLS["pursue-interest-sports"], {"success_check": "User confirms"}, "noted", [1, 2])
        self.assertTrue(ok); self.assertIn("not solicited", detail)
        self.assertFalse(ps.evaluate_success({}, {}, "fizzled", [])[0])
        self.assertTrue(ps.evaluate_success({"success": "nightly"}, {}, "noted", [1], elapsed=100, steps_done=1)[0])


class TestIntegration(unittest.TestCase):
    def test_reuses_research_pass_gate_and_search(self):
        self.assertIn("import nova_research_pass as rp", SRC)
        oc = _Cur([("FROM research_log", (0,))])
        rp = mock.Mock(is_allowed=mock.Mock(return_value=(True, "ok")),
                       searx=mock.Mock(return_value=[{"title": "T", "url": "https://u", "content": "Kyiv facts"}]))
        with mock.patch.dict(sys.modules, {"nova_research_pass": rp}):
            out = ps._web(oc, {"topic": "geopolitics", "hint": "geopolitics"}, "geopolitics", dry_run=False)
        self.assertEqual(out[0]["ref"], "https://u")
        self.assertTrue(any("'skill_researched'" in s for s in oc.sql))

    def test_blocked_question_never_searches(self):
        oc = _Cur([("FROM research_log", (0,))])
        rp = mock.Mock(is_allowed=mock.Mock(return_value=(False, "regex")), searx=mock.Mock())
        with mock.patch.dict(sys.modules, {"nova_research_pass": rp}):
            self.assertEqual(ps._web(oc, {"topic": "t"}, "q", dry_run=True), [])
        rp.searx.assert_not_called()
        self.assertFalse(any(s.startswith("INSERT") for s in oc.sql))     # dry run writes nothing

    def test_gather_uses_the_skill_sources_and_keyword_sample(self):
        mc = _Cur([("FROM memories", GEO_MEM)])
        with mock.patch.object(ps, "recall", return_value=[]) as rc:
            out = ps.gather(_Cur(), mc, {**ps.SKILLS["pursue-fascination-he-man-and-80s-cartoons"], "web": False},
                            "q", allow_web=False, dry_run=True)
        self.assertEqual(mc.params[0][0], ["he_man", "television"])
        self.assertTrue(any("text ~* %s" in s for s in mc.sql))
        self.assertEqual(out, [])                       # off-topic memories fail the must filter
        self.assertEqual(rc.call_args_list[0].kwargs["source"], "he_man")

    def test_unclaimed_time_delegates_to_an_implemented_skill(self):
        ut = _load("ut_for_skill", SCRIPTS / "nova_unclaimed_time.py")
        with mock.patch.dict(sys.modules, {"nova_pursue_skill": ps}), \
             mock.patch.object(ps, "run_skill", return_value={"handled": True, "outcome": "noted"}) as rs, \
             redirect_stdout(io.StringIO()):
            self.assertTrue(ut.try_skill(_Cur(), _Cur(), {"mode": "preoccupation", "topic": "sports"}))
            self.assertEqual(rs.call_args.args[0], "pursue-interest-sports")
            rs.return_value = {"handled": False}
            self.assertFalse(ut.try_skill(_Cur(), _Cur(), {"mode": "preoccupation", "topic": "sports"}))
            self.assertFalse(ut.try_skill(_Cur(), _Cur(), {"mode": "preoccupation", "topic": "nightly"}))
            self.assertFalse(ut.try_skill(_Cur(), _Cur(), {"mode": "preoccupation", "topic": "horology"}))


class TestFunctional(unittest.TestCase):
    def _run(self, oc, mc, urlopen, slug="pursue-interest-geopolitics", **kw):
        with mock.patch.object(ps.urllib.request, "urlopen", urlopen), mock.patch.object(ps.time, "sleep"), \
             mock.patch.dict(sys.modules, {"nova_research_pass": mock.Mock(
                 is_allowed=mock.Mock(return_value=(True, "ok")), searx=mock.Mock(return_value=[]))}):
            return ps.run_skill(slug, oc=oc, mc=mc, trigger="test", **kw)

    def test_golden_path_writes_grounded_note_everywhere(self):
        oc, mc = _ops(), _Cur([("FROM memories", GEO_MEM)])
        urlopen, calls = _net()
        r = self._run(oc, mc, urlopen)
        self.assertTrue(r["handled"]); self.assertEqual(r["outcome"], "noted"); self.assertTrue(r["success"])
        mem = [b for u, b in calls if u.endswith("/remember")][0]
        self.assertEqual(mem["source"], "pursuit")
        self.assertTrue(mem["text"].startswith("[Pursuit — geopolitics] Russia struck"))
        self.assertNotIn("TikTok", mem["text"])                     # unsupported sentence dropped
        self.assertIn("Sources:\n[1] geopolitics: Strikes on Odesa https://example.org/odesa", mem["text"])
        self.assertEqual(mem["metadata"]["skill"], "pursue-interest-geopolitics")
        thread = [p for s, p in zip(oc.sql, oc.params) if "INSERT INTO pursuit_threads" in s][0]
        self.assertEqual((thread[0], thread[3]), ("geopolitics", "read what the Kremlin's measures are"))
        self.assertTrue(any("UPDATE nova_skills SET uses = uses + 1" in s for s in oc.sql))
        self.assertTrue(any("INSERT INTO nova_skill_runs" in s for s in oc.sql))

    def test_dry_run_writes_nothing(self):
        oc, mc = _ops(card=CARD[:2] + ("proposed",) + CARD[3:]), _Cur([("FROM memories", GEO_MEM)])
        urlopen, calls = _net()
        r = self._run(oc, mc, urlopen, dry_run=True)
        self.assertEqual(r["outcome"], "noted")
        self.assertFalse(any(u.endswith("/remember") for u, _ in calls))
        self.assertFalse([s for s in oc.sql if s.lstrip().upper().startswith(("INSERT", "UPDATE", "CREATE"))])

    def test_retired_card_is_not_run(self):
        oc = _ops(card=CARD[:2] + ("retired",) + CARD[3:])
        urlopen, calls = _net()
        r = self._run(oc, _Cur(), urlopen)
        self.assertFalse(r["handled"]); self.assertEqual(calls, [])

    def test_error_path_model_down_records_no_note(self):
        oc, mc = _ops(), _Cur([("FROM memories", GEO_MEM)])
        urlopen, calls = _net(chat="", chat_fail=99)
        r = self._run(oc, mc, urlopen)
        self.assertEqual(r["outcome"], "llm_down"); self.assertFalse(r["success"])
        self.assertFalse(any(u.endswith("/remember") for u, _ in calls))
        self.assertFalse(any("INSERT INTO pursuit_threads" in s for s in oc.sql))

    def test_ungrounded_note_fizzles(self):
        oc, mc = _ops(), _Cur([("FROM memories", GEO_MEM)])
        urlopen, calls = _net(chat="TikTok rules Europe now. Everyone feels it.\nNEXT: nothing")
        r = self._run(oc, mc, urlopen)
        self.assertEqual(r["outcome"], "fizzled")
        self.assertFalse(any(u.endswith("/remember") for u, _ in calls))

    def test_nightly_waits_for_dark_then_runs_top_thread(self):
        card = ("pursue-interest-nightly",) + CARD[1:]
        urlopen, _ = _net()
        r = self._run(_ops(card=card), _Cur(), urlopen, slug="pursue-interest-nightly", now=datetime(2026, 10, 6, 14))
        self.assertEqual(r, {"handled": False, "why": "daytime"})
        oc = _Cur([("FROM nova_skills", card), ("SELECT topic FROM (", ("geopolitics",)),
                   ("FROM pursuit_threads WHERE topic=", None), ("count(DISTINCT", (0,)), ("FROM research_log", (0,))])
        urlopen, calls = _net()
        with mock.patch.object(ps, "NIGHTLY_STEPS", 1):
            r = self._run(oc, _Cur([("FROM memories", GEO_MEM)]), urlopen, slug="pursue-interest-nightly",
                          now=datetime(2026, 10, 6, 2))
        self.assertTrue(r["handled"]); self.assertEqual(r["topic"], "geopolitics"); self.assertTrue(r["success"])
        self.assertEqual([b for u, b in calls if u.endswith("/remember")][0]["metadata"]["skill"], "pursue-interest-nightly")


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        for flag in ("--help", "--selftest", "--list"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        with mock.patch.object(urllib_request(), "urlopen", side_effect=AssertionError("network")):
            _load("ps_again", SCRIPT)


def urllib_request():
    import urllib.request
    return urllib.request


if __name__ == "__main__":
    unittest.main()
