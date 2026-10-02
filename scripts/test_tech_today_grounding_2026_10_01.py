#!/usr/bin/env python3
"""Seven-category tests for the 2026-10-01 "do it all" batch: SearXNG dead-backend detection,
the SkipArticle path (no more stock topics), the self-inventory grounding block, AI-topic
detection, and the nova_dashboard_look organ. No DB, no network: every external edge is stubbed.
Run: python3 test_tech_today_grounding_2026_10_01.py"""
import io, json, os, re, sys, time, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# nova_journal imports a lot at module load; stub the heavy/side-effecting bits before import.
for name in ("nova_image_utils",):
    if name not in sys.modules:
        m = types.ModuleType(name); m.generate_image = lambda *a, **k: None; sys.modules[name] = m
import nova_journal as j
import nova_dashboard_look as dl


def _src(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


def _resp(payload: bytes):
    r = mock.MagicMock(); r.read.return_value = payload; r.__enter__.return_value = r; return r


# ───────────────────────────── 1. UNIT ─────────────────────────────
class TestUnit(unittest.TestCase):
    def test_ai_topic_regex(self):
        for t in ("Emerging AI capabilities", "What LLMs can't do", "Why Nova sleeps", "the agentic web", "a new language model"):
            self.assertTrue(j._is_ai_topic(t), t)
        for t in ("The semiconductor midlife crisis", "Roman fish sauce", "Open source governance fights"):
            self.assertFalse(j._is_ai_topic(t), t)

    def test_parse_look_shapes(self):
        self.assertEqual(dl.parse_look('{"status":"alarm","summary":"s","findings":["a"],"numbers":{"p":"1"}}')["status"], "alarm")
        self.assertEqual(dl.parse_look('noise {"status":"OK"} noise')["status"], "ok")
        self.assertEqual(dl.parse_look("")["status"], "watch")
        self.assertEqual(dl.parse_look("not json at all")["status"], "watch")
        d = dl.parse_look('{"status":"ok","findings":[{"panel title":"Services UP now","number":36,"unit":"ms"}],"numbers":"nope"}')
        self.assertEqual(d["findings"], ["panel title: Services UP now 36 ms"]); self.assertEqual(d["numbers"], {})

    def test_should_alert_rules(self):
        self.assertFalse(dl.should_alert("ok", None)); self.assertFalse(dl.should_alert("watch", None))
        self.assertTrue(dl.should_alert("alarm", None))
        self.assertFalse(dl.should_alert("alarm", 100.0, now=100.0 + 3600))
        self.assertTrue(dl.should_alert("alarm", 100.0, now=100.0 + dl.ALERT_DEDUPE_H * 3600))

    def test_render_url_and_prompt(self):
        u = dl.render_url("fleet-health")
        self.assertIn("/render/d/fleet-health?", u); self.assertIn("kiosk", u); self.assertIn("theme=dark", u)
        self.assertIn("JSON only", dl.prompt_for("x", "X")); self.assertIn("X", dl.prompt_for("x", "X"))


# ─────────────────────────── 2. INTEGRATION ───────────────────────────
class TestIntegration(unittest.TestCase):
    def test_empty_search_alerts_and_returns_empty(self):
        body = json.dumps({"results": [], "unresponsive_engines": [["brave", "Suspended: too many requests"], ["duckduckgo", "CAPTCHA"]]}).encode()
        with mock.patch.object(j.urllib.request, "urlopen", return_value=_resp(body)), \
             mock.patch.object(j, "_search_dead_alert") as alert:
            self.assertEqual(j._searxng_search("AI news today"), [])
            alert.assert_called_once()
            self.assertEqual(alert.call_args.args[1][1][1], "CAPTCHA")

    def test_nonempty_search_is_quiet(self):
        body = json.dumps({"results": [{"title": "t", "url": "u", "content": "c", "engine": "bing"}] * 3}).encode()
        with mock.patch.object(j.urllib.request, "urlopen", return_value=_resp(body)), \
             mock.patch.object(j, "_search_dead_alert") as alert:
            self.assertEqual(len(j._searxng_search("x", n=2)), 2); alert.assert_not_called()

    def test_topic_tech_today_skips_without_headlines(self):
        with mock.patch.object(j, "_searxng_search", return_value=[]), mock.patch.object(j, "get_recent", return_value=[]):
            with self.assertRaises(j.SkipArticle):
                j.topic_tech_today({})

    def test_topic_tech_today_uses_live_headlines(self):
        res = [{"title": f"Headline {i}", "content": "c", "url": f"u{i}", "engine": "bing"} for i in range(6)]
        with mock.patch.object(j, "_searxng_search", return_value=res), mock.patch.object(j, "get_recent", return_value=[]), \
             mock.patch.object(j, "recall_memories", return_value=[{"text": "m"}]):
            topic, mems = j.topic_tech_today({})
        self.assertTrue(topic.startswith("Headline")); self.assertTrue(any(m.get("source") == "web" for m in mems))

    def test_run_profile_skip_exits_zero_and_publishes_nothing(self):
        def boom(state, **kw): raise j.SkipArticle("no live headlines")
        with mock.patch.dict(j.PROFILES, {"tech-today": {**j.PROFILES["tech-today"], "topic_fn": boom}}), \
             mock.patch.object(j, "load_state", return_value={}), mock.patch.object(j, "publish_hugo") as pub, \
             mock.patch.object(j, "log"):
            self.assertEqual(j.run_profile("tech-today"), 0); pub.assert_not_called()

    def test_with_self_inventory_only_for_ai_topics(self):
        with mock.patch.object(j, "self_inventory_block", return_value="SELF-INVENTORY — test"):
            self.assertIn("SELF-INVENTORY", j._with_self_inventory("sys", "Emerging AI capabilities"))
            self.assertEqual(j._with_self_inventory("sys", "Roman fish sauce"), "sys")
            self.assertIn("SELF-INVENTORY", j._with_self_inventory("sys", "Roman fish sauce", force=True))

    def test_organ_run_stores_memory_and_alerts_on_alarm(self):
        look = {"status": "alarm", "summary": "down", "findings": ["Services DOWN now 3"], "numbers": {}}
        state = {}
        def cfg_get(cur, key, default=None): return state.get(key, default)
        def cfg_set(cur, key, value): state[key] = value
        with mock.patch.object(dl, "_pg", return_value=mock.MagicMock()), mock.patch.object(dl, "targets", return_value=[("fleet-health", "Fleet")]), \
             mock.patch.object(dl, "render", return_value=b"\x89PNG..."), mock.patch.object(dl, "look_at", return_value=look), \
             mock.patch.object(dl, "cfg_get", side_effect=cfg_get), mock.patch.object(dl, "cfg_set", side_effect=cfg_set), \
             mock.patch.object(dl, "remember", return_value=True) as rem, mock.patch.object(dl, "alert") as al, mock.patch.object(dl, "log"):
            self.assertEqual(dl.run(None, dry_run=False), 0)
            rem.assert_called_once(); al.assert_called_once()
            self.assertIn("last:fleet-health", state); self.assertIn("alerted:fleet-health", state)
            # second pass within the dedupe window: no second alert
            self.assertEqual(dl.run(None, dry_run=False), 0); al.assert_called_once()


# ───────────────────────────── 3. SECURITY ─────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_organ_is_read_only_and_writes_no_files(self):
        s = _src("nova_dashboard_look.py")
        for bad in ("subprocess", "os.remove", "shutil.rmtree", "open(", "launchctl", "systemctl", "DELETE FROM", "/Volumes/Data"):
            self.assertNotIn(bad, s.replace("urllib.request.urlopen(", ""), bad)
        self.assertIn('"privacy": "private"', s)        # her dashboard notes never reach the public journal

    def test_dry_run_touches_nothing(self):
        with mock.patch.object(dl, "_pg", side_effect=RuntimeError("no pg")), mock.patch.object(dl, "render", return_value=b"\x89PNG"), \
             mock.patch.object(dl, "look_at", return_value={"status": "alarm", "summary": "", "findings": [], "numbers": {}}), \
             mock.patch.object(dl, "remember") as rem, mock.patch.object(dl, "alert") as al, mock.patch.object(dl, "log"):
            dl.run(["fleet-health"], dry_run=True); rem.assert_not_called(); al.assert_not_called()

    def test_inventory_rules_forbid_stale_claims(self):
        with mock.patch.object(j, "log"):
            with mock.patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
                 mock.patch.object(j.urllib.request, "urlopen", side_effect=OSError("offline")):
                j._SELF_INVENTORY_CACHE.clear()
                b = j.self_inventory_block()
        self.assertIn("Never name a model as current", b); self.assertIn("not 'for three years'", b)
        self.assertTrue(b.startswith("SELF-INVENTORY"))

    def test_searxng_settings_keep_home_ip_safe_engines(self):
        import glob
        cands = glob.glob("/Volumes/nas/searxng_settings_2026-10-01.yml")
        if not cands:
            self.skipTest("NAS archive not mounted here")
        with open(cands[0]) as f:
            s = f.read()
        for e in ("brave", "duckduckgo", "startpage", "google\n"):
            self.assertNotIn(f"- {e}", s)
        for e in ("wikipedia", "hackernews", "arxiv", "bing news"):
            self.assertIn(f"- {e}", s)


# ──────────────────────────── 4. PERFORMANCE ────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_inventory_is_cached(self):
        j._SELF_INVENTORY_CACHE.clear()
        with mock.patch("psycopg2.connect", side_effect=RuntimeError("x")) as pc, \
             mock.patch.object(j.urllib.request, "urlopen", side_effect=OSError("x")), mock.patch.object(j, "log"):
            j.self_inventory_block(); n = pc.call_count
            j.self_inventory_block(); self.assertEqual(pc.call_count, n)

    def test_parse_is_cheap(self):
        t = time.perf_counter()
        for _ in range(5000):
            dl.parse_look('{"status":"ok","summary":"s","findings":["a","b"],"numbers":{"p":"1"}}')
        self.assertLess(time.perf_counter() - t, 1.5)

    def test_bounded_timeouts(self):
        self.assertLessEqual(dl.VLM_TIMEOUT_S, 300); self.assertLessEqual(dl.RENDER_TIMEOUT_S, 120)


# ───────────────────────────── 5. REGRESSION ─────────────────────────────
class TestRegression(unittest.TestCase):
    def test_no_stock_topic_left(self):
        self.assertNotIn('["emerging AI capabilities"]', _src("nova_journal.py"))

    def test_legacy_tech_today_no_longer_points_at_dot7(self):
        s = _src("nova_tech_today.py")
        self.assertNotIn('"http://192.168.1.7:8080/search"', s); self.assertIn("from nova_resolve import resolve_url", s)

    def test_generation_failures_still_abort(self):
        def boom(state, **kw): raise RuntimeError("model down")
        with mock.patch.dict(j.PROFILES, {"tech-today": {**j.PROFILES["tech-today"], "topic_fn": boom}}), \
             mock.patch.object(j, "load_state", return_value={}), mock.patch.object(j, "log"):
            self.assertEqual(j.run_profile("tech-today"), 1)

    def test_grounding_wired_into_three_generators(self):
        s = _src("nova_journal.py")
        self.assertEqual(s.count("_with_self_inventory(system, topic, force=True)"), 1)
        self.assertEqual(s.count("_with_self_inventory(system, topic)"), 1)
        self.assertEqual(s.count("_with_self_inventory(system, source)"), 1)


# ─────────────────────────── 6. RETRY / FAIL-OPEN ───────────────────────────
class TestRetry(unittest.TestCase):
    def test_search_dead_alert_dedupes_within_24h(self):
        cur = mock.MagicMock(); cur.fetchone.return_value = (json.dumps(time.time() - 60),)
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn), mock.patch.object(j, "notify") as n, mock.patch.object(j, "log"):
            j._search_dead_alert("q", [["brave", "CAPTCHA"]]); n.assert_not_called()
        cur.fetchone.return_value = (json.dumps(time.time() - 90000),)
        with mock.patch("psycopg2.connect", return_value=conn), mock.patch.object(j, "notify") as n, mock.patch.object(j, "log"):
            j._search_dead_alert("q", [["brave", "CAPTCHA"]]); n.assert_called_once()

    def test_search_dead_alert_without_pg_still_alerts(self):
        with mock.patch("psycopg2.connect", side_effect=RuntimeError("down")), mock.patch.object(j, "notify") as n, mock.patch.object(j, "log"):
            j._search_dead_alert("q", []); n.assert_called_once()

    def test_organ_fails_open_per_dashboard(self):
        with mock.patch.object(dl, "_pg", side_effect=RuntimeError("no pg")), mock.patch.object(dl, "log"), \
             mock.patch.object(dl, "render", side_effect=[RuntimeError("renderer down"), b"\x89PNG"]), \
             mock.patch.object(dl, "look_at", return_value={"status": "ok", "summary": "", "findings": [], "numbers": {}}):
            self.assertEqual(dl.run(["a", "b"], dry_run=True), 0)

    def test_thinking_model_fallback(self):
        data = json.dumps({"response": "", "thinking": 'blah {"status":"watch","summary":"from thinking"}'}).encode()
        with mock.patch.object(dl.urllib.request, "urlopen", return_value=_resp(data)):
            self.assertEqual(dl.look_at("u", "T", b"\x89PNG")["summary"], "from thinking")


# ───────────────────────────── 7. EDGES & DOCS ─────────────────────────────
class TestEdgesAndDocs(unittest.TestCase):
    def test_edges(self):
        self.assertFalse(j._is_ai_topic("")); self.assertFalse(j._is_ai_topic(None))
        self.assertEqual(dl.parse_look('{"status":"ALARM","findings":null,"numbers":null}')["status"], "alarm")
        self.assertEqual(len(dl.parse_look('{"status":"ok","findings":' + json.dumps(["x"] * 50) + '}')["findings"]), 8)
        self.assertEqual(j._with_self_inventory("sys", None), "sys")

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(HERE), "README.md")), "README lives in the repo root")
    def test_readme_mentions_the_change(self):
        with open(os.path.join(os.path.dirname(HERE), "README.md")) as f:
            r = f.read()
        for s in ("nova_dashboard_look.py", "self_inventory_block", "SkipArticle", "current-model-landscape"):
            self.assertIn(s, r)

    def test_scheduler_entry_present_on_studio(self):
        y = os.path.expanduser("~/.openclaw/config/scheduler.yaml")
        if not os.path.exists(y):
            self.skipTest("not the Studio")
        with open(y) as f:
            self.assertIn("dashboard_look:", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=1)
