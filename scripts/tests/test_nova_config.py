#!/usr/bin/env python3
"""Tests for nova_config.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The real macOS Keychain is never read: every `security` call goes through a mocked
subprocess.run, and the pgcrypto fallback (nova_secrets) is a stub module."""
import contextlib
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
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_config.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova-config-test-"))

_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    """Install stub modules, restoring ONLY the keys we touched (never the whole sys.modules)."""
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _no_resolve():
    # a nova_resolve WITHOUT resolve_url -> `from nova_resolve import resolve_url` raises -> static fallback
    return types.ModuleType("nova_resolve")


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("nova_config_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # module load must never shell out: prove it by making subprocess.run explode during exec
    with _stub_modules({"nova_resolve": _no_resolve()}), \
         patch.object(subprocess, "run", side_effect=AssertionError("security called at import")), \
         patch.dict(os.environ, {"HOME": str(TMP)}):
        spec.loader.exec_module(mod)
    return mod


cfg = _load()


def _sec(rc=1, out=""):
    """A subprocess.run stand-in for `security find-generic-password`."""
    return MagicMock(return_value=types.SimpleNamespace(returncode=rc, stdout=out, stderr=""))


def _secrets(value=None, exc=None):
    m = types.ModuleType("nova_secrets")

    def get_secret(service):
        if exc:
            raise exc
        return value
    m.get_secret = get_secret
    return m


class _Resp:
    def __init__(self, body, status=200):
        self._body = body; self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._body).encode()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"xox[bap]-[A-Za-z0-9-]{10,}", SRC))      # no literal Slack tokens
        self.assertIsNone(re.search(r"shell\s*=\s*True", SRC))                  # keychain/osascript are argv

    def test_every_secret_accessor_goes_through_keychain(self):
        for fn in ("slack_bot_token", "slack_app_token", "openrouter_api_key", "discord_bot_token"):
            body = SRC.split(f"def {fn}(")[1].split("\ndef ")[0]
            self.assertIn("_keychain(", body, fn)
        self.assertIsNone(cfg.JORDAN_WORK_EMAIL)   # the dead work placeholder must never be a recipient

    def test_keychain_never_touched_at_import_and_env_placeholders_rejected(self):
        # module loaded with subprocess.run raising -> import itself made no `security` call (see _load)
        with patch.object(subprocess, "run", _sec(1)), _stub_modules({"nova_secrets": _secrets(None)}), \
             patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": "${NOVA_SLACK_BOT_TOKEN}"}), redirect_stderr(io.StringIO()):
            self.assertEqual(cfg.slack_bot_token(), "")

    def test_private_sources_gate_and_blocked_content(self):
        for s in ("calendar", "safari_history", "imessage", "email", "healthkit", "financial_documents", "work_memo"):
            self.assertIn(s, cfg.PRIVATE_SOURCES)
            self.assertTrue(cfg.is_private_source(s), s)
        self.assertTrue(cfg.is_private_source(cfg._EMPLOYER_PREFIX + "_hr_docs"))
        self.assertTrue(cfg._contains_blocked_content("mentions " + cfg._EMPLOYER_PREFIX + " in passing"))
        self.assertFalse(cfg.is_private_source("reddit"))
        self.assertFalse(cfg.is_private_source("television"))

    def test_notify_local_escapes_quotes_before_osascript(self):
        with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as pop:
            cfg.notify_local('t"x', 'say "hi" it\'s', critical=True)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        self.assertIn('\\"hi\\"', argv[2]); self.assertIn("\\'s", argv[2])
        pop.assert_called_once()


