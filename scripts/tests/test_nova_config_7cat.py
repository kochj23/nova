#!/usr/bin/env python3
"""7-category tests for the 2026-10-07 nova_config change (is_private_source: a 'quarantine:<source>' row is still
private) and the retry/backoff on its outbound Slack/Discord posts. Security, Performance, Retry, Unit, Integration,
Functional, Frame. No real Slack/Discord/Keychain is touched. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_config.py"
_spec = importlib.util.spec_from_file_location("cfg7", SCRIPT)
cfg = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(cfg)


class _Resp:
    def __init__(self, body=None, status=200): self.status = status; self._b = json.dumps(body or {"ok": True}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _http(code): return urllib.error.HTTPError("u", code, "x", {}, None)


class TestSecurity(unittest.TestCase):
    def test_every_private_source_stays_private_when_quarantined(self):
        for src in sorted(cfg.PRIVATE_SOURCES):
            for form in (f"quarantine:{src}", f"QUARANTINE:{src.upper()}", f"  quarantine:{src} "):
                self.assertTrue(cfg.is_private_source(form), form)

    def test_quarantined_employer_and_keyword_sources_stay_private(self):
        for s in (f"quarantine:{cfg._EMPLOYER_PREFIX}_wiki", "quarantine:work_memo_2026", "quarantine:email_gmail",
                  "quarantine:health_kit"):
            self.assertTrue(cfg.is_private_source(s), s)

    def test_filter_drops_quarantined_private_memories(self):
        mems = [{"source": "quarantine:imessage", "text": "hi"}, {"source": "quarantine:reddit", "text": "cats"}]
        with patch.dict(sys.modules, {"nova_privacy_guards": None}):
            self.assertEqual([m["source"] for m in cfg.filter_private_memories(mems)], ["quarantine:reddit"])

    def test_prefix_only_strips_once_and_only_at_start(self):
        self.assertTrue(cfg.is_private_source("quarantine:quarantine:email"))   # substring 'email' still catches it
        self.assertFalse(cfg.is_private_source("reddit_quarantine:"))


class TestPerformance(unittest.TestCase):
    def test_gate_on_50k_quarantined_sources_under_a_second(self):
        srcs = [f"quarantine:src_{i}" for i in range(50000)]
        t0 = time.perf_counter(); [cfg.is_private_source(s) for s in srcs]
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_retry_backoff_bounded(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), \
                patch("time.sleep") as s:
            with self.assertRaises(urllib.error.URLError):
                cfg._urlopen_retry("req")
        self.assertEqual([c.args[0] for c in s.call_args_list], [0.5, 1.0])


class TestRetry(unittest.TestCase):
    def test_slack_post_retries_connection_refused(self):
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-test"), patch.object(cfg, "post_discord"), \
                patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("refused"), _Resp()]) as u, \
                patch("time.sleep"), redirect_stderr(io.StringIO()) as err:
            cfg.post_both("hi", slack_channel=cfg.SLACK_DIGEST)
        self.assertEqual(u.call_count, 2); self.assertEqual(err.getvalue(), "")

    def test_discord_retries_5xx_and_429(self):
        with patch.object(cfg, "discord_bot_token", return_value="tok"), patch("time.sleep"), \
                patch("urllib.request.urlopen", side_effect=[_http(503), _http(429), _Resp()]) as u:
            self.assertTrue(cfg.post_discord("x", "1"))
        self.assertEqual(u.call_count, 3)

    def test_timeout_and_4xx_not_retried_to_avoid_duplicates(self):
        for err in (urllib.error.URLError(TimeoutError("t")), TimeoutError("t"), _http(400)):
            with patch.object(cfg, "discord_bot_token", return_value="tok"), patch("time.sleep"), \
                    patch("urllib.request.urlopen", side_effect=err) as u, redirect_stderr(io.StringIO()):
                self.assertFalse(cfg.post_discord("x", "1"))
            self.assertEqual(u.call_count, 1, err)

    def test_persistent_failure_is_logged_not_silent(self):
        with patch.object(cfg, "discord_bot_token", return_value="tok"), patch("time.sleep"), \
                patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")) as u, \
                redirect_stderr(io.StringIO()) as e:
            self.assertFalse(cfg.post_discord("x", "1"))
        self.assertEqual(u.call_count, 3); self.assertIn("Discord post failed", e.getvalue())


class TestUnit(unittest.TestCase):
    def test_quarantine_of_public_sources_stays_public(self):
        for s in ("quarantine:sci_fi", "quarantine:television", "quarantine:", ""):
            self.assertFalse(cfg.is_private_source(s), s)
        self.assertFalse(cfg.is_private_source(None))


class TestIntegration(unittest.TestCase):
    def test_memory_quality_quarantine_format_matches_the_gate(self):
        mq = SCRIPTS / "nova_memory_quality.py"
        if not mq.exists():
            self.skipTest("nova_memory_quality.py not present")
        self.assertIn("quarantine:", mq.read_text())


class TestFunctional(unittest.TestCase):
    def test_golden_post_both_slack_then_discord(self):
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-test"), \
                patch.object(cfg, "discord_bot_token", return_value="tok"), \
                patch("urllib.request.urlopen", return_value=_Resp()) as u:
            cfg.post_both("hello", slack_channel=cfg.SLACK_ALERTS)
        urls = [c.args[0].full_url for c in u.call_args_list]
        self.assertEqual(urls, [f"{cfg.SLACK_API}/chat.postMessage", f"{cfg.DISCORD_API}/channels/{cfg.DISCORD_NOTIFY}/messages"])

    def test_error_path_slack_down_discord_still_tried(self):
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-test"), patch("time.sleep"), \
                patch.object(cfg, "post_discord") as d, \
                patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")) as u, \
                redirect_stderr(io.StringIO()) as e:
            cfg.post_both("hello", slack_channel=cfg.SLACK_ALERTS)
        self.assertEqual(u.call_count, 3); self.assertIn("Slack post failed", e.getvalue()); d.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_clean(self):
        r = subprocess.run([sys.executable, "-c", "import nova_config as c; assert c.is_private_source('quarantine:email')"],
                           cwd=SCRIPTS, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


# ══ 2026-10-08: post_both reports success/failure (was: swallowed errors, returned None) ══════════
# Callers that record "sent"/"posted" (nova_notifier, nova_self_repair_digest, nova_notify_jordan,
# nova_review_custody, nova_proactive_digest, nova_turing_scoreboard, nova_mesh_digest) check it.
def _pb(slack=None, discord=True, stok="xoxb-t", dtok="d-t", chan=None, dchan="123"):
    """Run post_both with Slack urlopen + Discord mocked; returns (result, urlopen mock, discord mock)."""
    eff = slack if slack is not None else [_Resp({"ok": True})]
    with patch.object(cfg, "slack_bot_token", return_value=stok), \
            patch.object(cfg, "discord_bot_token", return_value=dtok), \
            patch("urllib.request.urlopen", side_effect=eff) as u, \
            patch.object(cfg, "post_discord", return_value=discord) as d, \
            patch("time.sleep"), redirect_stderr(io.StringIO()):
        r = cfg.post_both("msg", slack_channel=chan or cfg.SLACK_CHAN, discord_channel=dchan)
    return r, u, d


class TestPostBothResultSecurity(unittest.TestCase):
    def test_failure_log_never_contains_token(self):
        err = io.StringIO()
        with patch.object(cfg, "slack_bot_token", return_value="xoxb-SECRET"), \
                patch.object(cfg, "post_discord", return_value=False), \
                patch("urllib.request.urlopen", return_value=_Resp({"ok": False, "error": "invalid_auth"})), \
                redirect_stderr(err):
            self.assertFalse(cfg.post_both("m", slack_channel=cfg.SLACK_CHAN, discord_channel="1"))
        self.assertNotIn("xoxb-SECRET", err.getvalue())

    def test_no_tokens_means_false_not_a_silent_success(self):
        self.assertFalse(_pb(stok=None, discord=False)[0])


class TestPostBothResultPerformance(unittest.TestCase):
    def test_one_slack_call_on_success(self):
        r, u, d = _pb()
        self.assertTrue(r); self.assertEqual(u.call_count, 1); self.assertEqual(d.call_count, 1)


class TestPostBothResultRetry(unittest.TestCase):
    def test_slack_5xx_retried_then_succeeds(self):
        r, u, _ = _pb(slack=[_http(502), _Resp({"ok": True})], discord=False)
        self.assertTrue(r); self.assertEqual(u.call_count, 2)

    def test_slack_exhausted_and_discord_down_is_false(self):
        r, u, _ = _pb(slack=[_http(503)] * 3, discord=False)
        self.assertFalse(r); self.assertEqual(u.call_count, 3)


class TestPostBothResultUnit(unittest.TestCase):
    def test_truth_table(self):
        self.assertTrue(_pb()[0])                                                     # both ok
        self.assertTrue(_pb(discord=False)[0])                                         # slack only
        self.assertTrue(_pb(slack=[_Resp({"ok": False, "error": "x"})])[0])            # discord only
        self.assertFalse(_pb(slack=[_Resp({"ok": False, "error": "x"})], discord=False)[0])
        self.assertIs(type(_pb()[0]), bool)

    def test_slack_only_tier_false_when_slack_fails(self):
        r, _, d = _pb(slack=[_Resp({"ok": False, "error": "channel_not_found"})], dchan="")
        self.assertFalse(r); d.assert_not_called()


class TestPostBothResultIntegration(unittest.TestCase):
    def _mod(self, name):
        spec = importlib.util.spec_from_file_location(f"{name}_pb7", SCRIPTS / f"{name}.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

    def test_self_repair_digest_never_marks_day_on_falsy_post(self):
        d = self._mod("nova_self_repair_digest")
        nc = type("NC", (), {"SLACK_CHAN": "C", "post_both": staticmethod(lambda *a, **k: False)})
        with self.assertRaises(RuntimeError):
            d._post_or_raise(nc, "body")
        nc.post_both = staticmethod(lambda *a, **k: True)
        self.assertTrue(d._post_or_raise(nc, "body"))

    def test_callers_check_the_return_value(self):
        for name, needle in (("nova_notifier", "if not nova_config.post_both("),
                             ("nova_notify_jordan", "if not nova_config.post_both("),
                             ("nova_self_repair_digest", "if not nova_config.post_both("),
                             ("nova_review_custody", "posted = bool(nova_config.post_both("),
                             ("nova_proactive_digest", "posted = bool(nova_config.post_both("),
                             ("nova_turing_scoreboard", "if not nova_config.post_both("),
                             ("nova_mesh_digest", "if not nc.post_both(")):
            self.assertIn(needle, (SCRIPTS / f"{name}.py").read_text(), name)


class TestPostBothResultFunctional(unittest.TestCase):
    def test_notifier_failed_post_marks_error_not_sent(self):
        src = (SCRIPTS / "nova_notifier.py").read_text()
        i = src.index("if not nova_config.post_both(msg, slack_channel=channel):")
        self.assertIn("raise RuntimeError", src[i:i + 200])            # falls into the except -> status='error'
        self.assertLess(i, src.index("SET status='sent'"))

    def test_backward_compatible_truthy_on_success(self):
        self.assertTrue(_pb()[0])


class TestPostBothResultFrame(unittest.TestCase):
    def test_signature_annotated_bool(self):
        import inspect
        self.assertIn(inspect.signature(cfg.post_both).return_annotation, (bool, "bool"))


if __name__ == "__main__":
    unittest.main()
