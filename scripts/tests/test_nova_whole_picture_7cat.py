"""nova_whole_picture — all seven test categories. Live-database tests skip when the database is unreachable."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_config as NC  # noqa: E402
import nova_whole_picture as W  # noqa: E402


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_digest_reads_no_private_sources():
    src = (SCRIPTS / "nova_whole_picture.py").read_text()
    for private in ("email_archive", "imessage", "claude_memories", "memories"):
        assert private not in src.split("def gather")[1], private
    assert "pg-primary.digitalnoise.net" not in src


def test_security_digest_does_not_write_to_database():
    src = (SCRIPTS / "nova_whole_picture.py").read_text()
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert verb not in src


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_digest_lines_handle_many_conflicts_quickly():
    conflicts = [(f"a{i}", f"b{i}", "why") for i in range(5000)]
    t = time.perf_counter()
    lines = W.digest_lines(conflicts, "all rooms reporting", (100000, 5), 0)
    # header line + 5000 conflict lines + HomeKit line + actions line
    assert len(lines) == 5000 + 3 and time.perf_counter() - t < 1.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_connection_uses_shared_helper():
    assert "NC.pg_connect()" in (SCRIPTS / "nova_whole_picture.py").read_text()


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_digest_says_none_when_clear():
    text = "\n".join(W.digest_lines([], "all rooms reporting", (0, 0), 0))
    assert "Rule conflicts to review: none" in text and "Actions in window: none" in text


def test_unit_digest_names_failed_checks():
    assert "Checks that failed (not counted as conflicts): 3" in \
        "\n".join(W.digest_lines([], "x", (0, 0), 3))


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_gather_feeds_digest_from_live_database():
    try:
        conn = NC.pg_connect(attempts=1)
    except Exception:  # noqa: BLE001
        pytest.skip("ops database unreachable")
    try:
        g = W.gather(conn, 24)
    finally:
        conn.close()
    lines = W.digest_lines(g["conflicts"], g["reachability"], g["actions"], g["review_errors"])
    assert lines and lines[0].startswith("Rule conflicts")


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_dry_run_prints_digest():
    try:
        NC.pg_connect(attempts=1).close()
    except Exception:  # noqa: BLE001
        pytest.skip("ops database unreachable")
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_whole_picture.py"), "--dry-run"],
                         capture_output=True, text=True, timeout=60, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
    assert "HomeKit:" in out.stdout


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_help_exits_zero():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_whole_picture.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
