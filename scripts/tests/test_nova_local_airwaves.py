#!/usr/bin/env python3
"""Tests for nova_local_airwaves.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The journal module (LLM, image gen, Hugo, git push, Slack) is
replaced with a mock at load; PG is mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aw = _load("nova_local_airwaves_t", SCRIPTS / "nova_local_airwaves.py")
SRC = (SCRIPTS / "nova_local_airwaves.py").read_text()
REAL_NJ = aw.nj
# every outbound path (LLM, image, publish, git push, Slack) is a mock from load onward
aw.nj = mock.MagicMock(name="nova_journal")
aw.nova_voice = types.SimpleNamespace(system_prompt=lambda ctx: "SYS:" + ctx)
HIST = types.SimpleNamespace(recent_articles_context=lambda section: "")


class _Cur:
    def __init__(self, counts, texts):
        self.counts = counts; self.texts = texts; self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = params[0]

    def fetchone(self):
        return (self.counts.get(self._last, 0),)

    def fetchall(self):
        return [(t,) for t in self.texts.get(self._last, [])]


class _Conn:
    def __init__(self, cur):
        self.c = cur; self.autocommit = False

    def cursor(self):
        return self.c


def _fresh_nj(llm_outputs):
    nj = mock.MagicMock(name="nova_journal")
    nj.call_openrouter.side_effect = list(llm_outputs)
    nj.today_str.return_value = "Oct 5"
    nj.generate_image.return_value = "/tmp/img.png"
    aw.nj = nj
    return nj


def _main(counts, texts, llm_outputs):
    nj = _fresh_nj(llm_outputs)
    cur = _Cur(counts, texts)
    with mock.patch.object(aw.psycopg2, "connect", return_value=_Conn(cur)), \
         mock.patch.dict(sys.modules, {"nova_article_history": HIST}):
        rc = aw.main()
    return rc, nj, cur


SAMPLE = {"scanner": ["unit 12 responding to a 459 at Olive and Glenoaks, suspect fled northbound on foot"]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        _, _, cur = _main({"scanner": 1}, SAMPLE, ["clean line one here", "TITLE: Busy Night On Olive\n\nbody"])
        for sql, params in cur.sql:
            self.assertIn("source=%s", sql)
            self.assertEqual(len(params), 1)


class TestPerformance(unittest.TestCase):
    def test_clean_transcripts_10k_lines(self):
        nj = _fresh_nj([])
        nj.call_openrouter.side_effect = None
        nj.call_openrouter.return_value = "\n".join(f"- cleaned transmission {i}" for i in range(10_000))
        t0 = time.perf_counter()
        out = aw.clean_transcripts(["x" * 50] * 10_000, "Police")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 10_000)


class TestRetry(unittest.TestCase):
    def test_llm_down_in_denoise_fails_open_to_raw(self):
        # RETRY GAP: clean_transcripts()/call_openrouter — no retry here; an empty answer keeps the raw sample
        nj = _fresh_nj([None])
        raw = ["a raw transmission that is long enough"]
        self.assertEqual(aw.clean_transcripts(raw, "Police"), raw)
        self.assertEqual(nj.call_openrouter.call_count, 1)

    def test_image_failure_is_non_fatal(self):
        nj = _fresh_nj(["clean line one here", "TITLE: Busy Night On Olive\n\nbody"])
        nj.get_image_prompt.side_effect = RuntimeError("image backend down")
        cur = _Cur({"scanner": 1}, SAMPLE)
        with mock.patch.object(aw.psycopg2, "connect", return_value=_Conn(cur)), \
             mock.patch.dict(sys.modules, {"nova_article_history": HIST}):
            self.assertEqual(aw.main(), 0)
        self.assertIsNone(nj.publish_hugo.call_args[1]["image_path"])


class TestUnit(unittest.TestCase):
    def test_clean_empty_input_skips_llm(self):
        nj = _fresh_nj([])
        self.assertEqual(aw.clean_transcripts([], "x"), [])
        nj.call_openrouter.assert_not_called()

    def test_clean_strips_bullets_and_drops_short(self):
        _fresh_nj(["- engine 4 on scene, smoke showing\n• ok\n* CHP responding to a crash on the 134"])
        out = aw.clean_transcripts(["x"], "Fire")
        self.assertEqual(out, ["engine 4 on scene, smoke showing", "CHP responding to a crash on the 134"])

    def test_beats_cover_core_sources(self):
        self.assertEqual([b[0] for b in aw.BEATS][:4], ["scanner", "fire", "chp", "rail"])


class TestIntegration(unittest.TestCase):
    def test_uses_journal_helpers_and_memories_table(self):
        self.assertIs(type(REAL_NJ), types.ModuleType)
        for fn in ("call_openrouter", "publish_hugo", "git_push", "notify_slack", "generate_image"):
            self.assertTrue(hasattr(REAL_NJ, fn), fn)
        self.assertIn("dbname=nova_memories", aw.MEM_DSN)
        self.assertIn("FROM memories", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes(self):
        rc, nj, _ = _main({"scanner": 3}, SAMPLE,
                          ["unit 12 responding to a burglary at Olive", "TITLE: \"Burglary on Olive\"\n\nThe body."])
        self.assertEqual(rc, 0)
        args = nj.publish_hugo.call_args[0]
        self.assertEqual(args[0], "Burglary on Olive")
        self.assertEqual(args[1], "The body.")
        self.assertEqual(args[2], "local")
        nj.git_push.assert_called_once_with("local", "Burglary on Olive")
        nj.notify_slack.assert_called_once()

    def test_degenerate_title_replaced(self):
        rc, nj, _ = _main({"scanner": 1}, SAMPLE, ["clean line one here", "TITLE: the the the the\n\nbody"])
        self.assertEqual(nj.publish_hugo.call_args[0][0], "On the Airwaves — Oct 5")

    def test_quiet_day_aborts_without_publishing(self):
        rc, nj, _ = _main({}, {}, [])
        self.assertEqual(rc, 1)
        nj.publish_hugo.assert_not_called()
        nj.call_openrouter.assert_not_called()

    def test_empty_llm_article_aborts(self):
        rc, nj, _ = _main({"scanner": 1}, SAMPLE, ["clean line one here", ""])
        self.assertEqual(rc, 1)
        nj.git_push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_local_airwaves"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("PUBLISHED", r.stdout)


if __name__ == "__main__":
    unittest.main()
