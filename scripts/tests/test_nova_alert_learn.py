#!/usr/bin/env python3
"""Tests for nova_alert_learn.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_alert_learn.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


al = _load("alearn", SCRIPT)
SRC = SCRIPT.read_text()
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


class _Cur:
    """RealDictCursor stand-in: answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        hit = next((v for k, v in self.answers if k in sql), None)
        if isinstance(hit, Exception):
            raise hit
        self._last = hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = True; self.commits = 0; self.closed = 0

    def cursor(self, *a, **k): return self._cur

    def commit(self): self.commits += 1

    def close(self): self.closed += 1


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _args(**kw):
    base = {"slack": False, "dry_run": False, "window": 20, "min": 8, "since": None, "until": None,
            "min_age_hours": 2.0, "window_days": 14, "days": 30, "min_occ": 20, "no_seed": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _storm_rows(n=10, sig="task 'X' is STALE", source="task_sentinel", host=""):
    return [{"id": 100 + i, "ts": T0 + timedelta(seconds=i), "source": source, "category": "scheduler",
             "level": "warning", "title": sig.replace("X", f"job{i}"), "host": host} for i in range(n)]


def _triage_row(**kw):
    r = {"id": 1, "ts": T0, "title": "task 'backup' is STALE", "level": "warning", "category": "scheduler",
         "source": "task_sentinel", "verdict": "real_actionable", "decision": "page", "hard_override": False}
    r.update(kw)
    return r


def _grade_cur(incident=0, persisted_titles=(), acted=0):
    return _Cur([("corr_role = 'root'", {"n": incident}),
                 ("LIMIT 200", [{"title": t} for t in persisted_titles]),
                 ("FROM claude_actions", {"n": acted})])


# ── recurrence (moved here from nova_pattern_sense.py, M7 2026-10-09) ─────────
RECUR_ROWS = [{"title": "Multiple services down: searxng, tinychat", "n": 8},
              {"title": "Disk full on nas", "n": 3}, {"title": "blip", "n": 1}]


def _recur_cur(seen=None, incidents=None):
    return _Cur([("FROM incidents", RECUR_ROWS if incidents is None else incidents),
                 ("FROM service_config", {"value": {"seen": seen or {}}}),
                 ("INSERT INTO service_config", None)])


def _posting(posts, fail=None):
    def fake(req, timeout=None):
        if fail:
            raise fail
        posts.append((req.full_url, json.loads(req.data.decode())))
        return _Resp({"id": 1})
    return fake


class TestRecurrenceM7(unittest.TestCase):
    """The recurring-incident half of Pattern Sense, now `nova_alert_learn.py recurrence`.
    Covers Security/Retry/Unit/Integration/Functional for the new subcommand."""

    def _run(self, cur, dry_run=False, fail=None):
        posts, conn = [], _Conn(cur)
        with patch.object(urllib.request, "urlopen", _posting(posts, fail)), \
                patch.object(al, "_pattern_stamp", lambda: {}), redirect_stdout(io.StringIO()) as out:
            rc = al.cmd_recurrence(conn, _args(dry_run=dry_run))
        return rc, posts, conn, out.getvalue()

    def test_security_window_is_a_bound_parameter(self):
        seg = SRC[SRC.index("def cmd_recurrence"):SRC.index("# 2. FEEDBACK")]
        self.assertNotIn('execute(f"', seg)
        cur = _recur_cur()
        self._run(cur, dry_run=True)
        sql, params = cur.executed("FROM incidents")[0]
        self.assertIn("make_interval(days => %s)", sql)
        self.assertEqual(params, (al.RECUR_WINDOW_DAYS,))

    def test_unit_patterns_and_wording(self):
        self.assertEqual(al.recurrence_patterns([("A", 3), ("B", 2)]), [{"title": "A", "count": 3}])
        self.assertEqual(al.recurrence_patterns([]), [])
        txt = al.recur_insight({"title": "Disk full", "count": 5})
        self.assertTrue(txt.startswith("Pattern I can finally see: 'Disk full' has recurred 5 times in the last 30 days."))
        self.assertIn("All of this has happened before", txt)
        self.assertRegex(al._pattern_sig("recur", "x"), r"^[0-9a-f]{16}$")
        today = T0.date()
        self.assertTrue(al._pattern_fresh({}, "s", today))
        self.assertFalse(al._pattern_fresh({"s": (today - timedelta(days=3)).isoformat()}, "s", today))
        self.assertTrue(al._pattern_fresh({"s": "garbage"}, "s", today))

    def test_integration_same_source_and_shared_dedupe_row_merged(self):
        rc, posts, conn, _ = self._run(_recur_cur())
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(u.endswith("/remember") and b["source"] == "pattern_sense" for u, b in posts))
        self.assertEqual({b["metadata"]["kind"] for _, b in posts}, {"recurrence"})
        self.assertEqual({b["metadata"]["organ"] for _, b in posts}, {"nova_pattern_sense"})
        (sql, params), = conn._cur.executed("INSERT INTO service_config")
        self.assertEqual(params[:2], ("nova_pattern_sense", "high_water"))
        self.assertIn("coalesce(service_config.value->'seen', '{}'::jsonb) || (EXCLUDED.value->'seen')", sql)
        self.assertEqual(len(json.loads(params[2])["seen"]), 2)
        self.assertEqual(conn.commits, 1)

    def test_functional_already_seen_is_silent_and_dry_run_writes_nothing(self):
        today = datetime.now(timezone.utc).date().isoformat()
        seen = {al._pattern_sig("recur", r["title"]): today for r in RECUR_ROWS}
        rc, posts, conn, _ = self._run(_recur_cur(seen=seen))
        self.assertEqual((rc, posts, conn._cur.executed("INSERT")), (0, [], []))
        rc, posts, conn, out = self._run(_recur_cur(), dry_run=True)
        self.assertEqual((rc, posts, conn._cur.executed("INSERT")), (0, [], []))
        self.assertIn("• Pattern I can finally see: 'Multiple services down", out)

    def test_retry_gap_memory_down_leaves_patterns_unmarked(self):
        # RETRY GAP: _remember_pattern — one POST, no backoff; failure is logged, nothing is marked seen,
        # so the next run tries again.
        rc, posts, conn, out = self._run(_recur_cur(), fail=OSError("memory down"))
        self.assertEqual(rc, 0)
        self.assertIn("recurrence memory failed", out)
        self.assertEqual(conn._cur.executed("INSERT INTO service_config"), [])

    def test_functional_cli_dispatches_recurrence(self):
        cur = _recur_cur()
        with patch.object(al, "_connect", lambda: _Conn(cur)), \
                patch.object(sys, "argv", ["nova_alert_learn.py", "--dry-run", "recurrence"]), \
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("posted")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(al.main(), 0)
        self.assertIn("recurrence: 2 recurring-incident pattern(s), 2 fresh (dry-run)", out.getvalue())

    def test_performance_patterns_10k(self):
        rows = [(f"t{i}", i % 7) for i in range(10_000)]
        t0 = time.perf_counter()
        out = al.recurrence_patterns(rows)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(out), sum(1 for _, c in rows if c >= al.RECUR_MIN))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_alert_text_is_always_a_bound_parameter(self):
        # title/source/category reach SQL only through %s; the f-string queries carry typed argparse numbers
        grade = SRC[SRC.index("def _grade"):SRC.index("def cmd_feedback")]
        self.assertNotIn('execute(f"', grade)
        self.assertNotIn(".format(", SRC)
        cur = _grade_cur()
        al._grade(_Conn(cur), _triage_row(title="x'); DROP TABLE alert_triage_log; --"))
        self.assertTrue(all("DROP" not in s for s, _ in cur.sql))

    def test_never_deletes_raw_rows(self):
        self.assertIsNone(re.search(r"\bDELETE\b", SRC))
        self.assertIn("collapsed_into IS NULL", SRC)   # claims orphans only, never overwrites

    def test_hard_critical_signatures_are_never_learned_normal(self):
        for t in ("nightly backup failed", "security probe fail", "replica unreachable", "data loss on .10"):
            self.assertTrue(al._NEVER_BASELINE.search(t), t)
        self.assertFalse(al._NEVER_BASELINE.search("printer nozzle idle temperature"))
        self.assertTrue(al._NORMALCY.search("the replica is fenced by design"))
        self.assertFalse(al._NORMALCY.search("the replica crashed"))