class TestPerformance(unittest.TestCase):
    def test_private_gate_on_10k_sources(self):
        srcs = [f"source_{i}" if i % 3 else "work_internal_x" for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(cfg.is_private_source(s) for s in srcs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(hits, 10_000 // 3 + 1)

    def test_filter_private_memories_on_10k(self):
        mems = [{"source": "reddit" if i % 2 else "calendar", "text": f"memory {i}"} for i in range(10_000)]
        t0 = time.perf_counter()
        out = cfg.filter_private_memories(mems)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 5_000)


class TestRetry(unittest.TestCase):
    def test_keychain_is_one_shot_per_backend_and_falls_through(self):
        # RETRY GAP: _keychain — `security` is tried once, then nova_secrets once, then env; no backoff
        run = _sec(1)
        with patch.object(subprocess, "run", run), _stub_modules({"nova_secrets": _secrets(None)}), \
             patch.dict(os.environ, {"NOVA_TEST_SVC": "from-env"}):
            self.assertEqual(cfg._keychain("nova-test-svc", required=False), "from-env")
        self.assertEqual(run.call_count, 1)

    def test_keychain_survives_missing_security_binary_and_exiting_secret_store(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("no security on linux")), \
             _stub_modules({"nova_secrets": _secrets(exc=SystemExit(1))}), \
             patch.dict(os.environ, {}, clear=False), redirect_stderr(io.StringIO()) as err:
            os.environ.pop("NOVA_ABSENT_SVC", None)
            self.assertEqual(cfg._keychain("nova-absent-svc", required=False), "")
        self.assertIn("non-fatal", err.getvalue())

    def test_required_secret_missing_exits_instead_of_returning_garbage(self):
        with patch.object(subprocess, "run", _sec(1)), _stub_modules({"nova_secrets": _secrets(None)}), \
             redirect_stderr(io.StringIO()):
            os.environ.pop("NOVA_ABSENT_SVC", None)
            with self.assertRaises(SystemExit):
                cfg._keychain("nova-absent-svc", required=True)

    def test_post_both_fails_open_on_slack_error(self):
        # RETRY GAP: post_both — one urlopen; a network error is logged, never raised, never retried
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-test"), \
             patch("urllib.request.urlopen", side_effect=OSError("down")) as u, \
             patch.object(cfg, "post_discord") as d, redirect_stderr(io.StringIO()) as err:
            cfg.post_both("hello", slack_channel=cfg.SLACK_ALERTS)
        self.assertEqual(u.call_count, 1)
        self.assertIn("Slack post failed", err.getvalue())
        d.assert_called_once_with("hello", cfg.DISCORD_NOTIFY)

    def test_post_discord_fails_open(self):
        # RETRY GAP: post_discord — single attempt, returns False on any error
        with patch.object(cfg, "discord_bot_token", return_value="tok"), \
             patch("urllib.request.urlopen", side_effect=OSError("down")) as u, redirect_stderr(io.StringIO()):
            self.assertFalse(cfg.post_discord("x", "123"))
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_keychain_happy_path_strips_and_uses_argv(self):
        run = _sec(0, "  s3cret\n")
        with patch.object(subprocess, "run", run):
            self.assertEqual(cfg._keychain("nova-slack-bot-token"), "s3cret")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["security", "find-generic-password", "-a", "nova", "-s", "nova-slack-bot-token", "-w"])
        self.assertTrue(run.call_args[1]["capture_output"])

    def test_keychain_empty_stdout_falls_to_pgcrypto_store(self):
        with patch.object(subprocess, "run", _sec(0, "   ")), _stub_modules({"nova_secrets": _secrets("from-pg")}):
            self.assertEqual(cfg._keychain("nova-x", account="other", required=False), "from-pg")

    def test_is_private_source_edges(self):
        self.assertFalse(cfg.is_private_source(None))
        self.assertFalse(cfg.is_private_source(""))
        self.assertTrue(cfg.is_private_source("  CALENDAR "))          # case/whitespace-insensitive
        self.assertTrue(cfg.is_private_source("my_email_archive_2020"))  # substring namespace
        self.assertTrue(cfg.is_private_source("apple_health"))
        self.assertFalse(cfg.is_private_source("scanner"))

    def test_truncate_at_boundary(self):
        self.assertEqual(cfg.truncate_at_boundary("short", 10), "short")
        t = cfg.truncate_at_boundary("one sentence. two sentence. " + "x" * 100, 40)
        self.assertEqual(t, "one sentence. two sentence.")
        self.assertEqual(cfg.truncate_at_boundary("a" * 50, 20), "a" * 20)
        self.assertEqual(cfg.truncate_at_boundary("word " * 20, 44), "word " * 7 + "word")

    def test_blocked_content_and_filter(self):
        self.assertFalse(cfg._contains_blocked_content(""))
        self.assertFalse(cfg._contains_blocked_content(None))
        self.assertFalse(cfg._contains_blocked_content("a perfectly public thought"))
        self.assertIs(cfg._get_blocked_keywords(), cfg._get_blocked_keywords())   # cached once
        mems = [{"source": "reddit", "text": "ok"}, {"source": "imessage", "text": "ok"},
                {"source": "reddit", "text": "about " + cfg._EMPLOYER_PREFIX}, {"text": "no source"}]
        self.assertEqual(cfg.filter_private_memories(mems), [{"source": "reddit", "text": "ok"}, {"text": "no source"}])

    def test_token_accessors_prefer_keychain_then_env(self):
        with patch.object(cfg, "_keychain", return_value="kc"):
            self.assertEqual(cfg.slack_app_token(), "kc")
            self.assertEqual(cfg.openrouter_api_key(), "kc")
            self.assertEqual(cfg.discord_bot_token(), "kc")
        with patch.object(cfg, "_keychain", return_value=""), \
             patch.dict(os.environ, {"NOVA_OPENROUTER_API_KEY": "env-or", "NOVA_DISCORD_TOKEN": "", "NOVA_SLACK_APP_TOKEN": "xapp-env"}), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cfg.openrouter_api_key(), "env-or")
            self.assertEqual(cfg.slack_app_token(), "xapp-env")
            self.assertEqual(cfg.discord_bot_token(), "")
        self.assertIn("discord_bot_token unavailable", err.getvalue())


