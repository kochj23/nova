#!/usr/bin/env python3
"""Tests for nova_turing_scoreboard.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

PG (both DBs), the Ollama judge, the memory server and Slack are mocked for the whole file."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_turing_scoreboard.py"
SRC = PATH.read_text()

import nova_restraint  # noqa: E402 — import-clean; harvest patched in main() tests


def _load():
    spec = importlib.util.spec_from_file_location("turing_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load()
_PATCHES = []


def setUpModule():
    for p in (patch.object(ts.psycopg2, "connect", side_effect=AssertionError("unmocked PG")),
              patch.object(ts.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(ts.nova_config, "post_both", side_effect=AssertionError("unmocked Slack")),
              patch.object(nova_restraint, "harvest_proactive_drops", return_value={"harvested": 0})):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


class _Cur:
    """Routes by SQL substring: routes = [(needle, fetchone_value, fetchall_value)]."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._one = None; self._all = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._one, self._all = (0, 0, 0), []
        for needle, one, allrows in self.routes:
            if needle in sql:
                self._one, self._all = one, allrows
                return

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def inserts(self):
        return [p for s, p in self.sql if s.startswith("INSERT INTO turing_scoreboard")]


TURNS = [(1, "Jordan: which NAS?\nNova: the UNAS, like we set up in September."),
         (2, "Jordan: hi\nNova: hello"), (3, "system note without a reply"), (4, None)]


def _mc():
    return _Cur([("source='conversation'", (5, "Jordan asked Nova"), TURNS),
                 ("count(*), coalesce(avg(access_count)", (3, 1.5, 4.5), []),
                 ("metadata->>'topic' AS topic", None, [("tides", 3, 2), ("knots", 1, 1)]),
                 ("source=%s", (4,), []),
                 ("metadata->>'type'='quiet'", (1,), [])])


def _oc(prev=None):
    return _Cur([("FROM research_log", (2, 9), []),
                 ("FROM scheduler_runs", (10, 60_000, 1), []),
                 ("FROM pursuit_threads", None, []),
                 ("FROM preoccupations", None, [("tides", 3)]),
                 ("SELECT value FROM turing_scoreboard", (prev,) if prev is not None else None, [])])


def _main(argv, judge="APT"):
    mc, oc = _mc(), _oc()
    conns = [MagicMock(), MagicMock()]
    conns[0].cursor.return_value, conns[1].cursor.return_value = mc, oc
    with patch.object(ts.psycopg2, "connect", side_effect=conns), patch.object(ts, "llm", return_value=judge), \
            patch.object(ts, "_recall", side_effect=lambda q, n=8, timed=False: ([{"text": "UNAS primary", "source": "s"}], 42.0) if timed else [{"text": "UNAS primary", "source": "s"}]), \
            patch.object(ts.nova_config, "post_both") as post, patch.object(sys, "argv", ["x", *argv]), \
            redirect_stdout(io.StringIO()) as out:
        rc = ts.main()
    return rc, post, mc, oc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_dry_run_never_writes_or_posts(self):
        rc, post, mc, oc, out = _main(["--dry-run"])
        self.assertEqual(rc, 0)
        post.assert_not_called()
        self.assertEqual(oc.inserts(), [])
        self.assertIn("DRY RUN — would post report", out)

    def test_blinded_eval_anonymizes(self):
        mc = _Cur([("ORDER BY random()", (9, "Jordan said hi to Nova"), [])])
        with patch.object(ts.nova_config, "post_both") as post, patch.object(sys, "argv", ["x"]):
            self.assertEqual(ts.blinded_eval(mc, _Cur()), 0)
        msg = post.call_args.args[0]
        self.assertIn("USER said hi to ASSISTANT", msg)
        self.assertNotIn("Jordan", msg.split("```")[1])

    def test_recall_query_url_encoded(self):
        r = MagicMock(); r.__enter__.return_value = r; r.read.return_value = b'{"memories": []}'
        with patch.object(ts.urllib.request, "urlopen", return_value=r) as u, patch.object(ts.json, "load", return_value={"memories": []}):
            ts._recall("a&b=c d")
        self.assertIn("q=a%26b%3Dc%20d&", u.call_args.args[0])


