#!/usr/bin/env python3
"""7-category tests for the Big Brother subagent-restart fix (2026-10-08).

Change under test: BB only watches installed subagents (SUBAGENTS=["sentinel"], plist must exist),
restarts via `launchctl kickstart -k gui/<uid>/com.nova.agent-<name>` with bounded retry/backoff,
and only reports "Restarted" when launchd accepted it. nova-boot.sh no longer touches the retired
lookout/analyst/librarian/coder agents.

Written by Jordan Koch.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_big_brother as bb  # noqa: E402

RETIRED = ("lookout", "analyst", "librarian", "coder")


def _ok():
    return mock.Mock(returncode=0, stdout="", stderr="")


def _fail(rc=1, err="Operation not permitted"):
    return mock.Mock(returncode=rc, stdout="", stderr=err)


def _redis(status=None):
    r = mock.MagicMock()
    r.get.return_value = status
    return r


class TestSecurity(unittest.TestCase):
    """Access control / injection / data boundaries of the restart path."""

    def test_kickstart_uses_argv_not_shell(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_ok()) as run:
            bb._restart_subagent("sentinel")
        args, kw = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kw.get("shell", False))

    def test_label_scoped_to_own_gui_domain_and_agent_prefix(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_ok()) as run:
            bb._restart_subagent("sentinel")
        label = run.call_args[0][0][-1]
        self.assertEqual(label, f"gui/{os.getuid()}/com.nova.agent-sentinel")
        self.assertNotIn("system/", label)

    def test_hostile_name_stays_one_argv_element(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_ok()) as run:
            bb._restart_subagent("x; rm -rf ~")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["launchctl", "kickstart", "-k"])
        self.assertEqual(len(argv), 4)

    def test_watch_list_is_constant_and_excludes_retired(self):
        self.assertEqual(bb.SUBAGENTS, ["sentinel"])
        for name in RETIRED:
            self.assertNotIn(name, bb.SUBAGENTS)

    def test_no_secrets_in_failure_log(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_fail(113, "Could not find service")), \
             mock.patch.object(bb, "log") as lg:
            bb._restart_subagent("sentinel")
        msg = lg.call_args[0][0]
        self.assertNotRegex(msg, r"(?i)token|password|xox[bp]-")


class TestPerformance(unittest.TestCase):
    """Bounded work: finite attempts, bounded sleeps, subprocess timeout set."""

    def test_attempts_and_backoff_bounded(self):
        self.assertLessEqual(bb.SUBAGENT_RESTART_ATTEMPTS, 3)
        total_sleep = sum(bb.SUBAGENT_RESTART_BACKOFF_S * i for i in range(1, bb.SUBAGENT_RESTART_ATTEMPTS))
        self.assertLessEqual(total_sleep, 5.0)

    def test_subprocess_timeout_passed(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_ok()) as run:
            bb._restart_subagent("sentinel")
        self.assertLessEqual(run.call_args[1]["timeout"], 15)

    def test_persistent_failure_stops_after_max_attempts(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_fail()) as run, \
             mock.patch.object(bb.time, "sleep") as sl, mock.patch.object(bb, "log"):
            self.assertFalse(bb._restart_subagent("sentinel"))
        self.assertEqual(run.call_count, bb.SUBAGENT_RESTART_ATTEMPTS)
        self.assertEqual(sl.call_count, bb.SUBAGENT_RESTART_ATTEMPTS - 1)

    def test_heartbeat_check_fast(self):
        with mock.patch("redis.from_url", return_value=_redis("running")):
            t = time.monotonic()
            for _ in range(200):
                bb._check_subagent_heartbeats()
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    """External call (launchctl) retries with backoff and never fails silently."""

    def test_transient_failure_then_success(self):
        with mock.patch.object(bb.subprocess, "run", side_effect=[_fail(), _ok()]) as run, \
             mock.patch.object(bb.time, "sleep") as sl, mock.patch.object(bb, "log") as lg:
            self.assertTrue(bb._restart_subagent("sentinel"))
        self.assertEqual(run.call_count, 2)
        sl.assert_called_once_with(bb.SUBAGENT_RESTART_BACKOFF_S)
        lg.assert_not_called()

    def test_timeout_exception_retried(self):
        boom = subprocess.TimeoutExpired(cmd="launchctl", timeout=15)
        with mock.patch.object(bb.subprocess, "run", side_effect=[boom, boom, _ok()]) as run, \
             mock.patch.object(bb.time, "sleep") as sl:
            self.assertTrue(bb._restart_subagent("sentinel"))
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list],
                         [bb.SUBAGENT_RESTART_BACKOFF_S * 1, bb.SUBAGENT_RESTART_BACKOFF_S * 2])

    def test_backoff_increases(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_fail()), \
             mock.patch.object(bb.time, "sleep") as sl, mock.patch.object(bb, "log"):
            bb._restart_subagent("sentinel")
        delays = [c[0][0] for c in sl.call_args_list]
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[-1], delays[0])

    def test_service_not_found_not_retried_but_logged(self):
        with mock.patch.object(bb.subprocess, "run", return_value=_fail(113, "Could not find service")) as run, \
             mock.patch.object(bb.time, "sleep") as sl, mock.patch.object(bb, "log") as lg:
            self.assertFalse(bb._restart_subagent("sentinel"))
        self.assertEqual(run.call_count, 1)
        sl.assert_not_called()
        self.assertIn("failed", lg.call_args[0][0])
        self.assertEqual(lg.call_args[1]["level"], bb.LOG_ERROR)

    def test_exhausted_retries_logged_as_error(self):
        with mock.patch.object(bb.subprocess, "run", side_effect=OSError("launchctl missing")), \
             mock.patch.object(bb.time, "sleep"), mock.patch.object(bb, "log") as lg:
            self.assertFalse(bb._restart_subagent("sentinel"))
        self.assertIn("launchctl missing", lg.call_args[0][0])


class TestUnit(unittest.TestCase):
    """_check_subagent_heartbeats in isolation."""

    def test_healthy_agent_not_stale(self):
        with mock.patch("redis.from_url", return_value=_redis("running")):
            self.assertEqual(bb._check_subagent_heartbeats(), [])

    def test_missing_heartbeat_installed_agent_is_stale(self):
        with mock.patch("redis.from_url", return_value=_redis(None)), \
             mock.patch.object(bb.Path, "exists", lambda self: True):
            self.assertEqual(bb._check_subagent_heartbeats(), ["sentinel"])

    def test_uninstalled_agent_ignored(self):
        with mock.patch.object(bb, "SUBAGENTS", ["sentinel", "lookout"]), \
             mock.patch("redis.from_url", return_value=_redis(None)), \
             mock.patch.object(bb.Path, "exists", lambda self: "lookout" not in str(self)):
            self.assertEqual(bb._check_subagent_heartbeats(), ["sentinel"])

    def test_redis_down_returns_empty(self):
        with mock.patch("redis.from_url", side_effect=ConnectionError("down")):
            self.assertEqual(bb._check_subagent_heartbeats(), [])


class TestIntegration(unittest.TestCase):
    """Watch list + plist dir + restart together, against a temp HOME's LaunchAgents."""

    def _home(self, installed):
        d = tempfile.TemporaryDirectory()
        la = Path(d.name) / "Library/LaunchAgents"
        la.mkdir(parents=True)
        for n in installed:
            (la / f"com.nova.agent-{n}.plist").write_text("<plist/>")
        return d

    def test_only_installed_stale_agents_get_kickstarted(self):
        d = self._home(["sentinel"])
        self.addCleanup(d.cleanup)
        with mock.patch.object(bb, "SUBAGENTS", ["sentinel", *RETIRED]), \
             mock.patch.object(bb.Path, "home", return_value=Path(d.name)), \
             mock.patch("redis.from_url", return_value=_redis(None)), \
             mock.patch.object(bb.subprocess, "run", return_value=_ok()) as run:
            restarted = [a for a in bb._check_subagent_heartbeats() if bb._restart_subagent(a)]
        self.assertEqual(restarted, ["sentinel"])
        self.assertEqual(run.call_count, 1)

    def test_nothing_installed_means_no_launchctl_calls(self):
        d = self._home([])
        self.addCleanup(d.cleanup)
        with mock.patch.object(bb.Path, "home", return_value=Path(d.name)), \
             mock.patch("redis.from_url", return_value=_redis(None)), \
             mock.patch.object(bb.subprocess, "run") as run:
            for a in bb._check_subagent_heartbeats():
                bb._restart_subagent(a)
        run.assert_not_called()


