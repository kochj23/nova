"""7-category tests for nova_self_repair_digest.py (the Christine rule).

Security / Performance / Retry / Unit / Integration / Functional / Frame.
No real DB or Slack: psycopg2.connect and nova_config are mocked; BB logs are temp files.

Written by Jordan Koch.
"""
import json
import subprocess
import sys
import time
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_self_repair_digest as D  # noqa: E402

NOW = datetime.now(timezone.utc)


def bb(msg, ts=None, source="big-brother"):
    return json.dumps({"ts": (ts or NOW).isoformat(), "source": source, "msg": msg})


class FakeCursor:
    def __init__(self, data=None, posted_today=False, broken=()):
        self.data = data or {}
        self.posted_today = posted_today
        self.broken = set(broken)
        self.executed, self._res = [], []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        self._res = []
        for key, table in (("remediation", "telemetry.remediations"), ("selfcheck", "selfcheck_runs"),
                           ("escalation", "claude_actions"), ("autonomy", "autonomy_ledger"),
                           ("guard", "restraint_ledger")):
            if f"FROM {table}" in sql:
                if key in self.broken:
                    raise RuntimeError(f'relation "{table}" does not exist')
                self._res = [(x,) for x in self.data.get(key, [])]
                return
        if "FROM self_repair_digest_log" in sql and self.posted_today:
            self._res = [(1,)]

    def fetchall(self):
        return self._res

    def fetchone(self):
        return self._res[0] if self._res else None

    def log_writes(self):
        return [a for s, a in self.executed if "INSERT INTO self_repair_digest_log" in s]


def run_main(cur, argv, post=None, bb_lines=None, tmp_path=None):
    conn = mock.MagicMock()
    conn.cursor.return_value = cur
    nc = types.SimpleNamespace(post_both=post or mock.MagicMock(), SLACK_CHAN="#nova-chat")
    files = []
    if bb_lines is not None:
        p = tmp_path / "nova.jsonl"
        p.write_text("\n".join(bb_lines))
        files = [p]
    real = D.bb_heals
    with mock.patch("psycopg2.connect", return_value=conn) as c, \
            mock.patch.dict(sys.modules, {"nova_config": nc}), \
            mock.patch.object(D, "bb_heals", lambda since, files=None, _f=files: real(since, files=_f)), \
            mock.patch.object(D.time, "sleep"):
        rc = D.main(argv)
    return rc, c, nc


# ── Security ────────────────────────────────────────────────────────────────
class TestSecurity:
    def test_dsn_has_no_credentials(self):
        assert "password" not in D.OPS_DSN.lower()

    def test_hours_must_be_int(self):
        with pytest.raises(SystemExit):
            D.main(["--hours", "24'; DROP TABLE x;--"])

    def test_interval_is_bound_parameter(self):
        cur = FakeCursor()
        D.pg_repairs(cur, 24)
        assert len(cur.executed) == 5
        for sql, args in cur.executed:
            assert args == ("24 hours",) and "24" not in sql

    def test_malformed_and_foreign_log_lines_ignored(self, tmp_path):
        p = tmp_path / "n.jsonl"
        p.write_text("\n".join([
            "not json big-brother → Restarted",
            bb("svc down → Restarted x", source="impostor") + "",
            bb('Log error detected {"ts": 1} → Restarted'),
            bb('[w] x {"ts" quoted → Fixed'),
            json.dumps({"source": "big-brother", "msg": "no ts → Restarted"}),
        ]))
        assert D.bb_heals(NOW - timedelta(hours=1), files=[p]) == Counter()

    def test_message_size_bounded(self):
        bbc = Counter({f"issue {chr(65 + i)} → Restarted": 1 for i in range(50)})
        pg = {"guard": ["g" * 500] * 40, "autonomy": [f"a{i}" for i in range(40)]}
        body = D.compose(bbc, pg)
        assert "(+44 more kinds)" in body and "(+35 more)" in body
        assert body.count("g" * 500) == 3  # guard list capped at 3

    def test_dry_run_never_posts_or_writes(self, tmp_path):
        cur = FakeCursor({"autonomy": ["restart soil — verified"]})
        rc, _, nc = run_main(cur, ["--dry-run"], bb_lines=[], tmp_path=tmp_path)
        assert rc == 0
        nc.post_both.assert_not_called()
        assert not cur.log_writes()


# ── Performance ─────────────────────────────────────────────────────────────
class TestPerformance:
    def test_big_log_parses_fast(self, tmp_path):
        p = tmp_path / "n.jsonl"
        noise = json.dumps({"ts": NOW.isoformat(), "source": "x", "msg": "hello"})
        lines = [noise] * 60000 + [bb(f"[w] svc {i} slow latency={i}ms → Restarted svc") for i in range(20000)]
        p.write_text("\n".join(lines))
        t = time.perf_counter()
        c = D.bb_heals(NOW - timedelta(hours=1), files=[p])
        assert time.perf_counter() - t < 5.0
        assert len(c) == 1 and sum(c.values()) == 20000  # numbers stripped → one kind, no blow-up

    def test_fixed_query_count(self):
        cur = FakeCursor({k: ["x"] * 1000 for k in ("remediation", "selfcheck", "escalation", "autonomy", "guard")})
        D.pg_repairs(cur, 24)
        assert len(cur.executed) == 5  # one per source, no N+1


