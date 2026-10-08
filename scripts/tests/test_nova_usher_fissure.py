#!/usr/bin/env python3
"""Tests for nova_usher_fissure.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_usher_fissure as U  # noqa: E402

SRC = (SCRIPTS / "nova_usher_fissure.py").read_text()
NODES = [("10.0.0.2", "core"), ("10.0.0.6", "studio")]
IPS = {"pg-primary.digitalnoise.net": "10.0.0.2", "core.digitalnoise.net": "10.0.0.2"}


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

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


def write_jobs(d):
    """Three scripts: a core watcher alerting via notify (Usher pair), one with a Slack fallback,
    one that never alerts."""
    a = Path(d) / "a.py"
    a.write_text("from nova_notify import notify\nURL = 'http://core.digitalnoise.net:18792/health'\n")
    b = Path(d) / "b.py"
    b.write_text("import nova_notify\nW.post_slack(x, c)\nURL = 'http://core.digitalnoise.net:1/'\n")
    c = Path(d) / "c.py"
    c.write_text("cur.execute('INSERT INTO t VALUES (1)')\n")
    return [("task:a", a), ("launchd:b", b), ("task:c", c), ("task:gone", Path(d) / "missing.py")]


def services(jobs, routes=None):
    cur = FakeCur(routes if routes is not None else {"FROM node_status": NODES})
    with mock.patch.object(U, "resolve", side_effect=IPS.get), \
            mock.patch.object(U, "local_node", return_value="studio"), \
            mock.patch.object(U, "PLIST_DIRS", ()), mock.patch("builtins.print"):
        return U.services(cur, jobs), cur


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b(?!127\.0\.0\.1|0\.0\.0\.0)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_watcher_name_stays_a_parameter(self):
        evil = "task:x'; DROP TABLE claude_actions; --"
        cur = FakeCur({"FROM node_status": NODES})
        pair = {"name": evil, "script": "x.py", "host": "studio", "paths": ["notify"], "hops": [{"core"}],
                "watched": ["core"], "writes": [], "reads": []}
        with mock.patch.object(U.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(U, "services", return_value=[pair]), mock.patch("builtins.print"):
            U.run(dry=False)
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO usher_fissure" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertIn(evil, ins[1])

    def test_read_only_observer(self):
        self.assertNotRegex(SRC, r"\bnotify\(|post_slack\(|post_both\(")   # never raises an alarm itself
        self.assertNotIn("import subprocess", SRC)
        self.assertNotIn("claude_queue", SRC)


class TestPerformance(unittest.TestCase):
    def test_fissures_blast_graph_10k(self):
        ws = [{"name": f"w{i}", "script": f"s{i % 500}.py", "host": "studio", "paths": ["notify"],
               "hops": [{"core"}, set()] if i % 3 else [{"core"}], "watched": ["core"] if i % 2 else ["nas"],
               "writes": [f"t{i % 50}"], "reads": [f"t{(i + 1) % 50}"]} for i in range(10000)]
        t = time.monotonic()
        pairs = U.fissures(ws)
        radius = dict(U.blast_radius(ws))
        g = U.dependency_graph(ws)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(radius["studio"], 10000)
        self.assertEqual(len(pairs), sum(1 for i in range(10000) if i % 2 and not i % 3))
        self.assertEqual(len([k for k in g if k.startswith("svc:")]), 500)

    def test_scan_large_source(self):
        src = "x = 'http://core.digitalnoise.net/'\n" * 20000 + "INSERT INTO t\n"
        t = time.monotonic()
        self.assertEqual(U.scan(src)["writes"], ["t"])
        self.assertLess(time.monotonic() - t, 3.0)


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection refused")
            return mock.MagicMock()
        fake_pg = mock.MagicMock(connect=flaky)
        with mock.patch.dict(sys.modules, {"psycopg2": fake_pg}):
            U.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_dns_miss_fails_open(self):
        # RETRY GAP: resolve — a DNS miss is not retried; it returns None and the host is skipped.
        with mock.patch("socket.gethostbyname", side_effect=OSError("nxdomain")):
            self.assertIsNone(U.resolve("nowhere.digitalnoise.net"))
            self.assertIsNone(U.node_of("nowhere.digitalnoise.net", {}, "studio"))

    def test_local_node_fails_open(self):
        with mock.patch("socket.socket", side_effect=OSError("no route")), \
                mock.patch("socket.gethostname", return_value="Studio.local"):
            self.assertEqual(U.local_node({}, "pg"), "Studio")

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(U.load_nodes(FakeCur(boom=True)), {})


class TestUnit(unittest.TestCase):
    def test_single_points(self):
        self.assertEqual(U.single_points("s", []), {"s"})
        self.assertEqual(U.single_points("s", [{"c"}]), {"s", "c"})
        self.assertEqual(U.single_points("s", [{"c"}, {"m"}]), {"s"})

    def test_fissures_empty_and_fallback(self):
        self.assertEqual(U.fissures([]), [])
        self.assertEqual(U.blast_radius([]), [])
        w = {"name": "a", "host": "s", "hops": [{"c"}, set()], "watched": ["c"]}
        self.assertEqual(U.fissures([w]), [])                       # Slack fallback skips c

    def test_scan_ignores_python_imports_and_dsn(self):
        s = U.scan("from foo import bar\nDSN = 'host=pg-primary.digitalnoise.net dbname=x'\n")
        self.assertEqual((s["reads"], s["hosts"], s["paths"]), ([], [], []))

    def test_node_of_local_names(self):
        for n in ("localhost", "127.0.0.1"):
            self.assertEqual(U.node_of(n, {}, "studio"), "studio")

    def test_scheduler_jobs_skips_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            y = Path(d) / "s.yaml"
            y.write_text("tasks:\n  live:\n    script: a.py\n  dead:\n    script: b.py\n    enabled: false\n"
                         "  noscript:\n    schedule: every 5m\n")
            jobs = U.scheduler_jobs(y)
            self.assertEqual([n for n, _ in jobs], ["task:live"])
            with mock.patch("builtins.print"):
                self.assertEqual(U.scheduler_jobs(Path(d) / "missing.yaml"), [])

    def test_launchd_jobs_finds_script(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "net.digitalnoise.x.plist").write_bytes(plistlib.dumps(
                {"Label": "net.digitalnoise.x", "ProgramArguments": ["/bin/sh", "-c", "exec python3 /s/nova_x.py"]}))
            (Path(d) / "net.digitalnoise.bin.plist").write_bytes(plistlib.dumps(
                {"Label": "net.digitalnoise.bin", "ProgramArguments": ["/usr/bin/redis-server"]}))
            (Path(d) / "com.apple.y.plist").write_bytes(b"x")
            jobs = U.launchd_jobs((Path(d), Path(d) / "missing"))
        self.assertEqual(jobs, [("launchd:net.digitalnoise.x", Path("/s/nova_x.py"))])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(U.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_jade_amulet import PLIST_DIRS, PLIST_PREFIXES", SRC)
        self.assertIn("W.set_config(", SRC)
        self.assertIn("W.get_config(cur, SERVICE", SRC)

    def test_speedy_circle_imports_this_graph(self):
        speedy = (SCRIPTS / "nova_speedy_circle.py").read_text()
        self.assertIn("from nova_usher_fissure import build_graph", speedy)

    def test_services_shape_and_graph(self):
        with tempfile.TemporaryDirectory() as d:
            svcs, _ = services(write_jobs(d))
        by = {s["name"]: s for s in svcs}
        self.assertNotIn("task:gone", by)                            # unreadable script skipped
        self.assertEqual(by["task:a"]["hops"], [{"core"}])           # notify rides pg-primary's node
        self.assertEqual(by["task:a"]["watched"], ["core"])
        self.assertEqual(by["launchd:b"]["paths"], ["notify", "slack"])
        self.assertEqual(by["task:c"]["paths"], [])
        pairs = U.fissures([s for s in svcs if s["paths"]])
        self.assertEqual([p["name"] for p in pairs], ["task:a"])
        g = U.dependency_graph(svcs)
        self.assertIn("table:t", g["svc:c.py"])
        self.assertIn("node:core", g["svc:a.py"])

    def test_contract_table(self):
        for col in ("ts timestamptz NOT NULL DEFAULT now()", "watcher text", "shared text[]"):
            self.assertIn(col, U.SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS usher_fissure", U.SCHEMA)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, d, routes=None):
        cur = FakeCur(routes or {"FROM node_status": NODES})
        with mock.patch.object(U.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(U, "scheduler_jobs", return_value=write_jobs(d)), \
                mock.patch.object(U, "launchd_jobs", return_value=[]), \
                mock.patch.object(U, "resolve", side_effect=IPS.get), \
                mock.patch.object(U, "local_node", return_value="studio"), \
                mock.patch.object(U, "PLIST_DIRS", ()), mock.patch("builtins.print"):
            return U.run(dry=dry), cur

    def test_run_writes_pairs_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            pairs, cur = self._run(False, d)
        self.assertEqual([p["name"] for p in pairs], ["task:a"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS usher_fissure" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO usher_fissure" in s for s, _ in cur.sql), 1)
        cfg = [p for s, p in cur.sql if "INSERT INTO service_config" in s]
        self.assertEqual(cfg[0][:2], ("nova_usher_fissure", "latest"))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            pairs, cur = self._run(True, d)
        self.assertEqual(len(pairs), 1)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))

    def test_pg_reads_fail_open(self):
        with tempfile.TemporaryDirectory() as d:
            cur = FakeCur(boom=True)
            with mock.patch.object(U.W, "connect", return_value=fake_conn(cur)), \
                    mock.patch.object(U, "scheduler_jobs", return_value=write_jobs(d)), \
                    mock.patch.object(U, "launchd_jobs", return_value=[]), \
                    mock.patch.object(U, "resolve", return_value=None), \
                    mock.patch.object(U, "local_node", return_value="studio"), \
                    mock.patch.object(U, "PLIST_DIRS", ()), mock.patch("builtins.print"):
                self.assertEqual(U.run(dry=True), [])   # no nodes known -> nothing watched off-host


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_usher_fissure.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_usher_fissure.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
