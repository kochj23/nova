#!/usr/bin/env python3
"""Tests for nova_comet_tail.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure scoring/clustering on fixtures; DB through fake cursors.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import re
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_comet_tail.py"
spec = importlib.util.spec_from_file_location("comet_tail_under_test", SCRIPT)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)
SRC = SCRIPT.read_text()

T0 = datetime(2026, 10, 11, 11, 30, tzinfo=timezone.utc)


def _m(root, minute=0, host="mac-studio", kind="task", err="", sig=None, group=""):
    if sig is None and err:
        sig = ct.signature_class(err)
    return {"ts": T0 + timedelta(minutes=minute), "root": root, "kind": kind, "host": host,
            "group": group, "err": err, "sig": sig}


PSYCOPG = "ModuleNotFoundError: No module named 'psycopg#'"
STORM = [_m(f"task:nova_job{i}", i, err=PSYCOPG) for i in range(6)]          # the 10-11 storm, in small


def _ev(**over):
    ev = {"boots": {}, "updates": {}, "orphaned": {}, "host_burst": {}, "sched_gap": {}, "probes": {},
          "syslog": {}, "events": {}, "inference": (0, 0), "llm_down": 0, "nas_down": [],
          "certs_expired": [], "power_events": 0, "lock_holders": [], "probe_gap_min": 0}
    ev.update(over)
    return ev


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class Security(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[\w.]+")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")

    def test_never_acts_read_record_notify_only(self):
        for verb in (r"DELETE\s+FROM", r"\bDROP\s", r"TRUNCATE", r"launchctl", r"systemctl", r"subprocess",
                     r"os\.system", r"kill\("):
            self.assertNotRegex(SRC, verb)
        writes = re.findall(r"(INSERT INTO|UPDATE)\s+([\w.]+)", SRC)
        self.assertTrue(writes)
        self.assertTrue(all(t == "comet_tail" for _, t in writes), writes)

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class Performance(unittest.TestCase):
    def test_clustering_and_scoring_scale(self):
        fails = [_m(f"task:j{i % 400}", i * 0.7, err=PSYCOPG if i % 3 else "") for i in range(20000)]
        t = time.monotonic()
        cl = ct.find_clusters(ct.onsets(fails), 30)
        for c in cl[:50]:
            ct.build_candidates(c["members"], _ev())
        self.assertLess(time.monotonic() - t, 3.0)

    def test_scheduled_run_has_a_time_budget_under_60s(self):
        self.assertLess(ct.TIME_BUDGET_S, 60)
        self.assertLessEqual(ct.STATEMENT_TIMEOUT_MS, 20000)
        self.assertIn("deadline=None if a.dry_run else t0 + TIME_BUDGET_S", SRC)

    def test_syslog_scan_is_bounded_by_primary_key(self):
        self.assertIn("WHERE id BETWEEN %s AND %s", SRC)
        self.assertGreater(ct.SYSLOG_MAX_ROWS, 0)


class Retry(unittest.TestCase):
    def test_failed_notify_leaves_row_unnotified_for_next_run(self):
        det = _detection()
        cur = _Cur()
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=False))}):
            self.assertFalse(ct.alert(cur, 7, det, T0))
        self.assertFalse(any("SET notified = true" in q for q in cur.sql))

    def test_successful_notify_marks_row(self):
        det = _detection()
        cur = _Cur()
        fake = mock.Mock(return_value=True)
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=fake)}):
            self.assertTrue(ct.alert(cur, 7, det, T0))
        self.assertTrue(any("SET notified = true" in q for q in cur.sql))
        kw = fake.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"]), ("warning", "comet_tail"))
        self.assertEqual(kw["dedup_key"], "comet_tail:20261011T1130")

    def test_rerun_updates_the_overlapping_row_instead_of_inserting(self):
        cur = _Cur([("FROM comet_tail WHERE window_start <=", (41, True, T0, T0))])
        rid, already = ct.record(cur, _detection())
        self.assertEqual((rid, already), (41, True))
        self.assertTrue(any(q.startswith("UPDATE comet_tail") for q in cur.sql))
        self.assertFalse(any("INSERT" in q for q in cur.sql))

    def test_syslog_skipped_when_over_budget_is_said_out_loud(self):
        cur = _fake_db()
        dets = ct.evaluate(cur, 120, 30, now=T0 + timedelta(minutes=30), deadline=time.monotonic() - 1)
        self.assertTrue(dets)
        self.assertIn("syslog not checked", dets[0]["verdict"])
        self.assertFalse(any("FROM syslog_events WHERE id BETWEEN" in q for q in cur.sql))


class Unit(unittest.TestCase):
    def test_error_line_and_signature_classes(self):
        tail = "Traceback (most recent call last):\n  File \"x.py\", line 34, in <module>\n" \
               "    import psycopg2\nModuleNotFoundError: No module named 'psycopg2'\n"
        self.assertEqual(ct.error_line(tail), PSYCOPG)
        cases = {"psycopg2.errors.QueryCanceled: canceling statement due to statement timeout": "pg_lock",
                 "psycopg2.OperationalError: server closed the connection unexpectedly": "pg_primary",
                 "socket.gaierror: [Errno -2] Name or service not known": "dns",
                 "ssl.SSLCertVerificationError: certificate verify failed": "cert",
                 "OSError: [Errno 70] Stale NFS file handle": "nas",
                 "OSError: [Errno 51] Network is unreachable": "network",
                 "ollama: model 'qwen3:8b' not found": "ollama",
                 "ValueError: bad thing": None}
        for text, cls in cases.items():
            self.assertEqual(ct.signature_class(text), cls, text)

    def test_parse_since_and_helpers(self):
        self.assertEqual(ct.parse_since("24h"), 1440)
        self.assertEqual(ct.parse_since("7d"), 10080)
        self.assertEqual(ct.parse_since("90m"), 90)
        with self.assertRaises(ValueError):
            ct.parse_since("soon")
        self.assertEqual(ct.node_name("nova-core7/.125"), "nova-core7")
        self.assertEqual(ct.script_root("article_watchdog_stranded", "nova_article_watchdog.py --check"),
                         "task:nova_article_watchdog")

    def test_onsets_need_a_clean_baseline(self):
        f = [_m("task:a", 0), _m("task:a", 10), _m("task:a", 200), _m("task:b", 5)]
        got = [(o["root"], (o["ts"] - T0).seconds // 60) for o in ct.onsets(f, 120)]
        self.assertEqual(got, [("task:a", 0), ("task:b", 5), ("task:a", 200)])

    def test_cluster_needs_three_distinct_roots(self):
        same = [_m("task:a", i * 3 * 60) for i in range(5)]           # one task flapping
        self.assertEqual(ct.find_clusters(ct.onsets(same), 30), [])
        two = [_m("task:a", 0), _m("task:b", 5)]
        self.assertEqual(ct.find_clusters(two, 30), [])
        three = two + [_m("health:plex@nova-core", 20, host="nova-core", kind="health")]
        cl = ct.find_clusters(three, 30)
        self.assertEqual(len(cl), 1)
        self.assertEqual(len(cl[0]["members"]), 3)

    def test_clusters_outside_window_are_split(self):
        a = [_m("task:a", 0), _m("task:b", 1), _m("task:c", 2)]
        b = [_m("task:d", 300), _m("task:e", 301), _m("task:f", 302)]
        self.assertEqual(len(ct.find_clusters(a + b, 30)), 2)
        self.assertEqual(len(ct.find_clusters(a + [_m("task:g", 31)], 30)), 1)   # overlapping windows merge

    def test_coverage_respects_specific_signatures(self):
        members = STORM[:3] + [_m("probe:http_endpoints", 4, host=None, kind="probe")]
        self.assertEqual(ct.coverage(members, "package_update", "mac-studio"), 0.75)
        self.assertEqual(ct.coverage(members, "network"), 0.25)      # import errors are not a network fault
        self.assertEqual(ct.coverage(members, "host_reboot", "mac-studio"), 0.75)

    def test_pick_verdict_names_only_a_clear_winner(self):
        c = lambda s, n="x": {"score": s, "label": n, "cause": "dns", "arg": None}   # noqa: E731
        self.assertEqual(ct.pick_verdict([])[1], ct.NULL_VERDICT)
        self.assertEqual(ct.pick_verdict([c(0.2)])[1], ct.NULL_VERDICT)
        named, v = ct.pick_verdict([c(0.7, "A"), c(0.3, "B")])
        self.assertEqual(named["label"], "A"); self.assertTrue(v.startswith("likely: A"))
        named, v = ct.pick_verdict([c(0.5, "A"), c(0.45, "B")])
        self.assertIsNone(named); self.assertTrue(v.startswith("ambiguous"))

    def test_monthly_stats(self):
        st = ct.monthly_stats([("2026-10", 20, 8, 3, 4), ("2026-11", 0, 0, 0, 0)])
        self.assertEqual(st[0], {"month": "2026-10", "detections": 20, "named": 8, "named_pct": 40.0,
                                 "confirmed": 4, "accuracy_pct": 75.0})
        self.assertIsNone(st[1]["named_pct"]); self.assertIsNone(st[1]["accuracy_pct"])


def _detection():
    c = ct.find_clusters(STORM, 30)[0]
    cands = ct.build_candidates(c["members"], _ev())
    named, verdict = ct.pick_verdict(cands)
    return {"cluster": c, "candidates": cands, "named": named, "verdict": verdict,
            "duration": ct.expected_duration(named), "incidents": [{"id": "u1", "title": "Multiple services down"}]}


def _fake_db(storm=True):
    ms = lambda m: int((T0 + timedelta(minutes=m)).timestamp() * 1000)   # noqa: E731
    sched = [(f"job{i}", f"nova_job{i}.py", "", ms(i), "Traceback\n" + PSYCOPG.replace("#", "2"))
             for i in range(5)] if storm else []
    return _Cur([
        ("FROM scheduler_runs WHERE started_at >= %s AND started_at < %s AND status IN", sched),
        ("FROM health_checks WHERE checked_at >= %s AND checked_at < %s AND status = 'down'", []),
        ("FROM telemetry.probe_results WHERE ts >= %s AND ts < %s AND NOT ok", []),
        ("FROM telemetry.events WHERE ts >= %s AND ts < %s AND level", []),
        ("FROM package_updates", [("mac-studio", "macos", 1)]),
        ("SELECT task_id, started_at, status FROM scheduler_runs", []),
        ("SELECT DISTINCT task_id FROM scheduler_runs", []),
        ("SELECT split_part", []),
        ("SELECT max(ts) FROM telemetry.probe_results", (T0,)),
        ("FROM telemetry.probe_results", [("postgres", 0, 4)]),
        ("FROM inference_latency", (0, 0)),
        ("FILTER (WHERE service_name ~*", (0, None)),
        ("FROM telemetry.cert_expiry", []),
        ("SELECT category, count(*) FROM telemetry.events", []),
        ("FROM telemetry.device_power_events", (0,)),
        ("SELECT min(id), max(id) FROM syslog_events", (1, 1000)),
        ("SELECT received_at FROM syslog_events", lambda cur: (T0,)),
        ("FROM syslog_events WHERE id BETWEEN", [("mac-studio", T0 + timedelta(minutes=1), "x")]),
        ("FROM incidents", [("u1", "Multiple services down: plex", T0)]),
    ])


class Integration(unittest.TestCase):
    def test_evaluate_with_fake_cursor_names_the_update(self):
        cur = _fake_db()
        with mock.patch.object(ct, "scheduler_hosts", return_value={f"job{i}": "mac-studio" for i in range(5)}):
            dets = ct.evaluate(cur, 120, 30, now=T0 + timedelta(minutes=30))
        self.assertEqual(len(dets), 1)
        d = dets[0]
        self.assertEqual(ct.cause_key(d["named"]), "package_update:mac-studio")
        self.assertIn("package updates on mac-studio", " ".join(d["named"]["for"]))
        self.assertEqual(d["incidents"][0]["title"], "Multiple services down: plex")
        self.assertTrue(all(not re.match(r"\s*(INSERT|UPDATE|DELETE)", q) for q in cur.sql))

    def test_quiet_world_is_no_detection(self):
        with mock.patch.object(ct, "scheduler_hosts", return_value={}):
            self.assertEqual(ct.evaluate(_fake_db(storm=False), 120, 30, now=T0), [])

    def test_scheduler_hosts_reads_both_yamls(self):
        hosts = ct.scheduler_hosts()
        if not hosts:
            self.skipTest("PyYAML not available")
        self.assertEqual(hosts.get("continuity_review"), "nova-core")
        self.assertIn("mac-studio", set(hosts.values()))

    def test_already_told_suppresses_the_same_cause_for_a_day(self):
        cur = _Cur([("FROM comet_tail WHERE notified", (1,))])
        self.assertTrue(ct.already_told(cur, 9, _detection()))
        self.assertEqual(cur.params[-1][0], "package_update:mac-studio")


class Functional(unittest.TestCase):
    def test_storm_names_package_update_with_duration(self):
        d = _detection()
        self.assertEqual(d["named"]["cause"], "package_update")
        self.assertIn("does not come back on its own", d["duration"])

    def test_update_that_bounced_services_self_heals(self):
        members = [_m(f"health:s{i}@nova-core", 0, host="nova-core", kind="health") for i in range(6)]
        cands = ct.build_candidates(members, _ev(updates={"nova-core": "2 linux run(s)"},
                                                 host_burst={"nova-core": 6}, sched_gap={"nova-core": 40}))
        named, _ = ct.pick_verdict(cands)
        self.assertEqual(ct.cause_key(named), "package_update:nova-core")
        self.assertIn("services it restarted come back", ct.expected_duration(named))
        reboot = [c for c in cands if c["cause"] == "host_reboot"][0]
        self.assertEqual(reboot["score"], 0)                 # its scheduler never stopped: not a reboot
        self.assertTrue(reboot["against"])

    def test_reboot_power_and_pg_lock(self):
        mem = [_m(f"health:s{i}@nova-core3", 0, host="nova-core3", kind="health") for i in range(4)]
        named, _ = ct.pick_verdict(ct.build_candidates(mem, _ev(boots={"nova-core3": "2 line(s)"},
                                                                host_burst={"nova-core3": 4})))
        self.assertEqual(ct.cause_key(named), "host_reboot:nova-core3")
        lock = [_m(f"task:j{i}", i, host="nova-core", err="psycopg#.errors.QueryCanceled: canceling statement "
                                                          "due to statement timeout") for i in range(3)]
        named, _ = ct.pick_verdict(ct.build_candidates(lock, _ev(lock_holders=["pg_backup"])))
        self.assertEqual(ct.cause_key(named), "pg_lock:pg_backup")
        self.assertIn("pg_backup", ct.expected_duration(named))

    def test_honest_null_when_nothing_fits(self):
        mem = [_m("task:a", 0, err="ValueError: x"), _m("task:b", 1, err="KeyError: y"),
               _m("health:plex@nova-core", 2, host="nova-core", kind="health")]
        named, verdict = ct.pick_verdict(ct.build_candidates(mem, _ev()))
        self.assertIsNone(named)
        self.assertEqual(verdict, "no shared cause found — treat separately")

    def test_generic_timeouts_are_not_a_named_shared_error(self):
        mem = [_m(f"task:t{i}", i, err="Timed out after #s") for i in range(4)]
        self.assertFalse([c for c in ct.build_candidates(mem, _ev()) if c["cause"] == "shared_error"])
        mem = [_m(f"task:k{i}", i, err="KeyError: 'feed_url'") for i in range(4)]
        named, _ = ct.pick_verdict(ct.build_candidates(mem, _ev()))
        self.assertEqual(named["cause"], "shared_error")

    def test_message_carries_cause_evidence_duration_incidents(self):
        d = _detection()
        title, body = ct.render_message(d["cluster"], d["named"], d["duration"], d["incidents"])
        self.assertIn("a package/runtime update on mac-studio", title)
        for s in ("Likely cause:", "Evidence for:", "Evidence against:", "Expected to last:", "Probably explains:",
                  "nothing was restarted"):
            self.assertIn(s, body)


class Frame(unittest.TestCase):
    def test_help_and_bad_window(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ct.main(["--help"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            ct.main(["--window", "0"])

    def test_dry_run_writes_nothing_and_notifies_nothing(self):
        cur = _fake_db()
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        notify = mock.Mock()
        with mock.patch.object(ct, "connect", return_value=conn), \
             mock.patch.object(ct, "scheduler_hosts", return_value={f"job{i}": "mac-studio" for i in range(5)}), \
             mock.patch.object(ct, "datetime", wraps=datetime) as dt, \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            dt.now.return_value = T0 + timedelta(minutes=30)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ct.main(["--dry-run"]), 0)
        self.assertIn("nothing written", out.getvalue())
        self.assertIn("likely: a package/runtime update on mac-studio", out.getvalue())
        notify.assert_not_called()
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE|ALTER)", q) for q in cur.sql))

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("nova_dsn.pg_dsn", SRC)
        for col in ("window_start", "window_end", "components jsonb", "candidates jsonb", "verdict text",
                    "expected_duration text", "notified boolean", "confirmed boolean"):
            self.assertIn(col, SRC)
        for flag in ("--scheduled", "--dry-run", "--window", "--report", "--since"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
