#!/usr/bin/env python3
"""Tests for nova_busab.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_busab as B  # noqa: E402

SRC = (SCRIPTS / "nova_busab.py").read_text()
W0 = date(2026, 10, 5)
DOC = "## Open items\n- Ivory Head: needs per-belief citations\n## 5. The Shine — BUILT DISABLED\n"


class FakeCur:
    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        s = str(sql)
        self.sql.append((s, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in s), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def writes(cur):
    return [s for s, _ in cur.sql if any(k in s for k in ("CREATE", "INSERT", "UPDATE", "DELETE"))]


def at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 18, tzinfo=timezone.utc)


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_never_blocks_or_posts(self):
        for forbidden in ("post_slack", "post_both", "notify(", "bootout", "launchctl", "kill"):
            self.assertNotIn(forbidden, SRC)
        self.assertIn("Little Mister decides", SRC)

    def test_doc_text_stays_a_parameter(self):
        cur = FakeCur()
        evil = "x'); DROP TABLE claude_queue; --"
        B.write(cur, B.assess({}, {}, {}, W0), [evil], [evil], None)
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO busab_weekly" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertTrue(any(evil in str(v) for v in ins[1]))


class TestPerformance(unittest.TestCase):
    def test_week_bucketing_and_scrape_10k(self):
        births = {f"nova_{i}.py": at(W0) - timedelta(hours=i) for i in range(10000)}
        doc = "## Open items\n" + "\n".join(f"- item {i}" for i in range(10000))
        t = time.monotonic()
        weeks = B.per_week(births)
        items = B.scrape_open("d", doc)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual((sum(weeks.values()), len(items)), (10000, 10000))


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg down")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            B.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    # RETRY GAP: chronic_per_week / half_deployed — single read, fail open to "nothing known".
    def test_query_failure_fails_open(self):
        with mock.patch("builtins.print"):
            self.assertEqual(B.chronic_per_week(FakeCur(boom=True), W0), {})
            self.assertEqual(B.half_deployed(FakeCur(boom=True)), [])
        self.assertFalse(B.assess({}, {}, {}, W0)["recommend"])   # nothing known -> no recommendation


class TestUnit(unittest.TestCase):
    def test_week_of(self):
        self.assertEqual(B.week_of(date(2026, 10, 11)), W0)                            # Sunday
        self.assertEqual(B.week_of(datetime(2026, 10, 12, 6, tzinfo=timezone.utc)), W0)  # still Sunday in LA
        self.assertEqual(B.week_of(datetime(2026, 10, 12, 8, tzinfo=timezone.utc)), date(2026, 10, 12))

    def test_assess_needs_both(self):
        prior = {W0 - timedelta(weeks=i): 3 for i in range(1, 9)}
        self.assertTrue(B.assess({**prior, W0: 9}, {}, {**prior, W0: 4}, W0)["recommend"])
        self.assertFalse(B.assess({**prior, W0: 3}, {}, {**prior, W0: 9}, W0)["recommend"])  # equal is not over
        self.assertFalse(B.assess({**prior, W0: 9}, {}, {**prior, W0: 3}, W0)["recommend"])

    def test_scrape(self):
        self.assertEqual(B.scrape_open("d", ""), [])
        self.assertEqual(B.scrape_open("d", DOC), ["d: Ivory Head: needs per-belief citations",
                                                   "d: 5. The Shine — BUILT DISABLED"])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(B.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_facts(self):
        import nova_chronic_failures as C
        import nova_yellow_eye as Y
        self.assertIs(B.scheduler_births, Y.scheduler_births)
        self.assertIs(B.script_births, Y.script_births)
        self.assertEqual((B.FAIL_PER_DAY, B.DAYS), (C.FAIL_PER_DAY, C.DAYS))
        self.assertIn("import nova_watch_common as W", SRC)

    def test_tables(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS busab_weekly", B.SCHEMA)
        self.assertIn("week_start date PRIMARY KEY", B.SCHEMA)
        self.assertIn("FROM scheduler_runs", SRC)
        self.assertIn("FROM agent_docs", SRC)

    def test_note_once_per_week(self):
        cur = FakeCur({"FROM claude_queue WHERE": [(3,)]})
        self.assertIsNone(B.file_note(cur, f"{B.QUEUE_PREFIX} (week of {W0}): x", "ctx"))
        self.assertEqual(writes(cur), [])


class TestFunctional(unittest.TestCase):
    def _run(self, dry, busy=True):
        prior = [W0 - timedelta(weeks=i) for i in range(1, 9)]
        scripts = {f"nova_old{i}.py": at(w) for i, w in enumerate(prior)}
        if busy:
            scripts.update({f"nova_new{i}.py": at(W0) for i in range(5)})
        chronic = [(w, 1) for w in prior] + [(W0, 4 if busy else 0)]
        cur = FakeCur({"FROM scheduler_runs": chronic, "FROM agent_docs": [("nova-lovecraft-organs", DOC)],
                       "FROM claude_queue WHERE": [], "RETURNING id": [(11,)]})
        with mock.patch.object(B, "script_births", return_value=scripts), \
                mock.patch.object(B, "scheduler_births", return_value={"new_task": at(W0)}), \
                mock.patch.object(B.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            a = B.run(dry=dry, week=W0)
        return cur, a

    def test_freeze_recommended_files_one_note(self):
        cur, a = self._run(False)
        self.assertTrue(a["recommend"])
        self.assertEqual((a["new_scripts"], a["new_entries"], a["chronic"]), (5, 1, 4))
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)
        self.assertIn("Little Mister decides", q[0][1])
        self.assertIn("nova_new0.py", q[0][2])
        self.assertIn("BUILT DISABLED", q[0][2])
        row = [p for s, p in cur.sql if "INSERT INTO busab_weekly" in s][0]
        self.assertEqual(row[-1], 11)

    def test_within_budget_writes_row_no_note(self):
        cur, a = self._run(False, busy=False)
        self.assertFalse(a["recommend"])
        self.assertFalse(any("claude_queue" in s for s in writes(cur)))
        self.assertTrue(any("INSERT INTO busab_weekly" in s for s in writes(cur)))

    def test_dry_run_writes_nothing(self):
        cur, a = self._run(True)
        self.assertTrue(a["recommend"])
        self.assertEqual(writes(cur), [])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_busab.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_busab.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
