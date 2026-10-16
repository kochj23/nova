"""nova_rater.py — 7 house categories (Security, Performance, Retry, Unit, Integration, Functional, Frame).
PG is always faked. Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_rater as nr  # noqa: E402

SRC = (SCRIPTS / "nova_rater.py").read_text()


class FakeCur:
    """Answers by substring of the SQL; records every statement."""
    def __init__(self, answers=None, missing=(), boom=()):
        self.sql, self.answers, self.missing, self.boom, self._last = [], answers or {}, set(missing), set(boom), ""

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last, self._params = sql, params
        for b in self.boom:
            if b in sql:
                raise RuntimeError(f"boom {b}")

    def fetchone(self):
        if "to_regclass" in self._last:
            return (self._params[0] not in self.missing,)
        for k, v in self.answers.items():
            if k in self._last:
                return v[0] if isinstance(v, list) and v and isinstance(v[0], tuple) and "DISTINCT ON" not in self._last \
                    and "class, ratio" not in self._last else v
        return None

    def fetchall(self):
        for k, v in self.answers.items():
            if k in self._last:
                return v
        return []


FULL = {
    "DISTINCT ON (name)": [("chat", 0.999, 0.99), ("web", 0.98, 0.995)],
    "WHERE name = %s": (0.97, 0.99),
    "FROM relationship_ledger": (2, 0),
    "FROM growth_commitments": (2, 1),
    "class, ratio": [("organs", 0.74), ("beliefs", 0.0)],
    "~* %s), count(*) FROM gateway_traces": (10, 20),
    "FROM soft_certainty_state": (206, 0.57, 0.48, 0.1),
    "FROM service_config": ({"text": "he asked something of me 15 time(s); by my own replies I was there for 6 of them."},),
    "FROM guard_decisions": (1, 8),
    "percentile_cont": (5800.0, 8000.0, 40),
    "FROM analytics_pageviews": (100, 7),
    "FROM telemetry.events": (700,),
}


class TestSecurity(unittest.TestCase):
    def test_sql_parameterised_and_read_only(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertNotRegex(SRC, r"\b(INSERT|UPDATE|DELETE|DROP|ALTER)\b\s")

    def test_jordan_parser_ignores_injection_and_huge_input(self):
        self.assertIsNone(nr.parse_jordan_scores("R6'; DROP TABLE x;-- A5 T4 E6"))
        self.assertIsNone(nr.parse_jordan_scores("R99 A5 T4 E6 S3"))
        self.assertIsNone(nr.parse_jordan_scores(None))

    def test_no_hardcoded_hosts(self):
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn("/Users/", SRC)


class TestPerformance(unittest.TestCase):
    def test_parse_and_score_fast(self):
        t = time.perf_counter()
        for _ in range(20_000):
            nr.parse_jordan_scores("R6 A5 T4 E6 S3")
            nr.score({"latency": {"value": 0.5}, "unanswered": {"value": 0.9}})
        nr.parse_jordan_scores("R6 " * 100_000)
        self.assertLess(time.perf_counter() - t, 3.0)


class TestRetry(unittest.TestCase):
    """No retries by design: one unreadable source degrades to 'not instrumented' and the card still forms."""
    def test_one_broken_source_does_not_sink_the_card(self):
        res = nr.assess(FakeCur(FULL, boom=("FROM soft_certainty_state",)), 7)
        self.assertEqual(res["indicators"]["calibration"]["status"], nr.NI)
        self.assertIn("unreadable", res["indicators"]["calibration"]["why"])
        self.assertIsNotNone(res["scores"]["R"])

    def test_rollback_attempted_on_failure(self):
        class C(FakeCur):
            rolled = 0
            class connection:  # noqa: N801
                @staticmethod
                def rollback():
                    C.rolled += 1
        nr.assess(C(FULL, boom=("FROM guard_decisions",)), 7)
        self.assertEqual(C.rolled, 1)


class TestUnit(unittest.TestCase):
    def test_parse_jordan_scores(self):
        self.assertEqual(nr.parse_jordan_scores("s3 e6 t4 a5 r6 thanks"), {"R": 6, "A": 5, "T": 4, "E": 6, "S": 3})
        self.assertEqual(nr.parse_jordan_scores("R:10 A=5 T 4 E6 S3")["R"], 10)
        self.assertIsNone(nr.parse_jordan_scores("R6 A5 T4 E6"))

    def test_components_and_score(self):
        self.assertEqual(nr.lat_component(10_000), 1.0)
        self.assertAlmostEqual(nr.lat_component(65_000), 0.5)
        self.assertEqual(nr.notif_component(500), 0.0)
        s = nr.score({"slo_attainment": {"value": 0.5}, "promises_kept": {"status": nr.NO_DATA}})
        self.assertEqual(s["R"], round(1 + 9 * 0.5))
        self.assertIsNone(s["T"])

    def test_render_line(self):
        self.assertEqual(nr.render_line({"R": 7, "A": None}), "R7 A? T? E? S?")


class TestIntegration(unittest.TestCase):
    def test_full_card_from_fake_tables(self):
        res = nr.assess(FakeCur(FULL), 7)
        ind = res["indicators"]
        self.assertEqual(ind["slo_attainment"]["met"], 1)
        self.assertEqual(ind["promises_kept"]["kept"], 4)
        self.assertEqual(ind["weight_of_presence"]["value"], 0.4)
        self.assertEqual(ind["latency"]["p95_ms"], 8000)
        self.assertEqual(ind["notification_volume"]["per_day"], 100.0)
        self.assertEqual(set(res["scores"]), set("RATES"))
        self.assertTrue(all(v is not None for v in res["scores"].values()))

    def test_missing_tables_are_not_instrumented(self):
        res = nr.assess(FakeCur({}, missing={"public.slo_status", "public.gateway_traces", "telemetry.events"}), 7)
        for k in ("slo_attainment", "slo_chat_jordan", "latency", "unanswered", "cited_reply_share",
                  "notification_volume"):
            self.assertEqual(res["indicators"][k]["status"], nr.NI, k)
        self.assertIn("latency", res["not_instrumented"])
        self.assertIsNone(res["scores"]["T"])


class TestFunctional(unittest.TestCase):
    def test_chat_jordan_absent_reads_not_instrumented(self):
        a = dict(FULL)
        a.pop("WHERE name = %s")
        res = nr.assess(FakeCur(a), 7)
        self.assertEqual(res["indicators"]["slo_chat_jordan"]["status"], nr.NI)

    def test_window_is_passed_to_sql(self):
        cur = FakeCur(FULL)
        nr.assess(cur, 30)
        self.assertTrue(any(p and 30 in p for _, p in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_and_help(self):
        for args in (["--selftest"], ["--help"]):
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_rater.py"), *args], capture_output=True,
                               text=True, timeout=30, cwd=SCRIPTS, env=dict(os.environ))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_header_names_owner_and_purpose(self):
        self.assertRegex(SRC[:3000], r"Owner: Nova")
        self.assertTrue(re.search(r"RATER", SRC[:500]))


if __name__ == "__main__":
    unittest.main()
