#!/usr/bin/env python3
"""Tests for nova_seldon_axioms.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import nova_charles as C  # noqa: E402
import nova_seldon_axioms as S  # noqa: E402
import nova_soft_certainty as SC  # noqa: E402

SRC = (SCRIPTS / "nova_seldon_axioms.py").read_text()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
CRIT = 'x ```check\n{"type": "mem_activity", "source": "fire", "expect": "active", "min": 1}\n```'
PRED = (7, T0, T0 + timedelta(hours=6), "The fire memory stream will stay active.", CRIT, 0.8, "correct")
PRED2 = (8, T0, T0 + timedelta(hours=6), "The horology memory stream will be silent.", "", 0.6, "incorrect")
ACTS = [{"ts": T0 + timedelta(hours=1), "source": "big_brother", "producer": "bb", "text": "→ Restarted fire_ingest"}]
_URL = mock.patch("urllib.request.urlopen", side_effect=OSError("offline"))


def setUpModule():
    _URL.start()


def tearDownModule():
    _URL.stop()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []
        self.connection = mock.MagicMock()

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


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_regex_metacharacters_in_statement_are_escaped(self):
        ns = S.needles("The '.*|(' stream will stay active.", "")
        self.assertTrue(ns)
        self.assertEqual(S.touching(ns, [{"text": "anything at all"}]), [])

    def test_hostile_statement_never_reaches_sql_text(self):
        evil = "x'); DROP TABLE predictions; --"
        cur = FakeCur({"FROM predictions": [(9, T0, T0 + timedelta(hours=1), evil, "", 0.5, "correct")]})
        with mock.patch.object(S.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(S, "own_actions", return_value=[]), mock.patch("builtins.print"):
            S.run(dry=False)
        self.assertFalse(any(evil in s for s, _ in cur.sql))

    def test_read_only_observer(self):
        self.assertNotIn("post_both", SRC)
        self.assertNotRegex(SRC, r"(?i)(UPDATE|ALTER TABLE) predictions")


class TestPerformance(unittest.TestCase):
    def test_tag_and_score_10k(self):
        acts = [{"ts": T0 + timedelta(seconds=10 * i), "source": "s", "producer": "p", "text": f"job {i} ok"}
                for i in range(10000)]
        ts = [a["ts"] for a in acts]
        preds = [(i, T0, T0 + timedelta(hours=6), "The fire memory stream will stay active.", "", 0.7, "correct")
                 for i in range(200)]
        t = time.monotonic()
        tags = [S.tag(p, acts, ts, []) for p in preds]
        S.score(preds * 50, tags * 50)
        self.assertLess(time.monotonic() - t, 10.0)


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            S.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    # RETRY GAP: run (PG reads via nova_charles._q) — a failed read is not retried; it fails open.
    def test_pg_reads_fail_open(self):
        cur = FakeCur(boom=True)
        with mock.patch.object(S.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            self.assertEqual(S.run(dry=False), {})


class TestUnit(unittest.TestCase):
    def test_n_class(self):
        self.assertEqual(S.n_class("Little Mister will reply by noon"), "little_mister")
        self.assertEqual(S.n_class("The LAPD scanner will be busy"), "neighbourhood")
        self.assertEqual(S.n_class("The bambu printer will finish"), "household")
        self.assertEqual(S.n_class("The scheduler will fail twice"), "fleet")
        self.assertEqual(S.n_class(""), "world")

    def test_needles(self):
        self.assertIn("fire", S.needles(PRED[3], CRIT))
        self.assertIn("automotive_rebuilds", S.needles("The automotive rebuilds memory stream will grow.", ""))
        self.assertIn("unifi-health", S.needles("The 'unifi-health' pings will stop.", ""))
        ac = '```check\n{"type": "autonomy_class_earned", "action_class": "observe:rebuild"}\n```'
        self.assertIn("rebuild", S.needles("I will earn it.", ac))
        self.assertEqual(S.needles("Rain tomorrow.", ""), set())

    def test_whole_word_match(self):
        acts = [{"text": "firewall rule"}, {"text": "Restarted fire_ingest"}]
        self.assertEqual(S.touching({"fire"}, acts), [acts[1]])
        self.assertEqual(S.touching(set(), acts), [])

    def test_disclosure_window_and_probe(self):
        texts = [(T0 - timedelta(hours=1), "watch_turnover", "#7 by 06:00"),
                 (T0 + timedelta(hours=1), "reach", "the fire memory stream will stay active. just so you know")]
        self.assertEqual(S.disclosure(7, PRED[3], T0, T0 + timedelta(hours=6), texts), "reach")
        self.assertIsNone(S.disclosure(7, "short", T0, T0 + timedelta(hours=6), texts[:1]))

    def test_tag_outside_window_is_untouched(self):
        late = [dict(ACTS[0], ts=T0 + timedelta(hours=9))]
        t = S.tag(PRED, late, [a["ts"] for a in late], [])
        self.assertIs(t["self_touched"], False)
        self.assertIsNone(S.tag((1, T0, T0, "Rain.", ""), ACTS, [ACTS[0]["ts"]], [])["self_touched"])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(S.selftest(), 0)


    def test_wrapper_delegates_to_charles(self):
        import nova_charles
        with mock.patch.object(nova_charles, "main", return_value=0) as cm, mock.patch("builtins.print") as pr:
            self.assertEqual(S.main(["--run", "--dry-run"]), 0)
            self.assertEqual(S.main(["--show"]), 0)
        self.assertEqual(cm.call_args_list[0].args[0], ["--forecasts"] + [] + ["--dry-run"])
        self.assertEqual(cm.call_args_list[1].args[0], ["--forecasts"] + [] + ["--show"])
        self.assertIn("merged into nova_charles.py --forecasts on 2026-10-09", pr.call_args_list[0].args[0])


class TestIntegration(unittest.TestCase):
    def test_shared_lookup_comes_from_charles(self):
        self.assertIs(S.own_actions, C.own_actions)
        self.assertIs(S._q, C._q)
        self.assertNotIn("def own_actions", SRC)

    def test_brier_is_soft_certainty_arithmetic(self):
        tags = [S.tag(p, ACTS, [ACTS[0]["ts"]], []) for p in (PRED, PRED2)]
        b = S.score([PRED, PRED2], tags)
        self.assertEqual(b["total"], SC.brier_stats([(0.8, 1.0), (0.6, 0.0)], min_n=1))
        self.assertEqual(b["self_touched"]["n"], 1)
        self.assertEqual(b["clean"]["n"], 1)

    def test_check_block_parsed_by_predictions(self):
        self.assertIn("from nova_predictions import extract_check", SRC)
        self.assertEqual(S.SERVICE, "seldon_axioms")


    def test_run_is_reached_through_charles(self):
        with mock.patch.object(S, "run", return_value={}) as r, mock.patch("builtins.print"):
            S.main(["--run"])
        r.assert_called_once()                      # wrapper -> nova_charles --forecasts -> this module's run()


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes):
        cur = FakeCur(routes)
        with mock.patch.object(S.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(S, "own_actions", return_value=ACTS) as oa, mock.patch("builtins.print"):
            res = S.run(dry=dry)
        return cur, res, oa

    ROUTES = {"FROM predictions": [PRED, PRED2],
              "FROM watch_turnover": [(T0 + timedelta(hours=2), "WATCH TURNOVER\n#8 by 06:00: horology")],
              "FROM reach_log": []}

    def test_run_tags_and_publishes(self):
        cur, res, oa = self._run(False, self.ROUTES)
        self.assertEqual(oa.call_args[0][1:], (T0, T0 + timedelta(hours=6)))
        ins = [p for s, p in cur.sql if "INSERT INTO prediction_axioms" in s]
        self.assertEqual([(p[0], p[2], p[4]) for p in ins], [(7, False, True), (8, True, False)])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS prediction_axioms" in s for s, _ in cur.sql))
        cfg = [p for s, p in cur.sql if "INSERT INTO service_config" in s]
        self.assertEqual(cfg[0][:2], ("seldon_axioms", "latest"))
        self.assertEqual(res["total"]["n"], 2)

    def test_dry_run_writes_nothing(self):
        cur, res, _ = self._run(True, self.ROUTES)
        self.assertEqual(res["disclosed"]["n"], 1)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER")))

    def test_no_predictions_writes_nothing(self):
        cur, res, oa = self._run(False, {})
        self.assertEqual(res, {})
        oa.assert_not_called()
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_seldon_axioms.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_seldon_axioms.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
