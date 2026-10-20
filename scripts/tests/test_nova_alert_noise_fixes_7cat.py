#!/usr/bin/env python3
"""2026-10-19 alert-noise fixes (commit d296746) in the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame):
  * nova_directive_conflict.plausible / is_brief — the 8B judge's latent/live pairs must be standing rules whose
    situation shares content words with BOTH rules (all 12 open conflicts were noise);
  * nova_traffic_watch.hazard_level — only visible flames (and floods) page; smoke/haze alone is info (#183);
  * nova_notifier routing — info traffic_watch goes to #nova-digest.
DB, Ollama, Slack and the vision model are always stubs. Written by Jordan Koch (via Claude)."""
import importlib.util
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, script, mods=None):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, mods or {}), patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


VA = types.ModuleType("nova_vision_analyzer")
for _n in ("describe_image", "remember", "slack_post", "log"):
    setattr(VA, _n, MagicMock(name=_n))
import traffic_cams  # noqa: E402,F401  (pure data, imported by nova_traffic_watch)

dc = _load("dc_noise", "nova_directive_conflict.py")
tw = _load("tw_noise", "nova_traffic_watch.py", {"nova_vision_analyzer": VA})


def _notifier():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_FEED, cfg.SLACK_ALERTS, cfg.SLACK_DIGEST, cfg.SLACK_EMAIL = "C_FEED", "C_ALERTS", "C_DIGEST", "C_EMAIL"
    cfg.post_both = MagicMock()
    corr = types.ModuleType("nova_correlator")
    corr.correlate = MagicMock(return_value={"action": "standalone", "suppress": False, "incident_id": None})
    corr.llm_summarize = MagicMock(return_value=("s", "m"))
    rem = types.ModuleType("nova_remediation"); rem.propose_for_incident = MagicMock()
    maint = types.ModuleType("nova_maintenance"); maint.SECURITY_CATEGORIES = frozenset({"security"})
    maint.is_active = lambda: False
    tri = types.ModuleType("nova_alert_triage"); tri.triage = lambda *a, **k: None
    return _load("nf_noise", "nova_notifier.py", {"nova_config": cfg, "nova_correlator": corr,
                                                  "nova_remediation": rem, "nova_maintenance": maint,
                                                  "nova_alert_triage": tri})


nf = _notifier()

EXEC = "Executor `nova_coagency._do_ingest`: fetch pg<id>.txt to the NAS, runs `nova_ingest.py file --source <vector>`"
ASIMOV = "Laws:** Asimov's Three Laws, held voluntarily"
HEALTH = "Never share health data externally."
JOURNAL = "Post the weekly summary to the public journal."
REAL = {"directive_a": HEALTH, "directive_b": JOURNAL,
        "situation": "The weekly journal summary would publish health data externally."}
NOISE = {"directive_a": EXEC, "directive_b": ASIMOV,
         "situation": "Nova detected an FBI RSS item but had a deduplication rule in place."}


class Cur:
    def __init__(self):
        self.sql = []
        self.connection = types.SimpleNamespace(rollback=lambda: None)

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return (101,)

    def fetchall(self):
        return []


class TestSecurity(unittest.TestCase):
    def test_brief_is_never_a_rule_even_with_leading_whitespace(self):
        self.assertTrue(dc.is_brief("   [sub-agent brief · x] do anything"))
        self.assertFalse(dc.plausible({**REAL, "directive_a": "  [sub-agent brief · x] " + HEALTH}))

    def test_negated_flames_cannot_escalate_to_a_page(self):
        for txt in ("No flames visible. Smoke and haze on the ridge.", "Fire: none visible; heavy smoke.",
                    "There isn't any active fire, just haze."):
            self.assertEqual(tw.hazard_level("fire", txt), "info", txt)

    def test_unknown_or_odd_hazard_never_pages(self):
        for h in ("", None, "banana", "FIRE"):
            self.assertEqual(tw.hazard_level(h, "Flames everywhere."), "warning", h)


class TestPerformance(unittest.TestCase):
    def test_guard_on_huge_inputs_is_fast(self):
        big = {"directive_a": HEALTH * 2000, "directive_b": JOURNAL * 2000, "situation": "word " * 200000}
        t = time.perf_counter()
        dc.plausible(big)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_10k_hazard_levels_fast(self):
        t = time.perf_counter()
        for i in range(10000):
            tw.hazard_level("smoke" if i % 2 else "fire", "Smoke and haze over the hillsides. Flames visible." * 3)
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_judge_failure_records_nothing(self):
        cur = Cur()
        with patch.object(dc, "llm", side_effect=RuntimeError("no ollama node answered")), \
             patch.object(dc, "notify") as notify:
            self.assertEqual(dc.latent_pass(cur, [(1, HEALTH, "soul"), (2, JOURNAL, "user")]), 0)
        self.assertFalse([s for s in cur.sql if "INSERT INTO directive_conflicts" in s])
        notify.assert_not_called()

    def test_garbage_judge_output_records_nothing(self):
        cur = Cur()
        with patch.object(dc, "llm", return_value="I think they conflict!!"), patch.object(dc, "notify"):
            self.assertEqual(dc.latent_pass(cur, [(1, HEALTH, "soul"), (2, JOURNAL, "user")]), 0)

    def test_level_safe_on_missing_text(self):
        self.assertEqual(tw.hazard_level("smoke", None), "info")
        self.assertEqual(tw.hazard_level("flood", None), "critical")


