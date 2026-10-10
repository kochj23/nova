"""nova_home_reachability — all seven test categories. Live-database tests skip when the database is unreachable."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_home_reachability as H  # noqa: E402


def _live_conn():
    import psycopg2
    try:
        return psycopg2.connect(H.DSN, connect_timeout=3)
    except Exception:  # noqa: BLE001 — skip, do not fail, when offline
        return None


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_dsn_comes_from_config_not_source():
    src = (SCRIPTS / "nova_home_reachability.py").read_text()
    assert "pg-primary.digitalnoise.net" not in src, "database host must not be written into the script"
    assert "NC.pg_dsn()" in src


def test_security_no_sql_built_from_strings():
    src = (SCRIPTS / "nova_home_reachability.py").read_text()
    assert 'f"SELECT' not in src and "% room" not in src and "+ room" not in src


def test_security_ping_uses_fixed_argv_not_shell():
    src = (SCRIPTS / "nova_home_reachability.py").read_text()
    assert "shell=True" not in src
    assert '["/sbin/ping", "-c", "1"' in src


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_diagnose_handles_large_snapshot_quickly():
    rows = [(f"room{i % 50}", f"acc{i}", i % 3 != 0) for i in range(20000)]
    t = time.perf_counter()
    H.diagnose(rows)
    assert time.perf_counter() - t < 1.0


def test_performance_ping_summary_does_not_repeat_pings():
    calls = []
    H.ping_summary({"a": "1.1.1.1", "b": "2.2.2.2"}, _ping=lambda ip: calls.append(ip) or True)
    assert calls == ["1.1.1.1", "2.2.2.2"]


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_ping_retries_then_succeeds():
    attempts = []

    def fake_run(*a, **k):
        attempts.append(1)
        return subprocess.CompletedProcess(a, 0 if len(attempts) == 2 else 1)

    real = H.subprocess.run
    H.subprocess.run = fake_run
    try:
        assert H.ping_ok("10.0.0.1", attempts=2, _sleep=lambda s: None) is True
    finally:
        H.subprocess.run = real
    assert len(attempts) == 2


def test_retry_ping_gives_up_after_attempts_and_reports_down():
    real = H.subprocess.run
    H.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no ping"))
    try:
        assert H.ping_ok("10.0.0.1", attempts=2, _sleep=lambda s: None) is False
    finally:
        H.subprocess.run = real


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_diagnose_thresholds():
    assert H.diagnose([("R", "a", True), ("R", "b", False)])["verdict"] == "ok"
    assert H.diagnose([("R", "a", False)])["verdict"] == "controller"
    assert H.diagnose([])["verdict"] == "no-data"


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_rows_feed_diagnose_from_live_snapshot():
    conn = _live_conn()
    if conn is None:
        pytest.skip("ops database unreachable")
    try:
        rows = H.latest_rows(conn)
    finally:
        conn.close()
    res = H.diagnose(rows)
    assert res["verdict"] in {"ok", "controller", "segment", "no-data"}


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_dry_run_prints_a_verdict_line():
    if _live_conn() is None:
        pytest.skip("ops database unreachable")
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_home_reachability.py"), "--dry-run", "--no-ping"],
                         capture_output=True, text=True, timeout=60, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("verdict: ")


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_script_launches_and_imports_cleanly():
    out = subprocess.run([sys.executable, "-c", "import nova_home_reachability"], capture_output=True,
                         text=True, timeout=30, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
