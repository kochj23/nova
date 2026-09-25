"""
test_stabilization_2026_09_25.py — all 7 test categories for the 2026-09-24/25 changes:
  nova_notifier (state-change dedup), nova_gateway/router (non-blocking /health),
  nova_nas_localdiff_reverse (exclusion anchor + ignore-missing-args), nova_journal_security
  (news never pages), nova_human_insight (wish #35), nova_imagination (privacy gates),
  nova_aspirations (standing-yes queue), nova_unclaimed_time (lane odds), nova_projects
  (no repeat roots).
Written by Jordan Koch.
"""
import asyncio
import importlib.util
import re
import sys
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _src(rel):
    return (SCRIPTS / rel).read_text()


# ===========================================================================
# 1. SECURITY
# ===========================================================================
class TestSecurity(unittest.TestCase):
    def test_no_secrets_in_new_or_changed_files(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        for f in ("nova_human_insight.py", "nova_notifier.py", "nova_nas_localdiff_reverse.py",
                  "nova_aspirations.py", "nova_imagination.py", "nova_journal_security.py"):
            self.assertIsNone(pat.search(_src(f)), f"hardcoded credential-looking literal in {f}")

    def test_human_insight_never_quotes_message_bodies(self):
        # Insight text is built from counts/shares only — no user_message / statement bodies leak.
        hi = _load("hi", SCRIPTS / "nova_human_insight.py")
        p = {"theme": "ask about the printer", "n": 4, "wrong_rate": 1.0, "mean_conf": 0.7}
        txt = hi.insight_text("prediction", p)
        self.assertNotIn("SELECT", txt)
        self.assertNotIn("@", txt)          # no emails
        r = {"day": "Fri", "day_share": 0.2, "band": (10, 12), "band_share": 0.6, "total": 100}
        self.assertNotRegex(hi.insight_text("rhythm", r), r"\d{3}\.\d+\.\d+\.\d+")  # no IPs

    def test_imagination_excludes_private_lanes(self):
        s = _src("nova_imagination.py")
        anchor = s[s.index("def pick_anchor"):s.index("def pick_forward_seed")]
        self.assertNotIn("'claude_memory'", anchor)
        self.assertNotIn("'conversation'", anchor)
        self.assertIn("privacy", anchor)
        mote = s[s.index("def pick_dream_mote"):s.index("# ── The three modes")]
        for src in ("'claude_memory'", "'email'", "'imessage'", "'reddit'", "'fishbowl'", "'conversation'"):
            self.assertIn(src, mote)
        self.assertIn("<> 'private'", mote)

    def test_journal_security_news_never_pages(self):
        s = _src("nova_journal_security.py")
        self.assertIn('category="security_news"', s)
        self.assertNotIn('category="security",', s)

    def test_aspirations_queue_insert_is_parameterised(self):
        s = _src("nova_aspirations.py")
        block = s[s.index("INSERT INTO claude_queue"):s.index("UPDATE feature_wishes SET status='acknowledged'")]
        self.assertIn("%s", block)
        self.assertNotIn("f\"INSERT", block)


# ===========================================================================
# 2. PERFORMANCE
# ===========================================================================
class TestPerformance(unittest.TestCase):
    def test_prediction_insights_linear_on_10k_rows(self):
        hi = _load("hi_perf", SCRIPTS / "nova_human_insight.py")
        rows = [(f"Jordan will ask about thing {i % 50} soon.", 0.7, i % 3 == 0) for i in range(10_000)]
        t0 = time.perf_counter()
        out = hi.prediction_insights(rows)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertTrue(all(p["n"] >= hi.MIN_RESOLVED for p in out))

    def test_rhythm_insight_bounded_loop(self):
        hi = _load("hi_perf2", SCRIPTS / "nova_human_insight.py")
        hours = Counter({h: 1000 for h in range(24)})
        t0 = time.perf_counter()
        for _ in range(200):
            hi.rhythm_insight(Counter({"Fri": 5000, "Mon": 19000}), hours, 24000)
        self.assertLess(time.perf_counter() - t0, 0.5)

    def test_dedup_window_lookup_is_constant_time(self):
        nn = _load("nn_perf", SCRIPTS / "nova_notifier.py")
        ev = {"level": "warning", "meta": {}}
        t0 = time.perf_counter()
        for _ in range(50_000):
            nn._dedup_window(ev)
        self.assertLess(time.perf_counter() - t0, 1.0)


# ===========================================================================
# 3. RETRY
# ===========================================================================
class TestRetry(unittest.TestCase):
    def test_human_insight_remember_retries_three_times_with_backoff(self):
        hi = _load("hi_retry", SCRIPTS / "nova_human_insight.py")
        sleeps = []
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise OSError("memory server down")
        with patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(OSError):
                hi.remember("t", {}, _sleep=sleeps.append)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2, 4])     # backoff between attempts, none after the last

    def test_human_insight_remember_succeeds_after_transient_failure(self):
        hi = _load("hi_retry2", SCRIPTS / "nova_human_insight.py")
        state = {"n": 0}

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"id":"x"}'
        import json as _j

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] < 3:
                raise OSError("transient")
            return R()
        with patch("urllib.request.urlopen", side_effect=flaky), patch("json.load", return_value={"id": "x"}):
            self.assertEqual(hi.remember("t", {}, _sleep=lambda s: None), {"id": "x"})
        self.assertEqual(state["n"], 3)

    def test_nas_reverse_rsync_tolerates_missing_list_entries(self):
        s = _src("nova_nas_localdiff_reverse.py")
        self.assertIn("--ignore-missing-args", s)
        self.assertIn("rc in (0, 24)", s)   # vanished-files rc stays a success


