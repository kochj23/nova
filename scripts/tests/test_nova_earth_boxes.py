#!/usr/bin/env python3
"""Tests for nova_earth_boxes.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_earth_boxes as E  # noqa: E402

SRC = (SCRIPTS / "nova_earth_boxes.py").read_text()
T0 = datetime(2026, 10, 8, 7, tzinfo=timezone.utc)
BURIED = [{"name": n, "host": "studio", "kind": "subagent", "retired_at": T0, "epoch": i}
          for i, n in enumerate(("lookout", "coder"), 1)]


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


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def bb_line(ts, msg, source="big-brother"):
    return json.dumps({"ts": ts.isoformat(), "level": "warn", "source": source, "msg": msg}) + "\n"


def write_log(lines):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.writelines(lines)
    f.close()
    return Path(f.name)


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertIn("digitalnoise.net", E.NOVA_CORE)   # DNS alias, not an address

    def test_hostile_name_stays_a_parameter(self):
        cur = FakeCur()
        evil = "x'; DROP TABLE claude_queue; --"
        E.bury(cur, evil, "service", "studio")
        sql, params = cur.sql[0]
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)

    def test_cli_rejects_bad_burial_name(self):
        with mock.patch.object(E.W, "connect") as c, mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                E.main(["--bury", "a b;rm -rf"])
            c.assert_not_called()

    def test_remote_command_is_read_only(self):
        for verb in ("rm ", "systemctl stop", "systemctl disable", "crontab -r", ">"):
            self.assertNotIn(verb, E.REMOTE_CMD.replace("2>/dev/null", ""))


class TestPerformance(unittest.TestCase):
    def test_boxes_10k_sources(self):
        src = [("studio", f"p{i}.plist", f"<string>com.nova.thing-{i}</string>\nline two") for i in range(10000)]
        src[5000] = ("studio", "hit.plist", "<string>com.nova.agent-coder</string>")
        t = time.monotonic()
        got = E.boxes(BURIED, src)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual([g["place"] for g in got], ["hit.plist"])

    def test_restarts_10k_lines(self):
        p = write_log([bb_line(T0, f"[warning] Subagent coder stale → Restarted via x {i}") for i in range(10000)])
        try:
            t = time.monotonic()
            rs = E.restarts(BURIED, files=(p,))
            self.assertLess(time.monotonic() - t, 3.0)
            self.assertEqual(rs, {("coder", "2026-10-08"): 10000})
        finally:
            p.unlink()


class TestRetry(unittest.TestCase):
    def test_ssh_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                return subprocess.CompletedProcess(a, 255, "", "timeout")
            return subprocess.CompletedProcess(a, 0, "### crontab\n* * * * * x\n", "")
        with mock.patch("subprocess.run", side_effect=flaky), mock.patch("builtins.print"):
            got = E.remote_sources(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(got, [("nova-core", "crontab", "\n* * * * * x\n")])

    def test_ssh_down_fails_open(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 40)), \
                mock.patch("builtins.print"):
            self.assertIsNone(E.remote_sources(_sleep=lambda s: None))

    def test_crontab_failure_fails_open(self):
        # RETRY GAP: _crontab (local `crontab -l`, single attempt; returns '' on failure)
        with mock.patch("subprocess.run", side_effect=OSError("no crontab binary")), mock.patch("builtins.print"):
            self.assertEqual(E._crontab(), "")

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            got = E.burials(FakeCur(boom=True))
        self.assertEqual({b["name"] for b in got}, {"lookout", "analyst", "librarian", "coder"})


class TestUnit(unittest.TestCase):
    def test_restart_list(self):
        self.assertEqual(E.restart_list('x = 1\nSUBAGENTS = ["sentinel"]\n'), '"sentinel"')
        self.assertEqual(E.restart_list("# SUBAGENTS = ['coder']"), "")
        self.assertEqual(E.restart_list(""), "")

    def test_name_rx_avoids_lookalikes(self):
        rx = E.name_rx("coder")
        for hit in ("com.nova.agent-coder", "python3 nova_agent_coder.py", "['coder']"):
            self.assertTrue(rx.search(hit), hit)
        for miss in ("qwen2.5-coder:32b", "coder-ish", "nova_agent_coders", "decoder"):
            self.assertFalse(rx.search(miss), miss)

    def test_boxes_empty(self):
        self.assertEqual(E.boxes([], [("h", "p", "agent-coder")]), [])
        self.assertEqual(E.boxes(BURIED, []), [])

    def test_restarts_respects_burial_time_and_kind_of_line(self):
        before = datetime(2026, 10, 7, tzinfo=timezone.utc)
        p = write_log([bb_line(T0, "[warning] Subagent lookout stale → Restarted via subagent_ctl.sh"),
                       bb_line(T0, "Restarted subagent coder"),
                       bb_line(before, "Restarted subagent coder"),                      # before burial
                       bb_line(T0, "Suppressed (escalation tier): Subagent coder stale/missing"),
                       bb_line(T0, "Restarted subagent sentinel"),                       # not buried
                       bb_line(T0, "Restarted subagent coder", source="watchdog"),
                       "not json Restarted big-brother\n"])
        try:
            self.assertEqual(E.restarts(BURIED, files=(p, Path("/nonexistent"))),
                             {("lookout", "2026-10-08"): 1, ("coder", "2026-10-08"): 1})
        finally:
            p.unlink()

    def test_split_sections(self):
        self.assertEqual(E.split_sections("h", "noise\n### a\nx"), [("h", "a", "\nx")])
        self.assertEqual(E.split_sections("h", ""), [])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(E.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("W.retry(", SRC)
        self.assertIn("W.connect()", SRC)

    def test_schema_contract(self):
        self.assertIn("CREATE SEQUENCE IF NOT EXISTS earth_box_epoch", E.SCHEMA)
        self.assertIn("nextval('earth_box_epoch')", E.SCHEMA)
        self.assertIn("UNIQUE (name, host, kind)", E.SCHEMA)
        self.assertIn("content_hash", E.SCHEMA)

    def test_seed_is_the_four_retired_subagents(self):
        self.assertEqual(sorted(n for n, *_ in E.SEED), ["analyst", "coder", "librarian", "lookout"])

    def test_findings_feed_claude_queue_with_dedup(self):
        f = E.findings([{"name": "coder", "kind": "subagent", "host": "nova-core", "place": "crontab",
                         "line": "x"}], {("coder", "2026-10-08"): 3})
        cur = FakeCur({"SELECT id FROM claude_queue": [(4,)]})          # already filed
        self.assertIsNone(E.file_item(cur, *f[0]))
        cur = FakeCur({"INSERT INTO claude_queue": [(9,)]})
        self.assertEqual(E.file_item(cur, *f[1]), 9)
        ins = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s][0]
        self.assertEqual(ins[0], E.QUEUE_SESSION)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, remote=(), rs=None):
        cur = FakeCur({"to_regclass": [("earth_box_burials",)],
                       "FROM earth_box_burials": [(b["name"], "studio", "subagent", T0, b["epoch"]) for b in BURIED],
                       "INSERT INTO claude_queue": [(42,)]})
        local = [("studio", "/L/com.nova.agent-coder.plist", "com.nova.agent-coder.plist\n")]
        with mock.patch.object(E.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(E, "local_sources", return_value=local), \
                mock.patch.object(E, "remote_sources", return_value=None if remote is None else list(remote)), \
                mock.patch.object(E, "restarts", return_value=rs or {("lookout", "2026-10-08"): 7}), \
                mock.patch("subprocess.run") as sp, mock.patch("builtins.print"):
            out = E.run(dry=dry)
        sp.assert_not_called()
        return cur, out

    def test_run_seeds_counts_and_files(self):
        cur, out = self._run(False, remote=[("nova-core", "crontab", "0 * * * * nova_agent_lookout.py")])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS earth_box_burials" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO earth_box_burials" in s for s, _ in cur.sql), 4)
        self.assertEqual({(b["name"], b["host"]) for b in out["boxes"]}, {("coder", "studio"), ("lookout", "nova-core")})
        self.assertEqual(out["filed"], [42, 42, 42])                  # two boxes + one Ross-loop day

    def test_dry_run_writes_nothing(self):
        cur, out = self._run(True)
        self.assertEqual(len(out["boxes"]), 1)
        self.assertEqual(out["filed"], [])
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))

    def test_nova_core_unreachable_still_counts_local(self):
        cur, out = self._run(False, remote=None)
        self.assertEqual([b["host"] for b in out["boxes"]], ["studio"])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_earth_boxes.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_earth_boxes.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
