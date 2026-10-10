#!/usr/bin/env python3
"""Tests for nova_selfcheck.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). This script restarts services, ssh's the fleet, unmounts shares and
escalates to `claude -p`, so EVERY command runner (sh, pg, slack, sleep) is stubbed at load and the
log/state paths point at a tempdir. Since merge M4 (2026-10-09) it also has --boot (nova_doctor's checks)
and --deep (nova_deep_healthcheck's checks); those modules are replaced by fakes here — the real chains are
tested in test_nova_doctor.py / test_nova_deep_healthcheck.py. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_selfcheck.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("selfcheck", SCRIPTS / "nova_selfcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load()
sc.LOG = Path(_TMP.name) / "selfcheck.log"
sc.STATE_DIR = Path(_TMP.name) / "state"
sc.PG_REBUILD_PENDING = sc.STATE_DIR / "pg_rebuild_pending.json"
_REAL_SH, _REAL_SLACK = sc.sh, sc.slack
sc.sh = MagicMock(return_value=(0, ""))          # no command ever reaches the world
sc.pg = MagicMock(return_value=None)
sc.slack = MagicMock()
sc.time = MagicMock(time=time.time)              # time.sleep is a no-op; time.time stays real
sc.print = lambda *a, **k: None


def _fresh():
    sc.DRY = False
    sc.FORCE_DIGEST = False
    sc.sh.reset_mock(side_effect=True, return_value=True); sc.sh.return_value = (0, "")
    sc.pg.reset_mock(side_effect=True, return_value=True); sc.pg.return_value = None
    sc.slack.reset_mock()
    sc.results.clear()
    for f in sc.STATE_DIR.glob("*") if sc.STATE_DIR.exists() else []:
        f.unlink()


def _cmds():
    return [c[0][0] for c in sc.sh.call_args_list]


@contextmanager
def _modstub(mapping):
    """Install fake modules and afterwards restore ONLY those keys (CONVENTIONS: never leak into sys.modules)."""
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


def _fake_doctor(checks, calls):
    d = types.ModuleType("nova_doctor")
    d.OK, d.WARN, d.FAIL = "ok", "warn", "fail"
    d.CHECKS = [(n, (lambda n=n, s=s, t=t: calls.append(n) or (s, t))) for n, s, t in checks]
    d.wait_for_boot = lambda: calls.append("wait")
    d.heal_home_assistant = lambda: calls.append("heal")

    def fmt(res):
        worst = "fail" if any(r[1] == "fail" for r in res) else "warn" if any(r[1] == "warn" for r in res) else "ok"
        return worst, f"BOOT {worst} " + ",".join(f"{n}={st}" for n, st, _ in res)
    d.format_report = fmt
    return d


def _fake_deep(rows, calls):
    d = types.ModuleType("nova_deep_healthcheck")
    d.run_checks = lambda: calls.append("checks") or rows
    d.format_report = lambda res: f"DEEP {sum(r['ok'] for r in res)}/{len(res)}"
    d.write_log = lambda res: calls.append(("write_log", len(res)))
    return d


def _row(name, ok, fixed=False, fix_detail=""):
    return {"name": name, "ok": ok, "detail": f"{name} detail", "fixed": fixed, "fix_detail": fix_detail,
            "needs_human": not ok and not fixed}


def _inserts():
    return [c[0][0] for c in sc.pg.call_args_list if "INSERT INTO selfcheck_runs" in c[0][0]]


class TestSecurity(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")
        self.assertIn("BatchMode=yes", sc.SSH)

    def test_slack_token_from_keychain_never_argv(self):
        with patch.object(sc.subprocess, "check_output", return_value="xoxb-from-keychain\n") as co, \
             patch.object(sc.urllib.request, "urlopen") as uo:
            _REAL_SLACK("C1", "hi")
        self.assertEqual(co.call_args[0][0][:2], ["security", "find-generic-password"])
        self.assertEqual(uo.call_args[0][0].headers["Authorization"], "Bearer xoxb-from-keychain")

    def test_rebuild_pending_standby_never_restarted(self):
        sc.STATE_DIR.mkdir(parents=True, exist_ok=True)
        sc.PG_REBUILD_PENDING.write_text(json.dumps({"ips": ["192.168.1.125"]}))
        answers = iter(["1|0", "192.168.1.10,192.168.1.7", "192.168.1.10,192.168.1.7"])
        sc.pg.side_effect = lambda *a, **k: next(answers)
        sc.check_replication()
        self.assertFalse(any("docker restart pg17-replica" in " ".join(c) for c in _cmds()))
        self.assertEqual(sc.results[-1][1], "ok")

    def test_backup_rerun_at_most_once_per_day(self):
        sc.pg.return_value = "0"
        sc.sh.return_value = (0, "done")
        sc.check_backups(); sc.results.clear(); sc.check_backups()
        reruns = [c for c in _cmds() if "nova_backup_agent.sh incremental" in " ".join(c)]
        self.assertEqual(len(reruns), 1)
        self.assertEqual(sc.results[-1][2], "rerun already attempted today")


    def test_redline_blocks_destructive_fixes(self):
        for desc in ("pg: promote standby to primary", "reboot the host", "delete old backups", "buy storage"):
            ok, detail = sc.fix_gate(desc, lambda: (True, "ran"))
            self.assertFalse(ok)
            self.assertIn("redline-blocked", detail)

    def test_every_fix_description_in_source_passes_the_redline(self):
        # the gate now wraps the 30-min fixes too; none of their descriptions may trip it (behaviour unchanged)
        descs = re.findall(r'\bfix\(f?"([^"]+)"', SRC)
        self.assertGreaterEqual(len(descs), 8)
        for d in descs:
            self.assertIsNone(sc._REDLINE.search(re.sub(r"\{[^}]*\}", "x", d)), d)

    def test_dry_run_default_mode_fixes_posts_and_writes_nothing(self):
        sc.sh.side_effect = lambda cmd, timeout=60: (0, "") if cmd[0] == "ping" else (1, "")
        sc.pg.return_value = None                                       # primary down -> CRITICAL + alert path
        with patch.object(sc, "slack", _REAL_SLACK), patch.object(sc.urllib.request, "urlopen") as uo, \
             patch.object(sc.subprocess, "check_output") as co, patch.object(sc, "mount_problem", return_value="not mounted"):
            self.assertEqual(sc.main(["--dry-run"]), 0)
        self.assertEqual([c for c in _cmds() if c[0] not in ("ping", "curl")], [])   # no restart, umount, claude
        uo.assert_not_called(); co.assert_not_called()
        self.assertEqual(_inserts(), [])
        self.assertIn("[dry-run] would: svc-gateway-v2: restart", sc.LOG.read_text())
        self.assertFalse((sc.STATE_DIR / "selfcheck_escalation.ts").exists())


class TestPerformance(unittest.TestCase):
    def test_voiceless_and_mount_parse_10k_fast(self):
        healthy = json.dumps({"ok": True, "backends": {"ollama": {"healthy": True}}})
        mount_out = "\n".join(f"/dev/disk{i} on /Volumes/v{i} (apfs, local)" for i in range(10_000))
        t0 = time.perf_counter()
        for _ in range(10_000):
            sc._gateway_voiceless(healthy)
        with patch.object(sc, "sh", return_value=(0, mount_out)):
            self.assertEqual(sc.mount_state("/Volumes/v9999")[0], "/dev/disk9999")
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_fix_gate_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            sc.fix_gate(f"svc-{i}: restart", lambda: (True, "x"))
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_lan_blip_retried_once_then_run_proceeds(self):
        pings = iter([(1, ""), (0, "")])
        sc.sh.side_effect = lambda cmd, timeout=60: next(pings) if cmd[0] == "ping" else (1, "")
        with patch.object(sc, "check_primary", return_value=False) as cp, \
             patch.object(sc, "check_services"), patch.object(sc, "check_mounts"), \
             patch.object(sc, "post_digest"), patch.object(sc, "escalate"):
            sc.main([])
        cp.assert_called_once()
        sc.time.sleep.assert_any_call(60)

    def test_lan_down_twice_skips_everything(self):
        sc.sh.side_effect = lambda cmd, timeout=60: (1, "")
        with patch.object(sc, "check_primary") as cp, patch.object(sc, "post_digest") as pd:
            self.assertEqual(sc.main([]), 0)
        cp.assert_not_called(); pd.assert_not_called()
        self.assertIn("local network down", sc.LOG.read_text())

    def test_sh_fails_open(self):
        with patch.object(sc.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(_REAL_SH(["x"]), (124, "timeout"))
        with patch.object(sc.subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(_REAL_SH(["x"]), (1, "nope"))


class TestUnit(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_gateway_voiceless(self):
        self.assertFalse(sc._gateway_voiceless("not json"))
        self.assertFalse(sc._gateway_voiceless('{"status":"ok"}'))
        self.assertTrue(sc._gateway_voiceless('{"ok":true,"backends":{"ollama":{"healthy":false},'
                                              '"openrouter":{"healthy":true}}}'))
        self.assertFalse(sc._gateway_voiceless('{"ok":true,"backends":{"mlx":{"healthy":true}}}'))

    def test_mount_problem_states(self):
        with patch.object(sc, "mount_state", return_value=(None, "")):
            self.assertEqual(sc.mount_problem("/Volumes/nas", "192.168.1.69"), "not mounted")
        with patch.object(sc, "mount_state", return_value=("//kochj@192.168.1.11/nas", "smbfs")):
            self.assertIn("want 192.168.1.69", sc.mount_problem("/Volumes/nas", "192.168.1.69"))
        with patch.object(sc, "mount_state", return_value=("//kochj@192.168.1.69/nas", "smbfs, read-only")):
            self.assertIn("read-only", sc.mount_problem("/Volumes/nas", "192.168.1.69"))
        with patch.object(sc, "mount_state", return_value=("/dev/disk9", "apfs")):
            self.assertIsNone(sc.mount_problem(_TMP.name, None))     # writable tempdir

    def test_fix_runs_sh_or_returns_125_when_gated(self):
        sc.sh.return_value = (0, "done")
        self.assertEqual(sc.fix("svc-x: restart", ["true"]), (0, "done"))
        sc.DRY = True
        self.assertEqual(sc.fix("svc-x: restart", ["true"]), (125, "[dry-run] would: svc-x: restart"))
        self.assertEqual(sc.sh.call_count, 1)

    def test_fix_gate_contains_exceptions(self):
        ok, detail = sc.fix_gate("svc: restart", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertFalse(ok)
        self.assertEqual(detail, "fix error: boom")

    def test_modes_are_exclusive(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit) as e:
            sc.main(["--boot", "--deep"])
        self.assertEqual(e.exception.code, 2)

    def test_ingest_age_thresholds(self):
        sc.pg.return_value = str(3600)
        sc.check_ingest()
        self.assertEqual(sc.results[-1][1], "ok")
        sc.sh.assert_not_called()


class TestIntegration(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_results_persisted_to_selfcheck_runs(self):
        sc.sh.side_effect = lambda cmd, timeout=60: (0, "") if cmd[0] == "ping" else (0, '{"status":"ok"}')
        with patch.object(sc, "check_primary", side_effect=lambda: sc.record("pg-primary", "ok") or True), \
             patch.object(sc, "check_replication"), patch.object(sc, "check_backups"), \
             patch.object(sc, "check_heartbeats"), patch.object(sc, "check_ingest"), patch.object(sc, "check_disks"), \
             patch.object(sc, "check_mounts"), patch.object(sc, "post_digest"), patch.object(sc, "escalate") as esc:
            sc.main([])
        inserts = [c[0][0] for c in sc.pg.call_args_list if "INSERT INTO selfcheck_runs" in c[0][0]]
        self.assertEqual(len(inserts), 3)                          # pg-primary + two services
        self.assertIn("$novaq$pg-primary$novaq$", inserts[0])
        esc.assert_not_called()

    def test_service_restart_only_when_unhealthy(self):
        replies = {"http://127.0.0.1:18790/health": '{"status":"ok"}',
                   "http://127.0.0.1:18792/health": '{"ok":true,"backends":{"ollama":{"healthy":false}}}'}
        sc.sh.side_effect = lambda cmd, timeout=60: (0, replies.get(cmd[-1], "")) if cmd[0] == "curl" else (0, "")
        sc.check_services()
        restarts = [c for c in _cmds() if c[0] != "curl"]
        self.assertEqual(restarts, [["launchctl", "kickstart", "-k", "gui/501/net.digitalnoise.nova-gateway-v2"]])
        self.assertEqual([r[1] for r in sc.results], ["ok", "FAIL"])


    def test_boot_records_boot_rows_and_never_runs_deep(self):
        calls = []
        doc = _fake_doctor([("Volumes", "ok", "fine"), ("Face stack", "fail", "timeout"), ("Disk", "warn", "88%")], calls)
        with _modstub({"nova_doctor": doc, "nova_deep_healthcheck": _fake_deep([], calls)}):
            rc = sc.main(["--boot", "--now"])
        self.assertEqual(rc, 1)
        self.assertNotIn("checks", calls)                         # boot never waits on the deep checks
        self.assertNotIn("wait", calls)                           # --now
        self.assertEqual([r[:2] for r in sc.results],
                         [("boot-volumes", "ok"), ("boot-face-stack", "FAIL"), ("boot-disk", "WARN")])
        self.assertEqual(len(_inserts()), 3)
        self.assertIn("$novaq$boot-face-stack$novaq$", _inserts()[1])
        self.assertEqual(sc.slack.call_args[0][0], sc.SLACK_ALERT_CHANNEL)

    def test_deep_records_deep_rows_and_keeps_deep_healthcheck_log(self):
        calls = []
        rows = [_row("postgres", True), _row("plex", False, fixed=True, fix_detail="refreshed")]
        with _modstub({"nova_deep_healthcheck": _fake_deep(rows, calls)}):
            rc = sc.main(["--deep"])
        self.assertEqual(rc, 0)
        self.assertEqual(sc.results, [("deep-postgres", "ok", None, "postgres detail"),
                                      ("deep-plex", "fixed", "refreshed", "plex detail")])
        self.assertIn(("write_log", 2), calls)
        self.assertEqual(sc.slack.call_args[0], (sc.SLACK_ALERT_CHANNEL, "DEEP 1/2"))   # something was fixed


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_failure_escalates_once_then_cooldown(self):
        sc.escalate([("pg-primary", "CRITICAL", None, "down")])
        sc.escalate([("pg-primary", "CRITICAL", None, "down")])
        claude = [c for c in _cmds() if c[0] == sc.CLAUDE]
        self.assertEqual(len(claude), 1)
        self.assertIn("pg-primary: down", claude[0][2])
        self.assertEqual(sc.slack.call_count, 1)
        self.assertIn("cooldown", sc.LOG.read_text())

    def test_primary_down_alerts_without_failover(self):
        sc.pg.return_value = None
        self.assertFalse(sc.check_primary())
        self.assertEqual(sc.results[-1][1], "CRITICAL")
        self.assertEqual(sc.slack.call_args[0][0], sc.SLACK_ALERT_CHANNEL)
        self.assertFalse(any("pg_ctl" in " ".join(c) or "promote" in " ".join(c) for c in _cmds()))

    def test_digest_posts_once_per_day(self):
        sc.pg.side_effect = lambda sql, **k: "backups|ok|48" if "GROUP BY" in sql else ""
        with patch.object(sc, "FORCE_DIGEST", True):
            sc.post_digest()
        self.assertIn("backups: ok×48", sc.slack.call_args[0][1])
        self.assertEqual((sc.STATE_DIR / "selfcheck_digest.date").read_text(), time.strftime("%Y-%m-%d"))


    def test_boot_golden_path_waits_heals_then_posts_digest(self):
        calls = []
        with _modstub({"nova_doctor": _fake_doctor([("Disk", "ok", "78%")], calls)}):
            self.assertEqual(sc.main(["--boot"]), 0)
        self.assertEqual(calls, ["wait", "heal", "Disk"])
        self.assertEqual(sc.slack.call_args[0], (sc.SLACK_DIGEST_CHANNEL, "BOOT ok Disk=ok"))

    def test_boot_dry_run_skips_heal_and_posts_nothing(self):
        calls = []
        with _modstub({"nova_doctor": _fake_doctor([("Disk", "ok", "x")], calls)}), \
             patch.object(sc, "slack", _REAL_SLACK), patch.object(sc.urllib.request, "urlopen") as uo:
            sc.main(["--boot", "--dry-run"])
        self.assertEqual(calls, ["wait", "Disk"])
        uo.assert_not_called()
        self.assertEqual(_inserts(), [])

    def test_boot_crashing_check_is_contained(self):
        doc = _fake_doctor([], [])
        doc.CHECKS = [("MLX", lambda: 1 / 0)]
        with _modstub({"nova_doctor": doc}):
            self.assertEqual(sc.main(["--boot", "--now"]), 1)
        self.assertEqual(sc.results[0][:2], ("boot-mlx", "FAIL"))
        self.assertIn("check crashed", sc.results[0][3])

    def test_deep_broken_exits_1_dry_run_skips_log(self):
        calls = []
        with _modstub({"nova_deep_healthcheck": _fake_deep([_row("gateway", False)], calls)}):
            self.assertEqual(sc.main(["--deep", "--dry-run"]), 1)
        self.assertEqual(calls, ["checks"])                       # no deep_healthcheck_log write in dry-run
        self.assertEqual(sc.results[0][:2], ("deep-gateway", "FAIL"))
        self.assertEqual(_inserts(), [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_selfcheck.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        for flag in ("--boot", "--deep", "--dry-run", "--now"):
            self.assertIn(flag, r.stdout)

    def test_import_never_runs_main(self):
        # running the script performs live checks and self-heals, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_selfcheck"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
