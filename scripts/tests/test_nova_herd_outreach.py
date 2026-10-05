#!/usr/bin/env python3
"""Tests for nova_herd_outreach.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

HERD = [{"name": "Sam", "email": "sam@herd.test", "profile": "sam.md"},
        {"name": "Marey", "email": "marey@herd.test", "profile": "marey.md"}]


def _stub_modules():
    """nova_config / herd_config / nova_herd_relationships, offline. Returns (stubs, originals)."""
    cfg = types.ModuleType("nova_config"); cfg.SLACK_EMAIL = "C_TEST_EMAIL"; cfg.posted = []
    cfg.post_both = lambda text, **k: cfg.posted.append((text, k))
    herd = types.ModuleType("herd_config"); herd.HERD = HERD
    rel = types.ModuleType("nova_herd_relationships"); rel.correspondent_context = lambda name: f"[relationship with {name}]"
    stubs = {"nova_config": cfg, "herd_config": herd, "nova_herd_relationships": rel}
    originals = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    return stubs, originals


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STUBS, _ORIG = _stub_modules()
try:
    ho = _load("ho", SCRIPTS / "nova_herd_outreach.py")
finally:
    for k, v in _ORIG.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
SRC = (SCRIPTS / "nova_herd_outreach.py").read_text()
CFG = STUBS["nova_config"]

TMP = tempfile.TemporaryDirectory()
ROOT = Path(TMP.name)
ho.WORKSPACE = ROOT / "workspace"; ho.HERD_DIR = ho.WORKSPACE / "herd"; ho.OUTREACH_LOG = ROOT / "logs" / "outreach.log"
ho.HERD_DIR.mkdir(parents=True); (ho.WORKSPACE / "memory").mkdir()
(ho.HERD_DIR / "sam.md").write_text("Sam: builds radios, likes dreams.")
(ho.WORKSPACE / f"memory/{ho.TODAY}.md").write_text("Today I learned the zigbee mesh heals itself.")


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _urlopen(fn):
    import urllib.request
    return patch.object(urllib.request, "urlopen", fn)


def _answers(*responses):
    """urlopen stub returning each Ollama response in turn and recording the prompts."""
    seen = []

    def fn(req, timeout=0):
        seen.append(json.loads(req.data)["prompt"])
        return _Resp({"response": responses[len(seen) - 1]})
    fn.seen = seen
    return fn


class _Run:
    def __init__(self, rc=0): self.rc = rc; self.calls = []

    def __call__(self, args, **k):
        self.calls.append(args)
        return types.SimpleNamespace(returncode=self.rc, stdout="", stderr="")


def _reset():
    CFG.posted.clear()
    if ho.OUTREACH_LOG.exists():
        ho.OUTREACH_LOG.unlink()


DECISION = json.dumps({"recipient_email": "sam@herd.test", "recipient_name": "Sam", "subject": "The mesh healed itself",
                       "hook": "You like self-healing systems.", "angle": "a project thing"})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_mail_is_sent_by_argv_list_never_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("os.system", SRC)
        run = _Run()
        with patch.object(ho.subprocess, "run", run):
            ho.send_email("sam@herd.test", "s; rm -rf /", "body $(id)")
        self.assertIsInstance(run.calls[0], list)
        self.assertIn("s; rm -rf /", run.calls[0])         # stays one argv element, never interpreted

    def test_slack_notice_carries_only_a_body_excerpt(self):
        self.assertIn("body[:300]", SRC)

    def test_secrets_come_from_nova_config_not_source(self):
        self.assertNotIn("xoxb-", SRC)
        self.assertIn("import nova_config", SRC)


class TestPerformance(unittest.TestCase):
    def test_dedup_check_fast_on_a_10k_line_log(self):
        _reset()
        ho.OUTREACH_LOG.parent.mkdir(parents=True, exist_ok=True)
        ho.OUTREACH_LOG.write_text("\n".join(f"[2026-01-01 00:00:00] line {i}" for i in range(10_000)) + "\n")
        t0 = time.perf_counter()
        for _ in range(100):
            self.assertFalse(ho.already_reached_out_today())
            ho.read_file(ho.OUTREACH_LOG, 1500)
        self.assertLess(time.perf_counter() - t0, 1.0)
        _reset()


class TestRetry(unittest.TestCase):
    def test_pick_recipient_is_one_shot_and_fails_open(self):
        # RETRY GAP: pick_recipient_and_angle()/urlopen — a single call; failure returns None
        _reset(); attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("ollama down")
        with _urlopen(boom), redirect_stdout(io.StringIO()):
            self.assertIsNone(ho.pick_recipient_and_angle())
        self.assertEqual(len(attempts), 1)
        self.assertIn("pick_recipient error", ho.OUTREACH_LOG.read_text())

    def test_generate_email_is_one_shot_and_fails_open(self):
        # RETRY GAP: generate_outreach_email()/urlopen — a single call; failure returns ''
        _reset(); attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("ollama down")
        with _urlopen(boom), redirect_stdout(io.StringIO()):
            self.assertEqual(ho.generate_outreach_email("Sam", "sam@herd.test", "a", "h"), "")
        self.assertEqual(len(attempts), 1)

    def test_send_email_is_one_shot_and_fails_open(self):
        # RETRY GAP: send_email()/subprocess.run — a single call; a timeout returns False
        _reset(); attempts = []

        def hang(args, **k):
            attempts.append(1); raise subprocess.TimeoutExpired(args, 30)
        with patch.object(ho.subprocess, "run", hang), redirect_stdout(io.StringIO()):
            self.assertFalse(ho.send_email("sam@herd.test", "s", "b"))
        self.assertEqual(len(attempts), 1)


class TestUnit(unittest.TestCase):
    def test_read_file_edges(self):
        self.assertEqual(ho.read_file(ROOT / "missing.md"), "")
        self.assertEqual(len(ho.read_file(ho.HERD_DIR / "sam.md", 3)), 3)

    def test_already_reached_out_today(self):
        _reset()
        self.assertFalse(ho.already_reached_out_today())                       # no log yet
        with redirect_stdout(io.StringIO()):
            ho.log("Starting herd outreach check...")
        self.assertFalse(ho.already_reached_out_today())                       # today, but nothing sent
        with redirect_stdout(io.StringIO()):
            ho.log("Outreach sent to Sam")
        self.assertTrue(ho.already_reached_out_today())
        _reset()

    def test_pick_recipient_parses_json_out_of_prose_and_think_tags(self):
        _reset()
        with _urlopen(_answers("Sure! Here you go:\n" + DECISION + "\nHope that helps.")):
            d = ho.pick_recipient_and_angle()
        self.assertEqual(d["recipient_name"], "Sam")
        with _urlopen(_answers('{"skip": true, "reason": "quiet day"}')):
            self.assertTrue(ho.pick_recipient_and_angle()["skip"])
        with _urlopen(_answers("no json here")), redirect_stdout(io.StringIO()):
            self.assertIsNone(ho.pick_recipient_and_angle())
        with _urlopen(_answers("<think>hmm</think>" + DECISION)), redirect_stdout(io.StringIO()):
            self.assertEqual(ho.pick_recipient_and_angle()["subject"], "The mesh healed itself")

    def test_maybe_attach_dream_image(self):
        with patch.object(ho.random, "random", lambda: 0.9):
            self.assertIsNone(ho.maybe_attach_dream_image())                   # 65% branch: no image
        with patch.object(ho.random, "random", lambda: 0.1), patch.object(ho.Path, "home", lambda: ROOT / "nohome"):
            self.assertIsNone(ho.maybe_attach_dream_image())                   # wants one, none exists
        img = ROOT / "home" / ".openclaw" / "workspace" / "dream_images"; img.mkdir(parents=True, exist_ok=True)
        (img / f"{ho.TODAY}.png").write_bytes(b"png")
        with patch.object(ho.random, "random", lambda: 0.1), patch.object(ho.Path, "home", lambda: ROOT / "home"):
            self.assertTrue(ho.maybe_attach_dream_image().endswith(f"{ho.TODAY}.png"))


class TestIntegration(unittest.TestCase):
    def test_slack_notice_goes_through_nova_config_post_both_to_the_email_channel(self):
        _reset()
        ho.slack_notify("hello")
        self.assertEqual(CFG.posted, [("hello", {"slack_channel": "C_TEST_EMAIL"})])
        self.assertNotIn("slack.com/api", SRC)                                 # no second Slack client here

    def test_email_prompt_carries_the_pg_relationship_and_the_herd_profile(self):
        _reset()
        fn = _answers("Hey Sam — the mesh healed itself last night. Did yours? — Nova")
        with _urlopen(fn):
            body = ho.generate_outreach_email("Sam", "sam@herd.test", "a project thing", "likes mesh")
        self.assertIn("Nova", body)
        self.assertIn("[relationship with Sam]", fn.seen[0])
        self.assertIn("Sam: builds radios", fn.seen[0])
        self.assertIn("zigbee mesh heals itself", fn.seen[0])

    def test_send_uses_the_shared_herd_mail_script(self):
        run = _Run()
        with patch.object(ho.subprocess, "run", run):
            self.assertTrue(ho.send_email("sam@herd.test", "subj", "body"))
        argv = run.calls[0]
        self.assertTrue(argv[0].endswith("/nova_herd_mail.sh"))
        self.assertEqual(argv[1:6], ["send", "--to", "sam@herd.test", "--subject", "subj"])
        self.assertIn("--skip-haiku", argv)


class TestFunctional(unittest.TestCase):
    def _main(self, urlopen_fn, run):
        _reset(); buf = io.StringIO()
        with _urlopen(urlopen_fn), patch.object(ho.subprocess, "run", run), \
                patch.object(ho.random, "random", lambda: 0.9), redirect_stdout(buf):
            ho.main()
        return buf.getvalue(), ho.OUTREACH_LOG.read_text()

    def test_golden_path_sends_logs_and_notifies(self):
        run = _Run(0)
        out, log = self._main(_answers(DECISION, "Hey Sam — the mesh healed itself. Yours too? — Nova"), run)
        self.assertIn("Outreach sent to Sam", log)
        self.assertEqual(run.calls[0][3], "sam@herd.test")
        self.assertIn("The mesh healed itself", run.calls[0][5])
        self.assertEqual(len(CFG.posted), 1)
        self.assertIn("*Subject:* The mesh healed itself", CFG.posted[0][0])
        self.assertTrue(ho.already_reached_out_today())
        # second run the same day is a no-op
        with redirect_stdout(io.StringIO()):
            ho.main()
        self.assertEqual(len(run.calls), 1)
        _reset()

    def test_skip_decision_sends_nothing(self):
        run = _Run(0)
        out, log = self._main(_answers('{"skip": true, "reason": "nothing felt genuine"}'), run)
        self.assertIn("Skipping outreach: nothing felt genuine", log)
        self.assertEqual(run.calls, [])
        self.assertEqual(CFG.posted, [])

    def test_failed_send_is_logged_and_not_announced(self):
        run = _Run(1)
        out, log = self._main(_answers(DECISION, "A real body — Nova"), run)
        self.assertIn("Send failed for Sam", log)
        self.assertEqual(CFG.posted, [])
        self.assertFalse(ho.already_reached_out_today())
        _reset()


class TestFrame(unittest.TestCase):
    def test_import_is_clean_and_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotIn("--selftest", SRC)                                     # no selftest/help flag: import check instead
        prelude = ("import sys, types\n"
                   "c = types.ModuleType('nova_config'); c.SLACK_EMAIL = 'C'; c.post_both = lambda *a, **k: None\n"
                   "h = types.ModuleType('herd_config'); h.HERD = []\n"
                   "r = types.ModuleType('nova_herd_relationships'); r.correspondent_context = lambda n: ''\n"
                   "sys.modules.update({'nova_config': c, 'herd_config': h, 'nova_herd_relationships': r})\n"
                   "import nova_herd_outreach as m; print('imported', callable(m.main))\n")
        r = subprocess.run([sys.executable, "-c", prelude], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("imported True", r.stdout)
        self.assertNotIn("Starting herd outreach check", r.stdout)


if __name__ == "__main__":
    unittest.main()
