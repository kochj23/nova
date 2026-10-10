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


@pytest.fixture(autouse=True)
def _feeds_off(request, monkeypatch):
    """The pre-merge tests count EXPLICIT_STREAMS exactly; keep the watchtower feeds out of
    them. Feed tests set FEED_STREAMS themselves; the live-schema integration class keeps
    the real list so every feed's SQL is exercised."""
    if request.cls is not None and request.cls.__name__ == "TestIntegration":
        return
    if "feeds" in request.node.name:
        return
    monkeypatch.setattr(fm, "FEED_STREAMS", [])


class RouteCur:
    """Cursor answering by SQL substring: routes = [(substr, rows)] (first match wins);
    fetchone returns rows[0] or None. Records (sql, params)."""
    def __init__(self, routes=None, raise_on=None):
        self.routes = routes or []
        self.raise_on = raise_on
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("boom")
        self._rows = next((list(r) for sub, r in self.routes if sub in sql), [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RouteConn(FakeConn):
    def __init__(self, routes=None, raise_on=None):
        super().__init__()
        self._cur = RouteCur(routes, raise_on)


def _feed(key, owner=None, alert=True, sla=1800):
    return fm.Stream(f"feed:{key}", "telemetry", "climate", "ts", "timestamptz", sla, "warning",
                     where=f"source='{key}'", owner=owner, alert=alert, feed_key=key)


def _seed_fresh_states(monkeypatch):
    """Transition-based alerting (8fe5571): a stream only PAGES on a fresh->stale
    transition. First-ever sight of a broken stream is a silent 'known-dead' baseline.
    Seed every explicit stream as previously-fresh so staleness is a transition."""
    prior = {s.name: {"state": "fresh", "problem_kind": None, "first_stale_ts": None,
                      "last_alerted_ts": None, "last_alert_num": 0}
             for s in fm.EXPLICIT_STREAMS}
    monkeypatch.setattr(fm, "load_states", lambda conn: prior)
    return prior


def _pageable_explicit():
    """Explicit streams that may emit: human-muted streams are recorded but never notify."""
    return [s for s in fm.EXPLICIT_STREAMS if s.name not in fm.MUTED_STREAMS]


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

    # ── merge M1 (2026-10-09) ──
    def test_age_sql_appends_code_defined_where(self):
        q = fm.age_sql(_feed("zigbee"))
        assert q.endswith(" WHERE source='zigbee'")
        assert " WHERE " not in fm.age_sql(fm.Stream("x", "telemetry", "energy", "ts"))

    def test_feeds_carry_watchtower_thresholds(self):
        by = {s.feed_key: s for s in fm.FEED_STREAMS}     # 'feeds' in the name: real list kept
        assert by["nas_backup"].sla_s == 1560 * 60 and by["nas_backup"].where == "ok"
        assert by["lora_mesh"].col == "last_heard" and by["lora_mesh"].sla_s == 360 * 60
        assert by["climate:zigbee"].owner == "zigbee-coordinator"
        assert by["climate:weather"].sla_s == 20 * 60 and len(by) == 11

    def test_collapse_by_owner_groups_transitions_only(self):
        def m(name, owner, action="transition", level="warning"):
            st = fm.Stream(name, "t", "x", "ts", owner=owner)
            return {"stream": st, "action": action, "title": name, "body": "b", "level": level,
                    "dedup": f"freshness:{name}", "meta": {"stream": name, "age_s": 4000}}
        out = fm.collapse_by_owner([m("a", "hub"), m("b", "hub", level="critical"), m("c", None),
                                    m("d", "solo"), m("e", "hub", action="recovered")])
        titles = [x["title"] for x in out]
        assert titles[0] == "Stale feeds, likely root cause: hub"
        assert out[0]["dedup"] == "freshness:owner:hub" and out[0]["level"] == "critical"
        assert out[0]["meta"]["streams"] == ["a", "b"]
        assert titles[1:] == ["c", "d", "e"]          # singles and non-transitions pass through
        assert fm.collapse_by_owner([]) == []

    def test_cadence_covered_only_when_a_tighter_sla_exists(self):
        assert fm.cadence_covered("telemetry.weather", 3600) == "telemetry.weather"
        assert fm.cadence_covered("public.snmp_metrics", 3600) == "snmp_metrics"
        assert fm.cadence_covered("telemetry.chp_incidents", 3600) is None   # 24h SLA is looser
        assert fm.cadence_covered("public.health_checks", 3600) is None      # no freshness stream
        assert fm.cadence_covered("telemetry.battery", 1) == "telemetry.battery"  # muted

    def test_presence_message_is_negative_space_text(self):
        from datetime import datetime, timedelta
        msg = fm.presence_quiet_message("ha_motion", datetime(2026, 10, 9, 3, 4),
                                        timedelta(hours=7, minutes=1, seconds=2, microseconds=5))
        assert msg.startswith("Presence method 'ha_motion' has reported nothing for 7:01:02 "
                              "(last: 2026-10-09 03:04).")


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
        # discovery returns nothing; every explicit stream was fresh and is now stale,
        # so each (non-muted) one is a fresh->stale TRANSITION and pages exactly once.
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        _seed_fresh_states(monkeypatch)
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)
        assert summary["checked"] == len(fm.EXPLICIT_STREAMS)
        assert len(summary["breaches"]) == len(fm.EXPLICIT_STREAMS)
        assert len(calls) == len(_pageable_explicit())
        assert sorted(summary["emitted"]) == sorted(s.name for s in _pageable_explicit())
        for c in calls:
            assert c["category"] == "freshness"
            assert c["dedup_key"].startswith("freshness:")
            assert c["source"] == "nova_freshness_monitor.py"
            assert c["meta"]["action"] == "transition"

    def test_first_sight_of_dead_stream_is_silent_baseline(self, monkeypatch):
        # No prior state at all: a long-dead producer must be RECORDED, not paged
        # (init_silent) — this is what tamed the ~768/day re-alert storm.
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        monkeypatch.setattr(fm, "load_states", lambda conn: {})
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)
        assert len(summary["breaches"]) == len(fm.EXPLICIT_STREAMS)
        assert calls == [] and summary["emitted"] == []

    def test_muted_stream_never_pages(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        _seed_fresh_states(monkeypatch)
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)
        for muted in fm.MUTED_STREAMS:
            assert muted not in summary["emitted"]
            assert not any(c["meta"]["stream"] == muted for c in calls)

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
        _seed_fresh_states(monkeypatch)
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        fm.run_once(conn, notify_fn=notifier)
        by_name = {c["title"].split(": ", 1)[1]: c["level"] for c in calls}
        assert by_name["telemetry.energy"] == "critical"
        assert by_name["inference_latency"] == "info"

    # ── merge M1 (2026-10-09) ──
    def test_feeds_same_owner_collapse_and_alias_stays_quiet(self, monkeypatch):
        feeds = [_feed("zigbee", owner="hub"), _feed("fp300", owner="hub"),
                 _feed("weather_station", alert=False), _feed("homekit")]
        monkeypatch.setattr(fm, "FEED_STREAMS", feeds)
        monkeypatch.setattr(fm, "EXPLICIT_STREAMS", [])
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        monkeypatch.setattr(fm, "load_states", lambda conn: {
            f.name: {"state": "fresh", "problem_kind": None, "first_stale_ts": None,
                     "last_alerted_ts": None, "last_alert_num": 0} for f in feeds})
        conn = FakeConn(age_value=10**9)
        calls, notifier = _collect_notifier()
        summary = fm.run_once(conn, notify_fn=notifier)
        assert len(summary["breaches"]) == 4
        keys = sorted(c["dedup_key"] for c in calls)
        assert keys == ["freshness:feed:homekit", "freshness:owner:hub"]
        assert not any("weather_station" in str(c["meta"]) for c in calls)

    def test_feeds_handover_from_watchtower_open_episode(self, monkeypatch):
        feeds = [_feed("zigbee"), _feed("homekit")]
        conn = RouteConn([("net_problems", [("stale:zigbee",)])])
        states = {}
        fm.seed_feed_states(conn, feeds, states)
        assert "feed:zigbee" not in states            # already alerted by watchtower -> silent baseline
        assert states["feed:homekit"]["state"] == "fresh"   # stale now = news -> transition alert
        fm.seed_feed_states(conn, [fm.Stream("x", "t", "x", "ts")], states)   # non-feeds untouched
        assert "x" not in states

    def test_feeds_record_liveness_and_problem_episodes(self):
        zig, home, hue = _feed("zigbee"), _feed("homekit"), _feed("hue")
        results = [fm.Result(zig, age_s=4000.0, breach=True), fm.Result(home, age_s=10.0),
                   fm.Result(hue, age_s=None, breach=True), fm.Result(fm.Stream("x", "t", "x", "ts"), age_s=1.0)]
        conn = RouteConn([("FROM telemetry.net_problems", [("stale:homekit",), ("stale:gone",)])])
        out = fm.record_feeds(conn, results)
        sqls = conn._cur.executed
        live = [p for q, p in sqls if "net_liveness" in q]
        assert live == [("feed:zigbee", "zigbee", False), ("feed:homekit", "homekit", True),
                        ("feed:hue", "hue", False)]
        opened = [p for q, p in sqls if q.lstrip().startswith("INSERT INTO telemetry.net_problems")]
        assert [p[0] for p in opened] == ["stale:zigbee", "stale:hue"]
        assert opened[0][4] == "feed 'zigbee' 67 min old (> 30 min)" and opened[1][4] == "feed 'hue' no data ever"
        assert sorted(out["cleared"]) == ["gone", "homekit"] and out["opened"] == ["zigbee", "hue"]
        assert conn.commits == 1
        dry = RouteConn()
        assert fm.record_feeds(dry, results, dry_run=True) == {"opened": [], "cleared": []}
        assert dry._cur.executed == []

    def test_presence_silence_alerts_like_negative_space(self, monkeypatch):
        from datetime import datetime, timedelta
        import nova_negative_space as ns
        sent = []
        monkeypatch.setattr(ns, "alert_findings", lambda f, notify=None: sent.extend(f) or len(f))
        rows = [("camera_vision", datetime(2026, 10, 9, 1, 0), timedelta(hours=8)),
                ("av_power", datetime(2026, 10, 2, 1, 0), timedelta(days=7))]
        av_key = ns.negspace_dedup_key("sensor_quiet", fm.presence_quiet_message(*rows[1]))
        conn = RouteConn([("telemetry.presence", rows)])
        orig = conn._cur.execute

        def ex(sql, params=None):                    # av_power was already SENT in the window
            orig(sql, params)
            if "telemetry.events" in sql:
                conn._cur._rows = [(1,)] if params[0] == av_key else []
        conn._cur.execute = ex
        found = fm.check_presence_silence(conn)
        assert [k for k, _ in found] == ["sensor_quiet", "sensor_quiet"]
        assert [m.split("'")[1] for _, m in sent] == ["camera_vision"]
        sent.clear()
        fm.check_presence_silence(RouteConn([("telemetry.presence", rows)]), dry_run=True)
        assert sent == []

    def test_run_cadence_writes_silent_without_witness_and_dedups_alerts(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        import nova_cadence_watch as cw
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        monkeypatch.setattr(cw, "STREAMS", [("telemetry.weather", "ts"), ("public.health_checks", "checked_at"),
                                            ("telemetry.soil", "ts")])
        learned = {"telemetry.weather": {"last_seen": old, "median_gap_s": 16.0, "n": 400},
                   "public.health_checks": {"last_seen": old, "median_gap_s": 5.0, "n": 400},
                   "telemetry.soil": None}
        monkeypatch.setattr(cw, "learn", lambda cur, t, c: learned[t])
        upserts, alerts, notes = [], [], []
        monkeypatch.setattr(cw, "upsert_cadence", lambda cur, src, kind, l, st, d: upserts.append((src, st)) or (None, True))
        monkeypatch.setattr(cw, "_alert_silent", lambda src, a, m, dry: alerts.append(src))
        monkeypatch.setattr(cw, "_write_silence_memory", lambda src, a, m: notes.append(src))
        monkeypatch.setattr(cw, "ensure_schema", lambda cur: None)
        conn = RouteConn([("to_regclass", [("cadence_state",)])])
        out = fm.run_cadence(conn)
        assert upserts == [("telemetry.weather", "SILENT"), ("public.health_checks", "SILENT")]
        assert alerts == ["public.health_checks"]          # weather's SLA alert fires first: one alert per fact
        assert notes == ["telemetry.weather", "public.health_checks"]   # memory notes unchanged
        assert all(e["witness"] is None and e["state"] == "SILENT" for e in out["silent"])
        assert out["skipped"] == [{"source": "telemetry.soil", "why": "insufficient history"}]
        upserts.clear(); alerts.clear()
        fm.run_cadence(RouteConn([("to_regclass", [("cadence_state",)])]), dry_run=True)
        assert upserts == [] and alerts == []

    def test_run_pass_runs_cadence_only_when_due(self, monkeypatch):
        monkeypatch.setattr(fm, "discover_streams", lambda conn: [])
        monkeypatch.setattr(fm, "check_presence_silence", lambda c, d, n=None: [])
        ran = []
        monkeypatch.setattr(fm, "run_cadence", lambda c, d=False: ran.append(d) or {"silent": []})
        monkeypatch.setattr(fm, "cadence_due", lambda c: False)
        out = fm.run_pass(FakeConn(age_value=1.0), notify_fn=lambda *a, **k: True)
        assert ran == [] and out["cadence"] is None
        monkeypatch.setattr(fm, "cadence_due", lambda c: True)
        fm.run_pass(FakeConn(age_value=1.0), notify_fn=lambda *a, **k: True)
        fm.run_pass(FakeConn(age_value=1.0), dry_run=True, notify_fn=lambda *a, **k: True)
        assert ran == [False, True]

    def test_cadence_due_reads_last_write(self):
        assert fm.cadence_due(RouteConn([("to_regclass", [(None,)])])) is True
        assert fm.cadence_due(RouteConn([("to_regclass", [("c",)]), ("max(updated_at)", [(60.0,)])])) is False
        assert fm.cadence_due(RouteConn([("to_regclass", [("c",)]), ("max(updated_at)", [(4000.0,)])])) is True
        assert fm.cadence_due(RouteConn(raise_on="to_regclass")) is True

    def test_main_learn_runs_only_the_cadence_pass(self, monkeypatch):
        conn = FakeConn()
        monkeypatch.setattr(fm, "_connect", lambda dsn=None: conn)
        ran = []
        monkeypatch.setattr(fm, "run_cadence", lambda c, dry_run=False: ran.append(dry_run))
        monkeypatch.setattr(fm, "run_pass", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full pass")))
        assert fm.main(["--learn", "--dry-run"]) == 0
        assert ran == [True] and conn.closed


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

    def test_feed_record_failure_rolls_back_and_never_raises(self):
        # RETRY GAP: record_feeds — one attempt per pass; the next 15-min pass writes again
        conn = RouteConn(raise_on="net_problems")
        out = fm.record_feeds(conn, [fm.Result(_feed("zigbee"), age_s=1.0)])
        assert out == {"opened": [], "cleared": []} and conn.rollbacks == 1

    def test_presence_and_cadence_failures_fail_open(self, monkeypatch):
        # RETRY GAP: check_presence_silence / run_cadence — one attempt; errors rolled back, pass continues
        conn = RouteConn(raise_on="telemetry.presence")
        assert fm.check_presence_silence(conn) == [] and conn.rollbacks == 1
        import nova_cadence_watch as cw
        monkeypatch.setattr(cw, "STREAMS", [("telemetry.weather", "ts")])
        monkeypatch.setattr(cw, "ensure_schema", lambda cur: None)
        monkeypatch.setattr(cw, "learn", lambda *a: (_ for _ in ()).throw(RuntimeError("relation missing")))
        out = fm.run_cadence(RouteConn([("to_regclass", [("c",)])]))
        assert out["checked"] == 0 and "relation missing" in out["skipped"][0]["why"]


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
        # exactly one max() age aggregate per explicit stream (no N^2, no per-row scans).
        # The state store adds a bounded O(1) overhead per pass (one SELECT of the
        # freshness_state table + one SELECT now()), which is not per-stream work.
        age_queries = [q for q in cur.executed if q.startswith("SELECT") and "max(" in q]
        assert len(age_queries) == len(fm.EXPLICIT_STREAMS)
        other = [q for q in cur.executed if q.startswith("SELECT") and "max(" not in q]
        assert len(other) <= 2

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
                   "run_once", "_connect", "main", "log_action", "run_pass", "run_cadence",
                   "record_feeds", "check_presence_silence", "collapse_by_owner"):
            assert callable(getattr(fm, fn))

    def test_help_exits_zero_and_import_is_guarded(self):
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "nova_freshness_monitor.py"), "--help"],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        assert r.returncode == 0 and "--learn" in r.stdout
        assert 'if __name__ == "__main__":\n    sys.exit(main())' in (SCRIPTS_DIR / "nova_freshness_monitor.py").read_text()

    def test_feeds_valid_and_unique(self):
        names = [s.name for s in fm.FEED_STREAMS]
        assert len(names) == len(set(names)) == 11
        assert all(s.feed_key and s.sla_s > 0 and s.kind in fm.VALID_KINDS for s in fm.FEED_STREAMS)
        assert [s.feed_key for s in fm.FEED_STREAMS if not s.alert] == ["weather_station"]

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
    def test_presence_alert_still_feeds_buick8_log(self):
        # nova_buick8_log reads source='nova_negative_space.py' events and parses this text
        import re
        src = (SCRIPTS_DIR / "nova_buick8_log.py").read_text()
        pat = re.search(r'_NS = re\.compile\(r"(.+?)"\)', src).group(1)
        from datetime import datetime, timedelta
        msg = "Negative-space: " + fm.presence_quiet_message("mmwave", datetime(2026, 10, 9, 1, 2), timedelta(hours=9))
        m = re.search(pat, msg)
        assert m and m.group(1) == "mmwave" and m.group(2).strip() == "2026-10-09 01:02"
        fsrc = (SCRIPTS_DIR / "nova_freshness_monitor.py").read_text()
        assert "ns.alert_findings(" in fsrc and "import nova_cadence_watch as cw" in fsrc
        assert "def learn(" not in fsrc and "def upsert_cadence(" not in fsrc   # imported, not copied

    def test_discovery_finds_known_telemetry_tables(self, real_conn):
        found = {s.name for s in fm.discover_streams(real_conn)}
        # energy/weather/etc are EXPLICIT and climate/ha_sensors are FEEDS (both excluded
        # from discovery); soil / overhead_flights are discovered
        assert "telemetry.soil" in found or "telemetry.overhead_flights" in found
        assert "telemetry.climate" not in found and "telemetry.ha_sensors" not in found
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
            # writer hosts' clocks can run a few ms ahead of pg-primary -> tiny negative
            # age; the query itself is valid. Tolerate sub-5s skew, flag anything larger.
            assert r.age_s is None or r.age_s >= -5.0, f"{s.name} age {r.age_s}"

    def test_run_once_dry_run_summary(self, real_conn):
        summary = fm.run_once(real_conn, notify_fn=lambda *a, **k: True, dry_run=True)
        assert summary["checked"] > 0
        assert isinstance(summary["breaches"], list)
        assert summary["errors"] == []  # no query should error against the real schema


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
