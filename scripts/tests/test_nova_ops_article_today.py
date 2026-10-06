#!/usr/bin/env python3
"""Tests for nova_ops_article_today.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This one-off has NO __main__ guard: the whole pipeline (LLM -> image -> publish) runs at import.
So it is only ever executed here with its three imports replaced by recording fakes, HOME pointed at a
tempdir (its log file), and the fakes' keys restored afterwards (never a bare sys.modules assignment)."""
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
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_article_today.py"
SRC = SCRIPT.read_text()
DEPS = ("nova_rando_daily_ops", "nova_image_utils", "nova_voice")


@contextmanager
def _deps(body="column " * 200, title="Fridge Feelings", img="/tmp/x.png", llm_exc=None):
    """Install fake deps under their own keys only, restoring exactly those keys afterwards."""
    rando = types.SimpleNamespace(call_llm=MagicMock(return_value=body, side_effect=llm_exc),
                                  generate_title=MagicMock(return_value=title), publish=MagicMock())
    imgs = types.SimpleNamespace(generate_image=MagicMock(return_value=img))
    voice = types.SimpleNamespace(system_prompt=MagicMock(side_effect=lambda ctx: "VOICE\n" + ctx),
                                  CONTEXT_JOURNAL_OPS="CTX-OPS\n")
    fakes = dict(zip(DEPS, (rando, imgs, voice)))
    saved = {k: sys.modules.get(k, None) for k in DEPS}
    present = {k: k in sys.modules for k in DEPS}
    try:
        for k, v in fakes.items():
            sys.modules[k] = v
        yield types.SimpleNamespace(**{"rando": rando, "imgs": imgs, "voice": voice})
    finally:
        for k in DEPS:
            if present[k]:
                sys.modules[k] = saved[k]
            else:
                sys.modules.pop(k, None)


def _exec(home):
    """Execute the script once (it runs on import). Returns (module, stdout)."""
    (Path(home) / ".openclaw/logs").mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("nova_ops_article_today_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(home)), redirect_stdout(io.StringIO()) as out:
        spec.loader.exec_module(mod)
    return mod, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_prompt_forbids_invention(self):
        with tempfile.TemporaryDirectory() as h, _deps() as d:
            _exec(h)
        system = d.rando.call_llm.call_args[0][0]
        self.assertIn("Do NOT invent events", system)
        self.assertTrue(system.startswith("VOICE\nCTX-OPS"))


class TestPerformance(unittest.TestCase):
    def test_full_run_with_fakes_is_fast(self):
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory() as h, _deps():
            for _ in range(20):
                _exec(h)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_llm_failure_aborts_before_publish(self):
        # RETRY GAP: top-level call_llm — one attempt, the exception stops the run before anything publishes
        with tempfile.TemporaryDirectory() as h, _deps(llm_exc=RuntimeError("llm down")) as d:
            with self.assertRaises(RuntimeError):
                _exec(h)
        self.assertEqual(d.rando.call_llm.call_count, 1)
        d.rando.publish.assert_not_called()

    def test_image_failure_still_publishes_without_cover(self):
        with tempfile.TemporaryDirectory() as h, _deps(img=None) as d:
            _, out = _exec(h)
        self.assertIsNone(d.rando.publish.call_args[0][2])
        self.assertIn("image=NO", out)


class TestUnit(unittest.TestCase):
    def test_log_appends_to_file_and_stdout(self):
        with tempfile.TemporaryDirectory() as h, _deps():
            mod, out = _exec(h)
            logtxt = (Path(h) / ".openclaw/logs/ops_article_today.log").read_text()
        self.assertIn("ARTICLE DONE", logtxt)
        self.assertIn("ARTICLE DONE", out)
        self.assertTrue(re.match(r"\[\d\d:\d\d:\d\d\] ", logtxt))

    def test_material_has_real_numbers(self):
        for fact in ("110.6 kWh", "1743 watts", "39F", "crash-looped"):
            self.assertIn(fact, SRC)


class TestIntegration(unittest.TestCase):
    def test_reuses_rando_pipeline(self):
        self.assertIn("from nova_rando_daily_ops import call_llm, generate_title, publish", SRC)
        real = (SCRIPTS / "nova_rando_daily_ops.py").read_text()
        for fn in ("def call_llm", "def generate_title", "def publish"):
            self.assertIn(fn, real)

    def test_title_built_from_body(self):
        with tempfile.TemporaryDirectory() as h, _deps(body="B" * 900) as d:
            _exec(h)
        d.rando.generate_title.assert_called_once_with("B" * 900)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_once(self):
        with tempfile.TemporaryDirectory() as h, _deps() as d:
            _, out = _exec(h)
        title, body, img = d.rando.publish.call_args[0]
        self.assertEqual(title, "Fridge Feelings")
        self.assertEqual(img, Path("/tmp/x.png"))
        self.assertEqual(d.imgs.generate_image.call_args[1]["section"], "operations")
        self.assertIn("PUBLISHED: Fridge Feelings | image=yes", out)


class TestFrame(unittest.TestCase):
    def test_runs_as_script_only_against_stubs(self):
        # no --help and no __main__ guard: a real run publishes. Smoke a COPY of the script in a tempdir
        # (a script's own directory is sys.path[0], so the copy sees only the stub deps beside it) with
        # HOME pointed at that tempdir (its ~/.openclaw/scripts sys.path insert is then empty).
        self.assertNotIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as h:
            box = Path(h) / "box"
            box.mkdir()
            (Path(h) / ".openclaw/logs").mkdir(parents=True)
            marker = Path(h) / "published.txt"
            (box / "nova_ops_article_today.py").write_text(SRC)
            (box / "nova_rando_daily_ops.py").write_text(
                "def call_llm(s, u, max_tokens=0): return 'x' * 800\n"
                "def generate_title(b): return 'T'\n"
                f"def publish(t, b, i): open({str(marker)!r}, 'w').write(t)\n")
            (box / "nova_image_utils.py").write_text("def generate_image(*a, **k): return None\n")
            (box / "nova_voice.py").write_text("CONTEXT_JOURNAL_OPS = ''\ndef system_prompt(c): return c\n")
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            r = subprocess.run([sys.executable, str(box / "nova_ops_article_today.py")], capture_output=True,
                               text=True, timeout=30, env={**env, "NOVA_TEST_QUIET": "1", "HOME": h})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(marker.read_text(), "T")
        self.assertIn("ARTICLE DONE", r.stdout)

if __name__ == "__main__":
    unittest.main()
