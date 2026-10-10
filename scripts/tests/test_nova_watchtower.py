#!/usr/bin/env python3
"""Tests for nova_watchtower.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

PG is a scripted fake (db() patched), the UniFi snapshot is synthetic, ping is a mocked subprocess.run,
and the module's nova_config is a local proxy (post_both is a MagicMock)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_watchtower.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wt = _load("nova_watchtower_t", SCRIPT)
wt.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_ALERTS="C_ALERTS")


class Cur:
    def __init__(self, db):
        self.db, self._last = db, None

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.db.sql.append((" ".join(sql.split()), params))
        if self.db.fail_on and self.db.fail_on in sql:
            raise RuntimeError("relation missing")
        self._last = next((ans(params) if callable(ans) else ans for needle, ans in self.db.rules if needle in sql), None)

    def fetchall(self): return list(self._last or [])

    def fetchone(self):
        a = self._last
        return (a[0] if a else None) if isinstance(a, list) else a


class FakeDB:
    def __init__(self, rules=(), fail_on=None):
        self.rules, self.sql, self.fail_on, self.commits = list(rules), [], fail_on, 0

    def __call__(self):
        return self

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self, cursor_factory=None): return Cur(self)
    def commit(self): self.commits += 1

    def writes(self, prefix):
        return [(s, p) for s, p in self.sql if s.startswith(prefix)]


class _Base(unittest.TestCase):
    def setUp(self):
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(wt.subprocess, "run", boom), patch.object(wt.psycopg2, "connect", boom),
                  patch.object(wt.U, "_unifi_login", boom), patch.object(wt.U, "_unifi_get", boom)):
            p.start()
            self.addCleanup(p.stop)
        wt.nova_config.post_both.reset_mock()
        wt.nova_config.post_both.side_effect = None
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_values_are_parameterized(self):
        # the lone f-string query interpolates nothing; values go through %s
        for m in re.finditer(r'execute\(f"""(.*?)"""', SRC, re.S):
            self.assertNotIn("{", m.group(1))
        db = FakeDB([("status='open'", [])])
        with patch.object(wt, "db", db):
            wt.reconcile_and_alert({"down:aa'--": ("device_down", "x'; --", "infra", "d")}, dryrun=False)
        sql, params = db.writes("INSERT INTO telemetry.net_problems")[0]
        self.assertNotIn("x'; --", sql)
        self.assertEqual(params[2], "x'; --")

    def test_ping_rejects_empty_ip_without_spawning(self):
        self.assertFalse(wt.ping_ok(""))
        self.assertFalse(wt.ping_ok("?"))
        wt.subprocess.run.assert_not_called()


class TestPerformance(_Base):
    def test_classify_10k_fast(self):
        names = ["USW-Pro-48", "SLZB-06", "Front Doorbell", "Kitchen Lamp", "Jordans-iPhone", "", "mystery"] * 1500
        t0 = time.perf_counter()
        tiers = [wt.classify(n, "", False) for n in names]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(tiers[:7], ["infra", "coordinator", "camera", "smart_home", "transient", "transient", "unknown"])


class TestRetry(_Base):
    def test_ping_exception_is_false(self):
        with patch.object(wt.subprocess, "run", side_effect=subprocess.TimeoutExpired("ping", 2)) as run:
            self.assertFalse(wt.ping_ok("10.0.0.9"))
        self.assertEqual(run.call_count, 1)

    def test_alert_post_failure_is_logged(self):
        wt.nova_config.post_both.side_effect = RuntimeError("slack down")
        with patch.object(wt, "evaluate", return_value=({"down:x": ("device_down", "x", "infra", "infra 'x'")}, {})), \
                patch.object(wt, "reconcile_and_alert", return_value=(["🔴 infra 'x'"], [])), patch.object(sys, "argv", ["w"]):
            self.assertEqual(wt.main(), 0)
        self.assertIn("alert post failed: slack down", self.out.getvalue())


class TestUnit(_Base):
    def test_classify_edges(self):
        self.assertEqual(wt.classify("anything", "", True), "infra")
        self.assertEqual(wt.classify("Hue Bridge", "", False), "coordinator")
        self.assertEqual(wt.classify("Living-Room Apple TV", "", False), "smart_home")
        self.assertEqual(wt.classify("Jordans-MacBook-Pro", "Apple, Inc.", False), "transient")
        self.assertEqual(wt.classify("(unnamed)", "", False), "transient")

    def test_num(self):
        self.assertEqual(wt._num("4.5"), 4.5)
        self.assertIsNone(wt._num(None))
        self.assertIsNone(wt._num("n/a"))


class TestIntegration(_Base):
    def test_reconcile_opens_new_and_clears_recovered(self):
        db = FakeDB([("status='open'", [{"key": "down:bb", "detail": "camera 'old' dropped"}])])
        with patch.object(wt, "db", db):
            new, cleared = wt.reconcile_and_alert({"down:aa": ("device_down", "cam", "camera", "camera 'cam' dropped")},
                                                  dryrun=False)
        self.assertEqual(new, ["🔴 camera 'cam' dropped"])
        self.assertEqual(cleared, ["🟢 recovered: camera 'old' dropped"])
        self.assertEqual(db.writes("UPDATE telemetry.net_problems")[0][1], ("down:bb",))

    def test_feed_episodes_are_left_to_the_freshness_monitor(self):
        # merge M1 (2026-10-09): 'stale:<feed>' rows belong to nova_freshness_monitor.record_feeds
        db = FakeDB([("status='open'", [])])
        with patch.object(wt, "db", db):
            wt.reconcile_and_alert({}, dryrun=True)
        self.assertIn("left(key, 6) <> 'stale:'", db.sql[0][0])
        self.assertNotIn("FEEDS", SRC.split('"""', 2)[2])
        self.assertFalse(hasattr(wt, "feed_age_minutes"))

    def test_dryrun_reconcile_writes_nothing(self):
        db = FakeDB([("status='open'", [])])
        with patch.object(wt, "db", db):
            new, _ = wt.reconcile_and_alert({"down:aa": ("device_down", "cam", "camera", "d")}, dryrun=True)
        self.assertEqual(len(new), 1)
        self.assertEqual(db.writes("INSERT"), [])
        self.assertEqual(db.commits, 0)


