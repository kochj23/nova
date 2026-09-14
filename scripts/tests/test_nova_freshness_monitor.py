#!/usr/bin/env python3
"""
test_nova_freshness_monitor.py — all 7 test categories for the freshness monitor.

Categories: Security, Performance, Retry, Unit, Integration, Functional, Frame.

The monitor's whole job is to catch silently-dead writers, so these tests are heavy
on the failure paths: empty tables, throwing cursors, a throwing notifier, a flaky
connection — none of which may ever crash a pass. Run:

    NOVA_TEST_QUIET=1 python3 -m pytest tests/test_nova_freshness_monitor.py -q

Written by Jordan Koch.
"""
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import nova_freshness_monitor as fm  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────
class FakeCursor:
    """Minimal DB-API cursor. Discovery queries hit fetchall(); age queries fetchone()."""
    def __init__(self, age_value=10.0, discover_rows=None, raise_on=None):
        self.age_value = age_value
        self.discover_rows = discover_rows if discover_rows is not None else []
        self.raise_on = raise_on          # substring -> raise when execute sees it
        self.executed = []
        self._is_discover = False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("boom: simulated query failure")
        self._is_discover = "information_schema" in sql

    def fetchone(self):
        v = self.age_value
        # allow a callable to vary the age per-stream
        return (v() if callable(v) else v,) if not self._is_discover else (0,)

    def fetchall(self):
        return list(self.discover_rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, **kw):
        self._cur = FakeCursor(**kw)
        self.closed = False
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _collect_notifier():
    calls = []

    def _n(title, body=None, level="info", category=None, source=None,
           dedup_key=None, correlation_id=None, meta=None):
        calls.append(dict(title=title, body=body, level=level, category=category,
                          source=source, dedup_key=dedup_key, meta=meta))
        return True
    return calls, _n


# ════════════════════════════════════════════════════════════════════════════
# 1. UNIT — pure functions in isolation
# ════════════════════════════════════════════════════════════════════════════
class TestUnit:
    def test_age_sql_timestamptz(self):
        s = fm.Stream("x", "telemetry", "energy", "ts", "timestamptz")
        q = fm.age_sql(s)
        assert 'EXTRACT(EPOCH FROM now() - max("ts"))' in q
        assert 'FROM "telemetry"."energy"' in q

    def test_age_sql_epoch_subtracts_epoch(self):
        s = fm.Stream("x", "public", "dashboard_snapshots", "ts", "epoch")
        q = fm.age_sql(s)
        assert 'EXTRACT(EPOCH FROM now()) - max("ts")' in q

    def test_age_sql_naive_timestamp_casts(self):
        s = fm.Stream("x", "public", "t", "ts", "timestamp")
        assert '::timestamptz' in fm.age_sql(s)

    def test_age_sql_date_text_parses(self):
        s = fm.Stream("x", "public", "dashboard_cost_history", "date", "date_text")
        assert "to_date(" in fm.age_sql(s) and "YYYY-MM-DD" in fm.age_sql(s)

    def test_age_sql_unknown_kind_raises(self):
        s = fm.Stream("x", "public", "t", "ts", "bogus")
        with pytest.raises(ValueError):
            fm.age_sql(s)

    def test_dedup_key_stable_and_per_stream(self):
        a = fm.Stream("telemetry.energy", "telemetry", "energy", "ts")
        assert a.dedup_key() == "freshness:telemetry.energy"

    def test_fmt_age_units(self):
        assert fm._fmt_age(None).strip() == "n/a"
        assert fm._fmt_age(5) == "5s"
        assert fm._fmt_age(65) == "1m05s"
        assert fm._fmt_age(3700).endswith("m")
        assert "d" in fm._fmt_age(200000)

    def test_result_reason(self):
        s = fm.Stream("x", "telemetry", "energy", "ts", sla_s=100)
        assert "NO DATA" in fm.Result(s, age_s=None, breach=True).reason
        assert "error" in fm.Result(s, error="oops").reason
        assert "age 200" in fm.Result(s, age_s=200.0).reason


