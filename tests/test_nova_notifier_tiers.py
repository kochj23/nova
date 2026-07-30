"""
test_nova_notifier_tiers.py — All 7 test categories for the 2026-07-29 changes to
nova_notifier.py: the three-tier routing policy and the per-event dedup window.
Written by Jordan Koch.

Scope note: tests/test_nova_notifier.py is the older pytest suite that drives
drain() against the REAL nova_ops database. This file is the hermetic companion
that pins the NEW policy — nothing here opens a socket or a DB connection:

  #nova-alerts — warning/critical (actionable, state-change only)
  #nova-digest — info-level events in DIGEST_CATEGORIES (rollups)
  #nova-feed   — everything else at info (ambient firehose, muted)
  CATEGORY_OVERRIDE beats level; a warning in a digest category still ALERTS.

HARD SAFETY: nova_config / nova_correlator / nova_remediation / nova_maintenance
are all stubbed BEFORE load, so post_both, the LLM summarizer and the mesh relay
can never fire. drain() is exercised against a fake connection.
"""

import ast
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_notifier.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

FEED = "C-FEED"
ALERTS = "C-ALERTS"
DIGEST = "C-DIGEST"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_FEED = FEED
_nova_cfg.SLACK_ALERTS = ALERTS
_nova_cfg.SLACK_DIGEST = DIGEST
_nova_cfg.post_both = MagicMock(return_value=None)
sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_correlator"] = MagicMock()
sys.modules["nova_remediation"] = MagicMock()
sys.modules["nova_maintenance"] = MagicMock()

_mod = load_script_compat(_SCRIPT, "nova_notifier")

# Deterministic, fail-open maintenance gate (no real window state in tests).
_mod._MAINT_CATS = frozenset()
_mod._maint_active = lambda: False

