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
TOC = "".join(f"{i}; 0 1 TABLE DATA public {t} kochj\n" for i, t in enumerate(G.RESTORE_TABLES))
ANSWERS = {p: json.dumps(w) if isinstance(w, dict) else w for _, _, p, w in D.CANARIES}


def fake_sh(rc=None):
    """R.sh stand-in: answers by command; `rc` maps a command name to a forced return code."""
    calls = []

    def sh(args, **kw):
        calls.append(args)
        code = (rc or {}).get(args[0], 0)
        out = TOC if args[:2] == ["pg_restore", "-l"] else "5" if args[0] == "psql" else ""
        return subprocess.CompletedProcess(args, code, out, "")
    return sh, calls


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_execute(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_scratch_allowlist(self):
        for bad in ("nova_ops", "nova_memories", "postgres", "x_ghola; DROP DATABASE nova_ops", "", "ghola"):
            self.assertFalse(G.safe_scratch(bad), bad)
        self.assertTrue(G.safe_scratch("nova_ops_ghola"))

    def test_restore_refuses_production(self):
        sh, calls = fake_sh()
        with mock.patch.object(G, "SCRATCH", "nova_ops"), mock.patch.object(G.R, "sh", sh):
            with self.assertRaises(RuntimeError):
                G.restore(Path("dump"))
            G.drop_scratch()
        self.assertEqual(calls, [])

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

    def test_toc_10k_lines(self):
        toc = "".join(f"{i}; 0 1 TABLE DATA public t{i} kochj\n" for i in range(10000))
        t = time.monotonic()
        self.assertEqual(len(G.toc_tables(toc)), 10000)
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_scratch_connect_retries_with_backoff(self):
        conn = mock.MagicMock()
        sleeps = []
        with mock.patch("psycopg2.connect", side_effect=[OSError("a"), OSError("b"), conn]) as pc:
            got = G.W.connect(G.scratch_dsn(), _sleep=sleeps.append)
        self.assertIs(got, conn)
        self.assertEqual(pc.call_count, 3)
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertIn("dbname=nova_ops_ghola", pc.call_args.args[0])

    def test_pg_cli_failure_fails_open(self):
        # RETRY GAP: restore (pg CLI via R.sh has no retry; a failure is reported, never raised)
        sh, calls = fake_sh({"createdb": 1})
        with mock.patch.object(G.R, "sh", sh):
            self.assertEqual(G.restore(Path("dump")), (None, ["createdb failed"]))
        self.assertNotIn("pg_restore", [c[0] for c in calls])

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

    def test_toc_and_egress(self):
        self.assertEqual(G.toc_tables(None), set())
        self.assertFalse(G.egress_ok("http://n:11434/api/pull"))
        self.assertTrue(G.egress_ok("http://n:11434/api/tags"))

    def test_scratch_dsn_targets_scratch(self):
        dsn = G.scratch_dsn()
        self.assertIn("dbname=nova_ops_ghola", dsn)
        self.assertNotIn("dbname=nova_ops ", dsn + " ")

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(G.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_restore_test_doorstep_and_eval(self):
        self.assertIn("import nova_backup_restore_test as R", SRC)
        for s in ("R.latest_dump(R.LOCAL_DIR", "R.sh(", "D.CANARIES", "D.score_item", "E.LABELS", "V.value_check"):
            self.assertIn(s, SRC)
        self.assertEqual(len(D.CANARIES), 20)
        self.assertEqual(len(E.LABELS), 25)

    def test_value_side_reads_the_given_dsn(self):
        seen = []

        def vc(action, context=""):
            seen.append(V.OPS_DSN)
            return {"allowed": True}
        live_dsn = V.OPS_DSN
        with mock.patch.object(V, "value_check", side_effect=vc):
            r = G.values_side(CASES[:3], G.scratch_dsn())
        self.assertEqual(set(seen), {G.scratch_dsn()})
        self.assertEqual(V.OPS_DSN, live_dsn)            # restored after
        self.assertEqual(len(r["verdicts"]), 3)

    def test_canaries_score_with_doorstep(self):
        with mock.patch.object(D, "ask", side_effect=lambda u, m, p: ANSWERS[p]):
            c = G.canaries("http://n:11434", "qwen3:8b")
        self.assertEqual((c["canary_exact"], c["canary_schema"], c["misses"]), (1.0, 1.0, []))

    def test_plan_reads_only(self):
        cur = FakeCur(ROUTES)
        sh, calls = fake_sh()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(G.R, "sh", sh), \
                mock.patch.object(G.R, "latest_dump", return_value=Path(d)):
            p = G.plan(cur)
        self.assertEqual(p["missing_in_dump"], [])
        self.assertEqual(p["value_cases"], {"labelled": 25, "found": 25})
        self.assertEqual([c[:2] for c in calls], [["pg_restore", "-l"]])
        self.assertTrue(all(s.lstrip().startswith("SELECT") for s, _ in cur.sql))


class TestFunctional(unittest.TestCase):
    def _run(self, dry, rc=None, flip=False):
        cur, scur = FakeCur(ROUTES), FakeCur({"FROM service_config": RANK})
        sh, calls = fake_sh(rc)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def vc(action, context=""):
            pid = int(action.split()[1])
            return {"allowed": E.LABELS[pid] != (flip and V.OPS_DSN == G.scratch_dsn())}
        with mock.patch.object(G.R, "sh", sh), mock.patch.object(G.R, "latest_dump", return_value=Path(tmp.name)), \
                mock.patch.object(G.W, "connect", return_value=fake_conn(scur)), \
                mock.patch.object(D, "model_digest", return_value="sha"), \
                mock.patch.object(D, "ask", side_effect=lambda u, m, p: ANSWERS[p]), \
                mock.patch.object(V, "value_check", side_effect=vc), mock.patch("builtins.print"):
            res = G.run(dry=dry, cur=cur)
        return res, cur, calls

    def _row(self, cur):
        return [p for s, p in cur.sql if "INSERT INTO ghola_drill" in s]

    def test_golden_path_records_same_and_drops_scratch(self):
        res, cur, calls = self._run(False)
        self.assertEqual(res["verdict"], "same")
        row = self._row(cur)[0]
        self.assertEqual(row[-1], "same")
        self.assertEqual(json.loads(row[6])["value_agree"], 1.0)
        self.assertEqual(calls[-1][0], "dropdb")
        self.assertIn("nova_ops_ghola", calls[-1])
        self.assertFalse(any("nova_ops" == a for c in calls for a in c))

    def test_ghola_that_decides_differently_diverges(self):
        res, cur, _ = self._run(False, flip=True)
        self.assertEqual(res["verdict"], "diverged")
        self.assertEqual(len(res["divergence"]["flips"]), 25)

    def test_dry_run_plans_and_writes_nothing(self):
        res, cur, calls = self._run(True)
        self.assertEqual(res["sandbox"]["db"], "nova_ops_ghola")
        self.assertEqual([c[:2] for c in calls], [["pg_restore", "-l"]])
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DROP")))

    def test_restore_failure_recorded_and_cleaned(self):
        res, cur, calls = self._run(False, rc={"createdb": 1})
        self.assertEqual(res["verdict"], "restore_failed")
        self.assertEqual(self._row(cur)[0][-1], "restore_failed")
        self.assertEqual(calls[-1][0], "dropdb")


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ghola_drill.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ghola_drill.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
