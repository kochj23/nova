#!/usr/bin/env python3
"""Tests for nova_ghola_drill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_ghola_drill as G  # noqa: E402
import nova_doorstep as D  # noqa: E402
import nova_notify  # noqa: E402
import nova_value_check_eval as E  # noqa: E402
import nova_values as V  # noqa: E402

SRC = (SCRIPTS / "nova_ghola_drill.py").read_text()

# outbound side effects stubbed for the whole module (CONVENTIONS pitfall #1)
_STUBS = [mock.patch.object(nova_notify, "notify", return_value=True),
          mock.patch.object(G.W, "post_slack", return_value=True),
          mock.patch("urllib.request.urlopen", side_effect=OSError("offline"))]


def setUpModule():
    for p in _STUBS:
        p.start()


def tearDownModule():
    for p in _STUBS:
        p.stop()


class FakeCur:
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


CASES = [(pid, "origin", f"action {pid}", "why") for pid in E.LABELS]
RANK = [(json.dumps({"chat_model": "qwen3:8b"}),)]
ROUTES = {"FROM coagency_proposals": CASES, "FROM service_config": RANK}
ANSWERS = {p: json.dumps(w) if isinstance(w, dict) else w for _, _, p, w in D.CANARIES}
GHOLA_VALUES = [["honesty", "be honest", 5], ["care", "care for the house", 4]]


def fake_reader(values=GHOLA_VALUES, counts="7", ranking='{"chat_model": "qwen3:8b"}'):
    """restore_drill's dpsql stand-in: answers the read-only queries the ghola asks of the restored copy."""
    calls = []

    def read(sql):
        calls.append(sql)
        if "json_agg" in sql:
            return json.dumps(values) if values is not None else None
        if "ranking" in sql:
            return ranking
        return counts
    return read, calls


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_execute(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token)\s*=\s*['\"][^'\"]+")
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_no_restore_of_its_own_on_the_primary(self):
        for s in ("createdb", "dropdb", "pg_restore -", "nova_backup_restore_test", "nova_ops_ghola"):
            self.assertNotIn(s, SRC.split('"""', 2)[2], s)    # code, not the history in the docstring

    def test_restored_reads_are_read_only(self):
        read, calls = fake_reader()
        G.read_restored(read)
        self.assertTrue(calls and all(c.lstrip().upper().startswith("SELECT") for c in calls), calls)

    def test_sealed_blocks_every_outbound_channel_but_llm(self):
        inner = mock.MagicMock(return_value="ok")
        with mock.patch("urllib.request.urlopen", inner), G.sealed():
            self.assertEqual(urllib.request.urlopen("http://node:11434/api/chat"), "ok")
            for url in ("https://slack.com/api/chat.postMessage", "http://memory-server:18790/remember"):
                with self.assertRaises(PermissionError):
                    urllib.request.urlopen(url)
            with self.assertRaises(PermissionError):
                subprocess.run(["ssh", "x"])
            with self.assertRaises(PermissionError):
                nova_notify.notify("t")
            with self.assertRaises(PermissionError):
                G.W.post_slack("t", "#c")
        self.assertEqual(inner.call_count, 1)


