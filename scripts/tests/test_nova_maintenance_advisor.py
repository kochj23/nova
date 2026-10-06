#!/usr/bin/env python3
"""
test_nova_maintenance_advisor.py — Tests for nova_maintenance_advisor.py.

Focus (per task):
  - The manual %s-escape into `psql -c` is an injection surface. Assert the
    escape helper neutralizes single quotes (SQL-injection can't break out of
    the string literal).
  - Unit tests for the trend/threshold check functions.
  - Assert queue writes are well-formed (priority, status, valid JSON context,
    dedup behavior).

All external deps (subprocess/psql/redis-cli/statvfs) are mocked — no live
service or DB is ever touched.

Run: NOVA_TEST_QUIET=1 python3 -m pytest tests/test_nova_maintenance_advisor.py -q
Written by Jordan Koch.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import nova_maintenance_advisor as mod


@pytest.fixture(autouse=True)
def _quiet_logger():
    """nova_logger.log appends to ~/.openclaw/logs/nova.jsonl — keep every test off the real log."""
    with patch.object(mod, "log"):
        yield


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_completed(returncode=0, stdout=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = ""
    return r


def _query_from_subprocess_call(call):
    """The built SQL string is always the last element of the psql cmd list."""
    cmd = call.args[0] if call.args else call.kwargs.get("args")
    return cmd[-1]


# ── SQL escape / injection invariant ─────────────────────────────────────────


class TestSqlEscapeInjection:
    def test_single_quote_is_doubled(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT * FROM t WHERE name = %s", ("O'Brien",))
        query = _query_from_subprocess_call(run.call_args)
        assert "'O''Brien'" in query
        # No lone/unbalanced quote can break out of the literal.
        assert query.count("'") % 2 == 0

    def test_classic_injection_payload_cannot_break_out(self):
        payload = "Robert'); DROP TABLE students;--"
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT 1 FROM t WHERE x = %s", (payload,))
        query = _query_from_subprocess_call(run.call_args)
        # The apostrophe in the payload must be doubled, keeping the whole
        # payload trapped inside one string literal.
        assert "Robert''); DROP TABLE students;--" in query
        # Quotes remain balanced -> nothing escaped the literal.
        assert query.count("'") % 2 == 0
        # The raw single-quote-then-paren break sequence must NOT appear.
        assert "Robert');" not in query

    def test_execute_path_escapes_too(self):
        payload = "x'; DELETE FROM claude_queue; --"
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_execute("INSERT INTO t VALUES (%s)", (payload,))
        query = _query_from_subprocess_call(run.call_args)
        assert "x''; DELETE FROM claude_queue; --" in query
        assert query.count("'") % 2 == 0

    def test_multiple_quotes_all_doubled(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT %s", ("a'b'c'",))
        query = _query_from_subprocess_call(run.call_args)
        assert "'a''b''c'''" in query
        assert query.count("'") % 2 == 0

    def test_none_becomes_null_unquoted(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT %s", (None,))
        query = _query_from_subprocess_call(run.call_args)
        assert "NULL" in query
        assert "'NULL'" not in query

    def test_int_param_not_quoted(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT %s", (42,))
        query = _query_from_subprocess_call(run.call_args)
        assert "SELECT 42" in query
        assert "'42'" not in query

    def test_multiple_params_filled_in_order(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT %s, %s, %s", ("a", 7, None))
        query = _query_from_subprocess_call(run.call_args)
        assert query == "SELECT 'a', 7, NULL"


# ── _pg_query parsing / failure handling ─────────────────────────────────────


class TestPgQueryParsing:
    def test_splits_rows_on_field_separator(self):
        out = "1\x1f2\x1f3\n4\x1f5\x1f6\n"
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, out)):
            rows = mod._pg_query("SELECT a,b,c FROM t")
        assert rows == [["1", "2", "3"], ["4", "5", "6"]]

    def test_nonzero_returncode_yields_empty(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(1, "boom")):
            assert mod._pg_query("SELECT 1") == []

    def test_exception_yields_empty(self):
        with patch.object(mod.subprocess, "run", side_effect=OSError("nope")):
            assert mod._pg_query("SELECT 1") == []

    def test_execute_returns_bool(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(0, "")):
            assert mod._pg_execute("INSERT INTO t VALUES (1)") is True
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(2, "")):
            assert mod._pg_execute("INSERT INTO t VALUES (1)") is False


# ── check_failure_trends ─────────────────────────────────────────────────────


class TestFailureTrends:
    def test_rate_increase_triggers_suggestion(self):
        side = [
            [["200", "40"]],   # this week: 20% failure
            [["200", "10"]],   # last week: 5% failure  -> +15% > 10% threshold
            [],                # failing_tasks: none
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            out = mod.check_failure_trends()
        assert len(out) == 1
        s = out[0]
        assert s["context"]["metric"] == "scheduler_failure_rate"
        assert s["context"]["increase"] == pytest.approx(0.15, abs=1e-6)
        assert "failure rate increased" in s["description"]

    def test_small_increase_no_suggestion(self):
        side = [
            [["200", "22"]],   # 11%
            [["200", "20"]],   # 10%  -> +1% under threshold
            [],
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            out = mod.check_failure_trends()
        assert out == []

    def test_insufficient_this_week_data(self):
        # this_week_total < 50 -> bail immediately, only one query issued
        q = MagicMock(return_value=[["30", "10"]])
        with patch.object(mod, "_pg_query", q):
            out = mod.check_failure_trends()
        assert out == []
        assert q.call_count == 1

    def test_insufficient_last_week_data(self):
        side = [
            [["200", "40"]],
            [["10", "1"]],     # last week total < 50 -> bail
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            out = mod.check_failure_trends()
        assert out == []

    def test_top_failing_tasks_reported(self):
        side = [
            [["100", "5"]],    # this week 5% (below rate threshold)
            [["100", "5"]],    # last week 5% -> no rate suggestion
            [["taskA", "9"], ["taskB", "6"], ["taskC", "2"]],  # top failing
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            out = mod.check_failure_trends()
        assert len(out) == 1
        assert out[0]["context"]["metric"] == "top_failing_tasks"
        assert "taskA(9x)" in out[0]["description"]

    def test_no_data_returns_empty(self):
        with patch.object(mod, "_pg_query", return_value=[]):
            assert mod.check_failure_trends() == []


# ── check_latency_trends ─────────────────────────────────────────────────────


class TestLatencyTrends:
    def test_latency_regression_triggers(self):
        side = [
            [["mlx", "1500"]],   # this week p95
            [["mlx", "1000"]],   # last week p95 -> +50% > 40%
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            out = mod.check_latency_trends()
        assert len(out) == 1
        assert out[0]["context"]["backend"] == "mlx"
        assert out[0]["context"]["increase_percent"] == pytest.approx(50.0, abs=0.1)

    def test_latency_stable_no_suggestion(self):
        side = [
            [["mlx", "1100"]],   # +10%, under 40% threshold
            [["mlx", "1000"]],
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            assert mod.check_latency_trends() == []

    def test_new_backend_with_no_baseline_ignored(self):
        side = [
            [["ollama", "9999"]],  # no prior-week entry for ollama
            [["mlx", "1000"]],
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            assert mod.check_latency_trends() == []

    def test_unparseable_p95_skipped(self):
        side = [
            [["mlx", "notanumber"]],
            [["mlx", "1000"]],
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            assert mod.check_latency_trends() == []


# ── check_disk_space ─────────────────────────────────────────────────────────


def _statvfs(free_gb, total_gb):
    frsize = 4096
    blocks = int(total_gb * (1024 ** 3) / frsize)
    bavail = int(free_gb * (1024 ** 3) / frsize)
    s = MagicMock()
    s.f_frsize = frsize
    s.f_bavail = bavail
    s.f_blocks = blocks
    return s


class TestDiskSpace:
    def test_low_disk_triggers(self):
        with patch.object(mod.os, "statvfs", return_value=_statvfs(3.0, 500.0)):
            out = mod.check_disk_space()
        # All three volumes report low -> one suggestion each.
        assert len(out) == 3
        assert all(s["context"]["metric"] == "disk_space" for s in out)
        assert all(s["context"]["free_gb"] < mod.DISK_WARN_GB for s in out)

    def test_ample_disk_no_suggestion(self):
        with patch.object(mod.os, "statvfs", return_value=_statvfs(400.0, 500.0)):
            assert mod.check_disk_space() == []

    def test_statvfs_error_swallowed(self):
        with patch.object(mod.os, "statvfs", side_effect=OSError("gone")):
            assert mod.check_disk_space() == []


# ── check_vector_growth ──────────────────────────────────────────────────────


class TestVectorGrowth:
    def test_growth_over_threshold_triggers(self):
        side = [
            [["130000"]],   # current
            [["100000"]],   # a week ago -> +30% > 20%
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            out = mod.check_vector_growth()
        assert len(out) == 1
        assert out[0]["context"]["metric"] == "vector_growth"
        assert out[0]["context"]["growth_percent"] == pytest.approx(30.0, abs=0.1)
        assert out[0]["context"]["new_this_week"] == 30000

    def test_modest_growth_no_suggestion(self):
        side = [
            [["105000"]],   # +5%
            [["100000"]],
        ]
        with patch.object(mod, "_pg_query", side_effect=side):
            assert mod.check_vector_growth() == []

    def test_missing_current_count(self):
        with patch.object(mod, "_pg_query", return_value=[]):
            assert mod.check_vector_growth() == []


# ── check_silent_failures ────────────────────────────────────────────────────


class TestSilentFailures:
    def test_silent_failures_reported(self):
        rows = [
            ["ingest_news", "nova_daily_news.py", "failure", "traceback...", "2", "1700"],
            ["media_scan", "nova_media.py", "failure", "boom", "1", "1700"],
        ]
        with patch.object(mod, "_pg_query", return_value=rows):
            out = mod.check_silent_failures()
        assert len(out) == 1
        ctx = out[0]["context"]
        assert ctx["metric"] == "silent_failures"
        assert len(ctx["tasks"]) == 2
        assert "ingest_news" in out[0]["description"]
        assert ctx["tasks"][0]["consecutive_failures"] == 2

    def test_no_silent_failures(self):
        with patch.object(mod, "_pg_query", return_value=[]):
            assert mod.check_silent_failures() == []

    def test_error_tail_truncated(self):
        long_tail = "E" * 500
        rows = [["t", "s.py", "failure", long_tail, "1", "1700"]]
        with patch.object(mod, "_pg_query", return_value=rows):
            out = mod.check_silent_failures()
        assert len(out[0]["context"]["tasks"][0]["error_tail"]) == 200


# ── check_redis_health ───────────────────────────────────────────────────────


class TestRedisHealth:
    def test_high_memory_triggers(self):
        # first call: INFO memory (600MB), second: DBSIZE (small)
        big = 600 * 1024 * 1024
        info = _fake_completed(0, f"# Memory\nused_memory:{big}\nused_memory_human:600M\n")
        dbsize = _fake_completed(0, "(integer) 42")
        with patch.object(mod.subprocess, "run", side_effect=[info, dbsize]):
            out = mod.check_redis_health()
        mem = [s for s in out if s["context"]["metric"] == "redis_memory"]
        assert len(mem) == 1
        assert mem[0]["context"]["used_mb"] == pytest.approx(600.0, abs=0.5)

    def test_normal_memory_and_keys_no_suggestion(self):
        small = 100 * 1024 * 1024
        info = _fake_completed(0, f"used_memory:{small}\n")
        dbsize = _fake_completed(0, "(integer) 200")
        with patch.object(mod.subprocess, "run", side_effect=[info, dbsize]):
            assert mod.check_redis_health() == []

    def test_high_key_count_triggers(self):
        small = 10 * 1024 * 1024
        info = _fake_completed(0, f"used_memory:{small}\n")
        dbsize = _fake_completed(0, "db0:keys=25000,expires=100,avg_ttl=0")
        with patch.object(mod.subprocess, "run", side_effect=[info, dbsize]):
            out = mod.check_redis_health()
        keys = [s for s in out if s["context"]["metric"] == "redis_keys"]
        assert len(keys) == 1
        assert keys[0]["context"]["key_count"] == 25000

    def test_redis_unavailable_returns_empty(self):
        with patch.object(mod.subprocess, "run",
                          return_value=_fake_completed(1, "")):
            assert mod.check_redis_health() == []


# ── _queue_suggestion (well-formed writes + dedup) ───────────────────────────


class TestQueueSuggestion:
    def test_write_is_well_formed(self):
        suggestion = {
            "description": "MAINTENANCE: disk low",
            "context": {"metric": "disk_space", "free_gb": 3.2},
        }
        with patch.object(mod, "_pg_query", return_value=[]) as q, \
             patch.object(mod, "_pg_execute", return_value=True) as ex:
            ok = mod._queue_suggestion(suggestion)
        assert ok is True

        # Find the INSERT INTO claude_queue call and inspect its params.
        insert_calls = [c for c in ex.call_args_list
                        if "INSERT INTO claude_queue" in c.args[0]]
        assert len(insert_calls) == 1
        sql, params = insert_calls[0].args[0], insert_calls[0].args[1]
        session_id, status, priority, description, context_json = params
        assert session_id == mod.BRIDGE_SESSION_ID
        assert status == "queued"
        assert priority == str(mod.MAINTENANCE_PRIORITY)
        assert description == "MAINTENANCE: disk low"
        # context must be valid JSON round-trippable.
        parsed = json.loads(context_json)
        assert parsed["metric"] == "disk_space"

    def test_dedup_skips_when_pending_exists(self):
        suggestion = {"description": "dup", "context": {}}
        with patch.object(mod, "_pg_query", return_value=[["1"]]) as q, \
             patch.object(mod, "_pg_execute", return_value=True) as ex:
            ok = mod._queue_suggestion(suggestion)
        assert ok is False
        # No INSERT INTO claude_queue when a duplicate is pending.
        assert not any("INSERT INTO claude_queue" in c.args[0]
                       for c in ex.call_args_list)

    def test_dedup_check_uses_parameterized_description(self):
        suggestion = {"description": "hello", "context": {}}
        with patch.object(mod, "_pg_query", return_value=[]) as q, \
             patch.object(mod, "_pg_execute", return_value=True):
            mod._queue_suggestion(suggestion)
        dedup_calls = [c for c in q.call_args_list
                       if "FROM claude_queue" in c.args[0]]
        assert len(dedup_calls) == 1
        # Description passed as a param tuple, not interpolated into SQL text.
        assert dedup_calls[0].args[1] == ("hello",)
        assert "hello" not in dedup_calls[0].args[0]

    def test_non_ascii_and_quotes_in_context_serialize(self):
        suggestion = {
            "description": "MAINTENANCE: task O'Brien failing 世界",
            "context": {"metric": "silent_failures", "note": "quote' inside"},
        }
        with patch.object(mod, "_pg_query", return_value=[]), \
             patch.object(mod, "_pg_execute", return_value=True) as ex:
            mod._queue_suggestion(suggestion)
        insert = [c for c in ex.call_args_list
                  if "INSERT INTO claude_queue" in c.args[0]][0]
        context_json = insert.args[1][4]
        assert json.loads(context_json)["note"] == "quote' inside"


# ── run_analysis orchestration ───────────────────────────────────────────────


class TestRunAnalysis:
    def test_all_nominal_queues_nothing(self, capsys):
        with patch.object(mod, "check_failure_trends", return_value=[]), \
             patch.object(mod, "check_latency_trends", return_value=[]), \
             patch.object(mod, "check_disk_space", return_value=[]), \
             patch.object(mod, "check_vector_growth", return_value=[]), \
             patch.object(mod, "check_silent_failures", return_value=[]), \
             patch.object(mod, "check_redis_health", return_value=[]), \
             patch.object(mod, "_queue_suggestion", return_value=True) as q:
            out = mod.run_analysis()
        assert out == []
        q.assert_not_called()
        assert "nominal" in capsys.readouterr().out

    def test_concerns_are_queued(self, capsys):
        sug = {"description": "MAINTENANCE: x", "context": {"metric": "m"}}
        with patch.object(mod, "check_failure_trends", return_value=[sug]), \
             patch.object(mod, "check_latency_trends", return_value=[]), \
             patch.object(mod, "check_disk_space", return_value=[]), \
             patch.object(mod, "check_vector_growth", return_value=[]), \
             patch.object(mod, "check_silent_failures", return_value=[]), \
             patch.object(mod, "check_redis_health", return_value=[]), \
             patch.object(mod, "_queue_suggestion", return_value=True) as q:
            out = mod.run_analysis()
        assert len(out) == 1
        q.assert_called_once_with(sug)

    def test_check_exception_does_not_abort_run(self):
        with patch.object(mod, "check_failure_trends",
                          side_effect=RuntimeError("boom")), \
             patch.object(mod, "check_latency_trends", return_value=[]), \
             patch.object(mod, "check_disk_space", return_value=[]), \
             patch.object(mod, "check_vector_growth", return_value=[]), \
             patch.object(mod, "check_silent_failures", return_value=[]), \
             patch.object(mod, "check_redis_health", return_value=[]), \
             patch.object(mod, "_queue_suggestion", return_value=True):
            # Should swallow the exception and complete.
            out = mod.run_analysis()
        assert out == []


# ── house categories added 2026-10-05 (7 unittest classes) ──────────────────
# Tests for nova_maintenance_advisor.py — the 7 house categories (Security, Performance, Retry, Unit,
# Integration, Functional, Frame). Written by Jordan Koch (via Claude).

import io as _io
import os as _os
import re as _re
import subprocess as _sp
import tempfile as _tempfile
import time as _time
import unittest
from contextlib import redirect_stdout as _redirect_stdout

_SRC = (Path(__file__).resolve().parents[1] / "nova_maintenance_advisor.py").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = _re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", _re.I)
        self.assertIsNone(pat.search(_SRC))

    def test_psql_is_argv_and_value_is_escaped(self):
        self.assertNotIn("shell=True", _SRC)
        with patch.object(mod.subprocess, "run", return_value=_fake_completed(0, "")) as run:
            mod._pg_execute("INSERT INTO t VALUES (%s)", ("x'); SELECT pg_sleep(9); --",))
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["psql", "-h", "127.0.0.1"])
        self.assertIn("'x''); SELECT pg_sleep(9); --'", argv[-1])


class TestPerformance(unittest.TestCase):
    def test_pg_query_parses_10k_rows_fast(self):
        out = "\n".join(f"task{i}\x1f{i}" for i in range(10_000))
        with patch.object(mod.subprocess, "run", return_value=_fake_completed(0, out)):
            t0 = _time.perf_counter()
            rows = mod._pg_query("SELECT 1")
        self.assertLess(_time.perf_counter() - t0, 1.0)
        self.assertEqual(len(rows), 10_000)
        self.assertEqual(rows[-1], ["task9999", "9999"])


class TestRetry(unittest.TestCase):
    def test_psql_timeout_fails_open(self):
        # RETRY GAP: _pg_query/_pg_execute — one psql attempt; timeout returns []/False (weekly job reruns)
        with patch.object(mod.subprocess, "run", side_effect=_sp.TimeoutExpired("psql", 15)) as run:
            self.assertEqual(mod._pg_query("SELECT 1"), [])
            self.assertFalse(mod._pg_execute("SELECT 1"))
        self.assertEqual(run.call_count, 2)

    def test_redis_timeout_fails_open(self):
        # RETRY GAP: check_redis_health — single redis-cli call
        with patch.object(mod.subprocess, "run", side_effect=_sp.TimeoutExpired("redis-cli", 5)):
            self.assertEqual(mod.check_redis_health(), [])


class TestUnit(unittest.TestCase):
    def test_param_substitution_edges(self):
        with patch.object(mod.subprocess, "run", return_value=_fake_completed(0, "")) as run:
            mod._pg_query("SELECT %s, %s, %s", (None, 7, "a"))
        self.assertTrue(run.call_args[0][0][-1].endswith("SELECT NULL, 7, 'a'"))
        with patch.object(mod.subprocess, "run", return_value=_fake_completed(0, "\n  \n")):
            self.assertEqual(mod._pg_query("SELECT 1"), [])

    def test_redis_dbsize_formats(self):
        mem = _fake_completed(0, "used_memory:1024\n")
        for out, expect in (("db0:keys=20000,expires=0", 1), ("(integer) 50", 0)):
            with patch.object(mod.subprocess, "run", side_effect=[mem, _fake_completed(0, out)]):
                self.assertEqual(len(mod.check_redis_health()), expect)


class TestIntegration(unittest.TestCase):
    def test_queue_targets_bridge_session_at_priority_4(self):
        with patch.object(mod, "_pg_execute", return_value=True) as ex, \
             patch.object(mod, "_pg_query", return_value=[]):
            self.assertTrue(mod._queue_suggestion({"description": "MAINTENANCE: d", "context": {"a": 1}}))
        sess, ins = ex.call_args_list
        self.assertIn("INSERT INTO claude_sessions", sess[0][0])
        self.assertEqual(sess[0][1][0], mod.BRIDGE_SESSION_ID)
        self.assertEqual(ins[0][1][:3], (mod.BRIDGE_SESSION_ID, "queued", "4"))
        self.assertEqual(json.loads(ins[0][1][4]), {"a": 1})


class TestFunctional(unittest.TestCase):
    def test_run_analysis_queues_disk_warning_end_to_end(self):
        st = MagicMock(f_bavail=1, f_frsize=1024 ** 3, f_blocks=100)     # 1GB free of 100GB
        written = []
        with patch.object(mod.os, "statvfs", return_value=st), \
             patch.object(mod, "check_failure_trends", return_value=[]), \
             patch.object(mod, "check_latency_trends", return_value=[]), \
             patch.object(mod, "check_vector_growth", return_value=[]), \
             patch.object(mod, "check_silent_failures", return_value=[]), \
             patch.object(mod, "check_redis_health", return_value=[]), \
             patch.object(mod, "_pg_query", return_value=[]), \
             patch.object(mod, "_pg_execute", side_effect=lambda sql, p=(): written.append((sql, p)) or True), \
             _redirect_stdout(_io.StringIO()) as out:
            res = mod.run_analysis()
        self.assertEqual(len(res), 3)
        self.assertEqual(sum("INSERT INTO claude_queue" in s for s, _ in written), 3)
        self.assertIn("3 queued", out.getvalue())

    def test_queue_write_failure_reports_zero_queued(self):
        sug = {"description": "MAINTENANCE: x"}
        with patch.object(mod, "check_failure_trends", return_value=[sug]), \
             patch.object(mod, "check_latency_trends", return_value=[]), \
             patch.object(mod, "check_disk_space", return_value=[]), \
             patch.object(mod, "check_vector_growth", return_value=[]), \
             patch.object(mod, "check_silent_failures", return_value=[]), \
             patch.object(mod, "check_redis_health", return_value=[]), \
             patch.object(mod, "_pg_query", return_value=[]), patch.object(mod, "_pg_execute", return_value=False), \
             _redirect_stdout(_io.StringIO()) as out:
            mod.run_analysis()
        self.assertIn("0 queued", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running the script performs the weekly analysis (psql, redis-cli), so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', _SRC)
        with _tempfile.TemporaryDirectory() as home:
            r = _sp.run([sys.executable, "-c", "import nova_maintenance_advisor"],
                        cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=30,
                        env={**_os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