_route = _mod._route
_dedup_window = _mod._dedup_window
_SRC = _SCRIPT.read_text()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, fetchall_queue=None, fetchone_queue=None):
        self.executed = []
        self._all = list(fetchall_queue or [])
        self._one = list(fetchone_queue or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._all.pop(0) if self._all else []

    def fetchone(self):
        return self._one.pop(0) if self._one else None

    def sql_containing(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **k):
        return self._cursor

    def close(self):
        self.closed = True


def _event(**kw):
    ev = {"id": 1, "level": "info", "category": None, "title": "t", "body": None,
          "source": "test", "dedup_key": None, "meta": None}
    ev.update(kw)
    return ev


def _standalone():
    return {"action": "standalone", "incident_id": None, "role": None,
            "suppress": False}


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

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC)

    def test_channel_ids_come_from_nova_config_not_literals(self):
        """Routing must never hardcode a Slack channel id."""
        self.assertIsNone(re.search(r'"C0[A-Z0-9]{8,}"', _SRC),
                          "Slack channel ids belong in nova_config")
        for name in ("SLACK_FEED", "SLACK_ALERTS", "SLACK_DIGEST"):
            self.assertIn(f"nova_config.{name}", _SRC)

    def test_db_connect_has_bounded_timeout(self):
        self.assertIn("connect_timeout=5", _SRC)

    def test_mesh_relay_is_lan_only_and_length_capped(self):
        """The out-of-band relay must stay on the LAN and never dump a full body."""
        self.assertIn(".local", _mod.MESH_BRIDGE_URL)
        self.assertNotIn("https://", _mod.MESH_BRIDGE_URL)
        self.assertIn("text[:200]", _SRC)

    def test_maintenance_gate_fails_open_without_leaking_events(self):
        """A broken gate must not silence anything, and rows must still be kept."""
        self.assertIn("except Exception:", _SRC)
        self.assertIn("status='suppressed'", _SRC)
        self.assertIn("channel='maintenance-muted'", _SRC)

    def test_events_sql_is_parameterized(self):
        self.assertNotIn('cur.execute(f"', _SRC)
        self.assertIn("q += \" AND source = %s\"", _SRC)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_drain_batch_is_bounded(self):
        self.assertIn("LIMIT 200", _SRC)

    def test_dedup_lookup_is_a_single_indexed_row(self):
        self.assertIn("ORDER BY sent_at DESC LIMIT 1", _SRC)

    def test_maintenance_gate_checked_once_per_drain_not_per_event(self):
        """_maint_active() must be evaluated outside the event loop."""
        tree = ast.parse(_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "drain")
        for node in ast.walk(fn):
            if isinstance(node, ast.For):
                calls = [c for c in ast.walk(node)
                         if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                         and c.func.id == "_maint_active"]
                self.assertEqual(calls, [],
                                 "_maint_active must not be called per event")

    def test_default_dedup_window_is_a_sane_finite_value(self):
        self.assertIsInstance(_mod.DEDUP_WINDOW_S, int)
        self.assertGreaterEqual(_mod.DEDUP_WINDOW_S, 60)
        self.assertLessEqual(_mod.DEDUP_WINDOW_S, 86400)

    def test_digest_category_set_is_a_frozenset(self):
        """Membership is checked per event — must be O(1), not a list scan."""
        self.assertIsInstance(_mod.DIGEST_CATEGORIES, frozenset)

    def test_mesh_relay_only_fires_for_critical(self):
        self.assertIn('if ev["level"] == "critical":', _SRC)
        self.assertIn("_mesh_relay(", _SRC)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_dedup_window_falls_back_on_malformed_meta(self):
        for meta in ("{not json", "", None, [], 17, {"dedup_window_s": "abc"},
                     {"dedup_window_s": None}):
            self.assertEqual(_dedup_window({"meta": meta}), _mod.DEDUP_WINDOW_S,
                             f"meta={meta!r} must fall back to the default")

    def test_dedup_window_falls_back_when_meta_key_absent(self):
        self.assertEqual(_dedup_window({}), _mod.DEDUP_WINDOW_S)

    def test_delivery_failure_marks_error_and_does_not_stop_the_batch(self):
        cur = FakeCursor(fetchall_queue=[[
            _event(id=1, level="warning", title="bad"),
            _event(id=2, level="info", title="good"),
        ]])
        calls = []

        def post(msg, slack_channel=None):
            calls.append(slack_channel)
            if slack_channel == ALERTS:
                raise OSError("slack down")

        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", side_effect=post), \
             patch.object(_mod.nova_correlator, "correlate",
                          return_value=_standalone()):
            sent = _mod.drain()
        self.assertEqual(sent, 1)
        errs = [p for s, p in cur.sql_containing("status='error'")]
        self.assertEqual(errs, [(1,)])
        self.assertIn(("UPDATE telemetry.events SET status='sent', channel=%s, "
                       "sent_at=now() WHERE id=%s", (FEED, 2)), cur.executed)

    def test_db_connect_failure_returns_zero(self):
        def boom():
            raise RuntimeError("no db")
        with patch.object(_mod, "_connect", boom):
            self.assertEqual(_mod.drain(), 0)

    def test_correlator_exception_falls_back_to_standalone_delivery(self):
        cur = FakeCursor(fetchall_queue=[[_event(id=3, level="warning")]])
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", MagicMock()), \
             patch.object(_mod.nova_correlator, "correlate",
                          side_effect=RuntimeError("correlator down")):
            self.assertEqual(_mod.drain(), 1)

    def test_mesh_relay_failure_is_non_fatal(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen",
                          side_effect=OSError("bridge unreachable")):
            _mod._mesh_relay("title", "body")   # must not raise

    def test_mesh_relay_posts_a_length_capped_payload(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen") as m:
            _mod._mesh_relay("t" * 300, "b" * 300)
        req = m.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(len(payload["text"]), 200)
        self.assertEqual(req.full_url, _mod.MESH_BRIDGE_URL)


# ===========================================================================
# 4. UNIT TESTS — the three-tier policy
# ===========================================================================

class TestRouteTiers(unittest.TestCase):

    def test_warning_goes_to_alerts(self):
        self.assertEqual(_route("warning", None), ALERTS)

    def test_critical_goes_to_alerts(self):
        self.assertEqual(_route("critical", None), ALERTS)

    def test_info_in_digest_category_goes_to_digest(self):
        for cat in sorted(_mod.DIGEST_CATEGORIES):
            self.assertEqual(_route("info", cat), DIGEST, f"info/{cat}")

    def test_other_info_goes_to_feed(self):
        for cat in (None, "disk_full", "unknown_thing", "relay", "probe"):
            self.assertEqual(_route("info", cat), FEED, f"info/{cat}")

    def test_warning_in_digest_category_still_alerts(self):
        """The regression this tier scheme must never re-introduce: an actionable
        warning in a rollup category must NOT be buried in #nova-digest."""
        for cat in sorted(_mod.DIGEST_CATEGORIES):
            self.assertEqual(_route("warning", cat), ALERTS, f"warning/{cat}")
            self.assertNotEqual(_route("warning", cat), DIGEST)

    def test_critical_in_digest_category_still_alerts(self):
        for cat in sorted(_mod.DIGEST_CATEGORIES):
            self.assertEqual(_route("critical", cat), ALERTS, f"critical/{cat}")

    def test_category_override_wins_over_level(self):
        for lvl in ("info", "warning", "critical"):
            self.assertEqual(_route(lvl, "security_news"), FEED, lvl)
            self.assertEqual(_route(lvl, "claude_code"), FEED, lvl)

    def test_override_wins_even_for_a_digest_category(self):
        """If a category is in both maps, the override must decide."""
        with patch.dict(_mod.CATEGORY_OVERRIDE, {"network": FEED}):
            self.assertEqual(_route("info", "network"), FEED)
            self.assertEqual(_route("warning", "network"), FEED)

    def test_unknown_level_falls_back_to_feed(self):
        for lvl in ("debug", "", "notice", None):
            self.assertEqual(_route(lvl, None), FEED, repr(lvl))

    def test_unknown_level_in_digest_category_falls_back_to_feed(self):
        self.assertEqual(_route("debug", "calendar"), FEED)

    def test_network_and_home_are_digest_categories(self):
        for cat in ("network", "home", "calendar", "telemetry", "finance", "backup"):
            self.assertIn(cat, _mod.DIGEST_CATEGORIES)

    def test_channel_map_covers_the_three_levels(self):
        self.assertEqual(_mod.CHANNEL["info"], FEED)
        self.assertEqual(_mod.CHANNEL["warning"], ALERTS)
        self.assertEqual(_mod.CHANNEL["critical"], ALERTS)


class TestDedupWindow(unittest.TestCase):

    def test_dict_meta_is_honoured(self):
        self.assertEqual(_dedup_window({"meta": {"dedup_window_s": 21600}}), 21600)

    def test_json_string_meta_is_honoured(self):
        self.assertEqual(
            _dedup_window({"meta": json.dumps({"dedup_window_s": 72000})}), 72000)

    def test_numeric_string_value_is_coerced_to_int(self):
        self.assertEqual(_dedup_window({"meta": {"dedup_window_s": "600"}}), 600)

    def test_other_meta_keys_are_ignored(self):
        self.assertEqual(_dedup_window({"meta": {"host": "studio"}}),
                         _mod.DEDUP_WINDOW_S)

    def test_zero_is_treated_as_unset(self):
        """0 is falsy — an explicit 0 must not disable dedup entirely."""
        self.assertEqual(_dedup_window({"meta": {"dedup_window_s": 0}}),
                         _mod.DEDUP_WINDOW_S)

    def test_return_type_is_always_int(self):
        for meta in ({"dedup_window_s": 900}, "{}", None, "garbage"):
            self.assertIsInstance(_dedup_window({"meta": meta}), int)


# ===========================================================================
# 5. INTEGRATION TESTS — drain() honours the policy end to end
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def _drain(self, events, fetchone_queue=None, correlate=None):
        cur = FakeCursor(fetchall_queue=[events], fetchone_queue=fetchone_queue)
        post = MagicMock()
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", post), \
             patch.object(_mod.nova_correlator, "correlate",
                          return_value=correlate or _standalone()):
            sent = _mod.drain()
        return sent, cur, post

    def test_info_network_event_is_delivered_to_digest(self):
        sent, cur, post = self._drain([_event(id=10, level="info",
                                              category="network", title="churn")])
        self.assertEqual(sent, 1)
        self.assertEqual(post.call_args.kwargs["slack_channel"], DIGEST)
        self.assertIn(("UPDATE telemetry.events SET status='sent', channel=%s, "
                       "sent_at=now() WHERE id=%s", (DIGEST, 10)), cur.executed)

    def test_warning_network_event_is_delivered_to_alerts(self):
        sent, cur, post = self._drain([_event(id=11, level="warning",
                                              category="network", title="wan flap")])
        self.assertEqual(sent, 1)
        self.assertEqual(post.call_args.kwargs["slack_channel"], ALERTS)

    def test_info_uncategorized_event_is_delivered_to_feed(self):
        sent, cur, post = self._drain([_event(id=12, level="info",
                                              category="media", title="now playing")])
        self.assertEqual(post.call_args.kwargs["slack_channel"], FEED)

    def test_override_category_is_delivered_to_feed_despite_warning(self):
        sent, cur, post = self._drain([_event(id=13, level="warning",
                                              category="claude_code", title="ran")])
        self.assertEqual(post.call_args.kwargs["slack_channel"], FEED)

    def test_per_event_dedup_window_is_passed_to_the_lookup_query(self):
        ev = _event(id=14, level="warning", dedup_key="probe-x-fail",
                    meta={"dedup_window_s": 21600})
        sent, cur, post = self._drain([ev], fetchone_queue=[None])
        lookup = cur.sql_containing("make_interval(secs => %s)")
        self.assertEqual(len(lookup), 1)
        self.assertEqual(lookup[0][1], ("probe-x-fail", 21600))

    def test_default_dedup_window_used_when_meta_absent(self):
        ev = _event(id=15, level="warning", dedup_key="k", meta=None)
        sent, cur, post = self._drain([ev], fetchone_queue=[None])
        lookup = cur.sql_containing("make_interval(secs => %s)")
        self.assertEqual(lookup[0][1], ("k", _mod.DEDUP_WINDOW_S))

    def test_json_string_meta_window_used_by_drain(self):
        ev = _event(id=16, level="info", category="network", dedup_key="mesh-churn-daily",
                    meta=json.dumps({"dedup_window_s": 72000}))
        sent, cur, post = self._drain([ev], fetchone_queue=[None])
        lookup = cur.sql_containing("make_interval(secs => %s)")
        self.assertEqual(lookup[0][1], ("mesh-churn-daily", 72000))

    def test_duplicate_inside_window_is_suppressed_not_posted(self):
        ev = _event(id=17, level="warning", dedup_key="k", meta={"dedup_window_s": 600})
        sent, cur, post = self._drain([ev], fetchone_queue=[{"id": 9}])
        self.assertEqual(sent, 0)
        post.assert_not_called()
        self.assertTrue(cur.sql_containing("status='suppressed', collapsed_into=%s"))
        self.assertTrue(cur.sql_containing("dispatch_count = dispatch_count + 1"))

    def test_no_dedup_key_skips_the_lookup_entirely(self):
        sent, cur, post = self._drain([_event(id=18, level="warning", dedup_key=None)])
        self.assertEqual(cur.sql_containing("make_interval(secs => %s)"), [])
        self.assertEqual(sent, 1)

    def test_critical_also_goes_out_over_the_mesh(self):
        cur = FakeCursor(fetchall_queue=[[_event(id=19, level="critical",
                                                 title="pg down", body="hard")]])
        relay = MagicMock()
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", MagicMock()), \
             patch.object(_mod, "_mesh_relay", relay), \
             patch.object(_mod.nova_correlator, "correlate",
                          return_value=_standalone()):
            _mod.drain()
        relay.assert_called_once_with("pg down", "hard")

    def test_non_critical_does_not_use_the_mesh(self):
        cur = FakeCursor(fetchall_queue=[[_event(id=20, level="warning")]])
        relay = MagicMock()
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", MagicMock()), \
             patch.object(_mod, "_mesh_relay", relay), \
             patch.object(_mod.nova_correlator, "correlate",
                          return_value=_standalone()):
            _mod.drain()
        relay.assert_not_called()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_mixed_batch_lands_in_all_three_tiers(self):
        events = [
            _event(id=30, level="info", category="network", title="mesh churn"),
            _event(id=31, level="info", category="media", title="now playing"),
            _event(id=32, level="warning", category="network", title="wan failed"),
            _event(id=33, level="critical", category="telemetry", title="pg down"),
            _event(id=34, level="info", category="claude_code", title="ran a task"),
        ]
        cur = FakeCursor(fetchall_queue=[events])
        post = MagicMock()
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", post), \
             patch.object(_mod, "_mesh_relay", MagicMock()), \
             patch.object(_mod.nova_correlator, "correlate",
                          return_value=_standalone()):
            sent = _mod.drain()
        self.assertEqual(sent, 5)
        channels = [c.kwargs["slack_channel"] for c in post.call_args_list]
        self.assertEqual(channels, [DIGEST, FEED, ALERTS, ALERTS, FEED])

    def test_digest_never_receives_an_actionable_event(self):
        """Sweep every category at every level: DIGEST only ever sees info."""
        for cat in sorted(_mod.DIGEST_CATEGORIES):
            for lvl in ("warning", "critical"):
                self.assertNotEqual(_route(lvl, cat), DIGEST, f"{lvl}/{cat}")

    def test_alerts_never_receives_a_plain_info_event(self):
        for cat in [None, "media", "network", "home", "unknown"]:
            self.assertNotEqual(_route("info", cat), ALERTS, str(cat))

    def test_message_body_carries_title_body_and_tag(self):
        cur = FakeCursor(fetchall_queue=[[_event(id=35, level="warning",
                                                 category="network",
                                                 title="WAN failed",
                                                 body="12:04 — WAN1 down",
                                                 source="nova_unifi_monitor.py")]])
        post = MagicMock()
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", post), \
             patch.object(_mod.nova_correlator, "correlate",
                          return_value=_standalone()):
            _mod.drain()
        msg = post.call_args.args[0]
        self.assertIn(":warning:", msg)
        self.assertIn("WAN failed", msg)
        self.assertIn("12:04 — WAN1 down", msg)
        self.assertIn("nova_unifi_monitor.py", msg)

    def test_empty_queue_delivers_nothing(self):
        cur = FakeCursor(fetchall_queue=[[]])
        post = MagicMock()
        with patch.object(_mod, "_connect", lambda: FakeConn(cur)), \
             patch.object(_mod.nova_config, "post_both", post):
            self.assertEqual(_mod.drain(), 0)
        post.assert_not_called()


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_notifier.py has syntax errors: {e}")

    def test_three_tier_policy_is_documented_in_the_header(self):
        doc = ast.get_docstring(ast.parse(_SRC)) or ""
        self.assertIn("ROUTE", doc)
        for chan in ("#nova-alerts", "#nova-digest", "#nova-feed"):
            self.assertIn(chan, _SRC, f"{chan} should be named in the policy comment")

    def test_public_callables_present(self):
        for fn in ("drain", "_route", "_dedup_window", "_fmt", "_connect",
                   "_mesh_relay", "main"):
            self.assertTrue(callable(getattr(_mod, fn, None)), f"missing: {fn}")

    def test_routing_targets_are_distinct_channels(self):
        self.assertNotEqual(_mod.nova_config.SLACK_ALERTS, _mod.nova_config.SLACK_DIGEST)
        self.assertNotEqual(_mod.nova_config.SLACK_ALERTS, _mod.nova_config.SLACK_FEED)
        self.assertNotEqual(_mod.nova_config.SLACK_DIGEST, _mod.nova_config.SLACK_FEED)

    def test_every_route_result_is_a_known_channel(self):
        known = {FEED, ALERTS, DIGEST}
        cats = [None] + sorted(_mod.DIGEST_CATEGORIES) + sorted(_mod.CATEGORY_OVERRIDE) \
            + ["nonsense"]
        for lvl in ("info", "warning", "critical", "debug", ""):
            for cat in cats:
                self.assertIn(_route(lvl, cat), known, f"{lvl}/{cat}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
