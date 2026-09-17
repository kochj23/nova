#!/usr/bin/env python3
"""Safety invariants for the graduated-autonomy engine (Rungs 1-3).

These are the assertions that let us turn the dials up: the kill switch stops
everything, caps bound the blast radius, the ledger always records an undo, and an
action-class can only earn standing autonomy with a clean record AND good calibration
— and loses it the instant it's vetoed. All DB rows use a 'test:*' class and are
cleaned up; nothing here touches a real proposal or a real service.

Run: python3.14 -m pytest scripts/tests/test_nova_autonomy_safety.py -q
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_autonomy_safety as S            # noqa: E402
import nova_coagency as C                   # noqa: E402

TEST_CLASS = "test:autonomy-selfcheck"


@pytest.fixture()
def oc():
    conn = psycopg2.connect(S.OPS_DSN); conn.autocommit = True
    cur = conn.cursor()
    S.ensure_schema(cur)
    _clean(cur)
    yield cur
    _clean(cur)
    # never leave the tripwire behind
    try:
        os.remove(S.KILL_FILE)
    except FileNotFoundError:
        pass
    conn.close()


def _clean(cur):
    cur.execute("DELETE FROM autonomy_trust WHERE action_class LIKE 'test:%'")
    cur.execute("DELETE FROM autonomy_ledger WHERE action_class LIKE 'test:%'")


# ── pure functions: no DB, no side effects ──────────────────────────────────────
def test_action_class_normalizes_by_target():
    assert S.action_class_of("please restart it", "nova-freshness-monitor") == "restart:nova-freshness-monitor"
    assert S.action_class_of("restart nova-soil-monitor now") == "restart:nova-soil-monitor"


def test_observation_never_normalizes_to_a_restart():
    # The bug that force-restarted approved observations: a 'monitor X' note must NOT
    # become a 'restart:X' class, or it accrues restart-trust and gets executed.
    assert S.is_restart_action("Monitor motion detection events for security") is False
    assert S.is_restart_action("restart nova-soil-monitor now") is True
    assert S.action_class_of("Monitor motion events", "nova-face-gate-watch").startswith("observe:")
    assert S.action_class_of("please restart it", "nova-freshness-monitor") == "restart:nova-freshness-monitor"


def test_ledger_never_stores_null_rollback(oc):
    lid = S.record_ledger(oc, source="test", autonomy_level="rung1-selfheal",
                          action_class=TEST_CLASS, target="x", action="do x",
                          rollback_action="", executed=False)
    oc.execute("SELECT rollback_action FROM autonomy_ledger WHERE id=%s", (lid,))
    assert "none recorded" in oc.fetchone()[0].lower()      # empty undo is backfilled, never NULL


# ── kill switch ─────────────────────────────────────────────────────────────────
def test_kill_file_beats_everything(oc):
    open(S.KILL_FILE, "w").write("test")
    try:
        assert S.kill_switch_engaged(oc) is True
        ok, why = S.earned_ok(oc, TEST_CLASS)
        assert ok is False and "kill" in why.lower()
    finally:
        os.remove(S.KILL_FILE)
    assert S.kill_switch_engaged(oc) is False


# ── track record & granting ──────────────────────────────────────────────────────
def test_grant_requires_min_correct_and_good_calibration(oc, monkeypatch):
    # Pin calibration BELOW the gate so the only variable under test is the streak.
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    for i in range(S.MIN_CORRECT - 1):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    oc.execute("SELECT granted FROM autonomy_trust WHERE action_class=%s", (TEST_CLASS,))
    assert oc.fetchone()[0] is False                        # one short → still not granted
    S.note_human_decision(oc, TEST_CLASS, approved=True)     # hits MIN_CORRECT
    oc.execute("SELECT granted, correct_count FROM autonomy_trust WHERE action_class=%s", (TEST_CLASS,))
    granted, correct = oc.fetchone()
    assert granted is True and correct == S.MIN_CORRECT


def test_bad_calibration_blocks_grant_and_earned(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: S.MAX_CALIB + 0.10)   # too weak
    for _ in range(S.MIN_CORRECT + 2):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    oc.execute("SELECT granted FROM autonomy_trust WHERE action_class=%s", (TEST_CLASS,))
    assert oc.fetchone()[0] is False                        # streak alone can't buy freedom
    ok, why = S.earned_ok(oc, TEST_CLASS)
    assert ok is False and "calibration" in why.lower()


def test_rejection_poisons_the_streak(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    for _ in range(S.MIN_CORRECT):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    S.note_human_decision(oc, TEST_CLASS, approved=False)    # one rejection
    oc.execute("SELECT correct_count, wrong_count, granted FROM autonomy_trust WHERE action_class=%s", (TEST_CLASS,))
    correct, wrong, granted = oc.fetchone()
    assert correct == 0 and wrong == 1 and granted is False  # streak zeroed, grant revoked


def test_veto_revokes_and_distrusts(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    for _ in range(S.MIN_CORRECT):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    assert S.earned_ok(oc, TEST_CLASS)[0] is True            # earned
    S.note_veto(oc, TEST_CLASS, "not what I wanted")
    ok, why = S.earned_ok(oc, TEST_CLASS)
    assert ok is False and "distrust" in why.lower()


def test_earned_fails_closed_without_track_record(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    ok, why = S.earned_ok(oc, "test:never-seen")
    assert ok is False and "track record" in why.lower()


# ── blast-radius cap ──────────────────────────────────────────────────────────────
def test_rate_cap_blocks_when_daily_ceiling_hit(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    monkeypatch.setattr(S, "_caps", lambda _oc: {"per_hour": 100, "per_day": 3})
    for _ in range(S.MIN_CORRECT):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    # fill the day's ledger to the cap
    for i in range(3):
        S.record_ledger(oc, source="test", autonomy_level="rung3-earned",
                        action_class=TEST_CLASS, target="x", action="a",
                        rollback_action="undo", executed=True)
    ok, why = S.earned_ok(oc, TEST_CLASS)
    assert ok is False and "cap" in why.lower()


# ── the co-agency redline still refuses self-modification (defense in depth) ──────
@pytest.mark.parametrize("bad", [
    "disable the kill switch",
    "edit nova_coagency redline",
    "set coagency_mode to live",
    "add nova-gateway to safe_services",
    "replicate myself to another host",
    "resist shutdown",
])
def test_redline_blocks_self_modification(bad):
    assert C.redline_ok(bad) is False