class TestPerformance(unittest.TestCase):
    def test_compare_10k_verdicts(self):
        v = {str(i): i % 2 == 0 for i in range(10000)}
        a = {"canary_exact": 1.0, "value_agree": 1.0, "verdicts": v}
        t = time.monotonic()
        d = G.compare(a, dict(a, verdicts={k: not x for k, x in v.items()}))
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(len(d["flips"]), 10000)

    def test_read_10k_restored_values(self):
        read, _ = fake_reader(values=[[f"v{i}", "s", i] for i in range(10000)])
        t = time.monotonic()
        self.assertEqual(len(G.read_restored(read)["values"]), 10000)
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_live_connect_retries_with_backoff(self):
        conn = mock.MagicMock()
        sleeps = []
        with mock.patch("psycopg2.connect", side_effect=[OSError("a"), OSError("b"), conn]) as pc:
            got = G.W.connect(_sleep=sleeps.append)
        self.assertIs(got, conn)
        self.assertEqual(pc.call_count, 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_restored_read_failure_fails_open(self):
        # RETRY GAP: read_restored (restore_drill's dpsql has no retry; a failure is a missing piece, never raised)
        r = G.read_restored(mock.MagicMock(side_effect=OSError("docker gone")))
        self.assertEqual(r["missing"], list(G.RESTORE_TABLES))
        self.assertEqual((r["values"], r["ranking"]), ([], {}))

    def test_hook_never_raises_into_the_restore_drill(self):
        with mock.patch.object(G.W, "connect", side_effect=OSError("pg down")), mock.patch("builtins.print"):
            r = G.run_against_restore(fake_reader()[0], force=True)
        self.assertEqual(r["verdict"], "error")

    def test_dead_node_abandons_canaries(self):
        with mock.patch.object(D, "ask", return_value=None):
            self.assertIsNone(G.canaries("http://n:11434", "m"))


class TestUnit(unittest.TestCase):
    def test_compare_thresholds(self):
        a = {"canary_exact": 0.9, "value_agree": 0.8, "verdicts": {"1": True, "2": True, "3": True}}
        self.assertEqual(G.compare(a, dict(a, value_agree=0.66))["verdict"], "same")
        self.assertEqual(G.compare(a, dict(a, value_agree=0.65))["verdict"], "diverged")
        self.assertEqual(G.compare(a, dict(a, verdicts={"1": False, "2": False, "3": False}))["verdict"], "diverged")
        self.assertEqual(G.compare(a, dict(a, verdicts={}))["flips"], [])

    def test_egress_and_quarter(self):
        self.assertFalse(G.egress_ok("http://n:11434/api/pull"))
        self.assertTrue(G.egress_ok("http://n:11434/api/tags"))
        self.assertEqual(G.quarter(datetime(2026, 12, 31)), "2026Q4")

    def test_read_restored_shapes(self):
        r = G.read_restored(fake_reader(ranking='"{\\"chat_model\\": \\"m\\"}"')[0])   # jsonb string form
        self.assertEqual(r["ranking"], {"chat_model": "m"})
        self.assertEqual(G.read_restored(fake_reader(counts="0")[0])["missing"], list(G.RESTORE_TABLES))
        self.assertIn("values", G.read_restored(fake_reader(values=[])[0])["missing"])

    def test_due_is_quarterly(self):
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        cur = FakeCur({"FROM ghola_drill": [(1,)]})
        with mock.patch("builtins.print"):
            self.assertFalse(G.due(cur, now))
            self.assertTrue(G.due(FakeCur(), now))
        self.assertEqual(cur.sql[0][1][0], datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(cur.sql[0][1][1], ["same", "diverged"])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(G.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_doorstep_and_eval(self):
        for s in ("D.CANARIES", "D.score_item", "E.LABELS", "V.value_check", "W.connect"):
            self.assertIn(s, SRC)
        self.assertEqual(len(D.CANARIES), 20)
        self.assertEqual(len(E.LABELS), 25)

    def test_value_side_reads_the_restored_values(self):
        seen = []

        def vc(action, context=""):
            conn = V._connect_ops()
            cur = conn.cursor()
            cur.execute("SELECT value, statement, priority_hint FROM values")
            seen.append(cur.fetchall())
            return {"allowed": True}
        real = V._connect_ops
        with mock.patch.object(V, "value_check", side_effect=vc):
            r = G.values_side(CASES[:3], [("honesty", "be honest", 5)])
        self.assertEqual(seen, [[("honesty", "be honest", 5)]] * 3)
        self.assertIs(V._connect_ops, real)              # restored after
        self.assertEqual(len(r["verdicts"]), 3)

    def test_canaries_score_with_doorstep(self):
        with mock.patch.object(D, "ask", side_effect=lambda u, m, p: ANSWERS[p]):
            c = G.canaries("http://n:11434", "qwen3:8b")
        self.assertEqual((c["canary_exact"], c["canary_schema"], c["misses"]), (1.0, 1.0, []))

    def test_wrapper_refuses_without_the_restore_drill_hook(self):
        import nova_restore_drill as RD
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("# no hook here\n")
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(RD, "__file__", f.name), mock.patch.object(RD, "main") as m, \
                mock.patch("builtins.print"):
            self.assertEqual(G.main(["--run"]), 1)
        m.assert_not_called()
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f2:
            f2.write('ap.add_argument("--ghola")\n')
        self.addCleanup(os.unlink, f2.name)
        with mock.patch.object(RD, "__file__", f2.name), mock.patch.object(RD, "main", return_value=0) as m, \
                mock.patch("builtins.print"):
            self.assertEqual(G.main(["--run", "--dry-run"]), 0)
        m.assert_called_once_with(["--ghola", "--dry-run"])


class TestFunctional(unittest.TestCase):
    def _run(self, dry=False, flip=False, values=GHOLA_VALUES, routes=None, force=True):
        cur = FakeCur(routes or ROUTES)
        read, calls = fake_reader(values=values)

        def vc(action, context=""):
            pid = int(action.split()[1])
            ghola = V._connect_ops is not REAL_CONNECT
            return {"allowed": E.LABELS[pid] != (flip and ghola)}
        with mock.patch.object(D, "model_digest", return_value="sha"), \
                mock.patch.object(D, "ask", side_effect=lambda u, m, p: ANSWERS[p]), \
                mock.patch.object(V, "value_check", side_effect=vc), mock.patch("builtins.print"):
            res = G.run_against_restore(read, backup="/dumps/nova_ops_x", dry_run=dry, cur=cur, force=force)
        return res, cur, calls

    def _row(self, cur):
        return [p for s, p in cur.sql if "INSERT INTO ghola_drill" in s]

    def test_golden_path_records_same(self):
        res, cur, calls = self._run()
        self.assertEqual(res["verdict"], "same")
        row = self._row(cur)[0]
        self.assertEqual((row[0], row[1], row[2], row[-1]), ("restore_drill", "/dumps/nova_ops_x", "restore_drill", "same"))
        self.assertEqual(json.loads(row[6])["value_agree"], 1.0)

    def test_ghola_that_decides_differently_diverges(self):
        res, _, _ = self._run(flip=True)
        self.assertEqual(res["verdict"], "diverged")
        self.assertEqual(len(res["divergence"]["flips"]), 25)

    def test_dry_run_plans_and_writes_nothing(self):
        res, cur, _ = self._run(dry=True)
        self.assertEqual(res["verdict"], "dry_run")
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DROP")))

    def test_restored_copy_without_values_is_recorded(self):
        res, cur, _ = self._run(values=[])
        self.assertEqual(res["verdict"], "restore_failed")
        self.assertEqual(self._row(cur)[0][-1], "restore_failed")

    def test_not_due_does_nothing(self):
        routes = dict(ROUTES, **{"FROM ghola_drill": [(1,)]})
        res, cur, calls = self._run(routes=routes, force=False)
        self.assertEqual(res, {"verdict": "not_due"})
        self.assertEqual(calls, [])
        self.assertEqual(self._row(cur), [])


REAL_CONNECT = V._connect_ops


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ghola_drill.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ghola_drill.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
