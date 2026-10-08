#!/usr/bin/env python3
"""Tests for nova_doorstep.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_doorstep as D  # noqa: E402

SRC = (SCRIPTS / "nova_doorstep.py").read_text()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
RANKING = {"chat_model": "m", "ollama": [{"url": "http://node:1", "status": "up", "loaded": ["m"]}]}


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = []
        for k, v in self.routes.items():
            if k in sql:
                self._last = list(v)
                break

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def perfect(url, model, prompt):
    """Answer every canary correctly."""
    for _cid, fam, p, want in D.CANARIES:
        if p == prompt:
            return json.dumps(want) if fam == "json" else str(want)
    return ""


def broken_json(url, model, prompt):
    return "```json\n{}\n```" if prompt.startswith(D.J) else perfect(url, model, prompt)


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_parameterized_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"(?i)(password|token|api_key)\s*=\s*['\"][^'\"]{6,}")

    def test_no_hardcoded_hosts_or_home(self):
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn(str(Path.home()), SRC)

    def test_model_output_is_data_not_code(self):
        # hostile output is only parsed as JSON / compared, never evaluated
        self.assertEqual(D.score_item("json", {"a": 1}, "__import__('os').system('x')"),
                         {"exact": False, "schema": False})
        self.assertNotRegex(SRC, r"\beval\(|\bexec\(")


class TestPerformance(unittest.TestCase):
    def test_scoring_10k_items(self):
        t = time.monotonic()
        items = [D.score_item("json", {"a": 5, "b": False}, '{"a": 5, "b": false}') for _ in range(5000)]
        items += [D.score_item("arith", "391", "391") for _ in range(5000)]
        self.assertEqual(D.rates(items), (1.0, 1.0))
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_ask_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionResetError("reset")
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = b'{"message": {"content": "391"}}'
            return resp
        with mock.patch("urllib.request.urlopen", side_effect=flaky), mock.patch("time.sleep") as sl:
            self.assertEqual(D.ask("http://node:1", "m", "q"), "391")
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.5, 1.0])

    def test_ask_fails_open(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), mock.patch("time.sleep"):
            self.assertIsNone(D.ask("http://node:1", "m", "q"))
            self.assertIsNone(D.model_digest("http://node:1", "m"))

    def test_dead_node_is_not_drift(self):
        cur = FakeCur({"FROM service_config": [(RANKING,)]})
        with mock.patch.object(D, "ask", return_value=None), mock.patch.object(D, "model_digest", return_value="d"):
            self.assertIsNone(D.run(cur=cur))
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))

    def test_missing_amulet_table_fails_open(self):
        self.assertFalse(D.amulet_saw_change(FakeCur(boom=True), "m", "d", T0))


class TestUnit(unittest.TestCase):
    def test_norm_and_score(self):
        self.assertTrue(D.score_item("fact", "au", " Au.")["exact"])
        self.assertFalse(D.score_item("arith", "31", "63")["exact"])
        self.assertEqual(D.score_item("json", {"x": [1]}, ""), {"exact": False, "schema": False})
        self.assertFalse(D.same_shape({"a": True}, {"a": 1}))   # bool is not int

    def test_empty_rates(self):
        self.assertEqual(D.rates([]), (0.0, 1.0))

    def test_threshold_edges(self):
        self.assertEqual(D.dropped((0.95, 1.0), (0.80, 1.0)), ["exact"])   # exactly 0.15
        self.assertEqual(D.dropped((0.95, 1.0), (0.85, 0.80)), ["schema"])
        self.assertEqual(D.dropped((0.5, 0.5), (1.0, 1.0)), [])          # improvement is not a drop

    def test_canary_set_is_frozen_and_hashed(self):
        self.assertEqual(len(D.CANARIES), 20)
        self.assertEqual(len(D.CANARY_HASH), 16)
        self.assertEqual({c[1] for c in D.CANARIES}, {"arith", "json", "fact"})

    def test_selftest(self):
        self.assertEqual(D.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_helpers(self):
        self.assertIn("import nova_llm_ping as P", SRC)
        self.assertIn("P._post(", SRC)
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn('log_unexplained("model_behaviour_change"', SRC)
        self.assertNotIn("cause", D.run.__code__.co_names)

    def test_reads_ranking_and_picks_loaded_node(self):
        cur = FakeCur({"FROM service_config": [(json.dumps(RANKING),)]})
        rk = D.load_ranking(cur)
        self.assertEqual(D.pick_node(rk, "m"), "http://node:1")
        self.assertIn("nova_llm_ping", cur.sql[0][0])

    def test_amulet_contract(self):
        cur = FakeCur({"jade_amulet_manifest": [(T0 + timedelta(days=1),)]})
        self.assertTrue(D.amulet_saw_change(cur, "m", "d", T0))
        self.assertIn("kind='ollama_model'", cur.sql[0][0])
        self.assertEqual(cur.sql[0][1], ("m", "d"))
        self.assertFalse(D.amulet_saw_change(FakeCur({"jade_amulet_manifest": [(T0,)]}), "m", "d", T0))


class TestFunctional(unittest.TestCase):
    def _run(self, routes, answer, dry=False):
        cur = FakeCur(dict({"FROM service_config": [(RANKING,)]}, **routes))
        with mock.patch.object(D, "ask", side_effect=answer), \
                mock.patch.object(D, "model_digest", return_value="d2"), \
                mock.patch("nova_buick8_log.log_unexplained") as b8:
            res = D.run(cur=cur, dry=dry)
        return res, cur, b8

    def test_first_run_is_baseline_and_written(self):
        res, cur, b8 = self._run({}, perfect)
        self.assertEqual((res["exact_rate"], res["schema_rate"]), (1.0, 1.0))
        self.assertEqual(res["detail"]["verdict"], "baseline")
        ins = [p for s, p in cur.sql if "INSERT INTO doorstep_runs" in s]
        self.assertEqual(ins[0][:3], ("m", "d2", D.CANARY_HASH))
        b8.assert_not_called()

    def test_unexplained_drop_goes_to_buick8(self):
        prev = {"FROM doorstep_runs": [(T0, 1.0, 1.0, "d1")], "jade_amulet_manifest": [(None,)]}
        res, cur, b8 = self._run(prev, broken_json)
        self.assertEqual(res["detail"]["verdict"], "unexplained")
        self.assertEqual(b8.call_args.args[:2], ("model_behaviour_change", "ollama:m"))

    def test_digest_change_is_expected_drift(self):
        prev = {"FROM doorstep_runs": [(T0, 1.0, 1.0, "d1")], "jade_amulet_manifest": [(T0 + timedelta(hours=1),)]}
        res, cur, b8 = self._run(prev, broken_json)
        self.assertEqual(res["detail"]["verdict"], "expected_drift")
        b8.assert_not_called()

    def test_dry_run_writes_nothing(self):
        prev = {"FROM doorstep_runs": [(T0, 1.0, 1.0, "d1")], "jade_amulet_manifest": [(None,)]}
        res, cur, b8 = self._run(prev, broken_json, dry=True)
        self.assertEqual(res["detail"]["verdict"], "unexplained")
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE")))
        b8.assert_not_called()

    def test_no_node_measures_nothing(self):
        cur = FakeCur({"FROM service_config": [({"ollama": []},)]})
        with mock.patch.object(D, "model_digest", return_value=None):   # model not on this host either
            self.assertIsNone(D.run(cur=cur))

    def test_prefers_this_hosts_ollama_when_it_has_the_model(self):
        cur = FakeCur({"FROM service_config": [({"ollama": [{"status": "up", "url": "http://other:11434",
                                                             "loaded": ["m"]}]},)]})
        asked = []
        with mock.patch.object(D, "model_digest", return_value="d1"), \
             mock.patch.object(D, "ask", side_effect=lambda url, *a: asked.append(url)):
            D.run(model="m", dry=True, cur=cur)
        self.assertTrue(asked and set(asked) == {D.LOCAL_OLLAMA})


class TestFrame(unittest.TestCase):
    def _cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "nova_doorstep.py"), *args], capture_output=True,
                              text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))

    def test_selftest_cli(self):
        r = self._cli("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = self._cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_doorstep"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
