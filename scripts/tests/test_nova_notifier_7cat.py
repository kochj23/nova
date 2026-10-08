#!/usr/bin/env python3
"""nova_notifier.py -> nova_voice_room hand-off — 7-category supplement (Security, Performance,
Retry, Unit, Integration, Functional, Frame). The voice module is always a stub: nothing speaks,
nothing posts. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import re
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_notifier.py"
SRC = SCRIPT.read_text()


def _stubs(voice=None):
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_FEED, cfg.SLACK_ALERTS, cfg.SLACK_DIGEST, cfg.SLACK_EMAIL = "C_FEED", "C_ALERTS", "C_DIGEST", "C_EMAIL"
    cfg.post_both = MagicMock()
    corr = types.ModuleType("nova_correlator")
    corr.correlate = MagicMock(return_value={"action": "standalone", "suppress": False, "incident_id": None})
    corr.llm_summarize = MagicMock(return_value=("s", "m"))
    rem = types.ModuleType("nova_remediation"); rem.propose_for_incident = MagicMock()
    maint = types.ModuleType("nova_maintenance"); maint.SECURITY_CATEGORIES = frozenset({"security"}); maint.is_active = lambda: False
    triage = types.ModuleType("nova_alert_triage"); triage.triage = lambda *a, **k: None
    mods = {"nova_config": cfg, "nova_correlator": corr, "nova_remediation": rem, "nova_maintenance": maint,
            "nova_alert_triage": triage}
    if voice is not None:
        mods["nova_voice_room"] = voice
    return mods


def _load(voice=None):
    spec = importlib.util.spec_from_file_location("nf_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs(voice)):
        spec.loader.exec_module(mod)
    return mod


def _voice_stub(side_effect=None):
    v = types.ModuleType("nova_voice_room")
    v.dispatch = MagicMock(side_effect=side_effect, return_value=True)
    return v


class _Cur:
    def __init__(self, events, prior=None):
        self.events, self.prior, self.sql, self._last = events, prior, [], ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def fetchall(self):
        return [dict(e) for e in self.events]

    def fetchone(self):
        return self.prior if "status='sent'" in self._last else None


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def close(self):
        pass


def _ev(**kw):
    e = {"id": 1, "title": "Smoke: hallway", "body": "", "level": "critical", "category": "smoke",
         "source": "ha", "dedup_key": "smoke-hall", "meta": None, "ts": 0}
    e.update(kw)
    return e


def _drain(nf, events, prior=None, order=None):
    cur = _Cur(events, prior)
    if order is not None:
        nf.nova_correlator.correlate = MagicMock(side_effect=lambda *a: order.append("correlate") or
                                                 {"action": "standalone", "suppress": False, "incident_id": None})
    with patch.object(nf, "_connect", lambda: _Conn(cur)), patch.object(nf, "_mesh_relay", MagicMock()), \
         patch.dict(sys.modules, {"nova_alert_triage": _stubs()["nova_alert_triage"]}), \
         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return nf.drain(), cur


class TestSecurity(unittest.TestCase):
    def test_dispatch_gets_a_copy_not_the_live_row(self):
        v = _voice_stub(side_effect=lambda ev: ev.update(level="info", title="tampered"))
        nf = _load(v)
        n, _ = _drain(nf, [_ev()])
        self.assertEqual(n, 1)
        self.assertIn("Smoke: hallway", nf.nova_config.post_both.call_args[0][0])  # mutation didn't leak

    def test_voice_import_is_guarded(self):
        self.assertRegex(SRC, r"try:[^\n]*\n\s+import nova_voice_room\nexcept Exception:\n\s+nova_voice_room = None")


class TestPerformance(unittest.TestCase):
    def test_dispatch_cost_is_negligible_per_event(self):
        nf = _load(_voice_stub())
        evs = [_ev(id=i, dedup_key=f"k{i}") for i in range(200)]
        t = time.perf_counter()
        n, _ = _drain(nf, evs)
        self.assertEqual(n, 200)
        self.assertEqual(nf.nova_voice_room.dispatch.call_count, 200)
        self.assertLess(time.perf_counter() - t, 2.0)


class TestRetry(unittest.TestCase):
    # No retry inside the hand-off by design: dispatch() is fire-and-forget; retries live in the
    # voice child (_event 3x, play 2x). What matters here: a voice failure never costs the Slack alert.
    def test_dispatch_exception_does_not_block_slack(self):
        nf = _load(_voice_stub(side_effect=RuntimeError("voice broke")))
        n, cur = _drain(nf, [_ev()])
        self.assertEqual(n, 1)
        nf.nova_config.post_both.assert_called_once()

    def test_broken_voice_module_import_falls_back_to_none(self):
        with patch.dict(sys.modules, {"nova_voice_room": None}):   # None in sys.modules -> ImportError
            nf = _load()
        self.assertIsNone(nf.nova_voice_room)
        n, _ = _drain(nf, [_ev()])
        self.assertEqual(n, 1)

    def test_db_down_returns_zero_and_daemon_retries_next_poll(self):
        nf = _load(_voice_stub())
        with patch.object(nf, "_connect", side_effect=OSError("pg down")), redirect_stderr(io.StringIO()):
            self.assertEqual(nf.drain(), 0)
        nf.nova_voice_room.dispatch.assert_not_called()
        self.assertIn("time.sleep(a.interval)", SRC)


class TestUnit(unittest.TestCase):
    def test_dispatch_block_sits_between_dedup_and_triage(self):
        i_dedup = SRC.index("dispatch_count = dispatch_count + 1")
        i_voice = SRC.index("nova_voice_room.dispatch(dict(ev))")
        i_triage = SRC.index("t = _triage_event(ev)")
        self.assertLess(i_dedup, i_voice)
        self.assertLess(i_voice, i_triage)


class TestIntegration(unittest.TestCase):
    def test_real_voice_module_dispatch_spawns_child_only_for_smoke(self):
        spec = importlib.util.spec_from_file_location("vr_for_nf", SCRIPTS / "nova_voice_room.py")
        vr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vr)
        vr.LOG = Path("/dev/null")
        nf = _load(vr)
        with patch.object(vr.subprocess, "Popen") as po:
            n, _ = _drain(nf, [_ev(id=1), _ev(id=2, category="storage", level="warning", dedup_key="d2")])
        self.assertEqual(n, 2)
        self.assertEqual(po.call_count, 1)
        self.assertEqual(po.call_args[0][0][-2:], ["--event", "1"])


class TestFunctional(unittest.TestCase):
    def test_new_event_dispatched_before_correlation(self):
        order = []
        v = _voice_stub(side_effect=lambda ev: order.append("voice"))
        nf = _load(v)
        _drain(nf, [_ev()], order=order)
        self.assertEqual(order, ["voice", "correlate"])

    def test_duplicate_is_not_dispatched(self):
        nf = _load(_voice_stub())
        n, _ = _drain(nf, [_ev()], prior={"id": 99})
        self.assertEqual(n, 0)
        nf.nova_voice_room.dispatch.assert_not_called()

    def test_maintenance_muted_event_not_dispatched(self):
        nf = _load(_voice_stub())
        with patch.object(nf, "_maint_active", return_value=True), patch.object(nf, "_MAINT_CATS", frozenset({"smoke"})):
            n, _ = _drain(nf, [_ev()])
        self.assertEqual(n, 0)
        nf.nova_voice_room.dispatch.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_with_voice_module_absent_is_clean(self):
        with patch.dict(sys.modules, {"nova_voice_room": None}):
            nf = _load()
        self.assertTrue(callable(nf.drain))
        self.assertIsNone(re.search(r"^drain\(|^main\(\)", SRC, re.M))


if __name__ == "__main__":
    unittest.main()
