#!/usr/bin/env python3
"""Tests for nova_postmortem.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.parse    # noqa: F401  (stdlib locked in before the sys.modules-scoped load)
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2         # noqa: F401
import psycopg2.extras  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_postmortem.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="postmortem_test_"))

import nova_config  # noqa: E402,F401

NJ = types.ModuleType("nova_journal")
NJ.git_push = MagicMock(name="git_push")
NJ.grafana_panel_image = MagicMock(name="grafana_panel_image", return_value=None)
IU = types.ModuleType("nova_image_utils")
IU.generate_image = MagicMock(name="generate_image", return_value=None)
OC = types.ModuleType("nova_ops_context")
OC.get_full_context = MagicMock(return_value={})
OC.format_security_brief = lambda c: "SEC BRIEF"
OC.format_infra_brief = lambda c: "INFRA BRIEF"


def _load():
    spec = importlib.util.spec_from_file_location("npm", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_journal": NJ, "nova_image_utils": IU, "nova_ops_context": OC}), \
         patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


pm = _load()
pm.HUGO_ROOT = TMP / "nova-journal"
pm.CONTENT_DIR = pm.HUGO_ROOT / "content" / "operations"
pm.IMAGES_DIR = pm.HUGO_ROOT / "static" / "images" / "operations"
REAL_CALL_LLM = pm.call_llm                                          # the genuine function, tested directly
pm.call_llm = MagicMock(name="call_llm", return_value=None)          # module-level guard: no LLM by default


ARTICLE = "Timeline. Root cause. " * 60


def _resp(payload):
    r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_internal_telemetry_only_goes_to_local_ollama(self):
        self.assertTrue(pm.OLLAMA_URL.startswith("http://127.0.0.1:"))
        self.assertNotRegex(SRC, r"anthropic|openai|api\.groq|claude -p")
        seen = []
        with patch.object(pm.urllib.request, "urlopen", side_effect=lambda req, timeout=None: seen.append(req.full_url) or _resp({"response": "ok"})):
            self.assertEqual(REAL_CALL_LLM("s", "u"), "ok")
        self.assertEqual(seen, [pm.OLLAMA_URL])

    def test_title_cannot_escape_content_dir_or_front_matter(self):
        pm.call_llm.side_effect = [ARTICLE, '../../etc/"passwd"']
        try:
            with patch.object(pm, "get_recent_incidents", return_value=([], [])), _quiet():
                self.assertTrue(pm.generate_postmortem("t"))
        finally:
            pm.call_llm.side_effect = None
        files = list(pm.CONTENT_DIR.glob("*etc-passwd*.md"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, pm.CONTENT_DIR)
        self.assertIn('title: "../../etc/passwd"', files[0].read_text())


class TestPerformance(unittest.TestCase):
    def test_slugify_10k_titles_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            re.sub(r'[^a-z0-9]+', '-', f"The Day Redis Ate Itself #{i}!".lower()).strip('-')[:60]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]", SRC)


class TestRetry(unittest.TestCase):
    def test_llm_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: call_llm() — one Ollama POST, no retry; failure returns None, nothing is published
        op = MagicMock(side_effect=OSError("ollama down"))
        with patch.object(pm.urllib.request, "urlopen", op), _quiet():
            self.assertIsNone(REAL_CALL_LLM("s", "u"))
        self.assertEqual(op.call_count, 1)
        NJ.git_push.reset_mock()
        pm.call_llm.return_value = None
        with patch.object(pm, "get_recent_incidents", return_value=([], [])), _quiet():
            self.assertFalse(pm.generate_postmortem("x"))
        NJ.git_push.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_call_llm_strips_think_and_empty(self):
        with patch.object(pm.urllib.request, "urlopen", return_value=_resp({"response": "<think>x</think> body"})):
            self.assertEqual(REAL_CALL_LLM("s", "u"), "body")
        with patch.object(pm.urllib.request, "urlopen", return_value=_resp({"response": "  "})):
            self.assertIsNone(REAL_CALL_LLM("s", "u"))

    def test_main_no_incidents_writes_nothing(self):
        with patch.object(pm, "get_recent_incidents", return_value=([], [{}] * 3)), \
             patch.object(pm, "generate_postmortem") as gen, patch.object(sys, "argv", ["x"]), _quiet():
            pm.main()
        gen.assert_not_called()

    def test_main_trigger_selection(self):
        with patch.object(pm, "get_recent_incidents", return_value=([], [{}] * 6)), \
             patch.object(pm, "generate_postmortem") as gen, patch.object(sys, "argv", ["x"]), _quiet():
            pm.main()
        self.assertEqual(gen.call_args[0][0], "Auto-postmortem: 6 Big Brother heals in last 6 hours")
        with patch.object(pm, "generate_postmortem") as gen, patch.object(sys, "argv", ["x", "redis", "died"]), _quiet():
            pm.main()
        self.assertEqual(gen.call_args[0][0], "redis died")


class TestIntegration(unittest.TestCase):
    def test_reads_incidents_and_heals_tables(self):
        cur = MagicMock(); cur.fetchall.side_effect = [[{"title": "i"}], [{"title": "h"}]]
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            inc, heals = pm.get_recent_incidents()
        sqls = " ".join(c[0][0] for c in cur.execute.call_args_list)
        self.assertIn("FROM incidents", sqls); self.assertIn("FROM grafana_annotations", sqls)
        self.assertEqual((inc, heals), ([{"title": "i"}], [{"title": "h"}]))
        conn.close.assert_called_once()

    def test_uses_shared_publish_helpers(self):
        self.assertIn("nj.git_push(", SRC)
        self.assertIn("from nova_ops_context import", SRC)
        self.assertIs(pm.nj, NJ)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_article_and_pushes(self):
        NJ.git_push.reset_mock(); pm.call_llm.reset_mock()
        NJ.grafana_panel_image.return_value = "/images/operations/snap.webp"
        pm.call_llm.side_effect = [ARTICLE, "**The Day Redis Ate Itself**"]
        inc = [{"severity": "high", "title": "Redis OOM", "started_at": "t",
                "events": json.dumps([{"desc": "evicted keys"}])}]
        try:
            with patch.object(pm, "get_recent_incidents", return_value=(inc, [{"title": "heal", "ts": "t"}])), _quiet():
                self.assertTrue(pm.generate_postmortem("redis died"))
        finally:
            pm.call_llm.side_effect = None; NJ.grafana_panel_image.return_value = None
        prompt = pm.call_llm.call_args_list[0][0][1]
        self.assertIn("TRIGGER: redis died", prompt); self.assertIn("evicted keys", prompt)
        self.assertIn("INFRA BRIEF", prompt)
        out = next(pm.CONTENT_DIR.glob("*the-day-redis-ate-itself.md")).read_text()
        self.assertIn('title: "The Day Redis Ate Itself"', out)
        self.assertIn("Fleet health at publish time", out)
        NJ.git_push.assert_called_once_with("operations", "The Day Redis Ate Itself")

    def test_short_article_is_not_published(self):
        NJ.git_push.reset_mock()
        pm.call_llm.return_value = "too short"
        try:
            with patch.object(pm, "get_recent_incidents", return_value=([], [])), _quiet():
                self.assertFalse(pm.generate_postmortem())
        finally:
            pm.call_llm.return_value = None
        NJ.git_push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: any argv is treated as a trigger and publishes, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_postmortem"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=60, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[postmortem", r.stdout)


if __name__ == "__main__":
    unittest.main()
