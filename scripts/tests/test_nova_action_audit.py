"""Tests for nova_action_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Offline: PG is a MagicMock cursor, Slack history is a fake callable, Big Brother log is a temp file."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_action_audit.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("nova_action_audit_t", SCRIPT)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _bot_msg(ts, text):
    return {"user": A.BOT_USER, "bot_id": A.BOT_ID, "ts": f"{ts.timestamp():.6f}", "text": text}


class TestSecurity(unittest.TestCase):
    def test_no_credentials_in_source(self):
        self.assertIsNone(re.search(r"xox[bp]-|password\s*=\s*['\"]\w|Bearer [A-Za-z0-9]{10,}", SRC))

    def test_sql_is_parameterized(self):
        for line in SRC.splitlines():
            if re.search(r"execute\(f[\"']", line):
                self.fail(f"f-string SQL: {line.strip()}")

    def test_preview_scrubs_numbers_and_addresses(self):
        probe = "text " + "kochj23" + "@" + "gmail.com" + " or +1 818 555 0100"
        p = A._safe_preview(probe)
        self.assertNotIn("gmail", p)
        self.assertNotIn("555", p)
        self.assertLessEqual(len(A._safe_preview("x" * 999)), 160)

    def test_imessage_target_masked(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur
        A.record_outbound("imessage", "+18185550100", "hi", _connect=lambda *a, **k: conn)
        args = cur.execute.call_args_list[-1][0][1]
        self.assertNotIn("+18185550100", args[1])
        self.assertTrue(args[1].startswith("…0100#"))


class TestPerformance(unittest.TestCase):
    def test_match_10k_under_bound(self):
        obs = [{"ts": T0 + timedelta(seconds=i), "kind": "slack", "text": f"alert number {i} disk full on host"}
               for i in range(5000)]
        led = [{"ts": T0 + timedelta(seconds=i), "text": f"alert number {i} disk full on host", "table": "t"}
               for i in range(0, 10000, 2)]
        t = time.time()
        m, u = A.match(obs, led)
        self.assertLess(time.time() - t, 10.0)
        self.assertEqual(len(m) + len(u), 5000)


class TestRetry(unittest.TestCase):
    def test_slack_failures_on_every_channel_raise_not_silent(self):
        def boom(*a, **k):
            raise OSError("down")      # nova_slack_answers.slack already retried 3x with backoff
        with self.assertRaises(RuntimeError):
            A.observe_slack(T0, slack=boom)

    def test_one_channel_failing_still_reports_others(self):
        calls = {"n": 0}

        def flaky(method, **p):
            calls["n"] += 1
            if p["channel"] == list(A.slack_channels().values())[0]:
                raise OSError("down")
            return {"ok": True, "messages": [_bot_msg(T0, "hello there everyone")]}
        out = A.observe_slack(T0 - timedelta(hours=1), slack=flaky)
        self.assertEqual(len(out), len(A.slack_channels()) - 1)

    def test_record_outbound_never_raises(self):
        # RETRY GAP: record_outbound — deliberately no retry: a send must never wait on the ledger
        self.assertFalse(A.record_outbound("slack", "c", "t",
                                           _connect=lambda *a, **k: (_ for _ in ()).throw(OSError("x"))))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(A.selftest(), 0)

    def test_similar_and_norm(self):
        self.assertTrue(A.similar("*Bodach Watch*: signals clustering near home",
                                  "Bodach Watch: signals clustering near home tonight"))
        self.assertFalse(A.similar("", "x"))
        self.assertEqual(A.norm("<https://x|link> *bold* :wave:"), "link bold")

    def test_signature_strips_digits(self):
        self.assertEqual(A.signature("Memory usage 91%\nmore"), A.signature("Memory usage 87%"))

    def test_by_producer_counts(self):
        o1 = {"kind": "slack", "text": "A thing"}
        o2 = {"kind": "slack", "text": "A thing"}
        bp = A.by_producer([o1, o2], [o2])
        self.assertEqual(bp["slack: a thing"], {"observed": 2, "unlogged": 1})

    def test_exact_slack_ts_match(self):
        m, u = A.match([{"ts": T0, "kind": "slack", "text": "x", "slack_ts": "5.5"}],
                       [{"ts": T0 - timedelta(hours=5), "text": "q", "table": "slack_prompts", "slack_ts": "5.5"}])
        self.assertEqual((len(m), len(u)), (1, 0))


    def test_mode_dispatch(self):
        with patch.object(A, "audit", return_value={}) as au, patch.object(A, "rationale") as ra, \
                patch.object(A, "oversight") as ov:
            for argv in (["--complete"], ["--audit"], ["--rationale"], ["--rationale", "--days", "3", "--dry-run"],
                         ["--oversight"], ["--oversight", "--dry-run"]):
                self.assertEqual(A.main(argv), 0)
        self.assertEqual([c.args for c in au.call_args_list], [(24, False), (24, False)])
        self.assertEqual([c.args for c in ra.call_args_list], [(7, False), (3, True)])
        self.assertEqual([c.args for c in ov.call_args_list], [(30, False), (30, True)])

    def test_modes_are_exclusive(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            A.main(["--complete", "--oversight"])


class TestIntegration(unittest.TestCase):
    def test_reuses_bb_regex_from_self_repair_digest(self):
        self.assertIn("from nova_self_repair_digest import BB_FIX_RX, BB_TEST_RX", SRC)

    def test_observe_bb_reads_timestamped_fixes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nova.jsonl"
            p.write_text(json.dumps({"ts": T0.isoformat(), "source": "big-brother",
                                     "msg": "[WARNING] subagent x stale → Restarted via ctl"}) + "\n" +
                         json.dumps({"ts": T0.isoformat(), "source": "other", "msg": "a → Restarted"}) + "\n")
            out = A.observe_bb(T0 - timedelta(hours=1), files=[p])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["producer"].startswith("big-brother subagent x stale"))

    def test_ledger_tables_read(self):
        for t in ("telemetry.events", "outbound_ledger", "gateway_traces", "reach_log", "slack_prompts",
                  "autonomy_ledger", "restraint_ledger", "shine_log", "escalation_log"):
            self.assertIn(t, SRC)

    def test_hooks_installed_in_chokepoints(self):
        self.assertIn('_ledger_outbound("slack", slack_channel, message)', (SCRIPTS / "nova_config.py").read_text())
        self.assertIn("_ledger_outbound(recipient, text)", (SCRIPTS / "nova_imessage.py").read_text())


    def test_absorbed_modes_call_their_modules(self):
        import nova_ae35_rule
        import nova_self_justification_audit
        with patch.object(nova_self_justification_audit, "run", return_value=["f"]) as jr, \
                patch.object(nova_ae35_rule, "audit", return_value=["e"]) as aa:
            self.assertEqual(A.rationale(7, True), ["f"])
            self.assertEqual(A.oversight(30, False), ["e"])
        jr.assert_called_once_with(7, True)
        aa.assert_called_once_with(30, dry=False)
        self.assertIn("self_justification_audit", (SCRIPTS / "nova_self_justification_audit.py").read_text())
        self.assertIn("ae35_events", nova_ae35_rule.SCHEMA)


class TestFunctional(unittest.TestCase):
    def _cur(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = (False,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_audit_golden_path_flags_unlogged_and_posts_once(self):
        conn, cur = self._cur()
        msgs = [_bot_msg(datetime.now(timezone.utc) - timedelta(minutes=5), "Unlogged weird post about things")]
        fake_slack = lambda method, **p: {"ok": True, "messages": msgs if p["channel"] == A.slack_channels()["#nova-chat"] else []}
        posted = []
        with patch("nova_watch_common.connect", return_value=conn), \
                patch.object(A, "observe_bb", return_value=[]), \
                patch.object(A, "file_hotwash", return_value=0):
            res = A.audit(24, dry_run=False, _slack=fake_slack, _post=lambda line: posted.append(line) or True)
        self.assertEqual(res["violations"], 1)
        self.assertEqual(len(posted), 1)
        self.assertIn("no ledger row", posted[0])
        sqls = " ".join(c[0][0] for c in cur.execute.call_args_list)
        self.assertIn("INSERT INTO action_audit", sqls)

    def test_dry_run_writes_and_posts_nothing(self):
        conn, cur = self._cur()
        posted = []
        with patch("nova_watch_common.connect", return_value=conn), patch.object(A, "observe_bb", return_value=[]):
            res = A.audit(24, dry_run=True, _slack=lambda m, **p: {"ok": True, "messages": []},
                          _post=lambda l: posted.append(l))
        self.assertEqual(res["violations"], 0)
        self.assertFalse(posted)
        self.assertNotIn("INSERT INTO action_audit", " ".join(c[0][0] for c in cur.execute.call_args_list))


    def test_oversight_dry_run_writes_nothing(self):
        conn, cur = self._cur()
        with patch("nova_watch_common.connect", return_value=conn), patch("builtins.print"):
            self.assertEqual(A.main(["--oversight", "--dry-run"]), 0)
        sqls = " ".join(c[0][0] for c in cur.execute.call_args_list)
        self.assertIn("coagency_proposals", sqls)
        for k in ("CREATE", "INSERT", "UPDATE"):
            self.assertNotIn(k, sqls)

    def test_rationale_dry_run_writes_and_posts_nothing(self):
        import nova_self_justification_audit as J
        conn, cur = self._cur()
        cur.description = []
        posted = MagicMock()
        with patch.object(J.psycopg2, "connect", return_value=conn), \
                patch.dict(sys.modules, {"nova_config": MagicMock(post_both=posted)}), patch("builtins.print"):
            self.assertEqual(A.main(["--rationale", "--dry-run"]), 0)
        sqls = " ".join(c[0][0] for c in cur.execute.call_args_list)
        self.assertIn("autonomy_ledger", sqls)
        for k in ("CREATE", "INSERT", "UPDATE"):
            self.assertNotIn(k, sqls)
        posted.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for arg in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


    def test_help_lists_the_three_modes(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
        for m in ("--complete", "--rationale", "--oversight"):
            self.assertIn(m, r.stdout)

if __name__ == "__main__":
    unittest.main()