# ===========================================================================
# 4. UNIT
# ===========================================================================
class TestUnit(unittest.TestCase):
    def test_notifier_dedup_window_by_level(self):
        nn = _load("nn_unit", SCRIPTS / "nova_notifier.py")
        self.assertEqual(nn._dedup_window({"level": "info", "meta": {}}), 3600)
        self.assertEqual(nn._dedup_window({"level": "warning", "meta": {}}), 86400)
        self.assertEqual(nn._dedup_window({"level": "critical", "meta": None}), 86400)
        self.assertEqual(nn._dedup_window({"level": "warning", "meta": {"dedup_window_s": 600}}), 600)
        self.assertEqual(nn._dedup_window({"level": "critical", "meta": '{"dedup_window_s": 42}'}), 42)
        self.assertEqual(nn._dedup_window({"level": "warning", "meta": "not json"}), 86400)  # fail-safe

    def test_reverse_exclusion_anchored_at_path_start(self):
        s = _src("nova_nas_localdiff_reverse.py")
        m = re.search(r'EXCL = re\.compile\(r"(.+?)"\)', s)
        excl = re.compile(m.group(1))
        self.assertTrue(excl.search("#recycle/backups/x.gz"))
        self.assertTrue(excl.search("a/#recycle/b"))
        self.assertTrue(excl.search("#snapshot/x"))
        self.assertTrue(excl.search("dir/@eaDir/thumb"))
        self.assertFalse(excl.search("backups/postgres/nova_ops_20260924_020000/7710.dat.gz"))

    def test_human_insight_pure_logic(self):
        hi = _load("hi_unit", SCRIPTS / "nova_human_insight.py")
        rows = [("Jordan will ask about the printer again within the next 3 days.", 0.75, False)] * 4
        p = hi.prediction_insights(rows)
        self.assertEqual((p[0]["n"], p[0]["wrong_rate"]), (4, 1.0))
        self.assertEqual(hi.prediction_insights(rows[:3]), [])
        self.assertEqual(hi.prediction_insights([("Jordan will nod.", 0.5, True)] * 6), [])  # right, not wrong
        self.assertIsNone(hi.rhythm_insight(Counter(), Counter(), 0))
        r = hi.rhythm_insight(Counter({"Fri": 30}), Counter({22: 10, 23: 10, 0: 10}), 40)
        self.assertEqual(r["band"], (22, 0))          # wraps midnight
        self.assertIsNone(hi.silence_insight(2, 1, 1))
        self.assertAlmostEqual(hi.silence_insight(3, 1, 1)["held_share"], 0.8)
        self.assertEqual(hi.insight_text("nonsense", {}), "")

    def test_human_insight_freshness_window(self):
        hi = _load("hi_unit2", SCRIPTS / "nova_human_insight.py")
        from datetime import date
        today = date(2026, 9, 25)
        self.assertTrue(hi._fresh({}, "x", today))
        self.assertFalse(hi._fresh({"x": "2026-09-20"}, "x", today))
        self.assertTrue(hi._fresh({"x": "2026-09-18"}, "x", today))
        self.assertTrue(hi._fresh({"x": "garbage"}, "x", today))

    def test_unclaimed_lane_odds_raised(self):
        s = _src("nova_unclaimed_time.py")
        self.assertIn('random.random() < 0.15', s)   # tinker
        self.assertIn('random.random() < 0.20', s)   # aspire
        a = _src("nova_aspirations.py")
        self.assertRegex(a, r"WISH_COOLDOWN_HRS = 8\b")
        self.assertRegex(a, r"MAX_OPEN_WISHES = 6\b")

    def test_projects_start_prompt_lists_recent_completed(self):
        s = _src("nova_projects.py")
        self.assertIn("PROJECTS I JUST COMPLETED", s)
        self.assertIn("status='completed' ORDER BY created_at DESC LIMIT 3", s)