# ── Retry ───────────────────────────────────────────────────────────────────
class TestRetry:
    def test_retry_backoff(self):
        fn = mock.MagicMock(side_effect=[OSError(), OSError(), 5])
        with mock.patch.object(D.time, "sleep") as sl:
            assert D._retry(fn, "x") == 5
        assert [c.args[0] for c in sl.call_args_list] == [1.0, 2.0]

    def test_retry_raises_after_three(self):
        fn = mock.MagicMock(side_effect=OSError("down"))
        with mock.patch.object(D.time, "sleep"), pytest.raises(OSError):
            D._retry(fn, "x")
        assert fn.call_count == 3

    def test_pg_connect_retried(self, tmp_path):
        conn = mock.MagicMock()
        conn.cursor.return_value = FakeCursor()
        with mock.patch("psycopg2.connect", side_effect=[OSError("refused"), OSError("refused"), conn]) as c, \
                mock.patch.object(D, "bb_heals", lambda since, files=None: Counter()), \
                mock.patch.object(D.time, "sleep"):
            assert D.main([]) == 0
        assert c.call_count == 3

    def test_slack_failure_does_not_mark_day_posted(self, tmp_path):
        cur = FakeCursor({"autonomy": ["restart soil — verified"]})
        post = mock.MagicMock(side_effect=OSError("slack down"))
        with pytest.raises(OSError):
            run_main(cur, [], post=post, bb_lines=[], tmp_path=tmp_path)
        assert post.call_count == 3
        assert not cur.log_writes()  # next run will try again

    def test_slack_transient_failure_recovers(self, tmp_path):
        cur = FakeCursor({"autonomy": ["restart soil — verified"]})
        post = mock.MagicMock(side_effect=[OSError("blip"), True])
        rc, _, _ = run_main(cur, [], post=post, bb_lines=[], tmp_path=tmp_path)
        assert rc == 0 and post.call_count == 2 and len(cur.log_writes()) == 1


# ── Unit ────────────────────────────────────────────────────────────────────
class TestUnit:
    def test_fix_regex(self):
        assert D.BB_FIX_RX.search("x → Restarted foo").group(1) == "Restarted"
        assert D.BB_FIX_RX.search("x → Re-enabled foo").group(1) == "Re-enabled"
        assert not D.BB_FIX_RX.search("x → Check scheduler")

    def test_test_fixture_regex(self):
        assert D.BB_TEST_RX.search("[warning] Second event → Fixed")
        assert not D.BB_TEST_RX.search("[warning] disk full")

    def test_old_entries_excluded(self, tmp_path):
        p = tmp_path / "n.jsonl"
        p.write_text(bb("[w] old → Restarted", ts=NOW - timedelta(days=2)))
        assert D.bb_heals(NOW - timedelta(hours=24), files=[p]) == Counter()

    def test_unreadable_file_skipped(self, tmp_path):
        assert D.bb_heals(NOW, files=[tmp_path / "missing.jsonl"]) == Counter()

    def test_compose_sections(self):
        body = D.compose(Counter(), {"remediation": ["r1", "r1"], "selfcheck": ["s"], "escalation": ["e"]})
        assert "runbook remediations: r1 ×2" in body
        assert "selfcheck fixes: s" in body and "selfcheck escalations to Claude: e" in body


# ── Integration ─────────────────────────────────────────────────────────────
class TestIntegration:
    def test_one_missing_table_does_not_hide_others(self):
        cur = FakeCursor({"autonomy": ["restart soil — verified"], "guard": ["physical: lock"]},
                         broken={"remediation", "selfcheck"})
        res = D.pg_repairs(cur, 24)
        assert res["autonomy"] == ["restart soil — verified"] and res["remediation"] == []
        body = D.compose(Counter(), res)
        assert "self-heal restarts" in body and "guards stopped me 1x" in body

    def test_bb_plus_pg_into_one_message(self, tmp_path):
        p = tmp_path / "n.jsonl"
        p.write_text("\n".join([bb("[w] Subagent coder stale → Restarted"), bb("[w] Subagent coder stale → Restarted")]))
        body = D.compose(D.bb_heals(NOW - timedelta(hours=1), files=[p]),
                         D.pg_repairs(FakeCursor({"autonomy": ["a"]}), 24))
        assert body.count("🔧") == 1 and "Big Brother: 2 heal(s)" in body and "×2" in body


# ── Functional ──────────────────────────────────────────────────────────────
class TestFunctional:
    def test_golden_path_posts_once_and_logs_day(self, tmp_path):
        cur = FakeCursor({"autonomy": ["restart soil — verified"]})
        rc, _, nc = run_main(cur, [], bb_lines=[bb("[w] soil down → Restarted")], tmp_path=tmp_path)
        assert rc == 0
        nc.post_both.assert_called_once()
        assert nc.post_both.call_args.args[0].startswith("🔧 What I fixed myself today")
        (args,) = cur.log_writes()
        assert args[0] == datetime.now().date()

    def test_already_posted_today(self, tmp_path):
        cur = FakeCursor({"autonomy": ["x"]}, posted_today=True)
        rc, _, nc = run_main(cur, [], bb_lines=[], tmp_path=tmp_path)
        assert rc == 0
        nc.post_both.assert_not_called()

    def test_force_overrides_daily_limit(self, tmp_path):
        cur = FakeCursor({"autonomy": ["x"]}, posted_today=True)
        rc, _, nc = run_main(cur, ["--force"], bb_lines=[], tmp_path=tmp_path)
        assert rc == 0
        nc.post_both.assert_called_once()

    def test_quiet_day_no_message(self, tmp_path):
        cur = FakeCursor()
        rc, _, nc = run_main(cur, [], bb_lines=[], tmp_path=tmp_path)
        assert rc == 0
        nc.post_both.assert_not_called()
        assert not cur.log_writes()


# ── Frame ───────────────────────────────────────────────────────────────────
class TestFrame:
    def test_imports(self):
        assert callable(D.main) and callable(D.compose)

    def test_help_runs(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_self_repair_digest.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "--hours" in r.stdout
