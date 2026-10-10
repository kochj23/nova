#!/usr/bin/env python3
"""Tests for nova_dead_mans_switch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Since 2026-10-09 (organ-audit merge M1) the script is a thin wrapper around
nova_output_drift.py --deliveries; these tests drive the wrapper and, end to end, the check it
calls, with PG and the bus faked."""
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_dead_mans_switch.py"
SRC = SCRIPT.read_text()

import nova_output_drift as od  # noqa: E402  (the module the wrapper imports)


@contextlib.contextmanager
def _modules(**mods):
    """Set sys.modules keys for the block and restore ONLY those keys afterwards."""
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is missing:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dm = _load("dead_mans_switch_under_test", SCRIPT)


class _Cur:
    """Routes queries by SQL substring (first match wins); records every statement."""
    def __init__(self, routes):
        self.routes, self.sql, self._rows = list(routes), [], []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.sql.append((sql, params))
        for key, val in self.routes:
            if key in sql:
                if isinstance(val, Exception):
                    raise val
                self._rows = list(val)
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _routes(hour=20, rows=(("morning_brief", True, 3), ("mail_deliver_pm", False, 3)), already=()):
    return [("AT TIME ZONE %s)::int", [(hour, date(2026, 10, 8))]),
            ("FROM scheduler_runs WHERE task_id = ANY", list(rows)),
            ("dedup_key='dead-mans-switch-recovery'", list(already))]


def _run(argv=(), routes=None, connect=None):
    cur = _Cur(routes if routes is not None else _routes())
    conn = types.SimpleNamespace(cursor=lambda: cur, autocommit=False)
    notify_mod = types.ModuleType("nova_notify"); notify_mod.notify = MagicMock(return_value=True)
    out = io.StringIO()
    with patch.object(od.psycopg2, "connect", connect or MagicMock(return_value=conn)), \
         _modules(nova_notify=notify_mod), redirect_stdout(out):
        rc = dm.main(list(argv))
    return rc, cur, notify_mod.notify, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_wrapper_runs_nothing_and_calls_no_http_port(self):
        # the old switch re-ran scripts via subprocess and polled 127.0.0.1:37460; the wrapper does neither
        for frag in ("subprocess", "urlopen", "37460", "shell=True"):
            self.assertNotIn(frag, SRC)
        with patch("subprocess.run") as run:
            _run()
        run.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_delivery_decision_on_10k_rows(self):
        rows = [(f"t{i}", i % 2 == 0, 3) for i in range(10_000)] + [("morning_brief", False, 3)]
        t0 = time.perf_counter()
        missed, skipped = od.delivered(rows, 20, None)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(missed, [("morning_brief", "Morning Brief (7am)")])


class TestRetry(unittest.TestCase):
    def test_pg_down_fails_open(self):
        # RETRY GAP: nova_output_drift.main / psycopg2.connect — one attempt; no PG means no check, rc 0, no alert
        rc, _, notify, out = _run(connect=MagicMock(side_effect=OSError("no pg")))
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        self.assertIn("no PG", out)


class TestUnit(unittest.TestCase):
    def test_argv_maps_to_the_deliveries_mode(self):
        with patch.object(od, "main", return_value=0) as m, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dm.main([]), 0)
            dm.main(["--dry-run"])
        self.assertEqual([c.args[0] for c in m.call_args_list], [["--deliveries"], ["--deliveries", "--dry-run"]])
        self.assertIn("merged into nova_output_drift.py (--deliveries) on 2026-10-09", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_delivery_table_carried_over_and_read_from_scheduler_runs(self):
        self.assertEqual([(t, h) for t, h, _ in od.DELIVERIES],
                         [("morning_brief", 9), ("mail_deliver_am", 9), ("mail_deliver_pm", 19)])
        for script in ("nova_morning_brief.py", "nova_mail_deliver.py"):
            self.assertTrue((SCRIPTS / script).exists(), script)
        _, cur, _, _ = _run()
        self.assertTrue(any("FROM scheduler_runs" in s for s, _ in cur.sql))
        self.assertIn("import nova_output_drift", SRC)


class TestFunctional(unittest.TestCase):
    def test_missed_delivery_raises_one_warning(self):
        rc, cur, notify, out = _run()
        self.assertEqual(rc, 0)
        notify.assert_called_once()
        (title,), kw = notify.call_args
        self.assertEqual(title, "Dead Man's Switch — Missed Deliveries")
        self.assertEqual((kw["category"], kw["dedup_key"], kw["level"]), ("scheduler", "dead-mans-switch-recovery", "warning"))
        self.assertIn("mail_deliver_am: skipped, no runs in 14d", out)       # retired delivery: no false alarm

    def test_everything_delivered_or_too_early_is_silent(self):
        rc, _, notify, out = _run(routes=_routes(rows=[("morning_brief", True, 3), ("mail_deliver_pm", True, 3)]))
        notify.assert_not_called()
        self.assertIn("all checked deliveries confirmed", out)
        _, _, notify, out = _run(routes=_routes(hour=8))
        notify.assert_not_called()
        self.assertIn("too early", out)

    def test_error_path_dry_run_and_already_raised(self):
        _, cur, notify, out = _run(["--dry-run"])
        notify.assert_not_called()
        self.assertIn("• [warning] Dead Man's Switch — Missed Deliveries", out)
        _, _, notify, out = _run(routes=_routes(already=[(1,)]))
        notify.assert_not_called()
        self.assertIn("already raised today", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_dead_mans_switch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