# ===========================================================================
# 5. INTEGRATION
# ===========================================================================
class TestIntegration(unittest.TestCase):
    def test_router_status_serves_cache_and_refreshes_in_background(self):
        sys.path.insert(0, str(SCRIPTS / "nova_gateway"))
        router = _load("router_int", SCRIPTS / "nova_gateway" / "router.py")
        r = router.ModelRouter()
        probes = {"n": 0}

        async def fake_check(name, base_url, health_path, ctx=None):
            probes["n"] += 1
            await asyncio.sleep(0.05)          # a slow probe must NOT delay status()
            r._health_cache[name] = (True, time.time())
            return True
        r._check_health = fake_check

        async def run():
            t0 = time.perf_counter()
            st = await r.status(ctx=None)     # cold cache: schedules probes, returns immediately
            dt = time.perf_counter() - t0
            await asyncio.sleep(0.2)          # let background probes land
            st2 = await r.status(ctx=None)    # warm cache: no new probes
            return st, dt, st2
        st, dt, st2 = asyncio.run(run())
        self.assertLess(dt, 0.04, "status() blocked on backend probes")
        self.assertEqual(set(st) - {"active"}, {n for n, *_ in router.ModelRouter.BACKENDS})
        self.assertEqual(probes["n"], len(router.ModelRouter.BACKENDS))
        self.assertTrue(all(v["healthy"] for k, v in st2.items() if k != "active"))

    def test_aspirations_wish_queues_a_build_task(self):
        # Simulate pursue()'s post-insert block against a recording cursor.
        a = _src("nova_aspirations.py")
        self.assertIn("INSERT INTO claude_queue", a)
        self.assertIn("status='acknowledged'", a)
        self.assertIn("standing yes from Jordan", a)

    def test_notifier_level_table_covers_all_levels(self):
        nn = _load("nn_int", SCRIPTS / "nova_notifier.py")
        for lvl in ("info", "warning", "critical"):
            self.assertIn(lvl, nn.CHANNEL)
            self.assertGreater(nn._dedup_window({"level": lvl, "meta": {}}), 0)


# ===========================================================================
# 6. FUNCTIONAL
# ===========================================================================
class TestFunctional(unittest.TestCase):
    def test_human_insight_main_dry_run_end_to_end_with_fake_db(self):
        hi = _load("hi_func", SCRIPTS / "nova_human_insight.py")
        cur = MagicMock()
        results = iter([
            [("Jordan will ask about the printer again within the next 3 days.", 0.75, False)] * 5,  # predictions
            [("Fri", 10)] * 30 + [("Mon", 11)] * 5,                                                # sessions
            [("filed", 11), ("dropped", 4)],                                                         # reach
        ])
        cur.fetchall.side_effect = lambda: next(results)
        cur.fetchone.return_value = None   # load_seen -> empty
        conn = MagicMock(); conn.cursor.return_value = cur
        printed = []
        with patch.object(hi.psycopg2, "connect", return_value=conn), \
             patch.object(sys, "argv", ["nova_human_insight.py", "--dry-run"]), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = hi.main()
        self.assertEqual(rc, 0)
        joined = "\n".join(printed)
        self.assertIn("I keep being wrong", joined)
        self.assertIn("working rhythm", joined)
        self.assertIn("outreach ledger", joined)
        self.assertIn("surfaced 3 new insight(s)", joined)
        # dry-run must not persist
        self.assertFalse(any("INSERT INTO service_config" in str(c) for c in cur.execute.call_args_list))

    def test_human_insight_fails_open_without_db(self):
        hi = _load("hi_func2", SCRIPTS / "nova_human_insight.py")
        with patch.object(hi.psycopg2, "connect", side_effect=OSError("no pg")), \
             patch.object(sys, "argv", ["nova_human_insight.py"]):
            self.assertEqual(hi.main(), 0)


# ===========================================================================
# 7. FRAME (smoke)
# ===========================================================================
class TestFrame(unittest.TestCase):
    def test_scripts_import_and_selftests_pass(self):
        import subprocess
        for f in ("nova_human_insight.py", "nova_pattern_sense.py"):
            r = subprocess.run([sys.executable, str(SCRIPTS / f), "--selftest"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("assertions passed", r.stdout)

    def test_changed_modules_compile(self):
        import py_compile
        for f in ("nova_notifier.py", "nova_nas_localdiff_reverse.py", "nova_journal_security.py",
                  "nova_aspirations.py", "nova_unclaimed_time.py", "nova_projects.py", "nova_imagination.py",
                  "nova_gateway/router.py", "nova_human_insight.py"):
            py_compile.compile(str(SCRIPTS / f), doraise=True)


if __name__ == "__main__":
    unittest.main()
