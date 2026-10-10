"""nova_directive_conflicts — all seven test categories. Live-database tests skip when the database is unreachable."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_config as NC  # noqa: E402
import nova_directive_conflicts as D  # noqa: E402


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_dsn_from_config_and_no_writes():
    src = (SCRIPTS / "nova_directive_conflicts.py").read_text()
    assert "pg-primary.digitalnoise.net" not in src
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert verb not in src


def test_security_rule_text_is_data_not_code():
    rules = [("a", "Always ask; $(echo hi) and quote ' and semicolon ;"), ("b", "Never ask; just execute")]
    assert isinstance(D.candidates(rules), list)


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_candidates_on_many_rules_is_bounded():
    rules = [(f"r{i}", f"Always ask about topic {i % 40} before acting on item {i}") for i in range(200)] + \
            [(f"s{i}", f"Never ask about topic {i % 40} just execute item {i}") for i in range(200)]
    t = time.perf_counter()
    D.candidates(rules)
    assert time.perf_counter() - t < 5.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_loader_uses_shared_retrying_connect():
    # The loader is read-only and runs once per invocation, so the shared helper's retry is the retry path.
    assert "_nova_dsn.pg_connect(" in (SCRIPTS / "nova_directive_conflicts.py").read_text()


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_families_and_threshold():
    assert D.MIN_SHARED == 2
    fams = {f[0] for f in D.FAMILIES}
    assert {"ask-before-acting", "publish", "alerts", "logging"} <= fams
    assert D.candidates([("a", "Always log the event"), ("b", "Never log anything")]) == []


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_loader_feeds_candidates_from_live_rules():
    try:
        conn = NC.pg_connect(attempts=1)
    except Exception:  # noqa: BLE001
        pytest.skip("ops database unreachable")
    try:
        rules = D.load_rules(conn)
    finally:
        conn.close()
    assert isinstance(D.candidates(rules), list)


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_dry_run_exits_zero():
    try:
        NC.pg_connect(attempts=1).close()
    except Exception:  # noqa: BLE001
        pytest.skip("ops database unreachable")
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_directive_conflicts.py"), "--dry-run"],
                         capture_output=True, text=True, timeout=60, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_help_exits_zero():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_directive_conflicts.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
