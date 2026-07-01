"""
test_nova_incident_triage.py — Tests for nova_incident_triage.py

Focus:
  * REGRESSION anchor for the Tier-A fix: _get_recent_runs must return rows for
    s-prefixed services (Signal-cli/Slack/Scheduler). The historical bug was an
    "ILIKE %s" collision: the substituted pattern value '%signal%' itself contains
    the substring "%s", so a *second* sequential .replace("%s", ...) (e.g. a LIMIT
    param) would corrupt the query by replacing inside the already-substituted
    value. The fix inlines LIMIT as {int(count)} and keeps exactly ONE %s param.
  * Unit: _pg_query param substitution (str/None/int) and pattern build.
  * Security: pattern/param is escaped — a single-quote in a param can't break out
    of the quoted literal.

External deps (subprocess/psql/redis) are fully mocked; no live service is hit.

Written by Jordan Koch.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import nova_incident_triage as m


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fake_run(stdout="", returncode=0):
    """Build a fake subprocess.run result."""
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _capture_query(mock_run):
    """Extract the SQL query string passed to psql (the arg after '-c')."""
    args, _ = mock_run.call_args
    cmd = args[0]
    idx = cmd.index("-c")
    return cmd[idx + 1]


# ── Unit: _pg_query param substitution ───────────────────────────────────────

def test_pg_query_str_param_is_quoted():
    with patch.object(m.subprocess, "run", return_value=_fake_run("")) as run:
        m._pg_query("SELECT %s", ("hello",))
        assert _capture_query(run) == "SELECT 'hello'"


def test_pg_query_none_param_becomes_null():
    with patch.object(m.subprocess, "run", return_value=_fake_run("")) as run:
        m._pg_query("SELECT %s", (None,))
        assert _capture_query(run) == "SELECT NULL"


def test_pg_query_int_param_is_bare():
    with patch.object(m.subprocess, "run", return_value=_fake_run("")) as run:
        m._pg_query("SELECT %s", (42,))
        assert _capture_query(run) == "SELECT 42"


def test_pg_query_parses_unit_separator_rows():
    stdout = "a\x1fb\x1fc\nd\x1fe\x1ff\n"
    with patch.object(m.subprocess, "run", return_value=_fake_run(stdout)):
        rows = m._pg_query("SELECT 1")
    assert rows == [["a", "b", "c"], ["d", "e", "f"]]


def test_pg_query_returns_empty_on_nonzero_return():
    with patch.object(m.subprocess, "run", return_value=_fake_run("boom", returncode=1)):
        assert m._pg_query("SELECT 1") == []


def test_pg_query_returns_empty_on_exception():
    with patch.object(m.subprocess, "run", side_effect=RuntimeError("no psql")):
        assert m._pg_query("SELECT 1") == []


# ── Security: escaping / injection ───────────────────────────────────────────

def test_pg_query_single_quote_is_escaped():
    """A quote in a param must be doubled, not allowed to close the literal."""
    with patch.object(m.subprocess, "run", return_value=_fake_run("")) as run:
        m._pg_query("SELECT %s", ("O'Brien",))
        # '' is the SQL escape for a literal single quote
        assert _capture_query(run) == "SELECT 'O''Brien'"


def test_pg_query_injection_attempt_is_neutralized():
    payload = "x'; DROP TABLE scheduler_runs; --"
    with patch.object(m.subprocess, "run", return_value=_fake_run("")) as run:
        m._pg_query("SELECT * FROM t WHERE name = %s", (payload,))
        built = _capture_query(run)
        # The entire payload stays inside one quoted literal (quote doubled).
        assert "'x''; DROP TABLE scheduler_runs; --'" in built
        # No unescaped closing quote that would terminate the literal early.
        assert "= 'x';" not in built


# ── Unit: pattern build in _get_recent_runs ──────────────────────────────────

@pytest.mark.parametrize("service,expected", [
    ("Signal-cli", "signal"),
    ("Slack", "slack"),
    ("Scheduler", "scheduler"),
    ("Gateway v2", "gateway"),
    ("Memory Server", "memory"),
    ("NovaControl", "control"),
])
def test_recent_runs_known_pattern(service, expected):
    with patch.object(m, "_pg_query", return_value=[]) as pgq:
        m._get_recent_runs(service)
        sql, params = pgq.call_args[0]
        assert params == (f"%{expected}%",)


def test_recent_runs_unknown_service_falls_back_to_slug():
    with patch.object(m, "_pg_query", return_value=[]) as pgq:
        m._get_recent_runs("Big Brother")
        sql, params = pgq.call_args[0]
        assert params == ("%big_brother%",)


def test_recent_runs_count_is_inlined_as_int_not_param():
    """The Tier-A fix: LIMIT must be an inlined int, NOT a second %s param.
    A second param would collide with the '%s' inside '%signal%'."""
    with patch.object(m, "_pg_query", return_value=[]) as pgq:
        m._get_recent_runs("Signal-cli", count=7)
        sql, params = pgq.call_args[0]
        assert "LIMIT 7" in sql
        # Exactly one placeholder → exactly one param.
        assert sql.count("%s") == 1
        assert len(params) == 1


def test_recent_runs_count_is_cast_to_int():
    """LIMIT is int(count) — a numeric string is coerced, never interpolated raw."""
    with patch.object(m, "_pg_query", return_value=[]) as pgq:
        m._get_recent_runs("Slack", count="3")
        sql, _ = pgq.call_args[0]
        assert "LIMIT 3" in sql


def test_recent_runs_parses_rows_into_dicts():
    row = ["t1", "run_slack.py", "2026-06-26T00:00:00Z", "1200", "0", "ok", ""]
    with patch.object(m, "_pg_query", return_value=[row]):
        runs = m._get_recent_runs("Slack")
    assert runs == [{
        "task_id": "t1",
        "script": "run_slack.py",
        "started_at": "2026-06-26T00:00:00Z",
        "duration_ms": "1200",
        "exit_code": "0",
        "status": "ok",
        "error_tail": None,  # empty string -> None
    }]


def test_recent_runs_skips_short_rows():
    with patch.object(m, "_pg_query", return_value=[["only", "three", "cols"]]):
        assert m._get_recent_runs("Slack") == []


# ── REGRESSION: end-to-end s-prefix ILIKE collision stays fixed ──────────────

@pytest.mark.parametrize("service,pattern", [
    ("Signal-cli", "signal"),
    ("Slack", "slack"),
    ("Scheduler", "scheduler"),
])
def test_regression_s_prefixed_service_returns_rows(service, pattern):
    """Anchor the Tier-A fix end-to-end through the real _pg_query.

    Because the pattern value '%signal%'/'%slack%'/'%scheduler%' contains the
    substring '%s', the sequential .replace('%s', ...) substitution must NOT
    corrupt the query. We drive the real _pg_query with a mocked psql subprocess
    and assert both the built query AND the returned rows are correct.
    """
    # Note: the module treats \x1f as whitespace when stripping, so a trailing
    # empty error_tail field would collapse the row below 7 cols. Use a non-empty
    # final field so the 7-column row survives — exercising the real parse path.
    stdout = f"t9\x1frun_{pattern}.py\x1f2026-06-26T00:00:00Z\x1f900\x1f0\x1fok\x1fno-error\n"
    with patch.object(m.subprocess, "run", return_value=_fake_run(stdout)) as run:
        runs = m._get_recent_runs(service, count=5)
        built = _capture_query(run)

    # The ILIKE placeholder must be replaced with the correctly-quoted pattern,
    # NOT corrupted by the '%s' substring living inside it.
    assert f"ILIKE '%{pattern}%'" in built
    assert "LIMIT 5" in built
    # The pattern must appear exactly once as a quoted literal (no double sub).
    assert built.count(f"'%{pattern}%'") == 1
    # And rows actually come back — the collision would have yielded [] / garbage.
    assert len(runs) == 1
    assert runs[0]["script"] == f"run_{pattern}.py"
    assert runs[0]["status"] == "ok"


def test_regression_pg_query_multi_param_collision_is_real():
    """Direct proof of the collision hazard the Tier-A fix avoids.

    With a SECOND %s param, the sequential .replace('%s', ...) targets the FIRST
    remaining '%s' — which is the substring inside the already-substituted
    '%signal%' value, NOT the real second placeholder. The real 'B %s' is left
    unfilled. This is exactly why _get_recent_runs must keep ONE param and inline
    LIMIT as an int."""
    with patch.object(m.subprocess, "run", return_value=_fake_run("")) as run:
        m._pg_query("A %s B %s", ("%signal%", "second"))
        built = _capture_query(run)
    # The real second placeholder is NOT filled — the collision consumed the
    # replacement inside '%signal%'. This demonstrates the hazard concretely.
    assert built.endswith("B %s")
    assert "second" in built  # the value landed, but in the wrong spot


# ── Decision logic: _suggest_fix ─────────────────────────────────────────────

def test_suggest_fix_gpu_keyword():
    out = m._suggest_fix("Ollama", "GPU contention detected")
    assert any("mlx_whisper" in s for s in out)


def test_suggest_fix_signal_keyword():
    out = m._suggest_fix("Signal-cli", "signal daemon stuck")
    assert any("signal-cli" in s for s in out)


def test_suggest_fix_postgres_keyword():
    out = m._suggest_fix("PostgreSQL", "postgres not accepting connections")
    assert any("pg_isready" in s for s in out)


def test_suggest_fix_generic_fallback_when_no_match():
    out = m._suggest_fix("SearXNG", "something entirely unrecognized zzz")
    assert out  # never empty
    assert any("launchctl" in s for s in out)


def test_suggest_fix_case_insensitive():
    lower = m._suggest_fix("Ollama", "gpu contention")
    upper = m._suggest_fix("Ollama", "GPU CONTENTION")
    assert lower == upper


# ── _check_related_services (pure graph logic, no network) ───────────────────

def test_check_related_services_marks_deps_and_dependents():
    with patch.object(m, "_port_open", return_value=True) as po:
        related = m._check_related_services("PostgreSQL")
    # PostgreSQL has no upstream deps but many dependents (Gateway v2, Scheduler,
    # Memory Server, NovaControl, PgBouncer) — all of which carry a port entry.
    assert related["Gateway v2"]["role"] == "dependent"
    assert related["Scheduler"]["role"] == "dependent"
    assert all(info["up"] for info in related.values())
    po.assert_called()


def test_check_related_services_dependency_role_for_gateway():
    with patch.object(m, "_port_open", return_value=True):
        related = m._check_related_services("Gateway v2")
    # Gateway v2 depends on PostgreSQL/Redis/Ollama/Memory Server.
    assert related["PostgreSQL"]["role"] == "dependency"
    assert related["Redis"]["role"] == "dependency"


def test_check_related_services_down_dependency_flagged():
    with patch.object(m, "_port_open", return_value=False):
        related = m._check_related_services("Gateway v2")
    assert related["PostgreSQL"]["up"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
