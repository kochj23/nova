"""nova_action_why — all seven test categories. Live-database tests skip when the database is unreachable."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_action_why as W  # noqa: E402
import nova_config as NC  # noqa: E402


def _live_conn():
    try:
        return NC.pg_connect(attempts=1)
    except Exception:  # noqa: BLE001
        return None


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_dsn_from_config_and_filters_are_parameterised():
    src = (SCRIPTS / "nova_action_why.py").read_text()
    assert "pg-primary.digitalnoise.net" not in src
    assert "ILIKE %s" in src and "btrim(rationale)" in src
    assert 'f"SELECT' not in src


def test_security_user_text_only_travels_as_a_parameter():
    class Cur:
        def __init__(self):
            self.sql, self.args, self.description = None, None, [("ts",)]

        def execute(self, sql, args=()):
            self.sql, self.args = sql, args

        def fetchall(self):
            return []

    class Conn:
        def __init__(self):
            self.c = Cur()

        def cursor(self):
            return self.c

    conn = Conn()
    hostile = "x'; --"
    W.fetch(conn, 5, hostile, False)
    assert hostile not in conn.c.sql
    assert f"%{hostile}%" in conn.c.args


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_explain_and_summary_scale_linearly():
    rows = [{"ts": "2026-10-09 00:00:00", "description": f"a{i}", "rationale": None if i % 2 else "r"}
            for i in range(20000)]
    t = time.perf_counter()
    lines = [W.explain(r) for r in rows]
    assert W.summarise(rows).startswith("20000 actions") and len(lines) == 20000
    assert time.perf_counter() - t < 2.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_connection_is_retried_by_shared_helper():
    import psycopg2
    calls = []
    real = psycopg2.connect

    def flaky(dsn, **k):
        calls.append(1)
        if len(calls) < 3:
            raise psycopg2.OperationalError("dropped")
        return "conn"

    psycopg2.connect = flaky
    try:
        assert NC.pg_connect(attempts=3, _sleep=lambda s: None) == "conn"
    finally:
        psycopg2.connect = real
    assert len(calls) == 3


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_explain_and_missing_reason_text():
    assert W.NO_RATIONALE in W.explain({"ts": "2026", "description": "x", "rationale": " "})
    assert W.summarise([{"rationale": None}, {"rationale": "y"}]) == "2 actions shown, 1 with no recorded rationale"


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_live_fetch_returns_dicts_with_rationale_key():
    conn = _live_conn()
    if conn is None:
        pytest.skip("ops database unreachable")
    try:
        rows = W.fetch(conn, 3, None, False)
    finally:
        conn.close()
    assert all("rationale" in r for r in rows)


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_cli_prints_summary_line():
    if _live_conn() is None:
        pytest.skip("ops database unreachable")
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_action_why.py"), "--limit", "2"],
                         capture_output=True, text=True, timeout=60, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
    assert "actions shown" in out.stdout


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_help_exits_zero():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_action_why.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