class TestPerformance(unittest.TestCase):
    def test_norm_sig_10k_titles_fast(self):
        titles = [f"[host{i}] task 'job{i}' is STALE for 12 min at 192.168.1.{i % 250}" for i in range(10_000)]
        t0 = time.perf_counter()
        sigs = {al.norm_sig(t) for t in titles}
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(sigs, {"[h] task 'x' is stale for n min at ip"})

    def test_correlate_groups_10k_orphans_in_bounded_time(self):
        cur = _Cur([("FROM telemetry.events", _storm_rows(10_000))])
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            al.cmd_correlate(_Conn(cur), _args(dry_run=True))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def flaky(req, timeout=45):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "<think>hmm</think>ok"}})
        with patch.object(urllib.request, "urlopen", flaky):
            self.assertEqual(al.llm("p"), "ok")
        self.assertEqual(len(calls), 3)

    def test_recall_and_remember_fail_open(self):
        # RETRY GAP: recall — one GET, errors become []
        # RETRY GAP: remember — one POST, errors become None (the baseline row is still written)
        def boom(*a, **k):
            raise OSError("memory server down")
        with patch.object(urllib.request, "urlopen", boom), redirect_stdout(io.StringIO()):
            self.assertEqual(al.recall("q"), [])
            self.assertIsNone(al.remember("t"))

    def test_slack_digest_fails_open(self):
        # RETRY GAP: _post_slack — best-effort single digest; a failure is logged and returns False
        stub = types.ModuleType("nova_config"); stub.SLACK_ALERTS = "C1"

        def post_both(*a, **k):
            raise OSError("slack down")
        stub.post_both = post_both
        with patch.dict(sys.modules, {"nova_config": stub}), redirect_stdout(io.StringIO()):
            self.assertFalse(al._post_slack("hi"))