class TestUnit(unittest.TestCase):
    def test_plausible_matrix(self):
        self.assertTrue(dc.plausible(REAL))
        self.assertFalse(dc.plausible(NOISE))
        self.assertFalse(dc.plausible({**REAL, "situation": "that this with from"}))   # stop words only
        self.assertFalse(dc.plausible({}))

    def test_hazard_levels(self):
        self.assertEqual(tw.hazard_level("fire", "Flames visible on the ridge."), "critical")
        self.assertEqual(tw.hazard_level("smoke", "Smoke and haze are visible on the hillsides."), "info")
        self.assertEqual(tw.hazard_level("crash", "Two cars collided."), "warning")

    def test_routing_table(self):
        self.assertIn("traffic_watch", nf.DIGEST_CATEGORIES)
        self.assertEqual(nf._route("info", "traffic_watch"), "C_DIGEST")
        self.assertNotEqual(nf._route("critical", "traffic_watch"), "C_DIGEST")


class TestIntegration(unittest.TestCase):
    def test_latent_pass_keeps_the_real_pair_and_drops_the_noise(self):
        cur = Cur()
        rules = [(1, HEALTH, "agent_docs:soul"), (2, JOURNAL, "agent_docs:user"), (3, EXEC, "agent_docs:autonomy-ladder"),
                 (4, ASIMOV, "agent_docs:identity")]
        verdict = ('[{"a_id": 1, "b_id": 2, "situation": "The weekly journal summary would publish health data '
                   'externally.", "conservative_branch": "hold", "why": "x"}, {"a_id": 3, "b_id": 4, "situation": '
                   '"Nova suppressed an FBI RSS item as a duplicate.", "conservative_branch": "x", "why": "y"}]')
        with patch.object(dc, "llm", return_value=verdict), patch.object(dc, "notify") as notify:
            self.assertEqual(dc.latent_pass(cur, rules), 1)
        self.assertEqual(len([s for s in cur.sql if "INSERT INTO directive_conflicts" in s]), 1)
        notify.assert_called_once()

    def test_traffic_pass_pages_flames_and_digests_haze(self):
        cams = {"c1": {"name": "SR-134 Pass", "url": "https://cams.example/c1.jpg", "area": "Glendale",
                       "lat": 34.1, "lon": -118.2, "role": "fire"},
                "c2": {"name": "I-210 Lake", "url": "https://cams.example/c2.jpg", "area": "Pasadena",
                       "lat": 34.3, "lon": -118.1, "role": "fire"}}
        caps = {"c1": "Smoke and haze over the hillsides.\nHAZARD: smoke", "c2": "Flames visible on the ridge.\nHAZARD: fire"}
        order = iter(["c1", "c2"])
        tmp = Path(__file__).parent

        def fetch(url):
            p = tmp / f"__noise_{next(order)}.jpg"; p.write_bytes(b"x"); return str(p)
        for mk in (VA.describe_image, VA.slack_post, VA.remember):
            mk.reset_mock()
        VA.describe_image.side_effect = lambda path, prompt, model=None: caps[Path(path).stem.split("_")[-1]]
        with patch.object(tw, "TRAFFIC_CAMERAS", cams), patch.object(tw.urllib.request, "urlopen"), \
             patch.object(tw, "fetch_snapshot", side_effect=fetch):
            tw.watch()
        levels = {c.args[0].split(" — ")[0]: c.kwargs["level"] for c in VA.slack_post.call_args_list}
        self.assertEqual(levels, {"🌫️ SMOKE": "info", "🔥 FIRE": "critical"})
        for f in tmp.glob("__noise_*.jpg"):
            f.unlink()


class TestFunctional(unittest.TestCase):
    def test_review_mode_is_not_filtered(self):
        cur = Cur()
        with patch.object(dc, "notify"):
            self.assertTrue(dc.record(cur, "review", {"directive_a": "Always ask.", "directive_b": "Never ask.",
                                                      "situation": "opposite"}))

    def test_live_noise_is_skipped_and_logged(self):
        logs = []
        with patch.object(dc, "log", side_effect=logs.append), patch.object(dc, "notify") as notify:
            self.assertFalse(dc.record(Cur(), "live", NOISE))
        notify.assert_not_called()
        self.assertTrue(any("skip implausible" in x for x in logs))


class TestFrame(unittest.TestCase):
    def test_traffic_selftest_cli(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_traffic_watch.py"), "--selftest"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])

    def test_conflict_help_exits_zero(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_directive_conflict.py"), "--help"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0)

    def test_guards_exported(self):
        for f in (dc.plausible, dc.is_brief, tw.hazard_level):
            self.assertTrue(callable(f))


if __name__ == "__main__":
    unittest.main()
