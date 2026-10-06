#!/usr/bin/env python3
"""Tests for nova_pg_postmortem.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pg_postmortem.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="pg_postmortem_"))


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    # nova_journal resolves service URLs through PG at import: stand it in for the load
    nj = types.ModuleType("nova_journal"); nj.git_push = MagicMock()
    with _stubbed({"nova_journal": nj}):
        spec = importlib.util.spec_from_file_location("pg_postmortem", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.CONTENT_DIR = TMP / "operations"
    mod.notify = MagicMock()
    return mod


pm = _load()


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _llm_net(*answers):
    seen = []
    q = list(answers)

    def urlopen(req, timeout=None):
        seen.append(req)
        a = q.pop(0)
        if isinstance(a, Exception):
            raise a
        return _Resp(json.dumps({"choices": [{"message": {"content": a}}]}).encode())
    return urlopen, seen


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("sk-or-", SRC)
        with patch.object(pm.subprocess, "run", return_value=MagicMock(stdout="k3y\n")) as run:
            self.assertEqual(pm.get_openrouter_key(), "k3y")
        self.assertEqual(run.call_args.args[0][:2], ["security", "find-generic-password"])

    def test_slug_and_title_are_sanitized(self):
        pm.nj.git_push.reset_mock()
        with redirect_stdout(io.StringIO()):
            url = pm.publish('../../etc/"evil" Title!!', "body")
        slug = url.rstrip("/").rsplit("/", 1)[1]
        self.assertRegex(slug, r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+$")
        post = next(pm.CONTENT_DIR.glob(f"{slug}.md"))
        self.assertTrue(post.resolve().is_relative_to(pm.CONTENT_DIR.resolve()))
        self.assertIn('title: "../../etc/evil Title!!"', post.read_text())


class TestPerformance(unittest.TestCase):
    def test_title_cleanup_10k(self):
        with patch.object(pm, "call_llm", side_effect=lambda *a, **k: ' "The Afternoon My Spine Died" '):
            t0 = time.perf_counter()
            titles = {pm.generate_title("x" * 5000) for _ in range(10_000)}
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(titles, {"The Afternoon My Spine Died"})


class TestRetry(unittest.TestCase):
    def test_llm_failure_is_one_shot_and_raises_before_publishing(self):
        # RETRY GAP: call_llm/urlopen — no retry; a one-shot script lets the error end the run before any write
        net, seen = _llm_net(OSError("503"), "never")
        pm.nj.git_push.reset_mock()
        with patch.object(pm, "get_openrouter_key", return_value="k"), patch("urllib.request.urlopen", net), \
             redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            pm.main()
        self.assertEqual(len(seen), 1)
        pm.nj.git_push.assert_not_called()

    def test_notify_failure_does_not_lose_the_url(self):
        pm.notify.side_effect = RuntimeError("slack down")
        try:
            with redirect_stdout(io.StringIO()) as out:
                url = pm.publish("Quiet Door", "body")
        finally:
            pm.notify.side_effect = None
        self.assertTrue(url.endswith("-quiet-door/"))
        self.assertIn("Slack note failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_generate_title_strips_quotes(self):
        with patch.object(pm, "call_llm", return_value=' "The Door Locked From Inside" \n'):
            self.assertEqual(pm.generate_title("x"), "The Door Locked From Inside")

    def test_ground_facts_present(self):
        for fact in ("LC_ALL", "postmaster became multithreaded", "June 9, 2026", "no data was lost"):
            self.assertIn(fact, pm.USER)


class TestIntegration(unittest.TestCase):
    def test_call_llm_payload(self):
        net, seen = _llm_net("hello")
        with patch.object(pm, "get_openrouter_key", return_value="k"), patch("urllib.request.urlopen", net):
            self.assertEqual(pm.call_llm("S", "U", max_tokens=7), "hello")
        req = seen[0]
        self.assertEqual(req.full_url, pm.OPENROUTER_URL)
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["max_tokens"], body["messages"][0]["content"]), (pm.OPENROUTER_MODEL, 7, "S"))
        self.assertEqual(req.get_header("Authorization"), "Bearer k")

    def test_publish_uses_journal_git_push(self):
        pm.nj.git_push.reset_mock()
        with redirect_stdout(io.StringIO()):
            pm.publish("Spine", "b")
        pm.nj.git_push.assert_called_once_with("operations", "Spine")


class TestFunctional(unittest.TestCase):
    def test_main_golden_path(self):
        net, seen = _llm_net("  The pollers kept writing.  ", '"Writing Into The Void"')
        pm.nj.git_push.reset_mock(); pm.notify.reset_mock()
        with patch.object(pm, "get_openrouter_key", return_value="k"), patch("urllib.request.urlopen", net), \
             redirect_stdout(io.StringIO()) as out:
            pm.main()
        self.assertIn("Done: https://nova.digitalnoise.net/operations/", out.getvalue())
        post = next(pm.CONTENT_DIR.glob("*-writing-into-the-void.md"))
        self.assertTrue(post.read_text().endswith("The pollers kept writing."))
        self.assertIn("Writing Into The Void", pm.notify.call_args.kwargs["body"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types, urllib.request\n"
                "nj = types.ModuleType('nova_journal'); sys.modules.update({'nova_journal': nj})\n"
                "def _no(*a, **k): raise AssertionError('network at import')\n"
                "urllib.request.urlopen = _no\n"
                "import nova_pg_postmortem as m\nprint(m.OPENROUTER_MODEL)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "google/gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