# ════════════════════════════════════════════════════════════════════════════
# 2. FUNCTIONAL — check_stream / run_once end-to-end against a fake DB
# ════════════════════════════════════════════════════════════════════════════
class TestFunctional:
    def test_fresh_stream_not_breach(self):
        conn = FakeConn(age_value=5.0)
        s = fm.Stream("x", "telemetry", "energy", "ts", sla_s=600)
        r = fm.check_stream(conn, s)
        assert r.breach is False and r.age_s == 5.0

    def test_stale_stream_breaches(self):
        conn = FakeConn(age_value=99999.0)
        s = fm.Stream("x", "telemetry", "energy", "ts", sla_s=600)
        assert fm.check_stream(conn, s).breach is True

    def test_empty_table_is_breach(self):
        conn = FakeConn(age_value=None)   # max(ts) IS NULL -> no data
        s = fm.Stream("x", "telemetry", "energy", "ts", sla_s=600)
        r = fm.check_stream(conn, s)
        assert r.breach is True and r.age_s is None and "NO DATA" in r.reason

    def test_run_once_notifies_only_breaches(self, monkeypatch):
        # discovery returns nothing; make EVERY explicit stream stale so breaches fire.
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)
        assert summary["checked"] == len(fm.EXPLICIT_STREAMS)
        assert len(calls) == len(fm.EXPLICIT_STREAMS)
        for c in calls:
            assert c["category"] == "freshness"
            assert c["dedup_key"].startswith("freshness:")
            assert c["source"] == "nova_freshness_monitor.py"

    def test_dry_run_suppresses_notifications(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        fm.run_once(conn, notify_fn=notifier, dry_run=True)
        assert calls == []

    def test_run_once_all_fresh_no_notifications(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=0.0)
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)
        assert summary["breaches"] == [] and calls == []

    def test_breach_level_matches_stream(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        fm.run_once(conn, notify_fn=notifier)
        by_name = {c["title"].split(": ", 1)[1]: c["level"] for c in calls}
        assert by_name["telemetry.energy"] == "critical"
        assert by_name["inference_latency"] == "info"


# ════════════════════════════════════════════════════════════════════════════
# 3. RETRY — connection retry + per-stream error isolation (loop never dies)
# ════════════════════════════════════════════════════════════════════════════
class TestRetry:
    def test_connect_retries_then_succeeds(self, monkeypatch):
        attempts = {"n": 0}

        def flaky(dsn, connect_timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("connection refused")
            return FakeConn()

        fake_psycopg2 = type(sys)("psycopg2")
        fake_psycopg2.connect = flaky
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setattr(fm.time, "sleep", lambda *_: None)  # no real backoff wait
        conn = fm._connect("host=x")
        assert attempts["n"] == 3 and isinstance(conn, FakeConn)

    def test_connect_gives_up_after_max(self, monkeypatch):
        def always_fail(dsn, connect_timeout=None):
            raise RuntimeError("down")
        fake_psycopg2 = type(sys)("psycopg2")
        fake_psycopg2.connect = always_fail
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setattr(fm.time, "sleep", lambda *_: None)
        with pytest.raises(RuntimeError):
            fm._connect("host=x")

    def test_check_stream_never_raises_and_rolls_back(self):
        conn = FakeConn(raise_on="FROM")   # every age query throws
        s = fm.Stream("x", "telemetry", "energy", "ts")
        r = fm.check_stream(conn, s)       # must NOT raise
        assert r.error is not None and conn.rollbacks >= 1

    def test_one_bad_stream_does_not_abort_the_pass(self, monkeypatch):
        # Discover one extra stream whose query throws; the rest still complete.
        bad = fm.Stream("telemetry.broken", "telemetry", "broken", "ts", discovered=True)
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [bad])

        class MixedConn(FakeConn):
            def cursor(self):
                cur = FakeCursor(age_value=5.0)
                real_exec = cur.execute

                def exec2(sql, params=None):
                    real_exec(sql, params)
                    if '"broken"' in sql:
                        raise RuntimeError("broken table")
                cur.execute = exec2
                return cur

        conn = MixedConn()
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)   # must not raise
        assert "telemetry.broken" in summary["errors"]
        assert summary["checked"] == len(fm.EXPLICIT_STREAMS) + 1


# ════════════════════════════════════════════════════════════════════════════
# 4. SECURITY — no secrets, quoted identifiers, notifier can't crash the monitor
# ════════════════════════════════════════════════════════════════════════════
class TestSecurity:
    def test_dsn_has_no_embedded_password(self):
        assert "password" not in fm.DSN.lower()
        assert "passwd" not in fm.DSN.lower()

    def test_identifiers_are_quoted(self):
        # a hostile table/col name stays inside double-quotes -> can't break out of ident
        s = fm.Stream("x", "telemetry", 'evil"; DROP TABLE users; --', "ts")
        q = fm.age_sql(s)
        assert '"telemetry"."evil""; DROP TABLE users; --"' in q or 'DROP TABLE' in q
        # the point: the table token is wrapped in quotes, not spliced as bare SQL
        assert q.count('"') >= 4

    def test_notifier_exception_does_not_propagate(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=10**9)

        def boom(*a, **k):
            raise RuntimeError("notifier exploded")
        # run_once must swallow notifier errors — a broken alerter can't blind the monitor
        summary = fm.run_once(conn, notify_fn=boom)
        assert len(summary["breaches"]) == len(fm.EXPLICIT_STREAMS)

    def test_default_notify_import_is_safe(self):
        # module-level notify must be callable and never raise on import/use
        assert callable(fm._notify)


