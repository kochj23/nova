#!/usr/bin/env python3
"""Tests for nova_sandbox.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Nothing here ever reaches docker or psql: every subprocess.run is a recorder stub, and the
tests prove the container limits (memory/cpus/pids/read-only/no mounts/no-new-privileges),
the timeout clamp, the kill-on-timeout path and the SQL escaping from the recorded argv."""
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_sandbox.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="sandbox-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch("subprocess.run", MagicMock(side_effect=AssertionError("subprocess at import"))):
        spec.loader.exec_module(mod)
    return mod


sb = _load("sandbox_under_test", SCRIPT)
sb.DOCKERFILE_DIR = TMP / "docker" / "nova-sandbox"      # never touch ~/.openclaw/docker


class _Runner:
    """subprocess.run stand-in: records argv, answers by the docker subcommand (or psql)."""
    def __init__(self, image="sha256:abc", run_rc=0, stdout="4\n", stderr="", run_exc=None,
                 build_rc=0, ps_out="", psql_rc=0):
        self.calls = []
        self.image, self.run_rc, self.stdout, self.stderr, self.run_exc = image, run_rc, stdout, stderr, run_exc
        self.build_rc, self.ps_out, self.psql_rc = build_rc, ps_out, psql_rc

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        exe = argv[0]
        if exe == "psql":
            return types.SimpleNamespace(returncode=self.psql_rc, stdout="", stderr="")
        sub = argv[1]
        if sub == "images":
            return types.SimpleNamespace(returncode=0, stdout=self.image, stderr="")
        if sub == "build":
            return types.SimpleNamespace(returncode=self.build_rc, stdout="", stderr="boom")
        if sub == "ps":
            return types.SimpleNamespace(returncode=0, stdout=self.ps_out, stderr="")
        if sub == "kill":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if sub == "run":
            if self.run_exc is not None:
                exc, self.run_exc = self.run_exc, None
                raise exc
            return types.SimpleNamespace(returncode=self.run_rc, stdout=self.stdout, stderr=self.stderr)
        raise AssertionError(f"unexpected argv {argv}")

    def of(self, sub):
        return [(a, k) for a, k in self.calls if (a[0] == "psql" and sub == "psql") or (a[0] != "psql" and a[1] == sub)]


def _run(code="print(2+2)", runner=None, **kw):
    runner = runner or _Runner()
    with patch.object(sb.subprocess, "run", runner), redirect_stdout(io.StringIO()):
        res = sb.run_sandboxed(code, **kw)
    return res, runner


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", SRC.lower())

    def test_container_limits_are_all_on_the_docker_run_line(self):
        res, r = _run()
        argv = r.of("run")[0][0]
        for flag, val in (("--memory", "2g"), ("--cpus", "2"), ("--pids-limit", "100"),
                          ("--tmpfs", "/tmp:size=100m"), ("--security-opt", "no-new-privileges"),
                          ("--network", "bridge")):
            self.assertEqual(argv[argv.index(flag) + 1], val, flag)
        self.assertIn("--read-only", argv)
        self.assertIn("--rm", argv)
        self.assertEqual(argv[-2:], [sb.IMAGE, "print(2+2)"])      # the code is the ENTRYPOINT arg, never a shell string

    def test_no_host_mounts_or_privilege_escalation(self):
        res, r = _run()
        argv = r.of("run")[0][0]
        for bad in ("-v", "--volume", "--mount", "--privileged", "--cap-add", "--pid=host", "--network=host"):
            self.assertNotIn(bad, argv)
        self.assertFalse(r.of("run")[0][1].get("shell"))
        self.assertNotIn("shell=True", SRC)
        self.assertEqual(SRC.count("--read-only"), 1)

    def test_untrusted_code_is_escaped_and_truncated_before_psql(self):
        evil = "x = \"'); DROP TABLE sandbox_runs; --\"" + "\n#" + "a" * 6000
        res, r = _run(evil)
        insert = r.of("psql")[0][0][-1]
        self.assertIn("INSERT INTO sandbox_runs", insert)
        self.assertNotIn("'); DROP", insert)                      # every single quote doubled
        self.assertIn("''); DROP", insert)
        self.assertLess(len(insert), 5000 + 600)                   # code capped at 5000 chars
        self.assertNotIn("DROP TABLE sandbox_runs; --'", insert)  # the quote never closes the literal early
        self.assertNotIn("DROP TABLE", r.of("run")[0][0][-1][:0])  # (code itself still goes to docker untouched)

    def test_image_is_unprivileged_user_on_slim_python(self):
        with patch.object(sb.subprocess, "run", _Runner()), redirect_stdout(io.StringIO()):
            self.assertTrue(sb.rebuild_image())
        df = (sb.DOCKERFILE_DIR / "Dockerfile").read_text()
        self.assertIn("USER sandbox", df)
        self.assertIn("FROM python:3.12-slim", df)
        self.assertTrue(df.index("USER sandbox") < df.index("ENTRYPOINT"))


