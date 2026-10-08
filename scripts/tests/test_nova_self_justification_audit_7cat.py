"""7-category tests for nova_self_justification_audit.py (P4 outside check).

Security / Performance / Retry / Unit / Integration / Functional / Frame.
No real DB, Slack or agent_docs writes: psycopg2.connect and nova_config are mocked.

Written by Jordan Koch.
"""
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_self_justification_audit as J  # noqa: E402

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
LEDGER_COLS = ["id", "ts", "source", "action_class", "target", "action", "executed", "verified",
               "result", "before_state", "after_state", "stated_rationale"]


def row(**kw):
    r = {"id": 1, "ts": T0, "source": "actor", "action_class": "restart:soil", "target": "soil@n",
         "action": "restart soil (health showed DOWN)", "executed": True, "verified": True, "result": "",
         "before_state": {"status": "down"}, "after_state": {"status": "up"},
         "stated_rationale": "health showed DOWN", "ledger": "autonomy_ledger"}
    r.update(kw)
    return r


class FakeCursor:
    """Answers the audit's SQL from in-memory data and records every statement."""

    def __init__(self, ledger=(), log_rows=(), health=None, queue_ids=(), log_missing=False):
        self.ledger, self.log_rows = list(ledger), list(log_rows)
        self.health = health or {}
        self.queue_ids = set(queue_ids)
        self.log_missing = log_missing
        self.executed = []
        self._res, self.description = [], None

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        s = " ".join(sql.split())
        self._res = []
        if "FROM autonomy_ledger" in s:
            self.description = [(c,) for c in LEDGER_COLS]
            self._res = [tuple(r[c] for c in LEDGER_COLS) for r in self.ledger]
        elif "FROM autonomy_log" in s:
            if self.log_missing:
                raise RuntimeError('relation "autonomy_log" does not exist')
            self._res = list(self.log_rows)
        elif "FROM health_checks" in s:
            before = "<= %s" in s
            st = self.health.get(args[0], (None, None))[0 if before else 1]
            self._res = [(st,)] if st else []
        elif "FROM claude_queue" in s:
            self._res = [(1,)] if args[0] in self.queue_ids else []

    def fetchall(self):
        return self._res

    def fetchone(self):
        return self._res[0] if self._res else None

    def inserts(self, table):
        return [a for s, a in self.executed if f"INSERT INTO {table}" in s]


def run_main(cur, argv, post=None):
    conn = mock.MagicMock()
    conn.cursor.return_value = cur
    nc = types.SimpleNamespace(post_both=post or mock.MagicMock(), SLACK_CHAN="#nova-chat")
    nas = types.SimpleNamespace(ensure_schema=mock.MagicMock())
    with mock.patch.object(J.psycopg2, "connect", return_value=conn) as c, \
            mock.patch.dict(sys.modules, {"nova_config": nc, "nova_autonomy_safety": nas}), \
            mock.patch.object(J.time, "sleep"):
        rc = J.main(argv)
    return rc, c, nc


# ── Security ────────────────────────────────────────────────────────────────
class TestSecurity:
    def test_dsn_has_no_credentials(self):
        assert "password" not in J.OPS_DSN.lower()

    def test_days_is_parameterised_not_interpolated(self):
        cur = FakeCursor()
        J.gather(cur, 7)
        for sql, args in cur.executed:
            assert "7" not in sql.replace("autonomy_ledger", "")  # value travels as a bind arg
            assert args == ("7",)

    def test_days_must_be_int(self):
        with pytest.raises(SystemExit):
            J.main(["--days", "7; DROP TABLE autonomy_ledger"])

    def test_stated_field_bounded(self):
        f = J.audit_rows([row(action="x" * 5000, before_state=None, after_state=None, action_class="observe:x")])
        assert all(len(x["stated"]) <= 600 for x in f)

    def test_audit_uses_no_model(self):
        src = (SCRIPTS / "nova_self_justification_audit.py").read_text()
        for bad in ("ollama", "openrouter", "anthropic", "llm", "11434"):
            assert bad not in src.lower()

    def test_dry_run_writes_nothing_and_posts_nothing(self):
        cur = FakeCursor(ledger=[row()], health={"soil": ("up", "down")})
        rc, _, nc = run_main(cur, ["--dry-run"])
        assert rc == 0
        assert not cur.inserts("self_justification_audit") and not cur.inserts("agent_docs")
        nc.post_both.assert_not_called()


