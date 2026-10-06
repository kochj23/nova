#!/usr/bin/env python3
"""Tests for nova_cloudkit_cache_watchdog.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cloudkit_cache_watchdog.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    nn = types.ModuleType("nova_notify")
    nn.notify = MagicMock(return_value=True)
    return {"nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):          # notify is bound at import; sys.modules restored after
        spec.loader.exec_module(mod)
    return mod


ck = _load("cloudkit_watchdog_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="cloudkit-wd-test-"))
ck.LOG = TMP / "logs" / "cloudkit_cache_watchdog.log"           # never touch ~/.openclaw/logs
ck.BIRD_CACHE = TMP / "bird"                                     # never touch the real CloudKit cache


def _bird(containers=2, with_assets=True, extra_file=True):
    """Build a fake bird cache: <id>/Assets/blob + a sibling file that must survive a purge."""
    import shutil
    shutil.rmtree(ck.BIRD_CACHE, ignore_errors=True)
    for i in range(containers):
        c = ck.BIRD_CACHE / f"container-{i}"
        if with_assets:
            (c / "Assets").mkdir(parents=True)
            (c / "Assets" / "blob").write_bytes(b"x" * 10)
        else:
            c.mkdir(parents=True)
        if extra_file:
            (c / "manifest.db").write_text("keep me")
    (ck.BIRD_CACHE / "stray-file").write_text("not a container")
    return ck.BIRD_CACHE


def _du(kb):
    return MagicMock(return_value=types.SimpleNamespace(stdout=f"{kb}\t/x\n", returncode=0))


class _Cur:
    def __init__(self):
        self.sql, self.params = [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)


def _pg(cur, fail=False):
    conn = types.SimpleNamespace(cursor=lambda: cur, close=MagicMock(), autocommit=False)
    if fail:
        return MagicMock(side_effect=OSError("pg unreachable"))
    return MagicMock(return_value=conn)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ck.DB)

    def test_sql_is_parameterized_and_single_table(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"shared_observations"})
        cur = _Cur()
        with patch("psycopg2.connect", _pg(cur)):
            ck.notify("t'); DROP TABLE shared_observations; --\nbody", "warning")
        self.assertNotIn("DROP", cur.sql[0]); self.assertIn("DROP", cur.params[0][0])

    def test_purge_only_removes_assets_subdirs(self):
        _bird(containers=2)
        with redirect_stdout(io.StringIO()):
            n = ck.purge_assets()
        self.assertEqual(n, 2)
        for i in range(2):
            self.assertFalse((ck.BIRD_CACHE / f"container-{i}" / "Assets").exists())
            self.assertEqual((ck.BIRD_CACHE / f"container-{i}" / "manifest.db").read_text(), "keep me")
        self.assertTrue((ck.BIRD_CACHE / "stray-file").exists())
        self.assertNotIn("Mobile Documents", SRC.split("def purge_assets")[1].split("def notify")[0])
        self.assertIn("du", SRC); self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_purge_300_containers_and_parse_10k_sizes_fast(self):
        _bird(containers=300)
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            n = ck.purge_assets()
        with patch.object(ck.subprocess, "run", _du(52_428_800)):
            sizes = [ck.dir_size_gb(ck.BIRD_CACHE) for _ in range(10_000)]
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(n, 300)
        self.assertEqual(sizes[0], 50.0)


class TestRetry(unittest.TestCase):
    def test_du_and_statvfs_fail_open(self):
        # RETRY GAP: dir_size_gb — one du attempt; any failure logs and returns 0.0 (never purges blind)
        with patch.object(ck.subprocess, "run", side_effect=subprocess.TimeoutExpired("du", 300)), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ck.dir_size_gb(ck.BIRD_CACHE), 0.0)
        self.assertIn("du failed", out.getvalue())
        with patch.object(ck.subprocess, "run", _du("garbage")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(ck.dir_size_gb(ck.BIRD_CACHE), 0.0)
        # RETRY GAP: free_gb — a bad mount returns -1.0 rather than raising
        self.assertEqual(ck.free_gb(str(TMP / "does-not-exist")), -1.0)

    def test_pg_record_failure_never_blocks_the_notification(self):
        # RETRY GAP: notify()/psycopg2.connect — one attempt; the bus notification already went out, PG error is logged
        ck.nova_notify = MagicMock()
        with patch("psycopg2.connect", _pg(None, fail=True)), redirect_stdout(io.StringIO()) as out:
            ck.notify("title\nbody", "warning")
        ck.nova_notify.assert_called_once()
        self.assertIn("PG record failed: pg unreachable", out.getvalue())

    def test_unpurgeable_container_is_logged_and_skipped(self):
        # RETRY GAP: purge_assets — rmtree is tried once per container; a failure is logged and the loop continues
        _bird(containers=2)
        real = ck.shutil.rmtree
        calls = []

        def flaky(p, *a, **k):
            calls.append(p)
            if len(calls) == 1:
                raise PermissionError("busy")
            return real(p, *a, **k)
        with patch.object(ck.shutil, "rmtree", flaky), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ck.purge_assets(), 1)
        self.assertIn("Failed to purge", out.getvalue()); self.assertEqual(len(calls), 2)


class TestUnit(unittest.TestCase):
    def test_dir_size_gb_converts_kilobytes(self):
        with patch.object(ck.subprocess, "run", _du(1024 * 1024 * 3)) as run:
            self.assertEqual(ck.dir_size_gb(Path("/some/where")), 3.0)
        self.assertEqual(run.call_args[0][0], ["du", "-sk", "/some/where"])

    def test_free_gb_on_a_real_mount_is_positive(self):
        self.assertGreater(ck.free_gb("/"), 0.0)

    def test_purge_edge_cases(self):
        import shutil
        shutil.rmtree(ck.BIRD_CACHE, ignore_errors=True)
        self.assertEqual(ck.purge_assets(), 0)                       # cache dir absent
        _bird(containers=3, with_assets=False)
        self.assertEqual(ck.purge_assets(), 0)                       # containers with no Assets

    def test_log_appends_timestamped_line_to_redirected_file(self):
        ck.LOG.unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()):
            ck.log("one"); ck.log("two")
        lines = ck.LOG.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[1], r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] two$")


class TestIntegration(unittest.TestCase):
    def test_notify_splits_title_body_and_feeds_the_bus_and_pg(self):
        self.assertIn("from nova_notify import notify as nova_notify", SRC)
        ck.nova_notify = MagicMock()
        cur = _Cur()
        with patch("psycopg2.connect", _pg(cur)) as pg:
            ck.notify(":floppy_disk: *purged 12GB*\nsecond line", severity="warning")
        ck.nova_notify.assert_called_once_with("*purged 12GB*", body="second line", level="warning", category="storage",
                                               dedup_key="cloudkit-cache-watchdog", meta={"host": "mac-studio"})
        pg.assert_called_once_with(ck.DB)
        self.assertIn("INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)", cur.sql[0])
        self.assertIn("'nova', 'storage', 'cloudkit-cache-watchdog'", cur.sql[0])
        self.assertEqual(cur.params[0][1:], ("warning", json.dumps({"threshold_gb": ck.THRESHOLD_GB})))

    def test_notify_without_body_passes_none(self):
        ck.nova_notify = MagicMock()
        with patch("psycopg2.connect", _pg(_Cur())):
            ck.notify("just a title")
        self.assertIsNone(ck.nova_notify.call_args[1]["body"]); self.assertEqual(ck.nova_notify.call_args[1]["level"], "info")


class TestFunctional(unittest.TestCase):
    def test_over_threshold_purges_and_alerts(self):
        _bird(containers=2)
        ck.nova_notify = MagicMock()
        sizes = iter([120.0, 1.5])
        cur = _Cur()
        with patch.object(ck, "dir_size_gb", lambda p: next(sizes)), patch.object(ck, "free_gb", MagicMock(side_effect=[20.0, 138.0])), \
             patch("psycopg2.connect", _pg(cur)), redirect_stdout(io.StringIO()) as out:
            ck.main()
        self.assertFalse((ck.BIRD_CACHE / "container-0" / "Assets").exists())
        title = ck.nova_notify.call_args[0][0]
        self.assertEqual(title, "*iCloud cache watchdog purged 118GB*")
        body = ck.nova_notify.call_args[1]["body"]
        self.assertIn("hit 120GB (limit 50GB). Purged 2 container(s) → now 1.5GB. Free space: 20GB → 138GB.", body)
        self.assertEqual(ck.nova_notify.call_args[1]["level"], "warning")
        self.assertEqual(len(cur.sql), 1)
        self.assertIn("OVER THRESHOLD", out.getvalue())

    def test_under_threshold_touches_nothing(self):
        _bird(containers=1)
        ck.nova_notify = MagicMock()
        with patch.object(ck, "dir_size_gb", lambda p: 12.0), patch("psycopg2.connect") as pg, redirect_stdout(io.StringIO()) as out:
            ck.main()
        self.assertTrue((ck.BIRD_CACHE / "container-0" / "Assets" / "blob").exists())
        ck.nova_notify.assert_not_called(); pg.assert_not_called()
        self.assertIn("Under threshold", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types, subprocess\n"
                "nn = types.ModuleType('nova_notify'); nn.notify = lambda *a, **k: True\n"
                "sys.modules['nova_notify'] = nn\n"
                "subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('main ran at import'))\n"
                "import nova_cloudkit_cache_watchdog as w\n"
                "print(w.THRESHOLD_GB)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "50")


if __name__ == "__main__":
    unittest.main()