class TestUnit(unittest.TestCase):
    def test_norm_sig_collapses_variable_parts(self):
        self.assertEqual(al.norm_sig("task 'nova_dream' is STALE"), "task 'x' is stale")
        self.assertEqual(al.norm_sig('service "foo" down on [nova-core]'), 'service "x" down on [h]')
        self.assertEqual(al.norm_sig("ping 192.168.1.77 lost 3 of 10"), "ping ip lost n of n")
        self.assertEqual(al.norm_sig("latency 12 ms / 1,024 bytes at 10:30"), "latency n ms / n bytes at n")
        self.assertEqual(al.norm_sig(None), "")
        self.assertEqual(len(al.norm_sig("a" * 500)), 160)
        self.assertEqual(al.sig_key(None, None, "x"), "||x")

    def test_grade_detector_fault_short_circuits_before_any_query(self):
        cur = _grade_cur(incident=1, persisted_titles=["task 'backup' is STALE"], acted=1)
        outcome, why = al._grade(_Conn(cur), _triage_row(verdict="detector_fault", decision="suppress"))
        self.assertEqual(outcome, "detector_fault")
        self.assertIn("bug filed", why)
        self.assertEqual(cur.sql, [])   # the persisting signature must not read as a dangerous miss

    def test_grade_outcomes(self):
        self.assertEqual(al._grade(_Conn(_grade_cur(incident=1)), _triage_row()), ("was_real", "incident"))
        self.assertEqual(al._grade(_Conn(_grade_cur(persisted_titles=["task 'other' is STALE"])), _triage_row()),
                         ("was_real", "persisted"))
        self.assertEqual(al._grade(_Conn(_grade_cur(acted=2)), _triage_row()), ("was_real", "acted"))
        self.assertEqual(al._grade(_Conn(_grade_cur(incident=1, acted=1)), _triage_row())[1], "incident,acted")
        self.assertEqual(al._grade(_Conn(_grade_cur(persisted_titles=["disk full"])), _triage_row())[0], "was_noise")

    def test_grade_skips_action_lookup_for_short_titles(self):
        cur = _grade_cur(acted=5)
        self.assertEqual(al._grade(_Conn(cur), _triage_row(title="x1"))[0], "was_noise")
        self.assertEqual(cur.executed("FROM claude_actions"), [])