# ── Performance ─────────────────────────────────────────────────────────────
class TestPerformance:
    def test_large_window_is_fast(self):
        rows = [row(id=i, ts=T0 + timedelta(seconds=i), target=f"svc{i % 50}@n",
                    action_class=f"restart:svc{i % 50}") for i in range(5000)]
        t = time.perf_counter()
        J.audit_rows(rows, health=lambda s, n, ts: ("down", "up"), queue_exists=lambda q: True)
        assert time.perf_counter() - t < 2.0

    def test_one_health_lookup_per_restart(self):
        calls = []
        rows = [row(id=i) for i in range(10)] + [row(id=99, action_class="observe:x", target="x")]
        J.audit_rows(rows, health=lambda s, n, ts: calls.append(s) or ("down", "up"))
        assert len(calls) == 10

    def test_treadmill_finding_not_per_row(self):
        rows = [row(id=i, ts=T0 + timedelta(minutes=i)) for i in range(200)]
        f = J.audit_rows(rows, health=lambda s, n, ts: ("down", "up"))
        assert sum("restarted" in x["finding"] for x in f) == 1


# ── Retry ───────────────────────────────────────────────────────────────────
class TestRetry:
    def test_retry_recovers_with_backoff(self):
        fn = mock.MagicMock(side_effect=[OSError("a"), OSError("b"), "ok"])
        with mock.patch.object(J.time, "sleep") as sl:
            assert J._retry(fn, "x") == "ok"
        assert fn.call_count == 3
        assert [c.args[0] for c in sl.call_args_list] == [1.0, 2.0]

    def test_retry_raises_after_last_attempt(self):
        fn = mock.MagicMock(side_effect=OSError("down"))
        with mock.patch.object(J.time, "sleep"), pytest.raises(OSError):
            J._retry(fn, "x")
        assert fn.call_count == 3

    def test_pg_connect_retried(self):
        conn = mock.MagicMock()
        conn.cursor.return_value = FakeCursor()
        with mock.patch.object(J.psycopg2, "connect", side_effect=[OSError("refused"), conn]) as c, \
                mock.patch.dict(sys.modules, {"nova_autonomy_safety": types.SimpleNamespace(ensure_schema=lambda oc: None)}), \
                mock.patch.object(J.time, "sleep"):
            assert J.main(["--dry-run"]) == 0
        assert c.call_count == 2

    def test_slack_post_retried_and_never_crashes(self):
        cur = FakeCursor(ledger=[row()], health={"soil": ("up", "down")})
        post = mock.MagicMock(side_effect=OSError("slack down"))
        rc, _, _ = run_main(cur, [], post=post)
        assert rc == 0 and post.call_count == 3
        assert cur.inserts("self_justification_audit")  # findings still recorded