class TestPerformance(unittest.TestCase):
    def test_thousand_mocked_runs_stay_fast(self):
        r = _Runner()
        t0 = time.perf_counter()
        with patch.object(sb.subprocess, "run", r), redirect_stdout(io.StringIO()):
            for i in range(1_000):
                sb.run_sandboxed(f"print({i})" + "'" * (i % 7))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(r.of("run")), 1_000)

    def test_escaping_a_large_payload_is_linear(self):
        code = ("it's " * 20_000)[:5000 * 4]
        t0 = time.perf_counter()
        _run(code)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_docker_run_failure_fails_open_with_failure_status(self):
        # RETRY GAP: run_sandboxed()/docker run — one attempt; an OSError becomes status=failure, nothing escapes
        res, r = _run(runner=_Runner(run_exc=OSError("docker daemon down")))
        self.assertEqual(res["status"], "failure")
        self.assertEqual(res["exit_code"], -1)
        self.assertIn("docker daemon down", res["stderr"])
        self.assertEqual(len(r.of("run")), 1)
        self.assertIn("status='failure'", r.of("psql")[-1][0][-1])

    def test_timeout_kills_the_container_once_and_reports_timeout(self):
        # RETRY GAP: run_sandboxed()/docker run — a hang is not retried; the container is killed by name
        res, r = _run(runner=_Runner(run_exc=subprocess.TimeoutExpired("docker", 5)), timeout=5)
        self.assertEqual(res["status"], "timeout")
        self.assertEqual(res["stderr"], "Timeout after 5s")
        kills = r.of("kill")
        self.assertEqual(len(kills), 1)
        self.assertTrue(kills[0][0][-1].startswith("nova-sandbox-"))
        self.assertEqual(len(r.of("run")), 1)

    def test_image_build_failure_fails_open_without_running_code(self):
        # RETRY GAP: ensure_image()/rebuild_image() — one build attempt; failure returns a failure dict, never runs code
        res, r = _run(runner=_Runner(image="", build_rc=1))
        self.assertEqual(res, {"exit_code": -1, "stdout": "", "stderr": "Failed to build sandbox image",
                               "duration_ms": 0, "status": "failure"})
        self.assertEqual(r.of("run"), [])
        self.assertIn("Image build failed", r.of("psql")[-1][0][-1])

    def test_psql_bookkeeping_failure_never_changes_the_result(self):
        # RETRY GAP: db_exec() — one psql attempt, return code ignored; a failed audit row never fails the run
        res, r = _run(runner=_Runner(psql_rc=2))
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(r.of("psql")), 2)