class TestIntegration(unittest.TestCase):
    def test_static_fallback_urls_when_mesh_resolver_is_unavailable(self):
        self.assertEqual(cfg.VECTOR_URL, f"http://{cfg.NOVA_HOST}:18790/remember")
        self.assertEqual(cfg.MEMORY_URL, f"http://{cfg.NOVA_HOST}:18790")
        self.assertTrue(cfg.NC_PLEX.startswith(cfg.NOVACONTROL))

    def test_channel_map_is_built_from_the_named_constants(self):
        self.assertEqual(cfg.SLACK_INFO, cfg.SLACK_FEED)                 # deprecated alias
        self.assertEqual(cfg.CHANNEL_MAP[cfg.SLACK_FEED], "")           # firehose never mirrors
        self.assertEqual(cfg.CHANNEL_MAP[cfg.SLACK_DIGEST], "")
        for ch in (cfg.SLACK_NOTIFY, cfg.SLACK_EMAIL, cfg.SLACK_PHOTOS, cfg.SLACK_ALERTS):
            self.assertEqual(cfg.CHANNEL_MAP[ch], cfg.DISCORD_NOTIFY)
        self.assertEqual(cfg.CHANNEL_MAP[cfg.SLACK_CHAN], cfg.DISCORD_CHAT)

    def test_each_accessor_names_its_own_keychain_service(self):
        seen = {}
        with patch.object(cfg, "_keychain", side_effect=lambda svc, **k: seen.setdefault(svc, "v")):
            cfg.slack_bot_token(); cfg.slack_app_token(); cfg.openrouter_api_key(); cfg.discord_bot_token()
        self.assertEqual(set(seen), {"nova-slack-bot-token", "nova-slack-app-token",
                                     "nova-openrouter-api-key", "nova-discord-token"})

    def test_post_both_routes_slack_tier_to_discord_via_channel_map(self):
        with patch.object(cfg, "slack_bot_token", return_value=""), patch.object(cfg, "post_discord") as d, \
             redirect_stderr(io.StringIO()):
            cfg.post_both("m", slack_channel=cfg.SLACK_FEED)
            d.assert_not_called()                                # "" -> Slack only
            cfg.post_both("m", slack_channel=cfg.SLACK_CHAN)
            d.assert_called_once_with("m", cfg.DISCORD_CHAT)
            d.reset_mock()
            cfg.post_both("m", slack_channel="C_UNKNOWN")
            d.assert_called_once_with("m", cfg.DISCORD_CHAT)     # unknown tier -> default chat
            d.reset_mock()
            cfg.post_both("m", slack_channel=cfg.SLACK_CHAN, discord_channel="")
            d.assert_not_called()                                # explicit opt-out wins


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_slack_payload_and_mirrors_discord(self):
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-test"), \
             patch("urllib.request.urlopen", return_value=_Resp({"ok": True})) as u, \
             patch.object(cfg, "discord_bot_token", return_value="dtok"):
            cfg.post_both("hello *world*", slack_channel=cfg.SLACK_ALERTS)
        self.assertEqual(u.call_count, 2)
        slack_req = u.call_args_list[0][0][0]
        self.assertEqual(slack_req.full_url, f"{cfg.SLACK_API}/chat.postMessage")
        self.assertEqual(json.loads(slack_req.data), {"channel": cfg.SLACK_ALERTS, "text": "hello *world*", "mrkdwn": True})
        self.assertEqual(slack_req.get_header("Authorization"), "Bearer xoxb-test")
        discord_req = u.call_args_list[1][0][0]
        self.assertEqual(discord_req.full_url, f"{cfg.DISCORD_API}/channels/{cfg.DISCORD_NOTIFY}/messages")
        self.assertEqual(discord_req.get_header("Authorization"), "Bot dtok")

    def test_retired_channel_is_dropped_and_no_token_means_no_slack(self):
        with patch("urllib.request.urlopen") as u, patch.object(cfg, "post_discord") as d, \
             patch.object(cfg, "slack_bot_token", return_value="xoxb-test"):
            cfg.post_both("x", slack_channel="#nova-notifications", discord_channel="")
            u.assert_not_called(); d.assert_not_called()
        with patch("urllib.request.urlopen") as u, patch.object(cfg, "post_discord"), \
             patch.object(cfg, "slack_bot_token", return_value=""):
            cfg.post_both("x", slack_channel=cfg.SLACK_CHAN)
            u.assert_not_called()

    def test_slack_api_error_is_reported_not_raised(self):
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-test"), \
             patch("urllib.request.urlopen", return_value=_Resp({"ok": False, "error": "channel_not_found"})), \
             patch.object(cfg, "post_discord"), redirect_stderr(io.StringIO()) as err:
            cfg.post_both("x", slack_channel=cfg.SLACK_CHAN)
        self.assertIn("channel_not_found", err.getvalue())

    def test_post_discord_truncates_to_2000_and_needs_a_token(self):
        with patch.object(cfg, "discord_bot_token", return_value=""), patch("urllib.request.urlopen") as u:
            self.assertFalse(cfg.post_discord("x")); u.assert_not_called()
        with patch.object(cfg, "discord_bot_token", return_value="t"), \
             patch("urllib.request.urlopen", return_value=_Resp({}, status=200)) as u:
            self.assertTrue(cfg.post_discord("y" * 5000, "999"))
        self.assertEqual(len(json.loads(u.call_args[0][0].data)["content"]), 2000)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free_and_compiles(self):
        self.assertNotIn("def main(", SRC)                       # a library module: nothing to run
        self.assertNotIn('if __name__ == "__main__"', SRC)
        boot = ("import sys, types; sys.modules['nova_resolve'] = types.ModuleType('nova_resolve'); "
                "import subprocess; subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('keychain at import')); "
                "import nova_config; print('IMPORT_OK', nova_config.VECTOR_URL)")
        r = subprocess.run([sys.executable, "-c", boot], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("IMPORT_OK http://192.168.1.6:18790/remember", r.stdout)


if __name__ == "__main__":
    unittest.main()
