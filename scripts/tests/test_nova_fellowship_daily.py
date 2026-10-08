#!/usr/bin/env python3
"""Tests for nova_fellowship_daily.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fellowship_daily.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_fellowship_test_"))


def _stubs():
    nj = types.ModuleType("nova_journal")
    for f in ("publish_hugo", "git_push", "notify_slack"):
        setattr(nj, f, MagicMock())
    nj.today_str = lambda: "2026-01-01"
    nj.get_image_prompt = MagicMock(return_value="prompt"); nj.generate_image = MagicMock(return_value="/img.png")
    nv = types.ModuleType("nova_voice"); nv.CONTEXT_JOURNAL_OPS = "OPS:"; nv.system_prompt = lambda s: "VOICE " + s
    rd = types.ModuleType("nova_rando_daily_ops"); rd.call_llm = MagicMock(return_value="TITLE: Quiet Day\n\nAll good.")
    ah = types.ModuleType("nova_article_history"); ah.recent_articles_context = MagicMock(return_value="")
    return {"nova_journal": nj, "nova_voice": nv, "nova_rando_daily_ops": rd, "nova_article_history": ah}


STUBS = _stubs()


def _load():
    spec = importlib.util.spec_from_file_location("nfellow", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {k: v for k, v in STUBS.items() if k != "nova_article_history"}), \
         patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.ROTATION_STATE = TMP / "rotation.json"
    return mod


fd = _load()


def _conn(rows, threats):
    cur = MagicMock(); cur.fetchall.side_effect = [rows, threats]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _main(raw="TITLE: Quiet Day\n\nAll good.", image_exc=None):
    for k in ("publish_hugo", "git_push", "notify_slack"):
        getattr(fd.nj, k).reset_mock()
    fd.call_llm = MagicMock(return_value=raw)
    fd.nj.generate_image = MagicMock(side_effect=image_exc, return_value="/img.png")
    conn, cur = _conn([("nova-core", "running", 40), ("nova-core", "failed", 1)], [("nova-core", 12.0, 3.0)])
    with patch.object(fd.psycopg2, "connect", return_value=conn), \
         patch.dict(sys.modules, {"nova_article_history": STUBS["nova_article_history"]}), redirect_stdout(io.StringIO()):
        rc = fd.main()
    return rc, fd.call_llm


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')

    def test_prompt_carries_the_no_verbatim_guardrail(self):
        _, llm = _main()
        system = llm.call_args[0][0]
        self.assertIn("never quote actual dialogue", system)
        self.assertIn("Don't invent incidents", system)

    def test_only_reads_pg(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_rotation_1k_steps(self):
        fd.ROTATION_STATE.unlink(missing_ok=True)
        t0 = time.perf_counter()
        names = [fd.next_franchise()["name"] for _ in range(1000)]
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(names[0], names[len(fd.FRANCHISES)])


class TestRetry(unittest.TestCase):
    def test_empty_llm_aborts_without_publishing(self):
        # main()/call_llm — 3 LLM attempts (5 s / 10 s backoff); still empty -> returns 1 and publishes nothing
        with patch.object(fd.time, "sleep"):
            rc, llm = _main(raw="")
        self.assertEqual(rc, 1)
        self.assertEqual(llm.call_count, 3)
        fd.nj.publish_hugo.assert_not_called()

    def test_image_failure_is_non_fatal(self):
        rc, _ = _main(image_exc=RuntimeError("swarm down"))
        self.assertEqual(rc, 0)
        self.assertIsNone(fd.nj.publish_hugo.call_args.kwargs["image_path"])


class TestUnit(unittest.TestCase):
    def test_corrupt_rotation_state_restarts_at_lotr(self):
        fd.ROTATION_STATE.write_text("{nope")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fd.next_franchise()["tag"], "fellowship")
        self.assertEqual(json.loads(fd.ROTATION_STATE.read_text()), {"idx": 1})

    def test_rotation_wraps(self):
        fd.ROTATION_STATE.write_text(json.dumps({"idx": len(fd.FRANCHISES) - 1}))
        self.assertEqual(fd.next_franchise()["tag"], fd.FRANCHISES[-1]["tag"])
        self.assertEqual(json.loads(fd.ROTATION_STATE.read_text())["idx"], 0)

    def test_every_franchise_casts_all_nine(self):
        for f in fd.FRANCHISES:
            for host in ("mac-studio", "nova-core (", "nova-core2", "nova-core3", "nova-core4", "nova-core5",
                         "tv-movies-mini", "mac-mini", "UniFi"):
                self.assertIn(host, f["cast"], f["name"])
            self.assertTrue({"name", "emoji", "tag", "image_style"} <= set(f))


class TestIntegration(unittest.TestCase):
    def test_gather_status_shapes(self):
        conn, cur = _conn([("nova-core", "running", 40), ("nova-core", "failed", 1)], [("nova-core", 12.4, 3.0)])
        with patch.object(fd.psycopg2, "connect", return_value=conn):
            status, threats = fd.gather_today_status()
        self.assertEqual(status, "nova-core: 40 running, 1 failed")
        self.assertEqual(threats, "nova-core: recent max 12, avg 3")
        self.assertIn("FROM service_registry", cur.execute.call_args_list[0][0][0])

    def test_uses_shared_journal_and_voice(self):
        for imp in ("import nova_journal as nj", "import nova_voice", "from nova_rando_daily_ops import call_llm"):
            self.assertIn(imp, SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_and_notifies(self):
        fd.ROTATION_STATE.unlink(missing_ok=True)
        rc, llm = _main()
        self.assertEqual(rc, 0)
        self.assertIn("nova-core: 40 running, 1 failed", llm.call_args[0][1])
        args = fd.nj.publish_hugo.call_args[0]
        self.assertEqual(args[:3], ("Quiet Day", "All good.", "operations"))
        self.assertIn("fellowship", args[3])
        fd.nj.git_push.assert_called_once_with("operations", "Quiet Day")
        fd.nj.notify_slack.assert_called_once()

    def test_missing_title_falls_back_to_franchise_and_date(self):
        _main(raw="no title line here")
        self.assertTrue(fd.nj.publish_hugo.call_args[0][0].endswith("— 2026-01-01"))


class TestFrame(unittest.TestCase):
    def test_import_smoke_with_stubbed_siblings(self):
        # no --help: a bare run publishes, so the frame check imports with the journal/LLM siblings stubbed
        code = ("import sys, types; sys.path.insert(0, sys.argv[1])\n"
                "for n in ('nova_journal', 'nova_voice', 'nova_rando_daily_ops'):\n"
                "    m = types.ModuleType(n); m.call_llm = None; sys.modules[n] = m\n"
                "import nova_fellowship_daily as f; print(len(f.FRANCHISES))\n")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), len(fd.FRANCHISES))

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        STUBS["nova_journal"].publish_hugo.reset_mock()
        _load()
        STUBS["nova_journal"].publish_hugo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