class TestUnit(unittest.TestCase):
    def test_timeout_is_clamped_to_max(self):
        res, r = _run(timeout=99_999)
        self.assertEqual(r.of("run")[0][1]["timeout"], sb.MAX_TIMEOUT)
        res, r = _run(timeout=7)
        self.assertEqual(r.of("run")[0][1]["timeout"], 7)
        self.assertEqual(sb.MAX_TIMEOUT, 300)
        self.assertEqual(sb.DEFAULT_TIMEOUT, sb.MAX_TIMEOUT)

    def test_output_is_truncated(self):
        res, r = _run(runner=_Runner(stdout="o" * 20_000, stderr="e" * 9_000, run_rc=3))
        self.assertEqual(len(res["stdout"]), 10_000)
        self.assertEqual(len(res["stderr"]), 5_000)
        self.assertEqual((res["exit_code"], res["status"]), (3, "failure"))
        self.assertIsInstance(res["duration_ms"], int)

    def test_container_name_is_unique_per_run(self):
        _, r1 = _run()
        _, r2 = _run()
        n1, n2 = (r.of("run")[0][0] for r in (r1, r2))
        name1, name2 = n1[n1.index("--name") + 1], n2[n2.index("--name") + 1]
        self.assertNotEqual(name1, name2)
        self.assertRegex(name1, r"^nova-sandbox-[0-9a-f]{8}$")

    def test_cleanup_stale_kills_each_listed_container_and_nothing_when_empty(self):
        r = _Runner(ps_out="nova-sandbox-aaaaaaaa\nnova-sandbox-bbbbbbbb\n")
        with patch.object(sb.subprocess, "run", r), redirect_stdout(io.StringIO()):
            sb.cleanup_stale()
        self.assertEqual([a[-1] for a, _ in r.of("kill")], ["nova-sandbox-aaaaaaaa", "nova-sandbox-bbbbbbbb"])
        self.assertEqual(r.of("ps")[0][0][2:4], ["--filter", "name=nova-sandbox-"])
        r = _Runner(ps_out="")
        with patch.object(sb.subprocess, "run", r), redirect_stdout(io.StringIO()):
            sb.cleanup_stale()
        self.assertEqual(r.of("kill"), [])

    def test_rebuild_image_reports_build_failure(self):
        r = _Runner(build_rc=1)
        with patch.object(sb.subprocess, "run", r), redirect_stdout(io.StringIO()) as out:
            self.assertFalse(sb.rebuild_image())
        self.assertIn("Build failed: boom", out.getvalue())
        self.assertEqual(r.of("build")[0][0][1:4], ["build", "-t", sb.IMAGE])
        self.assertEqual(r.of("build")[0][1]["timeout"], 600)

    def test_log_prefix(self):
        with redirect_stdout(io.StringIO()) as out:
            sb.log("hi")
        self.assertEqual(out.getvalue(), "[sandbox] hi\n")


class TestIntegration(unittest.TestCase):
    def test_ensure_image_builds_only_when_missing(self):
        r = _Runner(image="sha256:deadbeef")
        with patch.object(sb.subprocess, "run", r), redirect_stdout(io.StringIO()):
            self.assertTrue(sb.ensure_image())
        self.assertEqual(r.of("build"), [])
        r = _Runner(image="")
        with patch.object(sb.subprocess, "run", r), redirect_stdout(io.StringIO()):
            self.assertTrue(sb.ensure_image())
        self.assertEqual(len(r.of("build")), 1)

    def test_run_records_start_then_completion_under_the_same_run_id(self):
        res, r = _run(session_id="s1", trace_id="t1")
        sqls = [a[-1] for a, _ in r.of("psql")]
        self.assertEqual(len(sqls), 2)
        run_id = re.search(r"VALUES \('([0-9a-f-]{36})', 's1', 't1'", sqls[0]).group(1)
        self.assertIn(f"WHERE run_id='{run_id}'", sqls[1])
        self.assertIn("status='success'", sqls[1])
        self.assertIn("exit_code=0", sqls[1])
        for a, k in r.of("psql"):
            self.assertEqual(a[:7], ["psql", "-h", sb.DB_HOST, "-U", sb.DB_USER, "-d", sb.DB_NAME])
            self.assertEqual(k["timeout"], 10)

    def test_docker_binary_is_resolved_not_assumed(self):
        self.assertIn("_shutil.which(\"docker\")", SRC)
        self.assertTrue(sb.DOCKER.endswith("docker"))
        self.assertEqual(sb.DB_NAME, "nova_ops")


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        res, r = _run("print(2+2)", session_id="cli", trace_id="manual")
        self.assertEqual(res, {"exit_code": 0, "stdout": "4\n", "stderr": "", "duration_ms": res["duration_ms"],
                               "status": "success"})
        self.assertEqual([a[1] for a, _ in r.calls if a[0] != "psql"], ["images", "run"])
        self.assertIn("'running'", r.of("psql")[0][0][-1])
        self.assertIn("stdout='4\n'", r.of("psql")[1][0][-1])

    def test_nonzero_exit_is_a_failure_with_captured_stderr(self):
        res, r = _run("raise SystemExit(2)", runner=_Runner(run_rc=2, stdout="", stderr="Traceback: it's bad"))
        self.assertEqual(res["status"], "failure")
        self.assertEqual(res["exit_code"], 2)
        self.assertIn("stderr='Traceback: it''s bad'", r.of("psql")[1][0][-1])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        for flag in ("--run", "--rebuild-image", "--cleanup"):
            self.assertIn(flag, r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_sandbox"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