class TestPerformance(unittest.TestCase):
    def test_trend_and_report_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            ts._trend(i, i - 1)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_llm_walks_node_pool(self):
        r = MagicMock(); r.__enter__.return_value = r
        r.read.return_value = json.dumps({"message": {"content": "APT"}}).encode()
        with patch.object(ts.urllib.request, "urlopen", side_effect=[OSError(), OSError(), r]) as u:
            self.assertEqual(ts.llm("p", system="s"), "APT")
        self.assertEqual(u.call_count, 3)
        with patch.object(ts.urllib.request, "urlopen", side_effect=OSError()) as u:
            self.assertEqual(ts.llm("p"), "")
        self.assertEqual(u.call_count, len(ts.OLLAMA_NODES))

    def test_recall_fails_open(self):
        # RETRY GAP: _recall() — one GET per probe; failure yields [] and a measured latency
        mems, dt = ts._recall("x", timed=True)
        self.assertEqual(mems, [])
        self.assertGreaterEqual(dt, 0.0)


class TestUnit(unittest.TestCase):
    def test_trend(self):
        self.assertEqual(ts._trend(None, 1), "—")
        self.assertEqual(ts._trend(2, 1), "▲ (was 1)")
        self.assertEqual(ts._trend(1, 2), "▼ (was 2)")
        self.assertEqual(ts._trend(1, 1), "= (was 1)")

    def test_callback_rate_only_judges_nova_turns(self):
        oc = _Cur()
        with patch.object(ts, "llm", side_effect=["APT", "none"]), patch.object(sys, "argv", ["x"]):
            rate, d = ts.metric_callback_rate(_mc(), oc)
        self.assertEqual((rate, d["turns_judged"], d["apt"]), (0.5, 2, 1))
        self.assertEqual(oc.inserts()[0][:2], ("unprompted_callback_rate", 0.5))

    def test_supersession_pass_fail(self):
        with patch.object(ts, "_recall", return_value=[]), patch.object(sys, "argv", ["x", "--dry-run"]):
            with patch.object(ts, "llm", return_value="unas"):
                self.assertEqual(ts.metric_supersession(_Cur())[0], 1.0)
            with patch.object(ts, "llm", return_value="SYNOLOGY"):
                self.assertEqual(ts.metric_supersession(_Cur())[0], 0.0)


class TestIntegration(unittest.TestCase):
    def test_elapsed_attention_and_quiet_wakes_compose(self):
        with patch.object(sys, "argv", ["x", "--dry-run"]):
            rate, d = ts.metric_elapsed_attention(_mc(), _oc())
            q, qd = ts.metric_quiet_wake_rate(_mc(), _oc())
            s, sd = ts.metric_pursuit_survival(_mc(), _oc())
        self.assertEqual((d["scheduled_wakes"], d["pursuits_landed"], d["fizzled_nothing_wakes"]), (10, 4, 2))
        self.assertEqual(rate, 0.6)
        self.assertEqual((qd["quiet_wakes"], q), (3, 0.3))
        self.assertEqual((sd["survived_across_wakes"], s), (1, 0.5))
        self.assertEqual(sd["preoccupation_ledger_recurring"], [{"topic": "tides", "returns": 3}])


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_metrics_and_posts_report(self):
        rc, post, mc, oc, out = _main([])
        self.assertEqual(rc, 0)
        metrics = [p[0] for p in oc.inserts()]
        self.assertEqual(metrics, ["unprompted_callback_rate", "recall_latency_ms", "supersession_correctness",
                                   "spark_and_research_landing", "elapsed_attention_preemption",
                                   "pursuit_survival", "quiet_wake_rate"])
        report = post.call_args.args[0]
        self.assertEqual(post.call_args.kwargs["slack_channel"], ts.REPORT_CHANNEL)
        self.assertIn("*100%*", report)
        self.assertIn("*42 ms*", report)
        self.assertIn("n too small", report)

    def test_report_handles_unreachable_memory_server(self):
        res = {"callback": (0.0, {"apt": 0, "turns_judged": 0}), "latency": (None, {}),
               "supersession": (0.0, {"judge_answer": ""}),
               "spark_research": (0, {"sparks_produced": 0, "research_memories": 0, "research_log_runs": 0,
                                      "landing_proxy_total_later_access": 0})}
        rep = ts.weekly_report(_oc(), res)
        self.assertIn("memory server unreachable", rep)
        self.assertIn("still ~0", rep)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_turing_scoreboard"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
