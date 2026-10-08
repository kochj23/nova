#!/usr/bin/env python3
"""7-category gap tests for nova_pursue_skill.py — the skills added 2026-10-07/08 (documentary #143,
aviation-ref #148, crime-drama #149) and the PG connect retry added here. Memory server, LLM and PG
are mocked. Base suite: test_nova_pursue_skill.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_pursue_skill_7cat.py
"""
import importlib.util
import io
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pursue_skill.py"
spec = importlib.util.spec_from_file_location("ps_7cat", SCRIPT)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)
SRC = SCRIPT.read_text()
NEW = {"pursue-interest-documentary": ("documentary", "documentary"),
       "pursue-interest-aviation-ref": ("aviation ref", "aviation_ref"),
       "pursue-interest-crime-drama": ("crime drama", "crime_drama")}


class _Quiet(unittest.TestCase):
    def setUp(self):
        r = redirect_stdout(io.StringIO()); r.__enter__(); self.addCleanup(r.__exit__, None, None, None)
        p = mock.patch.object(ps.time, "sleep"); self.sleep = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Quiet):
    def test_new_skills_stay_off_the_web(self):
        for slug in NEW:
            self.assertFalse(ps.SKILLS[slug]["web"], slug)

    def test_new_skills_read_only_their_own_vector(self):
        for slug, (_, src) in NEW.items():
            self.assertEqual(ps.SKILLS[slug]["sources"], [src])

    def test_unknown_or_hostile_slug_refused_before_db(self):
        with mock.patch.object(ps, "_pg_connect") as c:
            self.assertEqual(ps.run_skill("'; DROP --")["handled"], False)
        c.assert_not_called()

    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"password\s*=|sk-[A-Za-z0-9]{20}|/Users/[a-z]")


class TestPerformance(_Quiet):
    def test_connect_has_timeout_and_bounded_backoff(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("x")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            ps._pg_connect("dsn")
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 10)
        self.assertLessEqual(sum(a.args[0] for a in self.sleep.call_args_list), 10)

    def test_topic_lookup_fast(self):
        t = time.perf_counter()
        for _ in range(20000):
            ps.slug_for_topic("crime drama")
        self.assertLess(time.perf_counter() - t, 2.0)


class TestRetry(_Quiet):
    def test_pg_connect_retries_then_succeeds(self):
        conn = mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("failover"), conn]) as c:
            self.assertIs(ps._pg_connect("dsn"), conn)
        self.assertEqual(c.call_count, 2)
        self.sleep.assert_called_once_with(2.0)

    def test_pg_connect_gives_up_after_three(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            ps._pg_connect("dsn")
        self.assertEqual(c.call_count, 3)

    def test_non_operational_error_not_retried(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.ProgrammingError("bad dsn")) as c, \
                self.assertRaises(psycopg2.ProgrammingError):
            ps._pg_connect("dsn")
        self.assertEqual(c.call_count, 1)

    def test_run_skill_uses_retrying_connect(self):
        cur = mock.MagicMock(); cur.fetchone.return_value = None
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch.object(ps, "_pg_connect", return_value=conn) as c, \
                mock.patch.object(ps, "load_card", return_value=None):
            ps.run_skill("pursue-interest-crime-drama")
        self.assertEqual([a.args[0] for a in c.call_args_list], [ps.OPS_DSN, ps.MEM_DSN])


class TestUnit(_Quiet):
    def test_new_topics_map_to_their_skill(self):
        for slug, (topic, src) in NEW.items():
            self.assertEqual(ps.slug_for_topic(topic), slug)
            self.assertEqual(ps.slug_for_topic(topic.upper() + "  "), slug)
            self.assertEqual(ps.slug_for_source(src), slug)

    def test_crime_drama_looks_back_a_week(self):
        self.assertEqual(ps.SKILLS["pursue-interest-crime-drama"]["recent_days"], 7)

    def test_topics_unique_across_skills(self):
        topics = [c["topic"] for c in ps.SKILLS.values() if c.get("topic")]
        self.assertEqual(len(topics), len(set(topics)))


class TestIntegration(_Quiet):
    def test_build_query_uses_hint_for_new_skill(self):
        q = ps.build_query(ps.SKILLS["pursue-interest-aviation-ref"], None)
        self.assertIn("aviation", q.lower())

    def test_unapproved_card_never_runs(self):
        with mock.patch.object(ps, "load_card", return_value={"status": "proposed"}):
            r = ps.run_skill("pursue-interest-aviation-ref", oc=mock.MagicMock(), mc=mock.MagicMock())
        self.assertEqual(r, {"handled": False, "why": "status proposed"})


class TestFunctional(_Quiet):
    def test_selftest_passes_with_new_skills(self):
        self.assertEqual(ps.selftest(), 0)

    def test_pg_down_surfaces_error(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")), \
                self.assertRaises(psycopg2.OperationalError):
            ps.run_skill("pursue-interest-documentary")


class TestFrame(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_entrypoints(self):
        for n in ("main", "run_skill", "_pg_connect", "selftest"):
            self.assertTrue(callable(getattr(ps, n)))


if __name__ == "__main__":
    unittest.main()