# ── Unit ────────────────────────────────────────────────────────────────────
class TestUnit:
    def test_split_target(self):
        assert J._split_target("soil@n") == ("soil", "n")
        assert J._split_target("soil") == ("soil", None)
        assert J._split_target("") == ("", None)

    def test_no_independent_check_is_low(self):
        f = J.audit_rows([row()], health=lambda s, n, ts: ("down", None))
        assert [x["severity"] for x in f] == ["low"]

    def test_unexecuted_rows_ignored(self):
        assert J.audit_rows([row(executed=False)], health=lambda *a: ("up", "down")) == []

    def test_sent_disagreement(self):
        f = J.audit_rows([row(action_class="observe:x", target="x", after_state={"sent": False})])
        assert any(x["severity"] == "medium" and "disagrees" in x["finding"] for x in f)

    def test_sends_far_apart_not_duplicate(self):
        rows = [row(id=1, action_class="o", target="herd:OC", after_state={"sent": True}),
                row(id=2, action_class="o", target="herd:OC", after_state={"sent": True}, ts=T0 + timedelta(hours=1))]
        assert not any("twice" in x["finding"] for x in J.audit_rows(rows))

    def test_health_around_uses_node_filter(self):
        cur = FakeCursor(health={"soil": ("down", "up")})
        assert J._health_around(cur, "soil", "n", T0) == ("down", "up")
        assert all("node_name=%s" in s for s, _ in cur.executed)
        cur2 = FakeCursor()
        assert J._health_around(cur2, "soil", None, T0) == (None, None)
        assert all("node_name" not in s for s, _ in cur2.executed)


# ── Integration ─────────────────────────────────────────────────────────────
class TestIntegration:
    def test_gather_dedups_actor_rows_and_feeds_audit(self):
        log_rows = [(7, T0 + timedelta(seconds=20), "restart", "soil@n", True, True, "ok"),   # same minute: dup
                    (8, T0 + timedelta(hours=2), "restart", "kiln@n", True, True, "ok")]
        cur = FakeCursor(ledger=[row()], log_rows=log_rows)
        rows = J.gather(cur, 7)
        assert [(r["ledger"], r["id"]) for r in rows] == [("autonomy_ledger", 1), ("autonomy_log", 8)]
        assert rows[1]["action_class"] == "restart:kiln"
        f = J.audit_rows(rows, health=lambda s, n, ts: J._health_around(cur, s, n, ts))
        assert any(x["ledger"] == "autonomy_log" for x in f)

    def test_gather_survives_missing_autonomy_log(self):
        cur = FakeCursor(ledger=[row()], log_missing=True)
        assert len(J.gather(cur, 7)) == 1

    def test_write_doc_upserts_summary(self):
        cur = FakeCursor()
        f = J.audit_rows([row()], health=lambda *a: ("up", "down"))
        J.write_doc(cur, 7, [row()], f)
        (args,) = cur.inserts("agent_docs")
        assert "high=" in args[0] and "said soil was DOWN" in args[0]


# ── Functional ──────────────────────────────────────────────────────────────
class TestFunctional:
    def test_golden_path_records_findings_and_alerts(self):
        cur = FakeCursor(ledger=[row()], health={"soil": ("up", "down")})
        rc, _, nc = run_main(cur, ["--days", "3"])
        assert rc == 0
        ins = cur.inserts("self_justification_audit")
        assert ins and all(a[0] == 3 for a in ins)
        nc.post_both.assert_called_once()
        assert "didn't match" in nc.post_both.call_args.args[0]

    def test_clean_week_no_slack(self):
        cur = FakeCursor(ledger=[row()], health={"soil": ("down", "up")})
        rc, _, nc = run_main(cur, [])
        assert rc == 0
        nc.post_both.assert_not_called()
        assert cur.inserts("agent_docs")

    def test_health_query_error_is_not_fatal(self):
        cur = FakeCursor(ledger=[row()])
        orig = cur.execute

        def boom(sql, args=None):
            if "health_checks" in sql:
                raise RuntimeError("no table")
            return orig(sql, args)
        cur.execute = boom
        rc, _, _ = run_main(cur, ["--dry-run"])
        assert rc == 0


# ── Frame ───────────────────────────────────────────────────────────────────
class TestFrame:
    def test_imports_and_has_main(self):
        assert callable(J.main) and callable(J.audit_rows)

    def test_help_runs(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_self_justification_audit.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "--days" in r.stdout

    def test_empty_ledger_main(self):
        rc, _, nc = run_main(FakeCursor(), [])
        assert rc == 0
        nc.post_both.assert_not_called()