class TestFunctional(unittest.TestCase):
    """End to end: what the self-repair digest sees from BB's lines (golden + error paths)."""

    def _digest(self, lines):
        import nova_self_repair_digest as d
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for msg in lines:
                f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                    "source": "big-brother", "msg": msg}) + "\n")
        self.addCleanup(os.unlink, f.name)
        return d.bb_heals(datetime.now(timezone.utc) - timedelta(hours=1), files=[Path(f.name)])

    def test_golden_real_restart_counted(self):
        c = self._digest(["[warning] Subagent sentinel stale → Restarted via launchctl kickstart"])
        self.assertEqual(sum(c.values()), 1)

    def test_error_path_failed_restart_not_counted_as_fix(self):
        c = self._digest(["[warning] Subagent sentinel stale → restart failed — needs a look"])
        self.assertEqual(sum(c.values()), 0)

    def test_record_event_only_claims_restart_on_success(self):
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        i = src.index("stale = _check_subagent_heartbeats()")
        block = src[i:i + 700]
        self.assertIn("if _restart_subagent(agent):", block)
        self.assertIn("restart failed", block)


class TestFrame(unittest.TestCase):
    """Smoke: modules import, boot script parses and no longer starts retired agents."""

    def test_imports(self):
        for attr in ("SUBAGENTS", "SUBAGENT_RESTART_ATTEMPTS", "SUBAGENT_RESTART_BACKOFF_S",
                     "_check_subagent_heartbeats", "_restart_subagent"):
            self.assertTrue(hasattr(bb, attr), attr)

    def test_boot_script_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPTS / "nova-boot.sh")], capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_boot_script_drops_retired_agents(self):
        src = (SCRIPTS / "nova-boot.sh").read_text()
        for name in RETIRED:
            self.assertNotIn(f"com.nova.agent-{name}", src)
        self.assertIn("com.nova.agent-sentinel", src)

    def test_ctl_script_syntax(self):
        r = subprocess.run(["zsh", "-n", str(SCRIPTS / "nova_subagent_ctl.sh")], capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
