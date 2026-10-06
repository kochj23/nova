#!/usr/bin/env python3
"""Tests for nova_gateway/config.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "config.py"
SRC = SCRIPT.read_text()


def _load():
    # by path, so the package __init__ (which pulls in the whole gateway) never runs
    spec = importlib.util.spec_from_file_location("ngw_config", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cfg = _load()


def _r(rc=0, out=""):
    return types.SimpleNamespace(returncode=rc, stdout=out)


def _secrets(get):
    return patch.dict(sys.modules, {"nova_secrets": types.SimpleNamespace(get_secret=get)})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bap]-[0-9]")            # no Slack tokens
        self.assertNotIn("password", cfg.PG_DSN)

    def test_privacy_blocklist_catches_sensitive_content(self):
        for txt in ("my salary is", "ssh to 192.168.1.6", "the api key is", "Jordan said hi", "prescription refill"):
            self.assertTrue(cfg.is_private_content([{"content": txt}]), txt)
        self.assertFalse(cfg.is_private_content([{"content": "what is the capital of France"}]))

    def test_tokens_load_from_keychain_service_names(self):
        with patch.object(cfg, "keychain", side_effect=lambda s, account="nova": f"<{s}>") as kc:
            t = cfg.load_tokens()
        self.assertEqual(t["slack_bot"], "<nova-slack-bot-token>")
        self.assertEqual(kc.call_count, 4)


class TestPerformance(unittest.TestCase):
    def test_privacy_check_10k_messages(self):
        msgs = [{"content": f"benign message number {i} about weather"} for i in range(10_000)]
        t0 = time.perf_counter()
        self.assertFalse(cfg.is_private_content(msgs))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_keychain_miss_falls_through_to_fleet_store(self):
        get = MagicMock(return_value="from-pg")
        with patch.object(cfg.subprocess, "run", return_value=_r(44, "")) as run, _secrets(get):
            self.assertEqual(cfg.keychain("nova-x"), "from-pg")
        self.assertEqual(run.call_count, 1)
        get.assert_called_once_with("nova-x")

    def test_keyless_node_systemexit_falls_to_env(self):
        def boom(_):
            raise SystemExit("NOVA_SECRET_KEY missing")
        with patch.object(cfg.subprocess, "run", side_effect=FileNotFoundError), _secrets(boom), \
                patch.dict(os.environ, {"NOVA_X_TOKEN": "env-val"}):
            self.assertEqual(cfg.keychain("nova-x-token"), "env-val")

    def test_all_sources_empty_returns_blank(self):
        def boom(_):
            raise RuntimeError("pg down")
        with patch.object(cfg.subprocess, "run", return_value=_r(1, "")), _secrets(boom), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOVA_NOPE", None)
            self.assertEqual(cfg.keychain("nova-nope"), "")


class TestUnit(unittest.TestCase):
    def test_keychain_hit_strips_and_passes_account(self):
        with patch.object(cfg.subprocess, "run", return_value=_r(0, "  s3cr3t\n")) as run:
            self.assertEqual(cfg.keychain("svc", account="acct"), "s3cr3t")
        self.assertEqual(run.call_args[0][0], ["security", "find-generic-password", "-a", "acct", "-s", "svc", "-w"])

    def test_is_private_content_edges(self):
        self.assertFalse(cfg.is_private_content([]))
        self.assertFalse(cfg.is_private_content([{"role": "user"}]))     # no content key
        self.assertTrue(cfg.is_private_content([{"content": "x"}, {"content": "KEYCHAIN entry"}]))

    def test_limits_sane(self):
        self.assertLess(cfg.RESPONSE_RESERVE, min(cfg.CONTEXT_LIMITS.values()))
        self.assertTrue(0 < cfg.COMPACTION_THRESHOLD < 1)


class TestIntegration(unittest.TestCase):
    def test_gateway_modules_import_their_constants_from_here(self):
        for mod in ("main.py", "router.py", "session.py", "health.py"):
            self.assertIn("from nova_gateway.config import", (SCRIPTS / "nova_gateway" / mod).read_text())

    def test_version_matches_package(self):
        init = (SCRIPTS / "nova_gateway" / "__init__.py").read_text()
        self.assertIn(f'__version__ = "{cfg.VERSION}"', init)

    def test_every_channel_routes_to_a_known_agent(self):
        for ch, agent in cfg.CHANNEL_AGENT.items():
            self.assertIn(agent, cfg.CONTEXT_LIMITS, ch)


class TestFunctional(unittest.TestCase):
    def test_standby_flag_read_from_env(self):
        with patch.dict(os.environ, {"NOVA_GW_STANDBY": "1"}):
            self.assertTrue(_load().GW_STANDBY)
        with patch.dict(os.environ, {"NOVA_GW_STANDBY": "0"}):
            self.assertFalse(_load().GW_STANDBY)

    def test_private_conversation_flagged_end_to_end(self):
        convo = [{"role": "system", "content": "you are nova"},
                 {"role": "user", "content": "summarize my Synology backup log"}]
        self.assertTrue(cfg.is_private_content(convo))


class TestFrame(unittest.TestCase):
    def test_runs_as_script_silently(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_import_has_no_side_effects(self):
        self.assertNotIn('if __name__ == "__main__":', SRC)     # constants module: nothing to run
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.config as c; print(c.VERSION)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), cfg.VERSION)


if __name__ == "__main__":
    unittest.main()
