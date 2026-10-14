#!/usr/bin/env python3
"""Tests for nova_prr.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prr = _load("nova_prr_under_test", SCRIPTS / "nova_prr.py")
SRC = (SCRIPTS / "nova_prr.py").read_text()

YAML_SIX = """
tasks:
  alpha:
    script: nova_alpha.py
    schedule: every 5m
  beta:
    script: /abs/path/nova_beta.py
    schedule: cron 0 3 * * 0
  gone:
    script: nova_gone.py
    schedule: every 1h
    enabled: false
"""
YAML_CORE = """
tasks:
  alpha:
    script: nova_alpha.py
    schedule: every 5m
  gamma:
    script: nova_gamma.sh
    schedule: cron 5 * * * *
"""
GOOD_PY = '"""nova_alpha.py — does alpha things.\n\nWritten by Jordan Koch (via Claude)."""\nfrom nova_notify import notify\n'
SLACKY_PY = '"""nova_x.py — x.\n\nWritten by Jordan Koch."""\nimport nova_config\nnova_config.post_both("hi")\n'


def _ctx(now_ms=None, **over):
    now_ms = now_ms or int(time.time() * 1000)
    ctx = {"now_ms": now_ms, "docs": [("scripts", "nova_alpha.py runs every 5m")],
           "task_stats": {"alpha": {"last_success_ms": now_ms - 60_000, "fails_7d": 1, "total_7d": 100,
                                    "fails_long": 1, "total_long": 100, "last_status": "success", "last_cfs": 0}},
           "svc_stats": {}, "slo_rows": None, "slo_cols": [], "waivers": {},
           "test_names": ["test_nova_alpha.py"], "sources": {"nova_alpha.py": GOOD_PY}}
    ctx.update(over)
    return ctx


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"xox[bpa]-[0-9A-Za-z]{6,}")
        self.assertNotRegex(SRC, r"(?i)password\s*=\s*['\"]")

    def test_sql_is_parameterized(self):
        # No f-string SQL anywhere in the module.
        self.assertFalse(re.search(r'execute\(\s*f["\']', SRC))

    def test_evidence_masks_token_like_strings(self):
        hits = prr.direct_slack_hits('x = "xoxb-123-abc"; SLACK_TOKEN = 1\n')
        self.assertTrue(hits)
        self.assertNotIn("xoxb-123-abc", hits[0][1])

    def test_delete_is_scoped(self):
        for m in re.finditer(r"DELETE FROM[^\"]*", SRC):
            self.assertIn("WHERE", m.group(0))
        self.assertNotIn("TRUNCATE", SRC)


class TestPerformance(unittest.TestCase):
    def test_summarize_10k_components_fast(self):
        res = {c: (True, "ok") for c in prr.CHECKS}
        evals = [({"kind": "task", "name": f"t{i}"}, dict(res, docs=(i % 3 == 0, "x"))) for i in range(10_000)]
        t = time.time()
        s = prr.summarize(evals)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(s["components"], 10_000)
        self.assertLessEqual(len(s["worst"]), 10)

    def test_slack_grep_large_source_fast(self):
        src = "x = 1\n" * 20_000 + "post_both('a')\n"
        t = time.time()
        self.assertEqual(len(prr.direct_slack_hits(src)), 1)
        self.assertLess(time.time() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_connect_uses_nova_dsn_retrying_helper(self):
        # nova_dsn.pg_connect retries with backoff; nova_prr must go through it.
        fake = MagicMock()
        fake.pg_connect.return_value = "conn"
        with patch.dict(sys.modules, {"nova_dsn": fake}):
            self.assertEqual(prr.connect(), "conn")
        fake.pg_connect.assert_called_once_with("nova_ops")

    def test_core_yaml_ssh_failure_fails_open(self):
        # RETRY GAP: read_core_yaml -- single ssh attempt; proves it fails open with a note.
        with patch.object(prr, "CORE_YAML_LOCAL", Path("/nonexistent/scheduler-core.yaml")), \
                patch.object(prr.subprocess, "run", side_effect=OSError("no ssh")):
            text, note = prr.read_core_yaml()
        self.assertIsNone(text)
        self.assertIn("skipped", note)


class TestUnit(unittest.TestCase):
    def test_parse_interval(self):
        self.assertEqual(prr.parse_interval("every 5m"), 300)
        self.assertEqual(prr.parse_interval("every 4h"), 14400)
        self.assertEqual(prr.parse_interval("every 30s"), 30)
        self.assertEqual(prr.parse_interval("bogus"), 0)

    def test_cron_period(self):
        self.assertEqual(prr.cron_period_s("5 * * * *"), 3600)
        self.assertEqual(prr.cron_period_s("*/15 * * * *"), 900)
        self.assertEqual(prr.cron_period_s("0 3 * * 0"), 7 * 86400)
        self.assertEqual(prr.cron_period_s("0 3 1 * *"), 31 * 86400)
        self.assertEqual(prr.cron_period_s("0 */6 * * *"), 6 * 3600)
        self.assertEqual(prr.cron_period_s("0 3 * * *"), 86400)
        self.assertEqual(prr.cron_period_s("garbage"), 86400)

    def test_tasks_from_yaml_skips_disabled_and_basenames(self):
        t = prr.tasks_from_yaml(YAML_SIX, ".6")
        self.assertEqual(set(t), {"alpha", "beta"})
        self.assertEqual(t["beta"]["script"], "nova_beta.py")

    def test_merge_tasks_unions_hosts(self):
        m = prr.merge_tasks(prr.tasks_from_yaml(YAML_SIX, ".6"), prr.tasks_from_yaml(YAML_CORE, "core"))
        self.assertEqual(m["alpha"]["hosts"], [".6", "core"])
        self.assertEqual(m["gamma"]["hosts"], ["core"])

    def test_check_tests(self):
        names = ["test_nova_alpha.py", "test_beta_7cat.py", "test_nova_alphabet.py"]
        self.assertTrue(prr.check_tests("nova_alpha.py", names)[0])
        self.assertNotIn("alphabet", prr.check_tests("nova_alpha.py", names)[1])
        self.assertTrue(prr.check_tests("nova_beta.py", names)[0])
        self.assertFalse(prr.check_tests("nova_zeta.py", names)[0])
        self.assertIsNone(prr.check_tests(None, names)[0])

    def test_check_docs_word_boundary(self):
        docs = [("scripts", "nova_hue_history.py writes hue state")]
        self.assertFalse(prr.check_docs(["nova_hue"], docs)[0])
        self.assertTrue(prr.check_docs(["nova_hue_history"], docs)[0])

    def test_check_header(self):
        self.assertTrue(prr.check_header("nova_alpha.py", GOOD_PY)[0])
        self.assertFalse(prr.check_header("x.py", '"""x.py — does a thing, no attribution."""\n')[0])
        self.assertFalse(prr.check_header("x.py", "import os\n")[0])
        self.assertFalse(prr.check_header("x.py", None)[0])
        sh = "#!/bin/bash\n# nova_x.sh — backs up stuff nightly\n# Written by Jordan Koch.\nset -e\n"
        self.assertTrue(prr.check_header("nova_x.sh", sh)[0])

    def test_check_alerts(self):
        self.assertTrue(prr.check_alerts("nova_alpha.py", GOOD_PY)[0])
        self.assertFalse(prr.check_alerts("nova_x.py", SLACKY_PY)[0])
        self.assertTrue(prr.check_alerts("nova_config.py", SLACKY_PY)[0])
        # Mentions in comments and docstrings are prose, not posting.
        self.assertTrue(prr.check_alerts("a.py", '"""Never use post_both()."""\n# SLACK_TOKEN\nx=1\n')[0])

    def test_task_health_and_chronic(self):
        now = 10_000_000_000
        self.assertTrue(prr.check_task_health({"last_success_ms": now - 600_000}, 300, now)[0])
        self.assertFalse(prr.check_task_health({"last_success_ms": now - 3 * 3600_000}, 300, now)[0])
        self.assertFalse(prr.check_task_health(None, 300, now)[0])
        self.assertTrue(prr.check_task_chronic({"fails_7d": 1, "total_7d": 10}, 300)[0])
        self.assertFalse(prr.check_task_chronic({"fails_7d": 5, "total_7d": 10}, 300)[0])
        self.assertIsNone(prr.check_task_chronic({}, 300)[0])
        dead = prr.check_task_chronic({"last_status": "failure", "last_cfs": 9, "fails_7d": 0, "total_7d": 50}, 300)
        self.assertFalse(dead[0])
        self.assertIn("dead-lettered", dead[1])

    def test_service_checks(self):
        now = 1_000_000
        self.assertTrue(prr.check_service_health({"last_ok_s": now - 60}, now)[0])
        self.assertFalse(prr.check_service_health({"last_ok_s": now - 7200}, now)[0])
        self.assertTrue(prr.check_service_health({"reg_status": "up", "reg_hb_s": now - 10}, now)[0])
        self.assertFalse(prr.check_service_chronic({"total_7d": 100, "down_7d": 10})[0])
        self.assertIsNone(prr.check_service_chronic({})[0])

    def test_slo_absent_is_na_present_matches(self):
        self.assertIsNone(prr.check_slo(None, [], ["alpha"])[0])
        rows = [{"name": "chat-latency", "components": ["alpha", "gateway"]}]
        self.assertTrue(prr.check_slo(rows, ["name", "components"], ["alpha"])[0])
        self.assertFalse(prr.check_slo(rows, ["name", "components"], ["beta"])[0])
        # Live schema: components text[] plus a numeric target column; name variants and disabled rows.
        live = [{"name": "memory_recall", "target": 0.995, "components": ["nova_memory_server", "memory-server"],
                 "enabled": True},
                {"name": "old", "target": 0.9, "components": ["beta"], "enabled": False}]
        cols = ["name", "target", "components", "enabled"]
        self.assertEqual(prr.slo_cover_columns(cols), ["components"])
        self.assertTrue(prr.check_slo(live, cols, ["memory_server"])[0])
        self.assertFalse(prr.check_slo(live, cols, ["beta"])[0])

    def test_waiver_turns_fail_into_pass(self):
        r = prr.apply_waiver((False, "no SLO"), "batch helper, no user impact")
        self.assertTrue(r[0])
        self.assertIn("waived", r[1])
        self.assertEqual(prr.apply_waiver((False, "x"), None), (False, "x"))

    def test_service_script_mapping(self):
        names = {"nova_big_brother.py", "memory_server.py"}
        self.assertEqual(prr.service_script("big_brother", names), "nova_big_brother.py")
        self.assertEqual(prr.service_script("memory_server", names), "memory_server.py")
        self.assertIsNone(prr.service_script("plex", names))

    def test_score(self):
        self.assertEqual(prr.score({"a": (True, ""), "b": (False, ""), "c": (None, "")}), (1, 1, 1, 50.0))
        self.assertEqual(prr.score({"a": (None, "")})[3], None)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_helpers(self):
        self.assertIn("nova_dsn", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotRegex(SRC, r"import\s+nova_config")

    def test_tables_and_view_in_schema(self):
        for t in ("prr_results", "prr_waivers", "prr_history", "VIEW prr_scores"):
            self.assertIn(t, prr.SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS", prr.SCHEMA)

    def test_evaluate_then_summarize_then_digest(self):
        ctx = _ctx()
        comps = [{"kind": "task", "name": "alpha", "script": "nova_alpha.py", "schedule": "every 5m", "hosts": [".6"]},
                 {"kind": "service", "name": "plex", "script": None, "schedule": "", "hosts": []}]
        evals = [(c, prr.evaluate_component(c, ctx)) for c in comps]
        self.assertEqual(set(evals[0][1]), set(prr.CHECKS))
        self.assertEqual(prr.score(evals[0][1])[1], 0)          # alpha: nothing fails
        s = prr.summarize(evals)
        title, body = prr.digest_text(s, prev_pct=50.0)
        self.assertIn("% production-ready", title)
        self.assertIn("last week 50.0%", title)
        self.assertIn("plex", body)
        self.assertIn("--component", body)


class TestFunctional(unittest.TestCase):
    def _run(self, argv, ctx):
        comps = [{"kind": "task", "name": "alpha", "script": "nova_alpha.py", "schedule": "every 5m", "hosts": [".6"]}]
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.fetchone.return_value = None
        out = io.StringIO()
        with patch.object(prr, "inventory", return_value=(comps, ["note"], {"nova_alpha.py"})), \
                patch.object(prr, "connect", return_value=conn), \
                patch.object(prr, "build_ctx", side_effect=lambda *a, **k: dict(ctx)), \
                patch.object(prr, "load_sources", side_effect=lambda c, x: None), \
                patch.object(prr, "store") as store, \
                patch.dict(sys.modules, {"nova_notify": MagicMock()}), \
                redirect_stdout(out):
            rc = prr.main(argv)
        return rc, out.getvalue(), store, None, conn

    def test_component_gate_passes(self):
        rc, out, store, _, _ = self._run(["--component", "alpha"], _ctx())
        self.assertEqual(rc, 0)
        self.assertIn("[PASS] tests", out)
        store.assert_not_called()

    def test_component_gate_fails_and_unknown(self):
        ctx = _ctx(test_names=[])
        rc, out, _, _, _ = self._run(["--component", "nova_alpha.py"], ctx)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] tests", out)
        rc, out, _, _, _ = self._run(["--component", "nope"], _ctx())
        self.assertEqual(rc, 2)

    def test_dry_run_writes_nothing(self):
        rc, out, store, _, conn = self._run(["--dry-run"], _ctx())
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        store.assert_not_called()
        conn.commit.assert_not_called()

    def test_digest_run_stores_and_notifies(self):
        notify_mod = MagicMock()
        comps = [{"kind": "task", "name": "alpha", "script": "nova_alpha.py", "schedule": "every 5m", "hosts": [".6"]}]
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        with patch.object(prr, "inventory", return_value=(comps, [], {"nova_alpha.py"})), \
                patch.object(prr, "connect", return_value=conn), \
                patch.object(prr, "build_ctx", side_effect=lambda *a, **k: _ctx()), \
                patch.object(prr, "load_sources", side_effect=lambda c, x: None), \
                patch.object(prr, "store") as store, \
                patch.dict(sys.modules, {"nova_notify": notify_mod}), redirect_stdout(io.StringIO()):
            self.assertEqual(prr.main(["--digest"]), 0)
        store.assert_called_once()
        conn.commit.assert_called_once()
        kw = notify_mod.notify.call_args.kwargs
        self.assertEqual(kw["level"], "info")
        self.assertEqual(kw["category"], "digest")
        self.assertTrue(kw["dedup_key"].startswith("prr-weekly-"))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_prr.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("--component", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertTrue(callable(prr.main))
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()


def test_scheduled_jobs_slo_covers_every_task():
    rows = [{"name": "scheduled_jobs", "components": ["nova_scheduler", "scheduler:*"], "enabled": True}]
    assert prr.check_slo(rows, ["name", "components", "enabled"], ["some_task"], "task")[0] is True
    assert prr.check_slo(rows, ["name", "components", "enabled"], ["some_service"], "service")[0] is False