class TestIntegration(unittest.TestCase):
    def test_correlate_rolls_a_storm_into_one_event_and_stamps_members(self):
        rows = _storm_rows(10, host="nova-core") + _storm_rows(2, sig="disk 'X' full", source="df")
        cur = _Cur([("FROM telemetry.events", rows), ("RETURNING id", {"id": 999})])
        conn = _Conn(cur)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(al.cmd_correlate(conn, _args()), 0)
        ev = cur.executed("INSERT INTO telemetry.events")
        self.assertEqual(len(ev), 1)                       # the 2-row group is under STORM_MIN
        self.assertIn("'rollup'", ev[0][0])
        self.assertEqual(ev[0][1][0], "warning")
        self.assertTrue(ev[0][1][1].startswith("Storm: 10× [task_sentinel/scheduler] task 'x' is stale"))
        meta = json.loads(ev[0][1][4])
        self.assertEqual((meta["count"], meta["hosts"], meta["member_id_min"]), (10, ["nova-core"], 100))
        storm = cur.executed("INSERT INTO alert_storms")[0][1]
        self.assertEqual(storm[5], 10)
        self.assertEqual(storm[9], 999)
        upd = cur.executed("UPDATE telemetry.events")[0]
        self.assertEqual(upd[1], (999, 999, list(range(100, 110))))
        self.assertEqual(conn.commits, 2)   # schema + the one rollup

    def test_feedback_grades_then_writes_precision(self):
        todo = [_triage_row(id=1), _triage_row(id=2, verdict="detector_fault", decision="suppress")]
        grid = [{"decision": "suppress", "outcome": "was_real", "n": 1}, {"decision": "page", "outcome": "was_noise", "n": 2},
                {"decision": "page", "outcome": "was_real", "n": 2}]
        cur = _Cur([("WHERE outcome IS NULL", todo), ("GROUP BY 1,2", grid), *_grade_cur(incident=1).answers])
        conn = _Conn(cur)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(al.cmd_feedback(conn, _args()), 0)
        upd = cur.executed("UPDATE alert_triage_log SET outcome")
        self.assertEqual([p for _, p in upd], [("was_real", 1), ("detector_fault", 2)])
        prec = cur.executed("INSERT INTO alert_triage_precision")[0][1]
        self.assertEqual(prec[:5], (14, 5, 4, 1, 0))
        self.assertEqual((prec[8], prec[9], prec[10], prec[11]), (1, 1.0, 2, 0.5))
        self.assertIn("DANGEROUS MISS", buf.getvalue())

    def test_baselines_seed_uses_recall_then_remember_with_the_baseline_source(self):
        remembered = []
        mems = [{"text": "rack idle temperature is normal, not a heat alert", "source": "operations", "score": 0.9},
                {"text": "backup failed is normal", "source": "operations", "score": 0.9},     # hard-critical: never
                {"text": "printer idle temp normal", "source": "nova_articles", "score": 0.9},  # content feed: never
                {"text": "rack idle temperature is normal", "source": "operations", "score": 0.2}]  # weak match
        cur = _Cur([("FROM telemetry.events", [])])
        with patch.object(al, "recall", lambda q, n=4, source=None: mems), \
                patch.object(al, "remember", lambda text, source="baseline", metadata=None: remembered.append((text, source, metadata)) or "m1"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(al.cmd_baselines(_Conn(cur), _args()), 0)
        self.assertEqual(len(remembered), len(al.SEED_QUERIES))   # exactly one eligible memory per seed query
        self.assertTrue(all(src == "baseline" and md["origin"] == "seed" for _, src, md in remembered))

    def test_baselines_auto_detect_requires_proven_self_heal(self):
        rows = []
        for d in range(8):
            for i in range(3):
                rows.append({"source": "snmp", "category": "temp", "title": f"rack temp 'r{i}' high",
                             "ts": T0 - timedelta(days=d, minutes=i), "incident_id": 10 + d})
        cur = _Cur([("FROM telemetry.events", rows),
                    ("FROM telemetry.incidents WHERE id = ANY", {"open_now": 0, "total": 8, "slow": 0, "avg_mttr_min": 4.0}),
                    ("SELECT memory_id FROM learned_baselines", None)])
        with patch.object(al, "remember", lambda *a, **k: "mem9"), redirect_stdout(io.StringIO()):
            al.cmd_baselines(_Conn(cur), _args(no_seed=True, min_occ=20))
        ins = cur.executed("INSERT INTO learned_baselines")
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1][0], "snmp|temp|rack temp 'x' high")
        self.assertEqual(ins[0][1][8], "mem9")
        # one still-open incident disqualifies the signature
        cur2 = _Cur([("FROM telemetry.events", rows),
                     ("FROM telemetry.incidents WHERE id = ANY", {"open_now": 1, "total": 8, "slow": 0, "avg_mttr_min": 4.0})])
        with redirect_stdout(io.StringIO()):
            al.cmd_baselines(_Conn(cur2), _args(no_seed=True, min_occ=20))
        self.assertEqual(cur2.executed("INSERT INTO learned_baselines"), [])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur):
        conn = _Conn(cur)
        buf = io.StringIO()
        with patch.object(al, "_connect", lambda: conn), patch.object(sys, "argv", ["nova_alert_learn.py", *argv]), \
                redirect_stdout(buf):
            rc = al.main()
        return rc, buf.getvalue(), conn

    def test_feedback_dry_run_grades_without_writing(self):
        cur = _Cur([("WHERE outcome IS NULL", [_triage_row()]), ("GROUP BY 1,2", []), *_grade_cur().answers])
        rc, out, conn = self._main(["--dry-run", "feedback"], cur)
        self.assertEqual(rc, 0)
        self.assertIn("→ was_noise", out)
        self.assertEqual(cur.executed("UPDATE"), [])
        self.assertEqual(cur.executed("INSERT INTO alert_triage_precision"), [])
        self.assertEqual(conn.closed, 1)

    def test_correlate_with_slack_posts_one_digest(self):
        posted = []
        cur = _Cur([("FROM telemetry.events", _storm_rows(9)), ("RETURNING id", {"id": 5})])
        with patch.object(al, "_post_slack", lambda m: posted.append(m) or True):
            rc, out, conn = self._main(["--slack", "correlate", "--min", "8"], cur)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posted), 1)
        self.assertIn("9 alerts → 1 events", posted[0])

    def test_error_path_grade_failure_is_unknown_not_noise(self):
        cur = _Cur([("WHERE outcome IS NULL", [_triage_row()]), ("GROUP BY 1,2", [])])

        def boom(conn, row):
            raise RuntimeError("pg hiccup")
        with patch.object(al, "_grade", boom):
            rc, out, conn = self._main(["feedback"], cur)
        self.assertEqual(rc, 0)
        self.assertEqual(cur.executed("UPDATE alert_triage_log SET outcome")[0][1], ("unknown", 1))
        self.assertIn("grade error", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        for sub in ("correlate", "feedback", "baselines", "recurrence"):
            self.assertIn(sub, r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(al.__name__, "alearn")


if __name__ == "__main__":
    unittest.main()
