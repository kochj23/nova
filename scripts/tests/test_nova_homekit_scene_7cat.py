#!/usr/bin/env python3
"""nova_homekit_scene.sh — 7-category tests (Security, Performance, Retry, Unit, Integration,
Functional, Frame). Written by Jordan Koch (via Claude).

The script runs from a temp copy with stubbed curl / shortcuts / psql / sleep first on PATH and HOME
pointed at a temp dir, so no scene is executed, nothing is written to PG and the real kill-switch file
is never read. The guard is the REAL nova_safety_guards.scene_guard, wrapped so report_block (restraint
ledger + Slack) is replaced by a log line."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_homekit_scene.sh"

GUARD_WRAPPER = r'''
import sys, importlib.util, os
sys.path.insert(0, os.environ["REAL_SCRIPTS"])
spec = importlib.util.spec_from_file_location("g", os.path.join(os.environ["REAL_SCRIPTS"], "nova_safety_guards.py"))
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
log = os.path.join(os.environ["STUB_LOG"], "guard.log")
g.report_block = lambda *a, **k: open(log, "a").write("BLOCK " + k.get("action", "") + "\n")
g._ops_cursor = lambda: None
sys.exit(g.main(sys.argv[1:]))
'''

STUBS = {
    "curl": '#!/bin/bash\nprintf "%s\\n" "$@" >> "$STUB_LOG/curl.log"; echo "---" >> "$STUB_LOG/curl.log"\n'
            '[ -n "${CURL_OUT:-}" ] && printf "%s" "$CURL_OUT"\nexit ${CURL_RC:-0}\n',
    "shortcuts": '#!/bin/bash\ncat >> "$STUB_LOG/shortcuts.stdin"; echo "$@" >> "$STUB_LOG/shortcuts.log"\n'
                 'n=$(wc -l < "$STUB_LOG/shortcuts.log")\n'
                 '[ "$n" -gt "${SHORTCUTS_FAIL_N:-0}" ] && exit 0\nexit 1\n',
    "psql": '#!/bin/bash\nprintf "%s\\n" "$@" > "$STUB_LOG/psql.args"; cat > "$STUB_LOG/psql.stdin"\n',
    "sleep": '#!/bin/bash\necho "$1" >> "$STUB_LOG/sleep.log"\n',
}


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hkscene-7cat-"))
        self.bin, self.log, self.home, self.app = (self.root / d for d in ("bin", "log", "home", "app"))
        for d in (self.bin, self.log, self.home / ".openclaw", self.app):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            p = self.bin / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        os.symlink(sys.executable, self.bin / "python3")
        shutil.copy(SCRIPT, self.app / SCRIPT.name)
        (self.app / "nova_safety_guards.py").write_text(GUARD_WRAPPER)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def sh(self, *args, **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "STUB_LOG": str(self.log),
             "REAL_SCRIPTS": str(SCRIPTS), "NOVA_TEST_QUIET": "1"}
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["/bin/bash", str(self.app / SCRIPT.name), *args], capture_output=True,
                              text=True, timeout=60, env=e)

    def read(self, name):
        p = self.log / name
        return p.read_text() if p.exists() else ""

    def curl_body(self):
        args = self.read("curl.log").split("\n")
        return json.loads(args[args.index("-d") + 1])


EXECUTED = '{"status": "executed", "scene": "x"}'


# ── Security ────────────────────────────────────────────────────────────────────
class TestSecurity(_Sandbox):
    def test_securing_scene_refused_exit3_before_any_call(self):
        for scene in ("Good Night", "Leave Home", "Lock Up", "Close Garage"):
            r = self.sh(scene, CURL_OUT=EXECUTED)
            self.assertEqual(r.returncode, 3, scene)
            self.assertIn("refused by physical guard", r.stderr)
        self.assertEqual(self.read("curl.log"), "")
        self.assertEqual(self.read("shortcuts.log"), "")
        self.assertIn("BLOCK scene Good Night", self.read("guard.log"))

    def test_guard_missing_fails_closed(self):
        (self.app / "nova_safety_guards.py").unlink()
        r = self.sh("movie_mode", CURL_OUT=EXECUTED)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.read("curl.log"), "")

    def test_kill_switch_holds(self):
        (self.home / ".openclaw" / ".autonomy-kill").write_text("1")
        r = self.sh("movie_mode", CURL_OUT=EXECUTED)
        self.assertEqual(r.returncode, 3)
        self.assertIn("kill switch", r.stderr)
        self.assertEqual(self.read("curl.log"), "")

    def test_sql_injection_scene_name_bound_as_variable(self):
        evil = "x$$); SELECT pg_sleep(9); --"
        r = self.sh(evil, CURL_OUT=EXECUTED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(evil, self.read("psql.stdin"))
        self.assertIn(":'scene'", self.read("psql.stdin"))
        self.assertIn(f"scene={evil}", self.read("psql.args"))

    def test_json_injection_scene_name_escaped(self):
        evil = 'movie", "admin": true, "x": "\\'
        self.sh(evil, CURL_OUT=EXECUTED)
        body = self.curl_body()
        self.assertEqual(body, {"name": evil})


# ── Performance ─────────────────────────────────────────────────────────────────
class TestPerformance(_Sandbox):
    def test_refusal_and_golden_path_fast(self):
        t0 = time.perf_counter()
        self.sh("Leave Home")
        self.sh("movie_mode", CURL_OUT=EXECUTED)
        self.assertLess(time.perf_counter() - t0, 10.0)

    def test_curl_has_connect_timeout_and_bounded_retries(self):
        self.sh("movie_mode", CURL_OUT=EXECUTED)
        args = self.read("curl.log").split("\n")
        self.assertEqual(args[args.index("--connect-timeout") + 1], "3")
        self.assertEqual(args[args.index("--retry") + 1], "2")


# ── Retry ───────────────────────────────────────────────────────────────────────
class TestRetry(_Sandbox):
    def test_api_post_retries_connrefused(self):
        self.sh("movie_mode", CURL_OUT=EXECUTED)
        args = self.read("curl.log").split("\n")
        self.assertIn("--retry-connrefused", args)
        self.assertEqual(args[args.index("--retry-delay") + 1], "1")

    def test_shortcuts_fallback_retries_with_backoff(self):
        r = self.sh("movie_mode", CURL_RC=7, SHORTCUTS_FAIL_N=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.read("shortcuts.log").splitlines()), 3)
        self.assertEqual(self.read("sleep.log").split(), ["1", "2"])

    def test_all_attempts_fail_reports_error_not_silent(self):
        r = self.sh("movie_mode", CURL_RC=7, SHORTCUTS_FAIL_N=99)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Failed to execute scene 'movie_mode'", r.stderr)
        self.assertEqual(len(self.read("shortcuts.log").splitlines()), 3)
        self.assertEqual(self.read("psql.stdin"), "")       # nothing logged as activated


# ── Unit ────────────────────────────────────────────────────────────────────────
class TestUnit(_Sandbox):
    def test_usage_without_args(self):
        r = self.sh()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stdout)

    def test_executed_detection_tolerates_spacing(self):
        r = self.sh("movie_mode", CURL_OUT='{"status":"executed"}')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read("shortcuts.log"), "")


# ── Integration ─────────────────────────────────────────────────────────────────
class TestIntegration(_Sandbox):
    def test_real_scene_guard_known_scene_reaches_api_and_logs(self):
        r = self.sh("movie_mode", CURL_OUT=EXECUTED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("http://127.0.0.1:37400/api/homekit/scenes/execute", self.read("curl.log"))
        self.assertEqual(self.curl_body(), {"name": "movie_mode"})
        self.assertIn("scene=movie_mode", self.read("psql.args"))
        self.assertIn("home_scene_activations", self.read("psql.stdin"))
        self.assertEqual(self.read("guard.log"), "")

    def test_psql_failure_never_breaks_scene(self):
        (self.bin / "psql").write_text("#!/bin/bash\nexit 2\n")
        r = self.sh("movie_mode", CURL_OUT=EXECUTED)
        self.assertEqual(r.returncode, 0)


# ── Functional ──────────────────────────────────────────────────────────────────
class TestFunctional(_Sandbox):
    def test_golden_path_api(self):
        r = self.sh("Reading Lights", CURL_OUT=EXECUTED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "executed")

    def test_fallback_to_shortcuts_when_api_down(self):
        r = self.sh("Movie Night Lights", CURL_RC=7)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["backend"], "Shortcuts CLI")
        self.assertIn("Movie Night Lights", self.read("shortcuts.stdin"))

    def test_list_mode_skips_guard(self):
        r = self.sh("--list", CURL_OUT='[{"name": "Movie"}]')
        self.assertEqual(r.returncode, 0)
        self.assertIn("Movie", r.stdout)
        self.assertEqual(self.read("guard.log"), "")


# ── Frame ───────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_syntax_and_executable(self):
        self.assertEqual(subprocess.run(["/bin/bash", "-n", str(SCRIPT)]).returncode, 0)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_real_path_usage_exits_1_without_side_effects(self):
        r = subprocess.run(["/bin/bash", str(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stdout)

    def test_no_user_specific_paths(self):
        self.assertNotRegex(SCRIPT.read_text(), r"/Users/[a-z]")


if __name__ == "__main__":
    unittest.main()
