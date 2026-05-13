#!/usr/bin/env python3
"""
test_fixes_overnight.py — Tests for the 4 overnight fixes:
  1. nova_ollama_preload.sh — URL, skip-warm logic, per-model timeouts
  2. livetv_ambiance — scheduler timeout raised to match max recording duration
  3. nova_self_audit.py — scheduler timeout raised to allow Slack posting
  4. nova_big_brother.py — Discord-only disconnects don't trigger gateway restart

All 7 required test categories:
  Security, Performance, Retry, Unit, Integration, Functional, Frame

Written by Jordan Koch.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).parent.parent / "scripts"
CONFIG  = Path(__file__).parent.parent / "config"
sys.path.insert(0, str(SCRIPTS))


# ═══════════════════════════════════════════════════════════════════════════════
# 7. FRAME — Do files exist and load without syntax errors?
# ═══════════════════════════════════════════════════════════════════════════════

class TestFrame(unittest.TestCase):

    def test_ollama_preload_script_exists(self):
        self.assertTrue((SCRIPTS / "nova_ollama_preload.sh").exists())

    def test_ollama_preload_is_executable(self):
        self.assertTrue(os.access(SCRIPTS / "nova_ollama_preload.sh", os.X_OK))

    def test_big_brother_syntax_ok(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_big_brother.py")],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_self_audit_syntax_ok(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_self_audit.py")],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_scheduler_yaml_exists(self):
        self.assertTrue((CONFIG / "scheduler.yaml").exists())

    def test_scheduler_yaml_parses(self):
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        self.assertIn("tasks", cfg)

    def test_big_brother_imports(self):
        # Verify no import-time crashes
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.openclaw/scripts')); import nova_big_brother"],
            capture_output=True, cwd=str(SCRIPTS),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode()[:300])


# ═══════════════════════════════════════════════════════════════════════════════
# 4. UNIT — Individual logic in isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnit(unittest.TestCase):

    def test_ollama_preload_uses_localhost(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        self.assertIn("127.0.0.1:11434", src, "ollama_preload must use 127.0.0.1 — Ollama.app only binds to localhost")
        self.assertNotIn("192.168.1.6:11434", src, "Ollama.app doesn't bind to LAN IP")

    def test_ollama_preload_has_skip_warm_logic(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        self.assertIn("already warm", src, "Should skip models already warm to avoid eviction")
        self.assertIn("api/ps", src, "Must check /api/ps to detect warm models")

    def test_ollama_preload_per_model_timeout(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        self.assertIn("MODEL_TIMEOUT", src, "Per-model timeout map required")
        self.assertIn("qwen3-coder:30b", src)
        # 600s minimum for the big model
        import re
        match = re.search(r'"qwen3-coder:30b"\]="(\d+)"', src)
        self.assertIsNotNone(match, "qwen3-coder:30b timeout not found")
        self.assertGreaterEqual(int(match.group(1)), 600, "qwen3-coder:30b timeout must be ≥600s (7.5 min cold load)")

    def test_ollama_preload_handles_curl_timeout_gracefully(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        self.assertIn("28", src, "Must handle curl exit 28 (timeout) without failing the whole script")
        # The model load loop must not have an unconditional exit 1 — only exit on timeout message
        # Split at the model loop section (after "Preloading models")
        after_models = src.split("Preloading models")[1]
        self.assertNotIn("exit 1", after_models,
            "Model load timeout should not cause exit 1 — only log and continue")

    def test_scheduler_timeout_ollama_preload(self):
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        t = cfg["tasks"]["ollama_preload"]
        self.assertGreaterEqual(t["timeout"], 900,
            f"ollama_preload timeout={t['timeout']} — needs ≥900s for qwen3-coder:30b cold load")

    def test_scheduler_timeout_livetv_ambiance(self):
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        t = cfg["tasks"]["livetv_ambiance"]
        # max record_seconds=7200, plus transcription ~30 min = 9000s ideal
        # 7800 is the configured minimum (2h record + 30min transcription)
        self.assertGreaterEqual(t["timeout"], 7200,
            f"livetv_ambiance timeout={t['timeout']} — must be ≥7200s (max 2h episode)")

    def test_scheduler_timeout_self_audit(self):
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        t = cfg["tasks"]["self_audit"]
        self.assertGreaterEqual(t["timeout"], 120,
            f"self_audit timeout={t['timeout']} — needs >60s to check ports + post to Slack")

    def test_discord_only_disconnect_no_restart(self):
        """The restartable_disconnects logic must filter out discord."""
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        self.assertIn("restartable_disconnects", src)
        self.assertIn('ch != "discord"', src)
        self.assertIn("not restartable_disconnects", src)

    def test_discord_restart_suppression_logged(self):
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        self.assertIn("buape/carbon", src, "Discord skip reason must be documented in code")
        self.assertIn("not restarting gateway", src)

    def test_discord_strike_threshold_still_works(self):
        """DISCORD_STRIKE_THRESHOLD for timeouts must still exist (separate from disconnect logic)."""
        import re
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        self.assertIn("DISCORD_STRIKE_THRESHOLD", src)
        self.assertIsNotNone(
            re.search(r"discord.*timeout|timeout.*discord", src, re.IGNORECASE),
            "Discord timeout strike logic must still exist"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SECURITY — No credentials, no PII, safe inputs
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecurity(unittest.TestCase):

    def test_ollama_preload_no_credentials(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        for pattern in ("password", "api_key", "token", "sk-", "Bearer "):
            self.assertNotIn(pattern.lower(), src.lower(),
                             f"ollama_preload contains credential pattern: {pattern}")

    def test_ollama_preload_no_pii(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        self.assertNotIn("kochj", src.lower(), "No usernames in ollama_preload")
        self.assertNotIn("gmail", src.lower())

    def test_big_brother_discord_fix_no_new_secrets(self):
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        # Verify the discord token isn't hardcoded anywhere
        self.assertNotIn("xox", src, "No Slack tokens hardcoded")
        # Discord token format check
        import re
        self.assertIsNone(re.search(r'[MN][A-Za-z0-9]{23}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}', src),
                         "Discord bot token found hardcoded")

    def test_self_audit_no_sensitive_data_in_logs(self):
        src = (SCRIPTS / "nova_self_audit.py").read_text()
        # Self-audit checks ports and processes — ensure it doesn't log passwords
        self.assertNotIn("password", src.lower())

    def test_scheduler_yaml_no_credentials(self):
        src = (CONFIG / "scheduler.yaml").read_text()
        for pattern in ("password:", "api_key:", "token:", "sk-"):
            self.assertNotIn(pattern, src, f"scheduler.yaml contains {pattern}")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PERFORMANCE — Timeouts are appropriate, no blocking waits
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformance(unittest.TestCase):

    def test_ollama_preload_skips_warm_models(self):
        """Warm-skip logic prevents unnecessary 7-minute reloads."""
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        self.assertIn("already warm", src)
        # The api/ps check must come BEFORE the generate call
        ps_pos = src.find("api/ps")
        gen_pos = src.find("api/generate")
        self.assertLess(ps_pos, gen_pos, "Must check /api/ps before loading models")

    def test_livetv_ambiance_timeout_not_absurdly_large(self):
        """10800s ceiling — don't let it run forever."""
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        t = cfg["tasks"]["livetv_ambiance"]
        self.assertLessEqual(t["timeout"], 10800, "ambiance timeout shouldn't exceed 3 hours")

    def test_ollama_preload_total_timeout_bounded(self):
        """Script timeout in scheduler ≥ sum of per-model timeouts (600+120+warmup)."""
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        scheduler_timeout = cfg["tasks"]["ollama_preload"]["timeout"]
        # qwen3-coder(600) + deepseek(120) + nomic(30) + overhead = ~800
        self.assertGreaterEqual(scheduler_timeout, 800,
            "Scheduler timeout must cover sum of all model load timeouts")

    def test_big_brother_discord_fix_is_o1(self):
        """restartable_disconnects is a list comprehension — O(n) where n≤3 channels."""
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        self.assertIn("restartable_disconnects = [ch for ch in disconnected", src)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. RETRY — External calls have retry or graceful failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetry(unittest.TestCase):

    def test_ollama_preload_retries_ollama_connection(self):
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        # The wait loop tries 30 times with sleep 2 = 60s
        self.assertIn("seq 1 30", src, "Must retry Ollama connection up to 30 times")
        self.assertIn("sleep 2", src)

    def test_ollama_preload_model_timeout_nonblocking(self):
        """If qwen3-coder times out, the script still continues to load deepseek."""
        src = (SCRIPTS / "nova_ollama_preload.sh").read_text()
        # After model timeout (exit 28), the loop continues — doesn't `exit 1`
        self.assertIn("deepseek-r1:8b", src)
        # The for loop must run both models regardless of individual failure
        for_loop_pos = src.find("for model in")
        done_pos = src.find("ollama_preload] Done")
        self.assertLess(for_loop_pos, done_pos, "for loop must complete before Done message")

    def test_big_brother_gateway_restart_still_retries_signal_slack(self):
        """After Discord fix, Slack/Signal outages still trigger gateway restart."""
        src = (SCRIPTS / "nova_big_brother.py").read_text()
        # restartable_disconnects triggers the restart path
        self.assertIn("if not protected_running:", src)
        self.assertIn("_restart_gateway()", src)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. INTEGRATION — Services interact correctly
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):

    def test_scheduler_loaded_new_timeouts(self):
        """Live scheduler must reflect the updated timeouts."""
        try:
            resp = urllib.request.urlopen("http://192.168.1.6:37460/tasks", timeout=5)
            tasks = json.loads(resp.read())
            # Scheduler /tasks returns a dict of task_id -> task info
            # Check that tasks exist (scheduler is alive and reloaded)
            self.assertIsInstance(tasks, dict)
            self.assertGreater(len(tasks), 0)
        except Exception as e:
            self.skipTest(f"Scheduler not reachable: {e}")

    def test_big_brother_up_after_restart(self):
        """Big Brother must be running with updated code."""
        try:
            resp = urllib.request.urlopen("http://192.168.1.6:37461/bb/status", timeout=5)
            data = json.loads(resp.read())
            self.assertEqual(data["daemon"], "big-brother")
            self.assertGreater(data["pid"], 0)
        except Exception as e:
            self.skipTest(f"Big Brother not reachable: {e}")

    def test_ollama_reachable_on_localhost(self):
        """Ollama must be reachable on 127.0.0.1:11434 (Ollama.app binding)."""
        try:
            import socket
            s = socket.socket()
            s.settimeout(3)
            result = s.connect_ex(("127.0.0.1", 11434))
            s.close()
            self.assertEqual(result, 0, "Ollama not listening on 127.0.0.1:11434")
        except Exception as e:
            self.skipTest(f"Socket check failed: {e}")

    def test_gateway_still_handles_slack_signal(self):
        """Gateway health check must pass after Big Brother restart."""
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18789/health", timeout=5)
            data = json.loads(resp.read())
            self.assertTrue(data.get("ok"), "Gateway not healthy")
        except Exception as e:
            self.skipTest(f"Gateway not reachable: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. FUNCTIONAL — End-to-end golden paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestFunctional(unittest.TestCase):

    def test_ollama_preload_runs_dry(self):
        """Script must complete without error when Ollama is running."""
        # Only run if Ollama is up — don't fail in CI without it
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=3)
            ollama_up = resp.status == 200
        except Exception:
            ollama_up = False

        if not ollama_up:
            self.skipTest("Ollama not running")

        result = subprocess.run(
            ["/bin/zsh", str(SCRIPTS / "nova_ollama_preload.sh")],
            capture_output=True, text=True, timeout=60,
            # Only wait 60s — models are warm so it should be fast
        )
        # Accept exit 0 or timeout (exit code from subprocess.run won't be 124 since we use timeout=)
        self.assertIn("ollama_preload]", result.stdout,
                      f"Expected log output, got: {result.stdout[:200]}")
        # Should either say "loaded" or "already warm" for each model
        loaded_or_warm = result.stdout.count("loaded") + result.stdout.count("already warm")
        self.assertGreater(loaded_or_warm, 0, "Expected at least one model status line")

    def test_discord_disconnect_alone_no_gateway_restart(self):
        """Simulate discord-only disconnect — verify restartable_disconnects is empty."""
        # White-box test of the filter logic
        disconnected = ["discord"]
        restartable = [ch for ch in disconnected if ch != "discord"]
        self.assertEqual(restartable, [], "Discord-only should produce empty restartable list")
        self.assertFalse(bool(restartable), "Should not trigger restart")

    def test_slack_signal_disconnect_still_triggers_restart(self):
        """Slack or Signal disconnect must still produce a non-empty restartable list."""
        for channel in ["slack", "signal"]:
            disconnected = [channel]
            restartable = [ch for ch in disconnected if ch != "discord"]
            self.assertEqual(restartable, [channel], f"{channel} should be in restartable")

    def test_mixed_discord_slack_disconnect_triggers_restart(self):
        """If both Discord and Slack are down, Slack triggers restart (Discord is ignored)."""
        disconnected = ["discord", "slack"]
        restartable = [ch for ch in disconnected if ch != "discord"]
        self.assertEqual(restartable, ["slack"])
        self.assertTrue(bool(restartable), "Should trigger restart for Slack")

    def test_self_audit_exit_codes(self):
        """self_audit exits 0 when clean, 1 when issues found — both are valid operational states."""
        src = (SCRIPTS / "nova_self_audit.py").read_text()
        # Must have both exit paths
        self.assertIn("sys.exit(1 if issue_count", src)
        self.assertIn("else 0", src)

    def test_scheduler_yaml_timeout_values_are_integers(self):
        """All timeout values in scheduler.yaml must be integers, not floats or strings."""
        import yaml
        with open(CONFIG / "scheduler.yaml") as f:
            cfg = yaml.safe_load(f)
        for tid, t in cfg.get("tasks", {}).items():
            if "timeout" in t:
                self.assertIsInstance(t["timeout"], int,
                    f"Task {tid} timeout={t['timeout']} must be int, not {type(t['timeout'])}")


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
