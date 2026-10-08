#!/usr/bin/env python3
"""Tests for nova_spectroscope.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("NOVA_TEST_QUIET", "1")
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_spectroscope as S  # noqa: E402

SRC = (SCRIPTS / "nova_spectroscope.py").read_text()


class FakeCur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None


def fake_memory(path, body=None):
    """Memory server stand-in: /random gives 7 memories, recall finds m3 low and misses m5."""
    if path.startswith("/random"):
        return {"memories": [{"id": f"m{i}", "text": f"text {i}"} for i in range(7)]}
    out = []
    for q in body["queries"]:
        mid = "m" + q["q"].split()[1]
        score = {"m3": 0.81}.get(mid, 1.0)
        hits = [] if mid == "m5" else [{"id": mid, "score": score}]
        out.append({"query": q["q"], "memories": hits + [{"id": "other", "score": 0.7}]})
    return {"results": out}


class TestSecurity(unittest.TestCase):
    def test_no_secrets_ips_or_fstring_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn(str(Path.home()), SRC)

    def test_remote_command_is_quoted_suite_only(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=json.dumps(S.EXPECTED))
            S._ssh_suite("nova-core")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertTrue(argv[-1].startswith("python3 -c '"))   # shlex-quoted, no shell injection

    def test_flagged_detail_never_carries_memory_text(self):
        with mock.patch.object(S, "_http_json", side_effect=fake_memory):
            row = S.check_memory(7)
        self.assertNotIn("text 3", json.dumps(row))


class TestPerformance(unittest.TestCase):
    def test_score_10k_fast(self):
        hits = [{"id": f"x{i}", "score": 0.9} for i in range(5)]
        t = time.monotonic()
        for i in range(10000):
            S.score_self_retrieval(f"x{i % 5}", hits)
            S.compare_known(S.EXPECTED, peer=S.EXPECTED)
        self.assertLess(time.monotonic() - t, 2.0)

    def test_local_suite_is_seconds_not_minutes(self):
        t = time.monotonic()
        S.run_local_suite()
        self.assertLess(time.monotonic() - t, 10.0)


class TestRetry(unittest.TestCase):
    def test_memory_call_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("memory server busy")
            return {"memories": []}
        with mock.patch.object(S, "_http_json", side_effect=flaky), mock.patch("time.sleep") as sl, \
                mock.patch("nova_watch_common.log"):
            self.assertEqual(S.memory_get("/random?n=1"), {"memories": []})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sl.call_count, 2)

    def test_ssh_retries_then_fails_open(self):
        with mock.patch("subprocess.run", side_effect=OSError("no route")) as run, \
                mock.patch("time.sleep"), mock.patch("nova_watch_common.log"):
            self.assertIsNone(S.run_remote_suite("nova-core"))
        self.assertEqual(run.call_count, 3)

    def test_memory_server_down_fails_open(self):
        with mock.patch.object(S, "_http_json", side_effect=OSError("down")), \
                mock.patch("time.sleep"), mock.patch("nova_watch_common.log"):
            row = S.check_memory(10)
        self.assertEqual((row["n"], row["mismatches"]), (0, 0))
        self.assertTrue(row["detail"]["unreachable"])


class TestUnit(unittest.TestCase):
    def test_self_retrieval_statuses(self):
        self.assertEqual(S.score_self_retrieval("a", [{"id": "a", "score": 0.9995}])["status"], "ok")
        self.assertEqual(S.score_self_retrieval("a", [{"id": "a", "score": 0.99}])["status"], "low_cosine")
        self.assertEqual(S.score_self_retrieval("a", [])["status"], "miss")
        self.assertEqual(S.score_self_retrieval("a", [{"id": "a", "score": 0.5}])["status"], "miss")

    def test_compare_known(self):
        self.assertEqual(S.compare_known({}), {"wrong": sorted(S.EXPECTED), "peer_disagrees": []})
        odd = dict(S.EXPECTED, int_sum=0)
        self.assertEqual(S.compare_known(S.EXPECTED, peer=odd)["peer_disagrees"], ["int_sum"])

    def test_local_suite_matches_expected(self):
        self.assertEqual(S.run_local_suite(), S.EXPECTED)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(S.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_helpers(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("W.retry(", SRC)
        self.assertIn("from nova_buick8_log import log_unexplained", SRC)

    def test_memory_only_via_memory_server(self):
        self.assertNotIn("nova_memories", SRC.replace("nova_memories is never touched", ""))
        self.assertIn("memory-server.digitalnoise.net", SRC)
        self.assertNotIn("/remember", SRC)
        self.assertNotIn("/forget", SRC)

    def test_check_memory_shape(self):
        with mock.patch.object(S, "_http_json", side_effect=fake_memory):
            row = S.check_memory(7)
        self.assertEqual((row["check"], row["n"], row["mismatches"]), ("memory_integrity", 7, 2))
        self.assertEqual({f["id"]: f["status"] for f in row["detail"]["flagged"]},
                         {"m3": "low_cosine", "m5": "miss"})


class TestFunctional(unittest.TestCase):
    def test_run_writes_rows_and_logs_buick8(self):
        bad = dict(S.EXPECTED, matmul="corrupt")
        cur = FakeCur()
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        with mock.patch.object(S, "_http_json", side_effect=fake_memory), \
                mock.patch.object(S, "run_remote_suite", return_value=bad), \
                mock.patch("nova_watch_common.connect", return_value=conn), \
                mock.patch("nova_buick8_log.log_unexplained") as lu:
            rows = S.run(7)
        self.assertEqual(len(rows), 3)
        inserts = [p for s, p in cur.sql if "INSERT INTO spectroscope_runs" in s]
        self.assertEqual(len(inserts), 3)
        kinds = {c.args[0] for c in lu.call_args_list}
        sigs = {c.args[1] for c in lu.call_args_list}
        self.assertEqual(kinds, {"substrate_mismatch"})
        self.assertEqual(len(sigs), 3)   # memory + both hosts (local disagrees with its peer)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS spectroscope_runs" in s for s, _ in cur.sql))

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(S, "_http_json", side_effect=fake_memory), \
                mock.patch.object(S, "run_remote_suite", return_value=None), \
                mock.patch("nova_watch_common.connect") as conn:
            rows = S.run(7, dry=True)
        conn.assert_not_called()
        remote = rows[-1]
        self.assertTrue(remote["detail"]["unreachable"])     # unreachable is not a mismatch
        self.assertEqual(remote["mismatches"], 0)


class TestFrame(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_spectroscope.py"), "--help"],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_spectroscope.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
