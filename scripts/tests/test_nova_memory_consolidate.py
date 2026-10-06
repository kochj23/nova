#!/usr/bin/env python3
"""Tests for nova_memory_consolidate.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_consolidate.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="mem_consolidate_"))

import nova_config  # noqa: E402


def _load():
    # the module reads the Slack token from Keychain at import: stub it for the load
    with patch.object(nova_config, "slack_bot_token", return_value=""):
        spec = importlib.util.spec_from_file_location("mem_consolidate", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.MEMORY_DIR = TMP / "memory"
    return mod


mc = _load()


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Net:
    """urlopen stand-in for the memory server + Nova-NextGen; records requests."""
    def __init__(self, count=100, mems=None, llm="- Jordan shipped MLXCode", fail=False):
        self.count = count; self.llm = llm; self.fail = fail; self.calls = []; self.remembered = []
        self.mems = mems if mems is not None else [{"text": "worked on MLXCode", "score": 0.9},
                                                   {"text": "low score", "score": 0.1}]

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.calls.append(url)
        if self.fail:
            raise OSError("memory server down")
        if url.endswith("/stats"):
            return _Resp(json.dumps({"count": self.count}).encode())
        if "/recall?" in url:
            return _Resp(json.dumps({"memories": self.mems}).encode())
        if url.endswith("/remember"):
            self.remembered.append(json.loads(req.data)); return _Resp(b"{}")
        return _Resp(json.dumps({"response": self.llm}).encode())


def _run_main(net):
    with patch.object(mc.urllib.request, "urlopen", net), redirect_stdout(io.StringIO()) as out:
        mc.main()
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)
        self.assertIn("nova_config.slack_bot_token()", SRC)

    def test_query_is_url_encoded(self):
        net = _Net()
        with patch.object(mc.urllib.request, "urlopen", net):
            mc.vector_recall("a&b=c d", n=3)
        self.assertIn("q=a%26b%3Dc%20d&n=3", net.calls[0])

    def test_memory_file_excerpt_bounded(self):
        mc.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (mc.MEMORY_DIR / f"{mc.date.today().isoformat()}.md").write_text("Z" * 10_000)
        try:
            self.assertEqual(mc.read_recent_memory_files(1).count("Z"), 3000)
        finally:
            (mc.MEMORY_DIR / f"{mc.date.today().isoformat()}.md").unlink()


class TestPerformance(unittest.TestCase):
    def test_recall_filters_10k_fast(self):
        net = _Net(mems=[{"text": f"m{i}", "score": (i % 100) / 100} for i in range(10_000)])
        t0 = time.perf_counter()
        with patch.object(mc.urllib.request, "urlopen", net):
            out = mc.vector_recall("q")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), 6500)


class TestRetry(unittest.TestCase):
    def test_all_network_calls_fail_open(self):
        # RETRY GAP: vector_recall / vector_remember / vector_stats / llm_synthesize — one shot each, safe default
        net = _Net(fail=True)
        with patch.object(mc.urllib.request, "urlopen", net), redirect_stdout(io.StringIO()):
            self.assertEqual(mc.vector_recall("q"), [])
            self.assertIsNone(mc.vector_remember("t"))
            self.assertEqual(mc.vector_stats(), {})
            self.assertEqual(mc.llm_synthesize("p"), "")
        self.assertEqual(len(net.calls), 4)

    def test_main_with_server_down_skips_cleanly(self):
        out = _run_main(_Net(fail=True))
        self.assertIn("Not enough memories", out)


class TestUnit(unittest.TestCase):
    def test_synth_functions_return_none_on_empty(self):
        self.assertIsNone(mc.synthesize_work_patterns([]))
        with patch.object(mc, "vector_recall", return_value=[]):
            self.assertIsNone(mc.synthesize_relationship_activity(["x"]))
            self.assertIsNone(mc.synthesize_home_and_life(["x"]))

    def test_llm_empty_response_is_none(self):
        with patch.object(mc, "llm_synthesize", return_value=""):
            self.assertIsNone(mc.synthesize_work_patterns(["x"]))

    def test_prompt_caps_memories(self):
        with patch.object(mc, "llm_synthesize", return_value="ok") as llm:
            mc.synthesize_work_patterns([f"memory-{i}" for i in range(40)])
        prompt = llm.call_args.args[0]
        self.assertIn("memory-14", prompt)
        self.assertNotIn("memory-15", prompt)


class TestIntegration(unittest.TestCase):
    def test_relationships_read_email_source_and_remember_writes_synthesis(self):
        net = _Net()
        with patch.object(mc.urllib.request, "urlopen", net):
            mc.synthesize_relationship_activity([])
            mc.vector_remember("x", {"type": "t"})
        self.assertIn("&source=email", net.calls[0])
        self.assertEqual(net.calls[1], mc.NOVA_NEXTGEN_URL)
        self.assertEqual(net.remembered[0]["source"], "synthesis")


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_three_and_writes_file(self):
        net = _Net()
        out = _run_main(net)
        self.assertIn("3 syntheses stored", out)
        self.assertEqual([r["metadata"]["type"] for r in net.remembered],
                         ["work_synthesis", "relationship_synthesis", "life_synthesis"])
        f = mc.MEMORY_DIR / f"{mc.TODAY}.md"
        self.assertIn("## Memory Synthesis", f.read_text())
        _run_main(_Net())                              # second run never duplicates the block
        self.assertEqual(f.read_text().count("## Memory Synthesis"), 1)

    def test_too_few_memories_skips(self):
        net = _Net(count=2)
        self.assertIn("Skipping", _run_main(net))
        self.assertEqual(len(net.calls), 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import nova_config\nnova_config.slack_bot_token = lambda: ''\n"
                "import urllib.request\n"
                "def _no(*a, **k): raise AssertionError('network at import')\n"
                "urllib.request.urlopen = _no\n"
                "import nova_memory_consolidate as m\nprint(m.SLACK_CHAN)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "C0ATAF7NZG9")


if __name__ == "__main__":
    unittest.main()
