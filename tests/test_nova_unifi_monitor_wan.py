"""
test_nova_unifi_monitor_wan.py — All 7 test categories for the 2026-07-29 changes
to nova_unifi_monitor.py: the INTENTIONALLY_OFFLINE allow-list in find_problems
and the WAN event-log poller (wan_events).
Written by Jordan Koch.

WHY THESE CHANGES EXIST:
  * Kitchen U6 Enterprise is deliberately powered off. Its "disconnection" drove
    weeks of recurring "UniFi Network Health" incidents plus a permanent wlan
    "warning" — both are the known state, not a problem.
  * The 30-min stat/health sampling never landed inside a short outage window: it
    reported "WAN: ok" through 41 WAN1 failures on 2026-07-22..29. wan_events()
    polls the controller's v2 system-log with a timestamp cursor instead.

Scope note: tests/test_nova_unifi_monitor.py is the older broad suite; this file is
the hermetic companion for the two changed behaviours.

HARD SAFETY: nova_config and nova_notify are stubbed BEFORE load; api_post_v2 and
slack_post are always patched, so the UDM is never contacted and nothing reaches
the notification bus; the WAN cursor is redirected into a temp dir so the real
state file under ~/.openclaw/workspace/state is never touched.
"""

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_unifi_monitor.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-warning"
sys.modules["nova_config"] = _nova_cfg

_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

_mod = load_script_compat(_SCRIPT, "nova_unifi_monitor")
_SRC = _SCRIPT.read_text()

find_problems = _mod.find_problems
wan_events = _mod.wan_events
INTENTIONALLY_OFFLINE = _mod.INTENTIONALLY_OFFLINE
WAN_EVENT_LABELS = _mod.WAN_EVENT_LABELS

OFFLINE_AP = sorted(INTENTIONALLY_OFFLINE)[0]   # "Kitchen U6 Enterprise"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dev(name, state=1, cpu="1", mem="10", drops=0):
    return {"name": name, "state": state,
            "system-stats": {"cpu": cpu, "mem": mem},
            "uplink": {"drops": drops}}


def _event(ev, ts, wan="WAN1", isp="Spectrum"):
    return {"event": ev, "timestamp": ts,
            "parameters": {"WAN_ID": {"name": wan}, "ISP_NAME": {"name": isp}}}


