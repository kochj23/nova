#!/usr/bin/env python3
"""Tests for nova_autonomy_safety.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Safety invariants for the graduated-autonomy engine (Rungs 1-3).

These are the assertions that let us turn the dials up: the kill switch stops
everything, caps bound the blast radius, the ledger always records an undo, and an
action-class can only earn standing autonomy with a clean record AND good calibration
— and loses it the instant it's vetoed. Fully offline (2026-10-08): every DB call goes to an
in-memory cursor stub and the kill tripwire lives in a tempdir — nothing here writes
production autonomy_trust / autonomy_ledger, a real proposal, or a real service.

Run: python3.14 -m pytest scripts/tests/test_nova_autonomy_safety.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_autonomy_safety as S            # noqa: E402
import nova_coagency as C                   # noqa: E402

TEST_CLASS = "test:autonomy-selfcheck"


@pytest.fixture()
def oc(monkeypatch, tmp_path):
    """In-memory autonomy_trust/autonomy_ledger stub (_TrustCur, defined below).

    2026-10-08: this fixture used to open an autocommit connection to PRODUCTION nova_ops and
    write test:* rows into autonomy_trust / autonomy_ledger (and touch the real KILL_FILE).
    Tests must never write production tables, so everything is offline now and the kill
    tripwire points at a tempdir."""
    monkeypatch.setattr(S, "KILL_FILE", str(tmp_path / "autonomy-kill"))
    return _TrustCur()


def _trust(cur, ac):
    t = cur.trust[ac]
    return t["correct"], t["wrong"], t["granted"]


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
    S.record_ledger(oc, source="test", autonomy_level="rung1-selfheal",
                    action_class=TEST_CLASS, target="x", action="do x",
                    rollback_action="", executed=False)
    assert "none recorded" in oc.ledger[0][5].lower()       # empty undo is backfilled, never NULL


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
    # 2026-10-01 split the bar: a non-state-changing class (this one) graduates at
    # MIN_CORRECT_REVERSIBLE, restart:* keeps MIN_CORRECT. Ask the module which applies.
    need = S.min_correct_for(TEST_CLASS)
    for i in range(need - 1):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    assert _trust(oc, TEST_CLASS)[2] is False               # one short → still not granted
    S.note_human_decision(oc, TEST_CLASS, approved=True)     # hits the bar
    correct, _, granted = _trust(oc, TEST_CLASS)
    assert granted is True and correct == need


def test_bad_calibration_blocks_grant_and_earned(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: S.MAX_CALIB + 0.10)   # too weak
    for _ in range(S.MIN_CORRECT + 2):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    assert _trust(oc, TEST_CLASS)[2] is False                # streak alone can't buy freedom
    ok, why = S.earned_ok(oc, TEST_CLASS)
    assert ok is False and "calibration" in why.lower()


def test_rejection_poisons_the_streak(oc, monkeypatch):
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    for _ in range(S.MIN_CORRECT):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    S.note_human_decision(oc, TEST_CLASS, approved=False)    # one rejection
    correct, wrong, granted = _trust(oc, TEST_CLASS)
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
    oc.day = len(oc.ledger)                                  # stub: 24h ledger count
    ok, why = S.earned_ok(oc, TEST_CLASS)
    assert ok is False and "cap" in why.lower()


def test_suite_never_connects_to_postgres(oc, monkeypatch):
    """Regression for the 2026-10-08 production-write bug: the whole safety flow runs with
    psycopg2.connect forbidden."""
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prod DB")))
    monkeypatch.setattr(S, "calibration_error", lambda _oc: 0.05)
    for _ in range(S.MIN_CORRECT):
        S.note_human_decision(oc, TEST_CLASS, approved=True)
    assert S.earned_ok(oc, TEST_CLASS)[0] is True


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


# ═══════════════════════════════════════════════════════════════════════════════
# The 7 house categories — OFFLINE (stateful cursor stub; no PostgreSQL, no network)
# ═══════════════════════════════════════════════════════════════════════════════
import importlib.util
import json
import re
import subprocess
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_autonomy_safety.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="safety-test-"))
NO_KILL = str(TMP / "absent-kill-file")


class _TrustCur:
    """An in-memory autonomy_trust / autonomy_ledger / service_config, answering the exact SQL
    the module issues. Records every statement so tests can assert what was written."""
    def __init__(self, calib=0.05, kill=None, caps=None, hr=0, day=0, class_used=0):
        self.calib, self.kill, self.caps = calib, kill, caps
        self.hr, self.day, self.class_used = hr, day, class_used
        self.trust, self.ledger, self.sql, self.params, self._last = {}, [], [], [], None
        self.connection = types.SimpleNamespace(close=lambda: None)

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        s = " ".join(sql.split())
        if "FROM turing_scoreboard" in s:
            self._last = (self.calib,) if self.calib is not None else None
        elif "key='kill_switch'" in s and s.startswith("SELECT"):
            self._last = (self.kill,) if self.kill is not None else None
        elif "key='caps'" in s:
            self._last = (self.caps,) if self.caps is not None else None
        elif "INSERT INTO service_config" in s:
            self.kill = "true"
        elif "INSERT INTO autonomy_trust" in s:
            self.trust.setdefault(params[0], {"correct": 0, "wrong": 0, "granted": False, "daily_limit": 3, "notes": None})
        elif "UPDATE autonomy_trust" in s:
            t = self.trust[params[-1]]
            if "correct_count = correct_count + 1" in s:
                t["correct"] += 1
            elif "wrong_count = wrong_count + 1" in s:
                t["wrong"] += 1; t["correct"] = 0; t["granted"] = False
                if "notes = %s" in s:
                    t["notes"] = params[0]
            elif "granted=true" in s:
                t["granted"] = True; t["notes"] = params[0]
        elif "SELECT correct_count, wrong_count, granted, daily_limit" in s:
            t = self.trust.get(params[0])
            self._last = (t["correct"], t["wrong"], t["granted"], t["daily_limit"]) if t else None
        elif "SELECT correct_count, wrong_count, granted FROM" in s:
            t = self.trust.get(params[0])
            self._last = (t["correct"], t["wrong"], t["granted"]) if t else None
        elif "SELECT action_class FROM autonomy_trust WHERE granted" in s:
            self._last = [(k,) for k, v in self.trust.items() if v["granted"]]
        elif "INSERT INTO autonomy_ledger" in s:
            self.ledger.append(params); self._last = (len(self.ledger),)
        elif "action_class=%s AND ts > now()" in s:
            self._last = (self.class_used,)
        elif "interval '1 hour'" in s:
            self._last = (self.hr,)
        elif "interval '24 hours'" in s:
            self._last = (self.day,)
        elif "SELECT now() +" in s:
            self._last = (datetime(2026, 10, 5, 12, 0),)

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])


class _Boom:
    def execute(self, *a, **k): raise RuntimeError("db down")
    def fetchone(self): return None


def _earn(cur, ac, n=None):
    for _ in range(n or S.min_correct_for(ac)):
        S.note_human_decision(cur, ac, approved=True)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r'execute\([^)]*%\s*\(')          # no %-interpolated values into SQL

    def test_everything_fails_closed(self):
        with mock.patch.object(S, "KILL_FILE", NO_KILL):
            self.assertEqual(S.earned_ok(_Boom(), "restart:x"), (False, "no calibration score — fail closed"))
            ok, why = S.rate_ok(_Boom())
            self.assertFalse(ok); self.assertIn("fail closed", why)
            cur = _TrustCur(calib=None)
            _earn(cur, "observe:draft")
            self.assertFalse(cur.trust["observe:draft"]["granted"])   # no calibration ⇒ no grant

    def test_state_changing_classes_keep_the_full_bar(self):
        for ac in ("restart:nova-soil-monitor", "observe:adjust the thermostat", "observe:delete", "observe:set-mode"):
            self.assertEqual(S.min_correct_for(ac), S.MIN_CORRECT, ac)
        for ac in ("observe:draft a note", "ingest:gutenberg", "observe:monitor motion"):
            self.assertEqual(S.min_correct_for(ac), S.MIN_CORRECT_REVERSIBLE, ac)

    def test_tripwire_file_beats_the_database(self):
        kill = TMP / "kill"; kill.write_text("x")
        try:
            with mock.patch.object(S, "KILL_FILE", str(kill)):
                self.assertTrue(S.kill_switch_engaged(None))
                self.assertTrue(S.kill_switch_engaged(_Boom()))
        finally:
            kill.unlink()


class TestPerformance(unittest.TestCase):
    def test_classification_fast_on_10k_actions(self):
        acts = [f"restart nova-soil-monitor {i}" if i % 2 else f"monitor motion events {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        classes = [S.action_class_of(a) for a in acts]
        needs = [S.min_correct_for(c) for c in classes]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(needs.count(S.MIN_CORRECT), 5_000)


class TestRetry(unittest.TestCase):
    def test_ledger_write_fails_open_to_minus_one(self):
        # RETRY GAP: record_ledger — single INSERT attempt; a failure logs and returns -1, never raises
        lid = S.record_ledger(_Boom(), source="t", autonomy_level="rung1-selfheal", action_class="restart:x",
                              target="x", action="a", rollback_action="undo")
        self.assertEqual(lid, -1)

    def test_reads_fail_open_to_safe_defaults(self):
        # RETRY GAP: calibration_error / _caps / kill_switch_engaged — one attempt each, safe default
        self.assertIsNone(S.calibration_error(_Boom()))
        self.assertEqual(S._caps(_Boom()), S.DEFAULT_CAPS)
        with mock.patch.object(S, "KILL_FILE", NO_KILL):
            self.assertFalse(S.kill_switch_engaged(_Boom()))


class TestUnit(unittest.TestCase):
    def test_action_class_of_edges(self):
        self.assertEqual(S.action_class_of(""), "observe:note")
        self.assertEqual(S.action_class_of(None, None), "observe:note")
        self.assertEqual(S.action_class_of("please heal"), "restart:unknown")
        self.assertEqual(S.action_class_of("bounce nova-zigbee-lqi"), "restart:nova-zigbee-lqi")
        self.assertEqual(S.action_class_of("ingest gutenberg #1342 into literature — Pride"), S.INGEST_CLASS)
        self.assertEqual(S.action_class_of("Draft a note", "nova-soil-monitor"), "observe:nova-soil-monitor")

    def test_parse_ingest(self):
        self.assertEqual(S.parse_ingest("ingest gutenberg #1342 into philosophy — Title"), ("1342", "philosophy"))
        self.assertEqual(S.parse_ingest("Ingest Project Gutenberg 84"), ("84", None))
        self.assertIsNone(S.parse_ingest("ingest gutenberg #1234567"))
        self.assertIsNone(S.parse_ingest("read a book"))
        self.assertIsNone(S.parse_ingest(None))

    def test_caps_parsing_variants(self):
        self.assertEqual(S._caps(_TrustCur(caps='{"per_hour": 2}')), {"per_hour": 2, "per_day": 20})
        self.assertEqual(S._caps(_TrustCur(caps={"per_day": "9"})), {"per_hour": 6, "per_day": 9})
        self.assertEqual(S._caps(_TrustCur(caps="not json")), S.DEFAULT_CAPS)

    def test_kill_switch_db_values(self):
        with mock.patch.object(S, "KILL_FILE", NO_KILL):
            for v, want in (('"true"', True), ("on", True), ("1", True), ("false", False), (None, False)):
                self.assertEqual(S.kill_switch_engaged(_TrustCur(kill=v)), want, v)

    def test_ledger_backfills_empty_rollback(self):
        cur = _TrustCur()
        lid = S.record_ledger(cur, source="t", autonomy_level="rung1-selfheal", action_class="restart:x",
                              target="x", action="a", rollback_action="", result="r" * 1000)
        self.assertEqual(lid, 1)
        self.assertIn("none recorded", cur.ledger[0][5])
        self.assertEqual(len(cur.ledger[0][8]), 800)


class TestIntegration(unittest.TestCase):
    def test_schema_creates_both_tables(self):
        cur = _TrustCur(); S.ensure_schema(cur)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS autonomy_ledger" in q for q in cur.sql))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS autonomy_trust" in q for q in cur.sql))

    def test_reversible_class_graduates_at_the_lower_bar(self):
        cur = _TrustCur(calib=0.05)
        _earn(cur, "observe:draft", S.MIN_CORRECT_REVERSIBLE - 1)
        self.assertFalse(cur.trust["observe:draft"]["granted"])
        S.note_human_decision(cur, "observe:draft", approved=True)
        self.assertTrue(cur.trust["observe:draft"]["granted"])
        self.assertIn("granted at correct=3", cur.trust["observe:draft"]["notes"])

    def test_restart_class_needs_the_full_bar(self):
        cur = _TrustCur(calib=0.05)
        _earn(cur, "restart:nova-soil-monitor", S.MIN_CORRECT - 1)
        self.assertFalse(cur.trust["restart:nova-soil-monitor"]["granted"])
        S.note_human_decision(cur, "restart:nova-soil-monitor", approved=True)
        self.assertTrue(cur.trust["restart:nova-soil-monitor"]["granted"])

    def test_actor_and_coagency_import_this_module(self):
        import nova_autonomy_actor as A
        self.assertIs(A._safety, S)
        self.assertIs(C._safety, S)

    def test_status_line_reflects_earned_classes(self):
        cur = _TrustCur(calib=0.1, day=2)
        _earn(cur, "observe:draft")
        with mock.patch.object(S, "KILL_FILE", NO_KILL):
            st = S.autonomy_status(cur)
        self.assertEqual(st["earned_classes"], ["observe:draft"])
        self.assertIn("1 action-class(es)", st["line"])
        self.assertIn("Calibration 0.100", st["line"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_earn_execute_then_veto_revokes(self):
        cur = _TrustCur(calib=0.05)
        ac = "restart:nova-soil-monitor"
        with mock.patch.object(S, "KILL_FILE", NO_KILL):
            _earn(cur, ac)
            ok, why = S.earned_ok(cur, ac)
            self.assertTrue(ok); self.assertIn("earned (correct=5", why)
            lid = S.record_ledger(cur, source="earned", autonomy_level="rung3-earned", action_class=ac,
                                  target="x", action="restart", rollback_action="stop", executed=True, vetoable=True)
            self.assertEqual(lid, 1)
            self.assertEqual(cur.ledger[0][9], datetime(2026, 10, 5, 12, 0))     # veto window recorded
            S.note_veto(cur, ac, "not what I wanted")
            ok, why = S.earned_ok(cur, ac)
        self.assertFalse(ok); self.assertIn("distrusted (wrong_count=1)", why)
        self.assertEqual(cur.trust[ac]["notes"], "vetoed: not what I wanted")
        self.assertEqual(cur.trust[ac]["correct"], 0)

    def test_every_gate_is_rechecked_live(self):
        with mock.patch.object(S, "KILL_FILE", NO_KILL):
            cur = _TrustCur(calib=0.05); _earn(cur, "observe:draft")
            cur.hr = 100
            self.assertIn("per-hour cap", S.earned_ok(cur, "observe:draft")[1])
            cur.hr = 0; cur.class_used = 3
            self.assertIn("per-class daily limit", S.earned_ok(cur, "observe:draft")[1])
            cur.class_used = 0; cur.calib = 0.9
            self.assertIn("calibration too weak", S.earned_ok(cur, "observe:draft")[1])
            cur.calib = 0.05; cur.kill = "true"
            self.assertEqual(S.earned_ok(cur, "observe:draft"), (False, "kill switch engaged"))

    def test_engage_kill_writes_file_and_db_flag(self):
        kill = TMP / "engage"
        cur = _TrustCur()
        try:
            with mock.patch.object(S, "KILL_FILE", str(kill)):
                S.engage_kill(cur, note="test")
                self.assertIn("engaged", kill.read_text())
                self.assertTrue(any("INSERT INTO service_config" in q for q in cur.sql))
                self.assertTrue(S.kill_switch_engaged(cur))
        finally:
            kill.unlink()


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_autonomy_safety"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_is_guarded_by_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            spec = importlib.util.spec_from_file_location("safety_frame_probe", SCRIPT)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.earned_ok))


if __name__ == "__main__":
    unittest.main()
