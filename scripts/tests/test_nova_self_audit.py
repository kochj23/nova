#!/usr/bin/env python3
"""Tests for nova_self_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import atexit
import importlib.util
import io
import json
import os
import re
import socket
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
SCRIPT = SCRIPTS / "nova_self_audit.py"
SRC = SCRIPT.read_text()

_TMP = tempfile.TemporaryDirectory()            # fake $HOME: scripts dir, scheduler.yaml, state file and audit log all live here
atexit.register(_TMP.cleanup)
HOME = Path(_TMP.name)
for sub in (".openclaw/logs", ".openclaw/scripts", ".openclaw/config", ".openclaw/workspace/state"):
    (HOME / sub).mkdir(parents=True, exist_ok=True)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": nn}), patch("pathlib.Path.home", return_value=HOME), \
         patch("os.path.expanduser", lambda p: p.replace("~", str(HOME))):
        spec.loader.exec_module(mod)
    return mod


sa = _load("sa", SCRIPT)
YAML = """scheduler:
  interval: 60
tasks:
  daily_news:
    script: nova_news.py
    enabled: true
  old_job:
    script: nova_gone.py
    enabled: false
  dream:
    script: dream_pipeline.py
"""


def _fs(scripts=("nova_news.py", "dream_pipeline.py"), yaml=YAML):
    for p in sa.SCRIPTS_DIR.glob("*"):
        p.unlink()
    for s in scripts:
        (sa.SCRIPTS_DIR / s).write_text("#!/usr/bin/env python3\n")
    if yaml is None:
        sa.SCHEDULER_YAML.unlink(missing_ok=True)
    else:
        sa.SCHEDULER_YAML.write_text(yaml)
    sa.AUDIT_STATE_FILE.unlink(missing_ok=True)
    sa.notify.reset_mock()


def _audit(ports=True, procs=True):
    out = io.StringIO()
    with patch.object(sa, "_port_listening", lambda port, host="127.0.0.1": ports), \
         patch.object(sa, "_process_running", lambda m: procs), redirect_stdout(out):
        n = sa.run_audit()
    return n, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC); self.assertNotIn("os.system", SRC)
        self.assertNotIn("urlopen", SRC)                         # reporting goes through the notify bus only

    def test_pgrep_is_argv_and_the_match_string_is_never_interpreted(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            sa._process_running("x; rm -rf /")
        self.assertEqual(run.call_args.args[0], ["pgrep", "-f", "x; rm -rf /"])

    def test_probe_targets_are_private_addresses_only(self):
        for svc in sa.EXPECTED_SERVICES.values():
            self.assertRegex(svc["host"], r"^(127\.0\.0\.1|192\.168\.\d+\.\d+)$")

    def test_hostile_scheduler_yaml_is_parsed_not_executed(self):
        _fs(yaml="tasks:\n  evil:\n    script: $(rm -rf /) nova_x.py\n")
        refs = sa._scripts_in_scheduler()
        self.assertEqual(refs, {"evil": "$(rm -rf /) nova_x.py"})
        issues, _, _, _, _ = sa.audit_scripts()
        self.assertIn("doesn't exist", issues[0])


class TestPerformance(unittest.TestCase):
    def test_scheduler_parse_on_10k_tasks(self):
        text = "tasks:\n" + "".join(f"  t{i}:\n    script: nova_s{i}.py\n    enabled: {'false' if i % 3 else 'true'}\n" for i in range(10_000))
        _fs(yaml=text)
        t0 = time.perf_counter()
        refs = sa._scripts_in_scheduler()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(refs), 3_334)

    def test_script_regex_on_a_large_file(self):
        p = HOME / "big.txt"
        p.write_text(" ".join(f"nova_s{i}.py dream_d{i}.sh other{i}.py" for i in range(10_000)))
        t0 = time.perf_counter()
        found = sa._scripts_in_file(p)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(found), 20_000)


class TestRetry(unittest.TestCase):
    def test_process_check_fails_open_to_not_running(self):
        # RETRY GAP: _process_running — one pgrep; a timeout is swallowed and reported as "not running"
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pgrep", 5)) as run:
            self.assertFalse(sa._process_running("nova_scheduler.py"))
        self.assertEqual(run.call_count, 1)

    def test_port_probe_fails_open_to_not_listening(self):
        # RETRY GAP: _port_listening — one connect; refused/timeout/OSError all read as down
        class Boom:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def settimeout(self, t): pass
            def connect(self, addr): raise ConnectionRefusedError()
        with patch("socket.socket", return_value=Boom()):
            self.assertFalse(sa._port_listening(18792, "192.168.1.2"))

    def test_corrupt_state_file_is_treated_as_empty(self):
        sa.AUDIT_STATE_FILE.write_text("{not json")
        self.assertEqual(sa._load_last_audit_state(), {})
        sa.AUDIT_STATE_FILE.unlink()


class TestUnit(unittest.TestCase):
    def test_scheduler_parse_drops_disabled_and_top_level_keys(self):
        _fs()
        self.assertEqual(sa._scripts_in_scheduler(), {"daily_news": "nova_news.py", "dream": "dream_pipeline.py"})
        _fs(yaml=None)
        self.assertEqual(sa._scripts_in_scheduler(), {})

    def test_scripts_in_file_regex_and_missing_file(self):
        p = HOME / "doc.md"; p.write_text("run nova_a.py then dream_b.sh but not test_c.py or nova_d.txt")
        self.assertEqual(sa._scripts_in_file(p), {"nova_a.py", "dream_b.sh"})
        self.assertEqual(sa._scripts_in_file(HOME / "nope.md"), set())

    def test_scripts_on_disk_lists_py_and_sh_only(self):
        _fs(scripts=("nova_a.py", "nova_b.sh", "notes.txt"))
        self.assertEqual(sa._scripts_on_disk(), {"nova_a.py", "nova_b.sh"})

    def test_audit_docs_is_a_noop_and_state_roundtrips(self):
        self.assertEqual(sa.audit_docs(), [])
        sa._save_audit_state({"last_issue_key": "[]"})
        self.assertEqual(sa._load_last_audit_state(), {"last_issue_key": "[]"})
        sa.AUDIT_STATE_FILE.unlink()


class TestIntegration(unittest.TestCase):
    def test_report_goes_through_the_shared_notify_bus(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("def notify(", SRC)
        sa.notify.reset_mock()
        sa.slack_post("*Nova Self-Audit Report*\n\n*Issues (1):*\n  !! Plex is not listening")
        kw = sa.notify.call_args.kwargs
        self.assertEqual(sa.notify.call_args.args[0], "Nova Self-Audit Report")
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("critical", "health", "self-audit"))
        sa.slack_post("*Nova Self-Audit Report*\n\nAll clear — no discrepancies found.")
        self.assertEqual(sa.notify.call_args.kwargs["level"], "warning")

    def test_audit_scripts_composes_disk_and_scheduler(self):
        _fs(scripts=("nova_news.py", "nova_extra.py", "nova_agent_x.py", "test_y.py", "helper.py"))
        issues, info, disk, _, sched = sa.audit_scripts()
        self.assertEqual(issues, ["Scheduler task `dream` references `dream_pipeline.py` but it doesn't exist"])
        self.assertEqual((disk, sched), (5, 2))
        self.assertEqual(info, ["1 scripts on disk not in scheduler:", "  - nova_extra.py"])

    def test_service_and_process_audits_name_what_is_down(self):
        with patch.object(sa, "_port_listening", lambda port, host="127.0.0.1": port != 11434):
            issues, ok = sa.audit_services()
        self.assertEqual(issues, ["Ollama (:11434) is not listening"]); self.assertEqual(len(ok), len(sa.EXPECTED_SERVICES) - 1)
        with patch.object(sa, "_process_running", lambda m: m != "nova_scheduler.py"):
            issues, ok = sa.audit_processes()
        self.assertEqual(issues, ["Scheduler (`nova_scheduler.py`) is not running"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_all_clear_posts_nothing_and_records_state(self):
        _fs()
        n, out = _audit()
        self.assertEqual(n, 0)
        self.assertIn("*Scripts:* 2 on disk, 2 in scheduler", out)
        self.assertIn(f"*Services:* {len(sa.EXPECTED_SERVICES)}/{len(sa.EXPECTED_SERVICES)} up", out)
        self.assertIn("All clear — no discrepancies found.", out)
        self.assertEqual(json.loads(sa.AUDIT_STATE_FILE.read_text())["last_issue_key"], "[]")
        sa.notify.assert_called_once()                    # first run: previous key "" != "[]" -> all-clear is posted
        n, _ = _audit()
        sa.notify.assert_called_once()                    # unchanged all-clear: no repeat

    def test_issue_path_posts_once_then_dedups(self):
        _fs(scripts=("nova_news.py",))
        n, out = _audit(ports=False)
        self.assertEqual(n, 1 + len(sa.EXPECTED_SERVICES))
        self.assertIn("!! Scheduler task `dream` references `dream_pipeline.py`", out)
        sa.notify.assert_called_once()
        self.assertEqual(sa.notify.call_args.kwargs["level"], "critical")
        _audit(ports=False)
        sa.notify.assert_called_once()                    # identical issue set: skipped
        _audit(ports=True)
        self.assertEqual(sa.notify.call_count, 2)          # the set changed: posted again


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_main_is_guarded(self):
        # no --help; running the script probes the fleet, so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("run_audit()\n    sys.exit(0)", SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_self_audit"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