class _WanCase(unittest.TestCase):
    """Every WAN test gets a throwaway cursor file and a captured slack_post."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cursor_file = Path(self._tmp.name) / "wan_events_cursor.json"
        self.posts = []
        self._patches = [
            patch.object(_mod, "WAN_EVENTS_CURSOR", self.cursor_file),
            patch.object(_mod, "slack_post",
                         lambda text, **kw: self.posts.append((text, kw))),
            patch.object(_mod, "log", lambda msg: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def cursor(self):
        return json.loads(self.cursor_file.read_text())["last_ts_ms"]

    def set_cursor(self, ts):
        self.cursor_file.write_text(json.dumps({"last_ts_ms": ts}))

    def run_events(self, events):
        with patch.object(_mod, "api_post_v2", return_value={"data": events}):
            wan_events()


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        for pat in (r"xox[baprs]-\d{5,}", r"\bsk-[A-Za-z0-9]{20,}",
                    r"\bghp_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
                    r"(?i)password\s*=\s*['\"][^'\"]{4,}"):
            self.assertIsNone(re.search(pat, _SRC),
                              f"possible hardcoded credential matching {pat!r}")

    def test_api_key_never_literal(self):
        self.assertIn("nova-unifi-api-key", _SRC)
        self.assertIn("find-generic-password", _SRC)
        self.assertIsNone(re.search(r'api_key\s*=\s*"[A-Za-z0-9]{10,}"', _SRC))

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC)

    def test_udm_target_is_lan_only(self):
        self.assertIn("192.168.", _mod.UDM_HOST)
        self.assertTrue(_mod.UDM_V2.startswith(_mod.UDM_HOST))

    def test_v2_poll_requires_the_api_key(self):
        """No key must mean no request — never an unauthenticated call."""
        with patch.object(_mod, "get_api_key", return_value=""):
            with patch.object(_mod.urllib.request, "urlopen") as m:
                self.assertIsNone(_mod.api_post_v2("system-log/all", {}))
            m.assert_not_called()

    def test_v2_poll_sends_the_key_as_a_header_not_a_query_param(self):
        with patch.object(_mod, "get_api_key", return_value="test-key"), \
             patch.object(_mod.urllib.request, "urlopen") as m:
            m.return_value.__enter__.return_value.read.return_value = b'{"data":[]}'
            _mod.api_post_v2("system-log/all", {"pageNumber": 0})
        req = m.call_args.args[0]
        self.assertEqual(req.get_header("X-api-key"), "test-key")
        self.assertNotIn("test-key", req.full_url)

    def test_wan_event_message_carries_no_client_identities(self):
        """WAN lines name the WAN/ISP only — never a client, MAC or hostname."""
        fn = next(n for n in ast.walk(ast.parse(_SRC))
                  if isinstance(n, ast.FunctionDef) and n.name == "wan_events")
        src = ast.get_source_segment(_SRC, fn)
        for bad in ("hostname", "mac", "ip_address"):
            self.assertNotIn(bad, src)

    def test_state_writes_are_atomic(self):
        """A truncated cursor file would replay history to Slack."""
        self.assertIn('tmp = path.with_suffix(".tmp")', _SRC)
        self.assertIn("tmp.rename(path)", _SRC)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(_WanCase):

    def test_event_page_size_is_bounded(self):
        with patch.object(_mod, "api_post_v2", return_value=None) as m:
            wan_events()
        payload = m.call_args.args[1]
        self.assertEqual(payload["pageSize"], 100)
        self.assertEqual(payload["categories"], ["INTERNET_AND_WAN"])

    def test_posted_lines_are_capped_at_twenty(self):
        self.set_cursor(1000)
        events = [_event("NETWORK_WAN_FAILED_TEMPORARY", 1000 + i)
                  for i in range(1, 41)]
        self.run_events(events)
        text = self.posts[0][0]
        self.assertIn("40 new", text)
        self.assertEqual(text.count("WAN failed (temporary)"), 20)
        self.assertIn("and 20 more", text)

    def test_one_post_per_run_not_one_per_event(self):
        self.set_cursor(1000)
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 1001),
                         _event("NETWORK_WAN_RESTORED", 1002)])
        self.assertEqual(len(self.posts), 1)

    def test_no_post_when_nothing_is_new(self):
        self.set_cursor(5000)
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 4000),
                         _event("NETWORK_WAN_RESTORED", 5000)])
        self.assertEqual(self.posts, [])

    def test_cursor_advances_so_events_are_never_reprocessed(self):
        self.set_cursor(1000)
        events = [_event("NETWORK_WAN_FAILED_TEMPORARY", 1500),
                  _event("NETWORK_WAN_RESTORED", 2500)]
        self.run_events(events)
        self.assertEqual(self.cursor(), 2500)
        self.posts.clear()
        self.run_events(events)          # same page again
        self.assertEqual(self.posts, [], "a second poll must be a no-op")

    def test_dedup_key_is_stable_so_repeats_collapse(self):
        self.set_cursor(1000)
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 1001)])
        self.assertEqual(self.posts[0][1]["dedup_key"], "unifi-wan-events")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(_WanCase):

    def test_unreachable_controller_is_a_no_op(self):
        with patch.object(_mod, "api_post_v2", return_value=None):
            wan_events()      # must not raise
        self.assertEqual(self.posts, [])
        self.assertFalse(self.cursor_file.exists(),
                         "a failed poll must not baseline the cursor")

    def test_api_error_returns_none_instead_of_raising(self):
        with patch.object(_mod, "get_api_key", return_value="k"), \
             patch.object(_mod.urllib.request, "urlopen",
                          side_effect=OSError("timed out")):
            self.assertIsNone(_mod.api_post_v2("system-log/all", {}))

    def test_empty_response_payload_is_tolerated(self):
        with patch.object(_mod, "api_post_v2", return_value={}):
            wan_events()
        self.assertEqual(self.posts, [])

    def test_corrupt_cursor_file_falls_back_to_baselining(self):
        self.cursor_file.write_text("{not json")
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 7000)])
        self.assertEqual(self.posts, [], "a corrupt cursor must baseline, not spam")
        self.assertEqual(self.cursor(), 7000)

    def test_events_without_timestamps_do_not_crash_the_poll(self):
        self.set_cursor(1000)
        self.run_events([{"event": "NETWORK_WAN_RESTORED"},
                         _event("NETWORK_WAN_FAILED_TEMPORARY", 1001)])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("1 new", self.posts[0][0])

    def test_unknown_event_type_degrades_to_info(self):
        self.set_cursor(1000)
        self.run_events([_event("SOME_BRAND_NEW_UNIFI_EVENT", 1001)])
        self.assertEqual(self.posts[0][1]["level"], "info")
        self.assertIn("SOME_BRAND_NEW_UNIFI_EVENT", self.posts[0][0])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestIntentionallyOffline(unittest.TestCase):

    def test_kitchen_u6_is_on_the_allow_list(self):
        self.assertIn("Kitchen U6 Enterprise", INTENTIONALLY_OFFLINE)

    def test_offline_device_produces_no_problems_at_all(self):
        """Skip ALL checks for a deliberately powered-off device."""
        problems = find_problems({}, [_dev(OFFLINE_AP, state=0, cpu="99",
                                           mem="99", drops=9999)], [])
        self.assertEqual([p for p in problems if OFFLINE_AP in p["message"]], [])

    def test_a_genuinely_down_device_is_still_reported(self):
        problems = find_problems({}, [_dev("Garage U6", state=0)], [])
        msgs = [p["message"] for p in problems]
        self.assertTrue(any("Garage U6" in m and "not connected" in m for m in msgs))

    def test_wlan_warning_alone_is_suppressed(self):
        health = {"wlan": {"status": "warning"}}
        problems = find_problems(health, [_dev(OFFLINE_AP, state=0)], [])
        self.assertEqual(problems, [],
                         "a wlan warning caused only by the known-off AP is not news")

    def test_wlan_warning_is_reported_when_another_ap_is_down(self):
        health = {"wlan": {"status": "warning"}}
        problems = find_problems(health, [_dev(OFFLINE_AP, state=0),
                                          _dev("Garage U6", state=0)], [])
        msgs = [p["message"] for p in problems]
        self.assertTrue(any("wlan subsystem status: warning" in m for m in msgs))
        self.assertTrue(any("Garage U6" in m for m in msgs))
        self.assertFalse(any(OFFLINE_AP in m for m in msgs))

    def test_wlan_error_is_never_suppressed(self):
        """Only the 'warning' status is explainable by an intentional power-off."""
        health = {"wlan": {"status": "error"}}
        problems = find_problems(health, [_dev(OFFLINE_AP, state=0)], [])
        self.assertTrue(any("wlan subsystem status: error" in p["message"]
                            for p in problems))

    def test_other_subsystem_warning_is_never_suppressed(self):
        health = {"www": {"status": "warning"}}
        problems = find_problems(health, [_dev(OFFLINE_AP, state=0)], [])
        self.assertTrue(any("www subsystem status: warning" in p["message"]
                            for p in problems))

    def test_wan_latency_threshold_still_fires(self):
        health = {"wan": {"status": "ok", "latency": 80}}
        problems = find_problems(health, [], [])
        self.assertTrue(any("WAN latency: 80ms" in p["message"] for p in problems))

    def test_healthy_network_has_no_problems(self):
        health = {"wan": {"status": "ok", "latency": 12},
                  "wlan": {"status": "ok"}}
        problems = find_problems(health, [_dev("Garage U6"), _dev(OFFLINE_AP, state=0)],
                                 [])
        self.assertEqual(problems, [])

    def test_high_cpu_on_a_live_device_is_still_reported(self):
        problems = find_problems({}, [_dev("Garage U6", cpu="95")], [])
        self.assertTrue(any("CPU: 95%" in p["message"] for p in problems))


class TestWanEventLabels(unittest.TestCase):

    def test_failover_and_failure_events_are_warnings(self):
        for ev in ("NETWORK_WAN_FAILED_TEMPORARY",
                   "NETWORK_WAN_FAILED_MULTIPLE_TIMES",
                   "NETWORK_FAILED_OVER_TO_BACKUP_WAN",
                   "NETWORK_FAILED_OVER_TO_BACKUP_WAN_TEMPORARY",
                   "ISP_PACKET_LOSS"):
            self.assertEqual(WAN_EVENT_LABELS[ev][0], "warning", ev)

    def test_recovery_is_info(self):
        self.assertEqual(WAN_EVENT_LABELS["NETWORK_WAN_RESTORED"][0], "info")

    def test_quality_noise_is_info(self):
        self.assertEqual(WAN_EVENT_LABELS["ISP_HIGH_LATENCY"][0], "info")

    def test_every_label_is_a_level_and_human_string(self):
        for ev, (level, label) in WAN_EVENT_LABELS.items():
            self.assertIn(level, ("info", "warning"), ev)
            self.assertTrue(label and label[0].isupper(), ev)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestWanEventsIntegration(_WanCase):

    def test_first_run_baselines_the_cursor_without_replaying_history(self):
        events = [_event("NETWORK_WAN_FAILED_TEMPORARY", 1_000_000),
                  _event("NETWORK_WAN_RESTORED", 1_500_000),
                  _event("NETWORK_WAN_FAILED_MULTIPLE_TIMES", 2_000_000)]
        self.run_events(events)
        self.assertEqual(self.posts, [], "41 historical failures must not be replayed")
        self.assertEqual(self.cursor(), 2_000_000, "cursor sits at the newest event")

    def test_first_run_with_no_events_baselines_at_zero(self):
        self.run_events([])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.cursor(), 0)

    def test_subsequent_run_reports_only_events_newer_than_the_cursor(self):
        self.set_cursor(2_000_000)
        self.run_events([
            _event("NETWORK_WAN_FAILED_TEMPORARY", 1_000_000),   # old
            _event("NETWORK_WAN_RESTORED", 2_000_000),           # exactly at cursor
            _event("NETWORK_FAILED_OVER_TO_BACKUP_WAN", 2_500_000, wan="WAN2"),
        ])
        self.assertEqual(len(self.posts), 1)
        text = self.posts[0][0]
        self.assertIn("1 new", text)
        self.assertIn("Failed over to backup WAN", text)
        self.assertIn("WAN2", text)
        self.assertNotIn("WAN failed (temporary)", text)
        self.assertEqual(self.cursor(), 2_500_000)

    def test_worst_level_wins_for_a_mixed_batch(self):
        self.set_cursor(1000)
        self.run_events([_event("NETWORK_WAN_RESTORED", 1001),
                         _event("NETWORK_WAN_FAILED_TEMPORARY", 1002)])
        self.assertEqual(self.posts[0][1]["level"], "warning")

    def test_recovery_only_batch_stays_info(self):
        self.set_cursor(1000)
        self.run_events([_event("NETWORK_WAN_RESTORED", 1001),
                         _event("ISP_HIGH_LATENCY", 1002)])
        self.assertEqual(self.posts[0][1]["level"], "info")

    def test_post_is_categorized_as_network(self):
        self.set_cursor(1000)
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 1001)])
        self.assertEqual(self.posts[0][1]["category"], "network")

    def test_lines_are_sorted_oldest_first(self):
        self.set_cursor(1000)
        self.run_events([_event("NETWORK_WAN_RESTORED", 3000),
                         _event("NETWORK_WAN_FAILED_TEMPORARY", 2000)])
        text = self.posts[0][0]
        self.assertLess(text.index("WAN failed (temporary)"),
                        text.index("WAN restored"))

    def test_isp_name_is_included_when_present(self):
        self.set_cursor(1000)
        self.run_events([_event("ISP_PACKET_LOSS", 1001, isp="Spectrum")])
        self.assertIn("(Spectrum)", self.posts[0][0])

    def test_missing_isp_name_is_omitted_cleanly(self):
        self.set_cursor(1000)
        ev = _event("ISP_PACKET_LOSS", 1001)
        ev["parameters"]["ISP_NAME"] = None
        self.run_events([ev])
        self.assertIn("WAN1:", self.posts[0][0])
        self.assertNotIn("()", self.posts[0][0])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(_WanCase):

    def test_outage_storm_is_caught_that_health_sampling_missed(self):
        """The 2026-07-22..29 scenario: stat/health says ok, the log says 41 fails."""
        self.set_cursor(1_000_000)
        events = []
        ts = 1_000_100
        for i in range(41):
            events.append(_event("NETWORK_WAN_FAILED_TEMPORARY", ts))
            events.append(_event("NETWORK_WAN_RESTORED", ts + 30))
            ts += 60_000
        self.run_events(events)
        # health sampling would have reported nothing at all
        self.assertEqual(find_problems({"wan": {"status": "ok", "latency": 10}}, [], []),
                         [])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("82 new", self.posts[0][0])
        self.assertEqual(self.posts[0][1]["level"], "warning")
        self.assertEqual(self.cursor(), events[-1]["timestamp"])

    def test_known_off_ap_stays_silent_across_a_full_analysis(self):
        health = {"wan": {"status": "ok", "latency": 15},
                  "wlan": {"status": "warning"},
                  "www": {"status": "ok"}}
        devices = [_dev("Dream Machine"), _dev("Garage U6"),
                   _dev(OFFLINE_AP, state=0)]
        self.assertEqual(find_problems(health, devices, []), [],
                         "the whole recurring-incident scenario must be silent now")

    def test_baseline_then_report_then_quiet_cycle(self):
        # 1) first ever run — baseline only
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 5_000)])
        self.assertEqual(self.posts, [])
        # 2) a genuine new failover arrives — reported
        self.run_events([_event("NETWORK_WAN_FAILED_TEMPORARY", 5_000),
                         _event("NETWORK_FAILED_OVER_TO_BACKUP_WAN", 6_000)])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][1]["level"], "warning")
        # 3) nothing new — silent
        self.posts.clear()
        self.run_events([_event("NETWORK_FAILED_OVER_TO_BACKUP_WAN", 6_000)])
        self.assertEqual(self.posts, [])


class TestSlackPostBridge(unittest.TestCase):
    """Exercises the REAL slack_post (the WAN tests patch it out)."""

    def test_slack_post_routes_wan_events_onto_the_bus_as_network(self):
        notify = MagicMock()
        with patch.object(_mod, "notify", notify):
            _mod.slack_post("*WAN Events — 1 new*\n  12:00 — WAN1: WAN failed",
                            level="warning", category="network",
                            dedup_key="unifi-wan-events")
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["category"], "network")
        self.assertEqual(kwargs["level"], "warning")
        self.assertEqual(kwargs["dedup_key"], "unifi-wan-events")
        self.assertIn("12:00", kwargs["body"])

    def test_slack_post_first_line_becomes_the_title(self):
        notify = MagicMock()
        with patch.object(_mod, "notify", notify):
            _mod.slack_post("*WAN Events — 2 new*\n  line one\n  line two",
                            level="warning", category="network")
        self.assertEqual(notify.call_args.args[0], "WAN Events — 2 new")
        self.assertIn("line two", notify.call_args.kwargs["body"])


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_unifi_monitor.py has syntax errors: {e}")

    def test_public_callables_present(self):
        for fn in ("find_problems", "wan_events", "api_post_v2", "slack_post",
                   "_load_json", "_save_json"):
            self.assertTrue(callable(getattr(_mod, fn, None)), f"missing: {fn}")

    def test_intentionally_offline_is_a_set_of_names(self):
        self.assertIsInstance(INTENTIONALLY_OFFLINE, set)
        self.assertTrue(INTENTIONALLY_OFFLINE)
        for name in INTENTIONALLY_OFFLINE:
            self.assertIsInstance(name, str)

    def test_wan_cursor_lives_in_the_state_dir(self):
        self.assertEqual(_mod.WAN_EVENTS_CURSOR.parent, _mod.STATE_DIR)
        self.assertEqual(_mod.WAN_EVENTS_CURSOR.name, "wan_events_cursor.json")

    def test_v2_api_base_is_derived_from_the_host(self):
        self.assertIn("/proxy/network/v2/api/site/default", _mod.UDM_V2)

    def test_changes_are_documented_in_source(self):
        self.assertIn("Devices Jordan keeps intentionally powered off", _SRC)
        self.assertIn("First run: baseline only", _SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
