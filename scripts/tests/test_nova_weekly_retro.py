#!/usr/bin/env python3
"""Tests for nova_weekly_retro.py + the nova_hotwash.py --weekly-retro hook (plan #57)."""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_weekly_retro as r  # noqa: E402
import nova_hotwash  # noqa: E402


def _m(**now):
    base = dict(throughput=10, inflow=5, pages_per_day=3.0, page_precision=0.8, change_failures=0, changes=10,
                change_failure_rate=0.0, toil_minutes=0.0, wip=1, queued=4, oldest_open_days=3, lead_p85_days=2.0,
                top_problems=[], top_pager=None, top_toil=None, top_failing_change=None)
    base.update(now)
    prev = dict(pages_per_day=3.0, page_precision=0.8, change_failure_rate=0.0, toil_minutes=0.0)
    return {"now": base, "prev": prev}


class TestDecide(unittest.TestCase):
    def test_healthy_week_has_no_tries(self):
        d = r.decide(_m())
        self.assertEqual(d["try"], [])
        self.assertIn("queue flow 10 done vs 5 new", d["keep"])

    def test_noisy_week_proposes_actions_ranked(self):
        d = r.decide(_m(pages_per_day=80.0, top_pager=("traffic_watch", 200), page_precision=0.4,
                        top_problems=[("core:scheduler", 60)], throughput=5, inflow=9))
        titles = [t["title"] for t in d["try"]]
        self.assertEqual(titles[0], "close recurring problem core:scheduler")
        self.assertIn("cut pages from traffic_watch", titles)
        self.assertIn("raise page precision", titles)
        self.assertIn("queue flow 5 done vs 9 new", d["stop"])

    def test_toil_on_the_top_problem_merges_into_one_action(self):
        d = r.decide(_m(top_problems=[("core:scheduler", 60)], top_toil=("incident:scheduler", 900.0)))
        titles = [t["title"] for t in d["try"]]
        self.assertEqual(titles, ["close recurring problem core:scheduler"])
        self.assertIn("900 human minutes", d["try"][0]["why"])

    def test_regressions_land_in_stop(self):
        m = _m(pages_per_day=9.0)
        d = r.decide(m)
        self.assertTrue(any(s.startswith("pages/day worsened") for s in d["stop"]))

    def test_none_metrics_do_not_crash(self):
        m = _m(pages_per_day=None, page_precision=None, change_failure_rate=None, throughput=None, inflow=None)
        r.decide(m)
        self.assertEqual(len(r.summary_lines(m, r.decide(m), [], 0, 0)), 3)


class TestSummary(unittest.TestCase):
    def test_three_lines_with_last_week_status(self):
        lines = r.summary_lines(_m(top_problems=[("k", 3)]), {}, [({"title": "x"}, 12)], 2, 3)
        self.assertEqual(len(lines), 3)
        self.assertIn("x (#12)", lines[2])
        self.assertIn("Last week's actions: 2/3 done", lines[2])

    def test_week_key(self):
        self.assertEqual(r.week_key(date(2026, 10, 15)), "2026-W42")
        self.assertLess(r.week_key(date(2026, 12, 28)), r.week_key(date(2027, 1, 4)))


class TestRun(unittest.TestCase):
    def _run(self, prev_rows, done, total, tries, dry=False):
        cur = MagicMock()
        cur.fetchone.return_value = (101,)
        with patch.object(r, "gather", return_value=_m()), \
             patch.object(r, "decide", return_value={"keep": [], "stop": [], "try": tries}), \
             patch.object(r, "last_week_status", return_value=(prev_rows, done, total)), \
             patch.object(r, "_file", side_effect=lambda c, desc, ctx, status="queued", priority=3: (desc, status)) as f, \
             patch("nova_pg_schema.safe_ddl"):
            out = r.run(cur, dry=dry, today=date(2026, 10, 15))
        return out, f, cur

    def test_first_item_is_last_weeks_status_and_cap_is_three(self):
        tries = [dict(score=9 - i, title=f"t{i}", why="w") for i in range(5)]
        out, f, cur = self._run([(1, "done", "a"), (2, "queued", "b")], 1, 2, tries)
        descs = [c.args[1] for c in f.call_args_list]
        self.assertEqual(len(descs), 3)
        self.assertEqual(descs[0], "Weekly retro 2026-W42: status of last week's actions")
        self.assertEqual(f.call_args_list[0].kwargs["status"], "queued")
        self.assertEqual(descs[1:], ["Weekly retro 2026-W42: t0", "Weekly retro 2026-W42: t1"])

    def test_all_done_status_item_is_filed_done(self):
        out, f, cur = self._run([(1, "done", "a")], 1, 1, [])
        self.assertEqual(f.call_args_list[0].kwargs["status"], "done")

    def test_no_previous_retro_means_three_tries(self):
        tries = [dict(score=1, title=f"t{i}", why="w") for i in range(4)]
        out, f, cur = self._run([], 0, 0, tries)
        self.assertEqual(len(f.call_args_list), 3)

    def test_publishes_latest_for_the_digest(self):
        out, f, cur = self._run([], 0, 0, [])
        sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
        self.assertIn("'weekly_retro','latest'", sql)
        self.assertIn("INSERT INTO weekly_retro", sql)

    def test_dry_run_writes_nothing(self):
        out, f, cur = self._run([(1, "queued", "a")], 0, 1, [dict(score=1, title="t", why="w")], dry=True)
        f.assert_not_called()
        cur.execute.assert_not_called()

    def test_file_dedupes_on_description(self):
        cur = MagicMock()
        cur.fetchone.return_value = (77,)
        self.assertEqual(r._file(cur, "Weekly retro 2026-W42: x", "c"), 77)
        self.assertEqual(cur.execute.call_count, 1)          # only the SELECT


class TestHotwashHook(unittest.TestCase):
    def test_flag_routes_to_weekly_retro(self):
        fake = MagicMock(); fake.run.return_value = {"lines": ["a", "b", "c"], "keep": [], "stop": [], "try": [], "queue_ids": []}
        W = MagicMock()
        with patch.dict(sys.modules, {"nova_weekly_retro": fake, "nova_watch_common": W}):
            self.assertEqual(nova_hotwash.main(["--weekly-retro", "--dry-run"]), 0)
        fake.run.assert_called_once()
        self.assertTrue(fake.run.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
