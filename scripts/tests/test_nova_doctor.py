#!/usr/bin/env python3
"""Tests for nova_doctor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_doctor.py"
SRC = SCRIPT.read_text()


def _stub_config():
    cfg = types.ModuleType("nova_config")
    cfg.slack_bot_token = lambda: "xoxb-test-token"
    cfg.SLACK_API = "https://slack.test/api"
    cfg.SLACK_BB = "C_TEST_BB"
    return cfg


@contextmanager
def _modstub(mapping):
    """Install stub modules and afterwards restore ONLY those keys (patch.dict would also evict every module the
    script imported for the first time, e.g. urllib.request, leaving later patches aimed at a fresh copy)."""
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in mapping}
    sys.modules.update(mapping)
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
    with _modstub({"nova_config": _stub_config()}):     # slack_bot_token() hits Keychain at import
        spec.loader.exec_module(mod)
    return mod


doc = _load("doctor_under_test", SCRIPT)


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _http(table):
    """urlopen stub: table maps url -> dict or Exception."""
    seen = []

    def fake(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        seen.append(url)
        v = table.get(url, OSError("no route"))
        if isinstance(v, Exception):
            raise v
        return _Resp(v)
    fake.seen = seen
    return fake


def _pg(row):
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): self.sql = sql
        def fetchone(self): return row
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
    return lambda *a, **k: _Conn()


def _selfcheck(bad=None):
    sc = types.ModuleType("nova_selfcheck")
    sc.MOUNTS = {"/Volumes/Data": "local", "/Volumes/nas": "192.168.1.69"}
    sc.mount_problem = lambda m, want: (bad or {}).get(m)
    return sc


def _ha(states):
    m = types.ModuleType("nova_ha_poller"); m.ha_get_states = lambda: states
    return m


ALL_GREEN = [(n, doc.OK, "fine") for n, _ in doc.CHECKS]


def _run_main(results, argv=("nova_doctor.py",), token="xoxb-test-token", slack=None):
    checks = [(n, (lambda s=s, d=d: (s, d))) for n, s, d in results]
    posted = []
    slack = slack or (lambda req, timeout=None: posted.append(json.loads(req.data)) or _Resp({"ok": True}))
    with patch.object(doc, "CHECKS", checks), patch.object(sys, "argv", list(argv)), patch.object(doc, "SLACK_TOKEN", token), \
         patch("urllib.request.urlopen", slack), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        rc = doc.main()
    return rc, posted, out.getvalue(), err.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("nova_config.slack_bot_token()", SRC)
        self.assertNotIn("password", doc.PG_DSN)

    def test_sql_is_constant_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP)\b", SRC))

    def test_slack_post_carries_the_bearer_token_only_in_the_header(self):
        rc, posted, _, _ = _run_main(ALL_GREEN)
        self.assertEqual(posted[0]["channel"], "C_TEST_BB")
        self.assertNotIn("xoxb", json.dumps(posted[0]))

    def test_face_stack_canary_runs_a_fixed_command_without_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        seen = []
        with patch.object(doc.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or types.SimpleNamespace(returncode=0, stdout="ok 11.0", stderr="")):
            self.assertEqual(doc.check_face_stack(), (doc.OK, "imports clean (ok 11.0)"))
        self.assertEqual(seen[0][0][0], sys.executable)
        self.assertEqual(seen[0][1]["env"]["PYTHONPATH"], doc.PKG_PATH)


class TestPerformance(unittest.TestCase):
    def test_report_assembly_for_10k_checks_is_fast(self):
        results = [(f"check{i}", (doc.OK, doc.WARN, doc.FAIL)[i % 3], f"d{i}") for i in range(10_000)]
        t0 = time.perf_counter()
        rc, posted, out, _ = _run_main(results)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("\n"), 10_001)      # header + one line per check
        self.assertTrue(out.startswith(":robot_face: *Nova boot check — FAILURES*"))


class TestRetry(unittest.TestCase):
    def test_mlx_retries_the_lb_three_times_then_succeeds(self):
        calls, sleeps = [], []

        def flaky(url, timeout=None):
            calls.append(url)
            if len(calls) < 3:
                raise OSError("502")
            return {"data": [{"id": "/models/qwen3-30b"}]}
        with patch.object(doc, "http_json", flaky), patch.object(doc.time, "sleep", sleeps.append):
            self.assertEqual(doc.check_mlx(), (doc.OK, "serving qwen3-30b"))
        self.assertEqual(calls, [doc.MLX_MODELS] * 3)
        self.assertEqual(sleeps, [3, 3])

    def test_mlx_lb_blip_with_a_live_backend_is_ok_pool_down_is_warn(self):
        def lb_dead_backend_up(url, timeout=None):
            if url == doc.MLX_MODELS:
                raise OSError("502")
            return {"data": []}
        with patch.object(doc, "http_json", lb_dead_backend_up), patch.object(doc.time, "sleep", lambda s: None):
            s, d = doc.check_mlx()
        self.assertEqual(s, doc.OK); self.assertIn("LB blip (transient) — 2/2 backends", d)
        with patch.object(doc, "http_json", lambda u, timeout=None: (_ for _ in ()).throw(OSError("down"))), patch.object(doc.time, "sleep", lambda s: None):
            s, d = doc.check_mlx()
        self.assertEqual(s, doc.WARN); self.assertIn("MLX pool DOWN — 0/2", d)

    def test_single_shot_checks_fail_open_into_a_status(self):
        # RETRY GAP: check_gateway()/check_ollama()/check_postgres()/check_wal_archiver() — one attempt each; an
        # exception becomes FAIL/WARN text in the report rather than escaping and killing the whole run
        with patch("urllib.request.urlopen", _http({})):
            self.assertEqual(doc.check_gateway()[0], doc.FAIL)
            self.assertEqual(doc.check_ollama()[0], doc.WARN)
        with patch("psycopg2.connect", lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))):
            self.assertEqual(doc.check_postgres(), (doc.FAIL, "unreachable: refused"))
            self.assertEqual(doc.check_wal_archiver(), (doc.WARN, "could not read pg_stat_archiver: refused"))

    def test_slack_post_failure_never_breaks_the_run(self):
        # RETRY GAP: post_slack() — one attempt; a Slack outage is logged to stderr and main() still returns
        rc, posted, out, err = _run_main(ALL_GREEN, slack=lambda req, timeout=None: (_ for _ in ()).throw(OSError("slack down")))
        self.assertEqual(rc, 0)
        self.assertIn("slack post failed: slack down", err)


class TestUnit(unittest.TestCase):
    def test_gateway_states(self):
        with patch("urllib.request.urlopen", _http({doc.GATEWAY_HEALTH: {"ok": True, "version": "2.4.0", "uptime_s": 61.9}})):
            self.assertEqual(doc.check_gateway(), (doc.OK, "v2.4.0, uptime 61s"))
        with patch("urllib.request.urlopen", _http({doc.GATEWAY_HEALTH: {"ok": True, "degraded": True, "version": "2"}})):
            self.assertEqual(doc.check_gateway(), (doc.WARN, "degraded=true (v2)"))
        with patch("urllib.request.urlopen", _http({doc.GATEWAY_HEALTH: {"ok": False}})):
            self.assertEqual(doc.check_gateway(), (doc.FAIL, "health ok=false (v?)"))

    def test_postgres_and_wal(self):
        with patch("psycopg2.connect", _pg((1234,))):
            self.assertEqual(doc.check_postgres(), (doc.OK, "nova_ops accepting (1234 memories)"))
        with patch("psycopg2.connect", _pg((500, 0, None))):
            self.assertEqual(doc.check_wal_archiver(), (doc.OK, "500 archived, 0 failed"))
        with patch("psycopg2.connect", _pg((500, 3, "t"))):
            self.assertEqual(doc.check_wal_archiver(), (doc.WARN, "3 failed archives (last t); 500 ok"))

    def test_disk_thresholds(self):
        def st(used_pct):
            blocks = 1000
            return types.SimpleNamespace(f_blocks=blocks, f_bfree=int(blocks * (1 - used_pct / 100)),
                                         f_bavail=int(blocks * (1 - used_pct / 100)), f_frsize=1_000_000_000)
        with patch.object(doc.os, "statvfs", lambda p: st(50)):
            self.assertEqual(doc.check_disk()[0], doc.OK)
        with patch.object(doc.os, "statvfs", lambda p: st(90)):
            self.assertEqual(doc.check_disk()[0], doc.WARN)
        with patch.object(doc.os, "statvfs", lambda p: st(96)):
            self.assertEqual(doc.check_disk()[0], doc.FAIL)
        with patch.object(doc.os, "statvfs", lambda p: (_ for _ in ()).throw(OSError("x"))):
            self.assertEqual(doc.check_disk(), (doc.WARN, "statvfs failed: x"))

    def test_pg_dedup_and_launchctl(self):
        def lc(out):
            return lambda cmd, **kw: types.SimpleNamespace(stdout=out)
        with patch.object(doc.subprocess, "run", lc("com.kochj.postgresql17\n")):
            self.assertEqual(doc.check_pg_dedup(), (doc.OK, "single dedicated launchd job"))
        with patch.object(doc.subprocess, "run", lc("homebrew.mxcl.postgresql@17\ncom.kochj.postgresql17")):
            self.assertEqual(doc.check_pg_dedup()[0], doc.WARN)
        with patch.object(doc.subprocess, "run", lc("")):
            self.assertEqual(doc.check_pg_dedup(), (doc.WARN, "com.kochj.postgresql17 not loaded"))

    def test_home_assistant_availability_bands(self):
        def states(n, unavailable):
            return [{"state": "unavailable" if i < unavailable else "on"} for i in range(n)]
        with patch.dict(sys.modules, {"nova_ha_poller": _ha(states(100, 10))}):
            self.assertEqual(doc.check_home_assistant(), (doc.OK, "90/100 entities available (90%)"))
        with patch.dict(sys.modules, {"nova_ha_poller": _ha(states(100, 40))}):
            self.assertEqual(doc.check_home_assistant()[0], doc.WARN)
        with patch.dict(sys.modules, {"nova_ha_poller": _ha(states(100, 70))}):
            self.assertEqual(doc.check_home_assistant()[0], doc.FAIL)
        with patch.dict(sys.modules, {"nova_ha_poller": _ha([])}):
            self.assertEqual(doc.check_home_assistant(), (doc.FAIL, "unreachable on :8123"))

    def test_face_stack_failure_reports_last_stderr_line(self):
        with patch.object(doc.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nImportError: no PIL")):
            self.assertEqual(doc.check_face_stack(), (doc.FAIL, "import error: ImportError: no PIL"))


class TestIntegration(unittest.TestCase):
    def test_volumes_check_reuses_selfcheck_definition(self):
        self.assertIn("from nova_selfcheck import MOUNTS, mount_problem", SRC)
        with patch.dict(sys.modules, {"nova_selfcheck": _selfcheck()}):
            self.assertEqual(doc.check_volumes()[0], doc.OK)
        with patch.dict(sys.modules, {"nova_selfcheck": _selfcheck({"/Volumes/nas": "read-only"})}):
            self.assertEqual(doc.check_volumes(), (doc.FAIL, "/Volumes/nas: read-only"))

    def test_heal_restarts_ha_once_only_when_mostly_unavailable(self):
        ran = []
        with patch.dict(sys.modules, {"nova_ha_poller": _ha([{"state": "unavailable"}] * 6 + [{"state": "on"}] * 4)}), \
             patch.object(doc.subprocess, "run", lambda cmd, **kw: ran.append(cmd)), patch.object(doc.time, "sleep", lambda s: ran.append(("sleep", s))), \
             redirect_stdout(io.StringIO()):
            doc.heal_home_assistant()
        self.assertEqual(ran[0][:3], ["launchctl", "kickstart", "-k"])
        self.assertIn("com.nova.homeassistant", ran[0][3])
        self.assertEqual(ran[1], ("sleep", 75))
        ran.clear()
        with patch.dict(sys.modules, {"nova_ha_poller": _ha([{"state": "on"}] * 10)}), patch.object(doc.subprocess, "run", lambda cmd, **kw: ran.append(cmd)):
            doc.heal_home_assistant()
        self.assertEqual(ran, [])

    def test_check_registry_covers_the_boot_killers(self):
        names = [n for n, _ in doc.CHECKS]
        for n in ("Volumes", "Postgres", "WAL archive", "Gateway", "MLX", "Face stack", "Disk", "Home Assistant"):
            self.assertIn(n, names)
        self.assertTrue(all(callable(f) for _, f in doc.CHECKS))


class TestFunctional(unittest.TestCase):
    def test_all_green_posts_one_report_and_exits_zero(self):
        rc, posted, out, _ = _run_main(ALL_GREEN)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0]["text"].startswith(":robot_face: *Nova boot check — all green*\n:white_check_mark: *Volumes* — fine"))
        self.assertEqual(posted[0]["unfurl_links"], False)
        self.assertEqual(out.strip(), posted[0]["text"])

    def test_warn_does_not_fail_the_run_fail_does(self):
        rc, posted, out, _ = _run_main([("Ollama", doc.WARN, "not serving"), ("Disk", doc.OK, "x")])
        self.assertEqual(rc, 0)
        self.assertIn("warnings*", posted[0]["text"])
        self.assertIn(":warning: *Ollama* — not serving", posted[0]["text"])
        rc, posted, _, _ = _run_main([("Postgres", doc.FAIL, "unreachable"), ("Ollama", doc.WARN, "x")])
        self.assertEqual(rc, 1)
        self.assertIn("FAILURES*", posted[0]["text"])
        self.assertIn(":rotating_light: *Postgres* — unreachable", posted[0]["text"])

    def test_boot_mode_waits_and_heals_before_checking(self):
        order = []
        with patch.object(doc, "wait_for_boot", lambda: order.append("wait")), patch.object(doc, "heal_home_assistant", lambda: order.append("heal")), \
             patch.object(doc, "CHECKS", [("Disk", lambda: order.append("check") or (doc.OK, "x"))]), patch.object(doc, "SLACK_TOKEN", ""), \
             patch.object(sys, "argv", ["nova_doctor.py", "--boot"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = doc.main()
        self.assertEqual(order, ["wait", "heal", "check"])
        self.assertEqual(rc, 0)

    def test_no_token_skips_slack_and_slack_error_is_reported(self):
        rc, posted, out, err = _run_main(ALL_GREEN, token="")
        self.assertEqual(posted, [])
        self.assertIn("no Slack token; skipping post", err)
        rc, posted, out, err = _run_main(ALL_GREEN, slack=lambda req, timeout=None: _Resp({"ok": False, "error": "channel_not_found"}))
        self.assertIn("slack error: channel_not_found", err)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_checks(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        code = ("import sys, types\n"
                "cfg = types.ModuleType('nova_config'); cfg.slack_bot_token = lambda: ''; cfg.SLACK_API = ''; cfg.SLACK_BB = ''\n"
                "sys.modules['nova_config'] = cfg\n"
                "import nova_doctor; print(len(nova_doctor.CHECKS))")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "10")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