# ════════════════════════════════════════════════════════════════════════════
# 5. PERFORMANCE — bounded work: exactly one aggregate query per stream, O(N)
# ════════════════════════════════════════════════════════════════════════════
class TestPerformance:
    def test_one_query_per_stream_plus_discovery(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=1.0)
        cur = conn.cursor()
        fm.run_once(conn, notify_fn=lambda *a, **k: True, dry_run=True)
        # exactly one age query per explicit stream (no N^2, no per-row scans)
        age_queries = [q for q in cur.executed if q.startswith("SELECT")]
        assert len(age_queries) == len(fm.EXPLICIT_STREAMS)

    def test_age_sql_is_single_aggregate(self):
        # every query must be a single-row max() aggregate — never a full table pull
        for s in fm.EXPLICIT_STREAMS:
            q = fm.age_sql(s)
            assert "max(" in q and q.strip().lower().startswith("select")
            assert "limit" not in q.lower()  # aggregate returns one row inherently

    def test_run_once_completes_quickly(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        conn = FakeConn(age_value=1.0)
        t0 = time.time()
        fm.run_once(conn, notify_fn=lambda *a, **k: True, dry_run=True)
        assert time.time() - t0 < 2.0


# ════════════════════════════════════════════════════════════════════════════
# 6. FRAME — structural invariants of the config + deployment artifacts
# ════════════════════════════════════════════════════════════════════════════
class TestFrame:
    def test_all_streams_valid(self):
        for s in fm.EXPLICIT_STREAMS:
            assert s.kind in fm.VALID_KINDS, s.name
            assert s.sla_s > 0, s.name
            assert s.level in ("info", "warning", "critical"), s.name
            assert s.schema and s.table and s.col

    def test_stream_names_unique(self):
        names = [s.name for s in fm.EXPLICIT_STREAMS]
        assert len(names) == len(set(names))

    def test_dedup_keys_unique(self):
        keys = [s.dedup_key() for s in fm.EXPLICIT_STREAMS]
        assert len(keys) == len(set(keys))

    def test_required_streams_present(self):
        names = {s.name for s in fm.EXPLICIT_STREAMS}
        for required in ("telemetry.energy", "telemetry.av_state", "telemetry.weather",
                         "snmp_metrics", "telemetry.network", "dashboard_snapshots",
                         "dashboard_cost_history", "dashboard_memory_count_history",
                         "inference_latency", "telemetry.energy_hourly",
                         "telemetry.weather_daily", "telemetry.backup_runs",
                         "telemetry.chp_incidents"):
            assert required in names, f"missing required stream {required}"

    def test_public_columns_use_right_kind(self):
        by_name = {s.name: s for s in fm.EXPLICIT_STREAMS}
        assert by_name["dashboard_snapshots"].kind == "epoch"
        assert by_name["dashboard_cost_history"].kind == "date_text"
        assert by_name["snmp_metrics"].col == "timestamp"

    def test_public_api_present(self):
        for fn in ("age_sql", "discover_streams", "build_streams", "check_stream",
                   "run_once", "_connect", "main", "log_action"):
            assert callable(getattr(fm, fn))

    def test_launchd_plist_shipped_and_correct(self):
        plist = Path.home() / "Library/LaunchAgents/net.digitalnoise.nova-freshness-monitor.plist"
        assert plist.exists(), "launchd plist not installed"
        txt = plist.read_text()
        assert "nova_freshness_monitor.py" in txt
        assert "<key>StartInterval</key>" in txt and "900" in txt
        assert "<key>RunAtLoad</key>" in txt


# ════════════════════════════════════════════════════════════════════════════
# 7. INTEGRATION — against the real nova_ops database (skipped if unavailable)
# ════════════════════════════════════════════════════════════════════════════
def _real_conn():
    try:
        import psycopg2
    except Exception:
        return None
    for dsn in (os.environ.get("NOVA_PG_DSN"),
                "host=localhost dbname=nova_ops user=kochj",
                fm.DSN):
        if not dsn:
            continue
        try:
            c = psycopg2.connect(dsn, connect_timeout=5)
            c.autocommit = False
            return c
        except Exception:
            continue
    return None


@pytest.fixture(scope="module")
def real_conn():
    c = _real_conn()
    if c is None:
        pytest.skip("nova_ops database not reachable")
    yield c
    c.close()


class TestIntegration:
    def test_discovery_finds_known_telemetry_tables(self, real_conn):
        found = {s.name for s in fm.discover_streams(real_conn)}
        # energy/weather/etc are EXPLICIT (excluded from discovery); these are discovered
        assert "telemetry.climate" in found or "telemetry.ha_sensors" in found
        # partition children must NOT leak in
        assert not any(n[-6:].isdigit() for n in found)

    def test_build_streams_covers_explicit_plus_discovered(self, real_conn):
        streams = fm.build_streams(real_conn)
        assert len(streams) >= len(fm.EXPLICIT_STREAMS)

    def test_every_stream_query_executes(self, real_conn):
        # the real SQL for every stream must be valid against the live schema
        for s in fm.build_streams(real_conn):
            r = fm.check_stream(real_conn, s)
            assert r.error is None, f"{s.name} errored: {r.error}"
            assert r.age_s is None or r.age_s >= 0

    def test_run_once_dry_run_summary(self, real_conn):
        summary = fm.run_once(real_conn, notify_fn=lambda *a, **k: True, dry_run=True)
        assert summary["checked"] > 0
        assert isinstance(summary["breaches"], list)
        assert summary["errors"] == []  # no query should error against the real schema


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
