"""nova_dsn — all seven test categories. No real connection is opened; psycopg2 is faked."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_dsn as D  # noqa: E402


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_host_is_a_dns_name_not_an_address():
    assert not any(ch.isdigit() for ch in D.DEFAULT_HOST.split(".")[0])
    assert D.DEFAULT_HOST == "pg-primary.digitalnoise.net"


def test_security_no_password_in_any_dsn():
    for s in (D.pg_dsn(), D.pg_url(), D.pg_host_dbname()):
        assert "password" not in s.lower()


def test_security_host_override_only_from_environment():
    with mock.patch.dict(os.environ, {"NOVA_PG_HOST": "pg-replica.digitalnoise.net"}):
        assert "host=pg-replica.digitalnoise.net" in D.pg_dsn()


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_dsn_builds_are_cheap():
    t = time.perf_counter()
    for _ in range(20000):
        D.pg_dsn("nova_ops", "connect_timeout=5")
    assert time.perf_counter() - t < 2.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_connect_retries_operational_errors_then_succeeds():
    import psycopg2
    calls = []

    def flaky(dsn):
        calls.append(dsn)
        if len(calls) < 3:
            raise psycopg2.OperationalError("dropped")
        return "conn"

    with mock.patch("psycopg2.connect", side_effect=flaky):
        assert D.pg_connect("nova_ops", attempts=3, _sleep=lambda s: None) == "conn"
    assert len(calls) == 3


def test_retry_connect_gives_up_after_attempts():
    import psycopg2
    with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")):
        try:
            D.pg_connect("nova_ops", attempts=2, _sleep=lambda s: None)
        except psycopg2.OperationalError:
            return
    raise AssertionError("expected the error to be raised after retries")


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_dsn_format_and_extra_options():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOVA_PG_HOST", None)
        assert D.pg_dsn("nova_memories") == "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
        assert D.pg_dsn("nova_ops", "connect_timeout=5").endswith(" connect_timeout=5")


def test_unit_url_and_host_only_forms():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOVA_PG_HOST", None)
        assert D.pg_url("nova_ops") == "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"
        assert D.pg_host_dbname("nova_ops") == "host=pg-primary.digitalnoise.net dbname=nova_ops"


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_connect_passes_built_dsn_to_driver():
    with mock.patch("psycopg2.connect", return_value="conn") as c:
        D.pg_connect("nova_memories")
    assert c.call_args[0][0] == D.pg_dsn("nova_memories")


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_module_prints_nothing_on_import():
    out = subprocess.run([sys.executable, "-c", "import nova_dsn"], capture_output=True, text=True,
                         timeout=30, cwd=SCRIPTS)
    assert out.returncode == 0 and out.stdout == "" and out.stderr == ""


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_module_compiles():
    out = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_dsn.py")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