class TestFunctional(_Base):
    def test_evaluate_flags_down_wired_device_only(self):
        active = {"m1": {"mac": "m1", "name": "USW-Lite-8", "ip": "10.0.0.2", "sw_port": 1}}
        rules = [("tier = ANY",
                  [{"mac": "m1", "name": "USW-Lite-8", "ip": "10.0.0.2", "tier": "infra"},
                   {"mac": "m2", "name": "SLZB-06", "ip": "10.0.0.3", "tier": "coordinator"},
                   {"mac": "m3", "name": "Garage Cam", "ip": "10.0.0.4", "tier": "camera"}]),
                 ("SELECT max(", (datetime.now(),))]
        db = FakeDB(rules)
        ping = MagicMock(side_effect=lambda argv, **k: subprocess.CompletedProcess(argv, 0 if argv[-1] == "10.0.0.4" else 1))
        with patch.object(wt, "db", db), patch.object(wt, "unifi_snapshot", return_value=(active, {}, set())), \
                patch.object(wt.subprocess, "run", ping):
            problems, counts = wt.evaluate()
        self.assertEqual(set(problems), {"down:m2"})
        self.assertEqual(counts, {"infra": 1})
        self.assertFalse(any("telemetry.climate" in q or "'feed'" in q for q, _ in db.sql))   # feeds moved out

    def test_main_alerts_on_change_and_dryrun_is_silent(self):
        probs = ({"down:m2": ("device_down", "SLZB", "coordinator", "coordinator 'SLZB' dropped")}, {"infra": 3})
        with patch.object(wt, "evaluate", return_value=probs), patch.object(sys, "argv", ["w", "--dryrun"]), \
                patch.object(wt, "reconcile_and_alert") as rec:
            self.assertEqual(wt.main(), 0)
        rec.assert_not_called()
        wt.nova_config.post_both.assert_not_called()
        with patch.object(wt, "evaluate", return_value=probs), patch.object(sys, "argv", ["w"]), \
                patch.object(wt, "reconcile_and_alert", return_value=(["🔴 coordinator 'SLZB' dropped"], [])):
            wt.main()
        msg = wt.nova_config.post_both.call_args.args[0]
        self.assertIn("coordinator 'SLZB' dropped", msg)
        self.assertNotIn("stale feeds", msg)
        self.assertEqual(wt.nova_config.post_both.call_args.kwargs["slack_channel"], "C_ALERTS")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dryrun", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
