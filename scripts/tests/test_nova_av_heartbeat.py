#!/usr/bin/env python3
"""7-category tests for the AV-poller off/unreachable HEARTBEAT + freshness-monitor
reclassification (Little Mister's non-negotiable set): Security, Performance, Retry,
Unit, Integration, Functional, Frame.

Covers the 2026-09-09 fix: nova_av_poller used to SKIP writing when a device was
unreachable, so telemetry.av_state went silent while the Onkyos were merely off.
Now it records 'off' as data (on transition, and on a heartbeat interval), throttled.
"""
import importlib.util
import os
import time
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))


def _load(mod):
    path = os.path.join(SCRIPTS, f"{mod}.py")
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

avp = _load("nova_av_poller")


def _poller():
    """A poller instance WITHOUT running __init__ (no DB / no sockets)."""
    p = object.__new__(avp.AVPoller)
    p.db = None
    p.last_states = {}
    p.last_poll_times = {}
    p.last_write_times = {}
    p.running = True
    return p


@pytest.fixture
def captured(monkeypatch):
    """Capture record_state calls; no DB touched."""
    calls = []
    monkeypatch.setattr(avp, "record_state", lambda conn, name, zone, state: calls.append((name, zone, dict(state))))
    return calls


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_heartbeat_due_when_never_written():
    p = _poller()
    assert p._heartbeat_due("Onkyo/main") is True  # last_write defaults to 0 → due


def test_unit_heartbeat_not_due_right_after_write():
    p = _poller()
    p.last_write_times["Onkyo/main"] = time.time()
    assert p._heartbeat_due("Onkyo/main") is False


def test_unit_record_state_maps_unreachable_to_power_false():
    seen = {}
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): seen["power"] = params[2]  # power_bool position
    class Conn:
        def cursor(self): return Cur()
    avp.record_state(Conn(), "Onkyo", "main", {"power": "unreachable"})
    assert seen["power"] is False  # unreachable => off, not a crash/skip


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_first_unreachable_is_recorded(captured):
    p = _poller()
    p._handle_unreachable("Onkyo TX-NR696", "main", "Onkyo TX-NR696/main", {"power": "unreachable"})
    assert len(captured) == 1  # first sight → recorded (heartbeat due at last_write=0)
    assert captured[0][2]["power"] == "unreachable"


def test_functional_repeat_unreachable_is_throttled(captured):
    p = _poller()
    key = "Onkyo TX-NR696/main"
    p._handle_unreachable("Onkyo TX-NR696", "main", key, {"power": "unreachable"})  # writes
    p._handle_unreachable("Onkyo TX-NR696", "main", key, {"power": "unreachable"})  # throttled
    assert len(captured) == 1  # second call within heartbeat window → NOT written


def test_functional_transition_reachable_to_unreachable_records_immediately(captured):
    p = _poller()
    key = "Onkyo TX-NR696/main"
    p.last_states[key] = {"power": "on"}          # was reachable
    p.last_write_times[key] = time.time()          # just wrote → heartbeat NOT due
    p._handle_unreachable("Onkyo TX-NR696", "main", key, {"power": "unreachable"})
    assert len(captured) == 1  # transition forces a write regardless of throttle


# ── Retry / resilience ──────────────────────────────────────────────────────────
def test_retry_record_failure_triggers_reconnect(monkeypatch):
    p = _poller()
    reconnected = {"n": 0}
    def boom(conn, name, zone, state): raise RuntimeError("db gone")
    monkeypatch.setattr(avp, "record_state", boom)
    monkeypatch.setattr(p, "_reconnect_db", lambda: reconnected.__setitem__("n", reconnected["n"] + 1))
    p._record("Onkyo", "main", "Onkyo/main", {"power": "unreachable"})  # must not raise
    assert reconnected["n"] == 1


# ── Security ────────────────────────────────────────────────────────────────────
def test_security_heartbeat_interval_is_bounded():
    # a sane, finite cadence — never 0 (write-storm) nor absurdly long (silent again)
    assert 60 <= avp.AV_HEARTBEAT_SEC <= 3600


def test_security_record_uses_parameterized_insert():
    # record_state must not string-format device names into SQL (injection guard)
    src = open(os.path.join(SCRIPTS, "nova_av_poller.py")).read()
    assert "INSERT INTO telemetry.av_state" in src
    assert "VALUES (NOW(), %s" in src  # parameterized, not f-string


# ── Performance ──────────────────────────────────────────────────────────────────
def test_performance_heartbeat_due_is_cheap():
    p = _poller()
    start = time.perf_counter()
    for _ in range(100000):
        p._heartbeat_due("k")
    assert time.perf_counter() - start < 1.0


# ── Integration (cross-module: freshness monitor reclassification) ───────────────
def test_integration_freshness_slas_reclassified():
    fm = _load("nova_freshness_monitor")
    by_id = {s.name: s for s in fm.EXPLICIT_STREAMS}
    assert by_id["telemetry.av_state"].sla_s == 3600            # tightened (heartbeats now)
    assert "telemetry.activity" in by_id                        # newly explicit
    assert by_id["telemetry.activity"].sla_s == 3600
    dpe = by_id["telemetry.device_power_events"]
    assert dpe.sla_s == 7 * 24 * 3600 and dpe.level == "info"   # true event table
    assert by_id["telemetry.chp_incidents"].sla_s == 24 * 3600  # loosened from 6h


# ── Frame (boundary / malformed input) ───────────────────────────────────────────
def test_frame_unreachable_state_missing_keys(captured):
    p = _poller()
    p._handle_unreachable("Bose Kitchen", "main", "Bose Kitchen/main", {})  # no 'power' key
    assert len(captured) == 1  # still records; record_state tolerates missing fields


def test_frame_empty_key_does_not_crash():
    p = _poller()
    assert p._heartbeat_due("") is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
