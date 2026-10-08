#!/usr/bin/env python3
"""Tests for nova_bottle.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_bottle as B  # noqa: E402
import nova_notify  # noqa: E402

SRC = (SCRIPTS / "nova_bottle.py").read_text()
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

# outbound side effects stubbed for the whole module (CONVENTIONS pitfall #1)
_STUBS = [mock.patch.object(nova_notify, "notify", return_value=True),
          mock.patch.object(B.W, "post_slack", return_value=True),
          mock.patch("urllib.request.urlopen", side_effect=OSError("offline"))]


def setUpModule():
    for p in _STUBS:
        p.start()


def tearDownModule():
    for p in _STUBS:
        p.stop()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def writes(cur):
    return [s for s, _ in cur.sql if any(k in s for k in ("CREATE", "INSERT", "UPDATE", "DELETE"))]


GAP = (44, "gateway_restart", T0 + timedelta(hours=10), 36000.0, {"gap_is_upper_bound": True})
ROUTES = {
    "to_regclass": [("bottle_log",)],
    "FROM continuity_log": [GAP],
    "FROM gateway_traces": [(T0 + timedelta(hours=2), "slack", "are you there?")],
    "FROM claude_actions": [],
    "FROM watch_turnover": [({"at": "x", "degraded": ["nas"], "open_loops": ["proposal #9"]},)],
    "FROM claude_queue": [(7, "in_progress", T0, "rebuild index")],
    "FROM service_config": [],
}


def ok_run(*a, **k):
    return subprocess.CompletedProcess(a, 0, "", "")


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_core_dir_is_shell_quoted(self):
        with mock.patch("subprocess.run", side_effect=ok_run) as run:
            B.write_core("x; rm -rf /", "b.json", "{}", _sleep=lambda s: None)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["ssh", "-o"])
        self.assertIn("'x; rm -rf /'", argv[-1])

    def test_hostile_reason_sanitized_into_filename(self):
        n = B.bottle_name(T0, "h", "../../etc/passwd; rm")
        self.assertNotIn("/", n)
        self.assertNotIn(";", n)

    def test_unmounted_nas_never_falls_back_to_boot_disk(self):
        with tempfile.TemporaryDirectory() as d:   # a plain dir is not a mount point
            self.assertIsNone(B.nas_dir(B.DEFAULTS, roots=(d,)))
        with mock.patch("builtins.print"):
            self.assertIsNone(B.write_nas(None, "b.json", "{}"))

    def test_log_row_parameterized(self):
        cur, evil = FakeCur(), "x'); DROP TABLE bottle_log; --"
        B.log_row(cur, "gasp", evil, "h", evil, {}, {})
        sql, params = [(s, p) for s, p in cur.sql if "INSERT" in s][0]
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)


class TestPerformance(unittest.TestCase):
    def test_pick_bottle_10k(self):
        names = [B.bottle_name(T0 - timedelta(minutes=i), "h", "r") for i in range(10000)]
        t = time.monotonic()
        got = B.pick_bottle(names, T0 - timedelta(hours=2), T0)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(got, names[0])

    def test_render_10k_missed(self):
        p = {"gap": {"kind": "k", "start": T0.isoformat(), "end": T0.isoformat(), "seconds": 1},
             "bottle": None, "amulet": [], "missed": [{"at": "2026", "channel": "slack", "text": "hi"}] * 10000}
        t = time.monotonic()
        self.assertGreater(len(B.render_packet(p).splitlines()), 10000)
        self.assertLess(time.monotonic() - t, 3.0)


class TestRetry(unittest.TestCase):
    def test_ssh_retries_with_backoff(self):
        rcs = iter([255, 255, 0])
        sleeps = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: subprocess.CompletedProcess(a, next(rcs), "", "")) as run, \
                mock.patch("builtins.print"):
            got = B.write_core("nova-bottle", "b.json", "{}", _sleep=sleeps.append)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertEqual(got, "nova-core:nova-bottle/b.json")

    def test_ssh_down_fails_open(self):
        with mock.patch("subprocess.run", side_effect=OSError("no route")), mock.patch("builtins.print"):
            self.assertIsNone(B.write_core("d", "b.json", "{}", _sleep=lambda s: None))

    def test_pg_down_reads_degrade(self):
        with mock.patch("builtins.print"):
            self.assertEqual(B._q(FakeCur(boom=True), "SELECT 1"), [])
            self.assertEqual(B._q(None, "SELECT 1"), [])
            self.assertEqual(B.config(FakeCur(boom=True)), B.DEFAULTS)


class TestUnit(unittest.TestCase):
    def test_names_roundtrip(self):
        n = B.bottle_name(T0, "Office-M4-2", "scheduler_core_handover")
        self.assertEqual(B.name_ts(n), T0)
        self.assertIsNone(B.name_ts("bottle_.json"))
        self.assertEqual(B.slug(None), "unknown")

    def test_pick_bottle_window(self):
        early = B.bottle_name(T0 - timedelta(hours=3), "h", "r")
        self.assertIsNone(B.pick_bottle([early], T0, T0 + timedelta(hours=1)))
        self.assertIsNone(B.pick_bottle([], T0, T0))
        self.assertEqual(B.pick_bottle([early, "junk.json"], T0 - timedelta(hours=2), T0), early)

    def test_render_with_bottle_and_unexplained_change(self):
        p = {"gap": {"kind": "gateway_restart", "start": T0.isoformat(), "end": T0.isoformat(), "seconds": 3600},
             "bottle": {"file": "bottle_x.json", "reason": "gateway_unload"}, "missed": [],
             "amulet": [{"change": "changed", "kind": "ollama_model", "name": "m:1", "action_id": None}]}
        txt = B.render_packet(p)
        self.assertIn("bottle_x.json", txt)
        self.assertIn("UNEXPLAINED", txt)
        self.assertIn("nothing", txt)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(B.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("import nova_jade_amulet as J", SRC)
        self.assertIn("import nova_watch_bill as WB", SRC)
        self.assertIn("W.retry(", SRC)
        self.assertIn("W.get_config(cur, SVC, k)", SRC)

    def test_config_keys(self):
        self.assertEqual(B.SVC, "nova_bottle")
        self.assertEqual(set(B.DEFAULTS), {"nas_dir", "core_dir", "wake_gap_hours"})
        cur = FakeCur({"FROM service_config": [(json.dumps(12),)]})
        self.assertEqual(B.config(cur)["wake_gap_hours"], 12)

    def test_amulet_diff_uses_jade_amulet(self):
        import nova_jade_amulet as J
        cur = FakeCur({"SELECT max(ts)": [(T0,)], "SELECT min(ts)": [(T0 + timedelta(hours=10),)],
                       "SELECT kind, name, digest": [("ollama_model", "m:1", "a")]})
        with mock.patch.object(J, "diff", wraps=J.diff) as d, mock.patch.object(J, "actions_since", return_value=[]):
            self.assertEqual(B.amulet_diff(cur, "h", T0, T0 + timedelta(hours=10)), [])
        d.assert_called_once()

    def test_wake_packet_lands_in_watch_turnover(self):
        cur = FakeCur(ROUTES)
        with mock.patch.object(B, "nas_dir", return_value=None), mock.patch.object(B, "amulet_diff", return_value=[]), \
                mock.patch("builtins.print"):
            B.wake(days=30, cur=cur)
        ins = [p for s, p in cur.sql if "INSERT INTO watch_turnover" in s]
        self.assertEqual(ins[0][0], "wake")
        self.assertTrue(any("INSERT INTO bottle_log" in s for s, _ in cur.sql))


class TestFunctional(unittest.TestCase):
    def _gasp(self, dry, cur):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / "bottle"
        with mock.patch.object(B, "nas_dir", return_value=d), \
                mock.patch("subprocess.run", side_effect=ok_run) as run, mock.patch("builtins.print"):
            body = B.gasp("gateway_unload", dry=dry, cur=cur)
        return body, d, run

    def test_gasp_writes_nas_core_and_log(self):
        cur = FakeCur(ROUTES)
        body, d, run = self._gasp(False, cur)
        files = list(d.glob("bottle_*_gateway-unload.json"))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text())
        self.assertEqual(saved["open_loops"]["degraded"], ["nas"])
        self.assertEqual(saved["open_loops"]["queue"][0]["id"], 7)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(body["wrote"]["nas"] and body["wrote"]["core"])
        self.assertTrue(any("INSERT INTO bottle_log" in s for s in writes(cur)))

    def test_gasp_dry_run_writes_nothing(self):
        cur = FakeCur(ROUTES)
        body, d, run = self._gasp(True, cur)
        self.assertFalse(d.exists())
        run.assert_not_called()
        self.assertEqual(writes(cur), [])
        self.assertEqual(body["reason"], "gateway_unload")

    def test_gasp_with_pg_down_still_writes(self):
        with mock.patch.object(B.W, "connect", side_effect=RuntimeError("pg down")):
            body, d, run = self._gasp(False, None)
        self.assertEqual(len(list(d.glob("bottle_*.json"))), 1)
        self.assertEqual(body["doing"]["actions"], [])

    def test_wake_dry_run_writes_nothing(self):
        cur = FakeCur(ROUTES)
        with mock.patch.object(B, "nas_dir", return_value=None), mock.patch.object(B, "amulet_diff", return_value=[]), \
                mock.patch("builtins.print"):
            packets = B.wake(days=30, dry=True, cur=cur)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["missed"][0]["text"], "are you there?")
        self.assertTrue(packets[0]["gap"]["upper_bound"])
        self.assertEqual(writes(cur), [])

    def test_main_contains_failure(self):
        with mock.patch.object(B, "wake", side_effect=RuntimeError("boom")), mock.patch("builtins.print"):
            self.assertEqual(B.main(["--wake"]), 1)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_bottle.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_bottle.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
