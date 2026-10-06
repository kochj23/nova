#!/usr/bin/env python3
"""Tests for nova_journal_emergency.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
(Geofence specifics also live in tests/test_emergency_geo.py.)"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_journal_emergency.py"
SRC = PATH.read_text()
_TMP = Path(tempfile.mkdtemp(prefix="emerg_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_journal_emergency_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ej = _load()
# This private copy never touches real logs/state/Slack/LLM: every outbound name is rebound here.
ej.LOG_FILE = _TMP / "emergency.log"
ej.STATE_FILE = _TMP / "state.json"
ej.GEO_CACHE_FILE = _TMP / "geocache.json"
ej._bus_notify = mock.MagicMock()
ej.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_CHAN="C_TEST", NOVA_HOST="127.0.0.1")
for _n in ("publish_hugo", "git_push", "call_openrouter", "generate_image"):
    setattr(ej, _n, mock.MagicMock())
ej.weather_forecast_context = lambda: ""
ej.system_prompt = lambda ctx="", **k: ctx   # the real one reads PG (live facts / recent activity)

FIRE = "Brush fire in Burbank hills prompts mandatory evacuation order for residents near Wildwood Canyon area today"
QUIET = "Pasadena public health reminds residents to drink water during the warm afternoon in the foothills"
BODY = "Fire In The Foothills\n\n" + ("Evacuation orders are in effect near Wildwood Canyon. " * 12)


def _psql(items):
    out = "\n".join(f"{t}\x1fcalfire\x1fhttps://x\x1f2026-01-01" for t in items)
    return mock.Mock(returncode=0, stdout=out, stderr="")


class _Env(unittest.TestCase):
    def setUp(self):
        for f in (ej.STATE_FILE, ej.GEO_CACHE_FILE):
            f.unlink(missing_ok=True)
        for m in (ej._bus_notify, ej.nova_config.post_both, ej.publish_hugo, ej.git_push,
                  ej.call_openrouter, ej.generate_image):
            m.reset_mock(return_value=True, side_effect=True)
        ej.generate_image.return_value = None
        ps = [mock.patch("sys.stdout", new_callable=io.StringIO),
              mock.patch("nova_code_reference.code_reference_block", return_value=""),
              mock.patch.object(ej.urllib.request, "urlopen", side_effect=OSError("offline"))]
        for p in ps:
            p.start(); self.addCleanup(p.stop)


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_interpolates_only_constants_and_ints(self):
        sql_block = SRC[SRC.index("def get_recent_emergencies"):SRC.index("def recall_emergencies")]
        self.assertEqual(set(re.findall(r"\{(\w+)\}", sql_block)) & {"SOURCE_VECTOR", "hours", "limit"},
                         {"SOURCE_VECTOR", "hours", "limit"})
        for call in re.findall(r"get_recent_emergencies\(([^)]*)\)", SRC.split("def get_recent_emergencies")[1]):
            self.assertRegex(call, r"^hours=\d+, limit=\d+$")

    def test_recall_query_urlencoded(self):
        with mock.patch.object(ej.urllib.request, "urlopen", side_effect=OSError("x")) as uo:
            ej.recall_emergencies("fire & flood?source=evil")
        self.assertIn("q=fire+%26+flood%3Fsource%3Devil", uo.call_args[0][0])


class TestPerformance(_Env):
    def test_dedup_and_geo_helpers_10k(self):
        t0 = time.perf_counter()
        seen = [{"key": ej._event_key(f"item {i} " + QUIET), "date": "9999"} for i in range(10_000)]
        pruned = ej._prune_seen(seen)
        for i in range(10_000):
            ej._is_non_local(QUIET.lower()); ej._haversine_mi(34.1, -118.3, 34.2, -118.2)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(pruned), 2000)


class TestRetry(_Env):
    def test_pg_and_recall_fail_open(self):
        # RETRY GAP: get_recent_emergencies / recall_emergencies — one attempt each, [] on failure
        with mock.patch("subprocess.run", side_effect=OSError("no psql")) as run:
            self.assertEqual(ej.get_recent_emergencies(), [])
        self.assertEqual(run.call_count, 1)
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=2, stdout="", stderr="down")):
            self.assertEqual(ej.get_recent_emergencies(), [])
        self.assertEqual(ej.recall_emergencies("x"), [])

    def test_geocode_failure_not_cached_and_keeps_item(self):
        # RETRY GAP: _geocode — single Nominatim attempt; a transient miss is NOT cached, item kept (fail-safe)
        self.assertIsNone(ej._geocode("Somewhere, CA"))
        self.assertFalse(ej.GEO_CACHE_FILE.exists())
        ej.call_openrouter.return_value = "Somewhere, CA"
        self.assertEqual(ej.within_radius(FIRE)[0], True)


class TestUnit(_Env):
    def test_strip_meta_preamble(self):
        bad = "Right. No web permission yet. I'll write this as instructed.\n\n---\n\nReal news."
        self.assertEqual(ej._strip_meta_preamble(bad), "Real news.")
        self.assertEqual(ej._strip_meta_preamble("Plain story.\n\nMore."), "Plain story.\n\nMore.")
        self.assertIsNone(ej._strip_meta_preamble(None))

    def test_title_slug_key(self):
        self.assertEqual(ej._extract_title("**Fire Season Opens**\nbody"), "Fire Season Opens")
        self.assertEqual(ej._extract_title("hi\n"), "LA County Emergency Dispatch")
        self.assertEqual(ej._slug("Fire! In the Hills?"), "fire-in-the-hills")
        self.assertEqual(ej._event_key("A   B\nC"), "a b c")

    def test_geofence_distance(self):
        self.assertAlmostEqual(ej._haversine_mi(0, 0, 0, 1), 69.1, delta=0.2)
        self.assertFalse(ej.within_radius("Wildfire near Sacramento forces evacuation")[0])
        with mock.patch.object(ej, "_primary_location", return_value="Littlerock, CA"), \
                mock.patch.object(ej, "_geocode", return_value=(34.52, -117.98)):
            keep, miles, *_ = ej.within_radius(FIRE)
        self.assertFalse(keep)
        self.assertGreater(miles, ej.RADIUS_MI)


class TestIntegration(_Env):
    def test_psql_rows_feed_breaking_filter(self):
        ej.call_openrouter.return_value = "NONE"
        with mock.patch("subprocess.run", return_value=_psql([FIRE, QUIET])) as run:
            items = ej.get_recent_emergencies(hours=6, limit=60)
        self.assertIn("source = 'la_public_safety'", run.call_args[0][0][-1])
        self.assertEqual(items[0]["feed"], "calfire")
        self.assertEqual([i["text"] for i in ej.find_breaking(items)], [FIRE])

    def test_notify_goes_through_central_bus(self):
        ej.notify("T", "p", "slug", is_breaking=True)
        kw = ej._bus_notify.call_args[1]
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("critical", "emergency", "la-emergency-breaking-slug"))
        ej.nova_config.post_both.assert_called_once()
        ej.notify("T", "p", "slug", is_breaking=False)
        self.assertEqual(ej._bus_notify.call_args[1]["level"], "info")
        self.assertEqual(ej.nova_config.post_both.call_count, 1)


class TestFunctional(_Env):
    def test_breaking_publishes_once_then_dedups(self):
        ej.call_openrouter.side_effect = lambda s, u, **k: "NONE" if k.get("max_tokens") == 30 else BODY
        with mock.patch("subprocess.run", return_value=_psql([FIRE])):
            ej.generate_breaking()
            self.assertEqual(ej.publish_hugo.call_args[0][0], "Fire In The Foothills")
            ej.git_push.assert_called_once_with("local", "Fire In The Foothills")
            self.assertEqual(len(json.loads(ej.STATE_FILE.read_text())["seen"]), 1)
            ej.generate_breaking()
        self.assertEqual(ej.publish_hugo.call_count, 1)

    def test_breaking_skip_and_gate_reasoning_never_publish(self):
        for reply in ("SKIP", "The geography gate passes but this is not an active emergency. " * 5):
            ej.call_openrouter.side_effect = lambda s, u, r=reply, **k: "NONE" if k.get("max_tokens") == 30 else r
            with mock.patch("subprocess.run", return_value=_psql([FIRE])):
                ej.generate_breaking()
        ej.publish_hugo.assert_not_called()
        ej._bus_notify.assert_not_called()

    def test_daily_recap_publishes_and_records_seen(self):
        ej.call_openrouter.side_effect = lambda s, u, **k: "NONE" if k.get("max_tokens") == 30 else BODY
        with mock.patch("subprocess.run", return_value=_psql([FIRE, QUIET])):
            ej.generate_daily_recap()
        self.assertEqual(ej.publish_hugo.call_args[0][2], "local")
        self.assertEqual(ej._bus_notify.call_args[1]["level"], "info")
        st = json.loads(ej.STATE_FILE.read_text())
        self.assertEqual(len(st["seen"]), 2)
        self.assertIn("last_recap", st)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_journal_emergency"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
