#!/usr/bin/env python3
"""7-category tests for nova_face_gate_watch (Little Mister's set): Security, Performance,
Retry, Unit, Integration, Functional, Frame. DB and vision gate are mocked — no live
Postgres, no vision backend, no camera pipeline touched."""
import importlib.util, os, sys, tempfile, time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))
spec = importlib.util.spec_from_file_location("nova_face_gate_watch", os.path.join(SCRIPTS, "nova_face_gate_watch.py"))
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)


class FakeCursor:
    def __init__(self, rows): self._rows = rows; self.updates = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self._last = sql
        if sql.strip().upper().startswith("UPDATE"): self.updates.append(params)
    def fetchall(self): return self._rows


class FakeConn:
    def __init__(self, rows): self.cur = FakeCursor(rows); self.committed = 0
    def cursor(self): return self.cur
    def commit(self): self.committed += 1
    def close(self): pass


@pytest.fixture
def crop_alley():
    """A crop file named like the real ones so _camera_from_crop yields 'alley_north'."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "unknown_alley_north_latest_470_1224.jpg")
    open(p, "wb").write(b"x")
    yield p
    os.unlink(p); os.rmdir(d)


def _run(monkeypatch, rows, gate):
    conn = FakeConn(rows)
    monkeypatch.setattr(w, "_conn", lambda: conn)
    monkeypatch.setattr(w, "_stamp", lambda *a, **k: None)
    return conn, w.check_candidates(gate=gate)


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_non_person_counts_as_leak(monkeypatch, crop_alley):
    rows = [("id1", crop_alley, "2026-09-03T10:00:00+00:00")]
    conn, (checked, leaked, unver, leaks) = _run(monkeypatch, rows, gate=lambda p: False)
    assert checked == 1 and leaked == 1
    assert leaks[0][1] == "alley_north"  # camera derived from crop filename

def test_unit_person_not_leaked(monkeypatch, crop_alley):
    rows = [("id2", crop_alley, "2026-09-03T10:00:00+00:00")]
    conn, (checked, leaked, unver, leaks) = _run(monkeypatch, rows, gate=lambda p: True)
    assert checked == 1 and leaked == 0

def test_unit_camera_from_crop_parsing():
    assert w._camera_from_crop("/x/unknown_alley_north_latest_1_2.jpg") == "alley_north"
    assert w._camera_from_crop("/x/known_front_door_latest_5_6.jpg") == "front_door"
    assert w._camera_from_crop("garbage.jpg") == "?"


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_leak_gets_db_update(monkeypatch, crop_alley):
    rows = [("idX", crop_alley, "2026-09-03T10:00:00+00:00")]
    conn, _ = _run(monkeypatch, rows, gate=lambda p: False)
    assert len(conn.cur.updates) == 1 and "non-person" in conn.cur.updates[0][0]

def test_functional_mixed_batch(monkeypatch, crop_alley):
    rows = [("a", crop_alley, "2026-09-03T10:00:00+00:00"),
            ("b", crop_alley, "2026-09-03T10:01:00+00:00"),
            ("c", crop_alley, "2026-09-03T10:02:00+00:00")]
    seq = iter([True, False, False])  # first person, other two not
    conn, (checked, leaked, unver, leaks) = _run(monkeypatch, rows, gate=lambda p: next(seq))
    assert checked == 3 and leaked == 2


# ── Security (never auto-clear a real person) ────────────────────────────────
def test_security_real_person_never_cleared(monkeypatch, crop_alley):
    rows = [("real", crop_alley, "2026-09-03T10:00:00+00:00")]
    conn, _ = _run(monkeypatch, rows, gate=lambda p: True)
    assert conn.cur.updates == []  # no UPDATE issued for a person


# ── Retry / resilience ────────────────────────────────────────────────────────
def test_retry_gate_exception_is_unverifiable_not_crash(monkeypatch, crop_alley):
    rows = [("boom", crop_alley, "2026-09-03T10:00:00+00:00")]
    def boom(p): raise RuntimeError("vision down")
    conn, (checked, leaked, unver, leaks) = _run(monkeypatch, rows, gate=boom)
    assert unver == 1 and leaked == 0 and checked == 0

def test_retry_notify_failure_swallowed(monkeypatch):
    monkeypatch.setitem(sys.modules, "nova_notify", None)
    w._notify("x", level="warning")  # must not raise


# ── Performance ──────────────────────────────────────────────────────────────
def test_performance_many_candidates(monkeypatch, crop_alley):
    rows = [(f"id{i}", crop_alley, "2026-09-03T10:00:00+00:00") for i in range(500)]
    start = time.perf_counter()
    _run(monkeypatch, rows, gate=lambda p: False)
    assert time.perf_counter() - start < 2.0


# ── Integration ──────────────────────────────────────────────────────────────
def test_integration_thresholds_sane():
    assert w.LOOKBACK_HOURS >= 1 and w.LEAK_ALERT_THRESHOLD >= 1

def test_integration_module_has_expected_api():
    assert callable(w.check_candidates) and callable(w.main) and callable(w._camera_from_crop)


# ── Frame (boundary) ──────────────────────────────────────────────────────────
def test_frame_missing_crop_file_is_unverifiable(monkeypatch):
    rows = [("gone", "/nonexistent/unknown_x_latest_1_2.jpg", "2026-09-03T10:00:00+00:00")]
    conn, (checked, leaked, unver, leaks) = _run(monkeypatch, rows, gate=lambda p: False)
    assert unver == 1 and checked == 0 and leaked == 0

def test_frame_empty_candidate_set(monkeypatch):
    conn, (checked, leaked, unver, leaks) = _run(monkeypatch, [], gate=lambda p: False)
    assert (checked, leaked, unver) == (0, 0, 0)


# ── house classes (7 categories, unittest style) ─────────────────────────────
import re
import subprocess
import unittest
from unittest import mock

SRC = open(os.path.join(SCRIPTS, "nova_face_gate_watch.py")).read()


def _crop(tmp):
    p = os.path.join(tmp, "unknown_alley_north_latest_470_1224.jpg")
    open(p, "wb").write(b"x")
    return p


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_update_is_parameterized(self):
        self.assertIn("resolved_as=%s WHERE id=%s", SRC)
        self.assertNotRegex(SRC, r'execute\(f"UPDATE')


class TestPerformance(unittest.TestCase):
    def test_camera_parse_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            w._camera_from_crop(f"/x/unknown_cam{i}_latest_1_2.jpg")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_db_down_on_action_log_fails_open(self):
        # RETRY GAP: main()/_conn for claude_actions — one attempt, failure swallowed
        with mock.patch.object(w, "check_candidates", return_value=(0, 0, 0, [])), \
             mock.patch.object(w, "_conn", side_effect=RuntimeError("pg down")) as c:
            self.assertEqual(w.main([]), 0)
        self.assertEqual(c.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_camera_from_crop_edges(self):
        self.assertEqual(w._camera_from_crop(None), "?")
        self.assertEqual(w._camera_from_crop(""), "?")
        self.assertEqual(w._camera_from_crop("unknown_a_b_latest_1.jpg"), "a_b")


class TestIntegration(unittest.TestCase):
    def test_dry_run_issues_no_update_and_no_stamp(self):
        with tempfile.TemporaryDirectory() as td:
            conn = FakeConn([("id1", _crop(td), "t")])
            with mock.patch.object(w, "_conn", return_value=conn), \
                 mock.patch.object(w, "_stamp") as st:
                checked, leaked, _, _ = w.check_candidates(gate=lambda p: False, dry_run=True)
        self.assertEqual((checked, leaked), (1, 1))
        self.assertEqual(conn.cur.updates, [])
        st.assert_not_called()

    def test_stamp_writes_telemetry(self):
        conn = FakeConn([])
        w._stamp(conn, 3, 1, 0)
        self.assertIn("telemetry.face_gate_runs", conn.cur._last)
        self.assertEqual(conn.committed, 1)


class TestFunctional(unittest.TestCase):
    def test_main_notifies_on_leak(self):
        with mock.patch.object(w, "check_candidates", return_value=(2, 1, 0, [("a", "alley_north", "t")])), \
             mock.patch.object(w, "_conn", return_value=FakeConn([])), \
             mock.patch.object(w, "_notify") as n:
            self.assertEqual(w.main([]), 0)
        n.assert_called_once()
        self.assertIn("alley_north", n.call_args[0][0])
        self.assertEqual(n.call_args[1]["level"], "warning")

    def test_main_dry_run_never_notifies(self):
        with mock.patch.object(w, "check_candidates", return_value=(2, 1, 0, [("a", "x", "t")])), \
             mock.patch.object(w, "_conn", return_value=FakeConn([])), \
             mock.patch.object(w, "_notify") as n:
            w.main(["--dry-run"])
        n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_face_gate_watch"], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("checked=", r.stdout)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
