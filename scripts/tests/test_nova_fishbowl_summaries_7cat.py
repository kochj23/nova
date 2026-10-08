#!/usr/bin/env python3
"""7-category gap tests for nova_fishbowl_summaries.py — the 2026-10-07 KNOWN_FACTS change (Anthony
Farrer / Timepiece Gentleman is Fishbowl, archive-only, never watch news) as consumed by the dossier
prompt and by Watches and Friends, plus the PG connect retry added here. PG, the Claude CLI and
Slack are mocked. Base suite: test_nova_fishbowl_summaries.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_fishbowl_summaries_7cat.py
"""
import importlib.util
import io
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fishbowl_summaries.py"


def _load():
    nj = types.ModuleType("nova_journal"); nj.call_openrouter = MagicMock(return_value="A dossier.")
    nv = types.ModuleType("nova_voice"); nv.system_prompt = lambda c: c
    nc = types.ModuleType("nova_config"); nc.post_both = MagicMock()
    spec = importlib.util.spec_from_file_location("fishsum_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": nv, "nova_config": nc}):
        spec.loader.exec_module(mod)
    return mod


fs = _load()


class _Quiet(unittest.TestCase):
    def setUp(self):
        r = redirect_stdout(io.StringIO()); r.__enter__(); self.addCleanup(r.__exit__, None, None, None)
        p = patch("time.sleep"); self.sleep = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Quiet):
    def test_dossier_prompt_carries_ground_truth(self):
        fs.nj.call_openrouter.reset_mock()
        fs.summarize("Anthony Farrer", ["@Thetimepiecegentleman"], ["mem"])
        system = fs.nj.call_openrouter.call_args.args[0]
        self.assertIn("ARCHIVE footage", system)
        self.assertIn("never describe him as currently dealing", system)

    def test_memory_context_bounded(self):
        fs.nj.call_openrouter.reset_mock()
        fs.summarize("X", ["c"], ["m" * 5000] * 100)
        user = fs.nj.call_openrouter.call_args.args[1]
        self.assertLess(len(user), 25 * 1200 + 2000)

    def test_alias_search_binds_values_not_sql(self):
        cur = MagicMock(); cur.fetchall.return_value = []
        fs.gather(cur, ["x' OR 1=1 --", "y"])
        sql, params = cur.execute.call_args.args
        self.assertNotIn("1=1", sql)
        self.assertEqual(params, ["%x' OR 1=1 --%", "%y%"])


class TestPerformance(_Quiet):
    def test_connect_timeout_and_bounded_backoff(self):
        with patch.object(fs.psycopg2, "connect", side_effect=psycopg2.OperationalError("x")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            fs._connect("dsn")
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 10)
        self.assertLessEqual(sum(a.args[0] for a in self.sleep.call_args_list), 15)


class TestRetry(_Quiet):
    def test_pg_connect_retried(self):
        conn = MagicMock()
        with patch.object(fs.psycopg2, "connect", side_effect=[psycopg2.OperationalError("blip"), conn]):
            self.assertIs(fs._connect("dsn"), conn)
        self.sleep.assert_called_once_with(5)

    def test_pg_connect_gives_up_after_three(self):
        with patch.object(fs.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            fs._connect("dsn")
        self.assertEqual(c.call_count, 3)

    def test_llm_retry_is_inside_call_openrouter(self):
        self.assertIn("_attempts = 3", (SCRIPTS / "nova_journal.py").read_text())

    def test_empty_llm_dossier_skipped_and_logged(self):
        cur = MagicMock(); cur.fetchall.return_value = []; cur.fetchone.return_value = None
        conn = MagicMock(); conn.cursor.return_value = cur
        out = io.StringIO()
        with patch.object(fs, "_connect", return_value=conn), patch.object(fs, "discover_guests"), \
                patch.object(fs, "gather", return_value=["m"]), patch.object(fs, "summarize", return_value=""), \
                patch.object(fs, "PEOPLE", [{"name": "P", "aliases": ["P"], "channels": ["c"]}]), \
                patch.object(fs, "slack") as sl, redirect_stdout(out):
            fs.main()
        self.assertIn("P: LLM produced nothing", out.getvalue())
        sl.assert_called_once()   # nothing refreshed -> the 'still filling' notice


class TestUnit(_Quiet):
    def test_known_facts_farrer_entry(self):
        kf = fs.KNOWN_FACTS
        self.assertIn("Anthony Farrer", kf)
        self.assertIn("FISHBOWL / HATE-STREAM material", kf)
        self.assertIn("never watch news", kf)


class TestIntegration(_Quiet):
    def test_watches_and_friends_imports_known_facts(self):
        src = (SCRIPTS / "nova_watches_and_friends.py").read_text()
        self.assertIn("from nova_fishbowl_summaries import KNOWN_FACTS", src)

    def test_farrer_channel_not_in_watch_news(self):
        spec = importlib.util.spec_from_file_location("yw_for_fs", SCRIPTS / "nova_yt_ingest_watch.py")
        yw = importlib.util.module_from_spec(spec); spec.loader.exec_module(yw)
        horo = [c["url"].lower() for c in yw.CHANNELS if c["vector"] == "horology"]
        self.assertFalse(any("timepiecegentleman" in u for u in horo))


class TestFunctional(_Quiet):
    def test_golden_refreshes_dossier_row(self):
        cur = MagicMock(); cur.fetchall.return_value = []; cur.fetchone.return_value = ("sig",)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(fs, "_connect", return_value=conn), patch.object(fs, "discover_guests"), \
                patch.object(fs, "gather", return_value=["m1", "m2"]), patch.object(fs, "summarize", return_value=" D "), \
                patch.object(fs, "PEOPLE", [{"name": "P", "aliases": ["P"], "channels": ["c"]}]), \
                patch.object(fs, "slack") as sl:
            fs.main()
        ins = [c for c in cur.execute.call_args_list if c.args[0].startswith("INSERT INTO fishbowl_people (name,aliases,channels,summary")]
        self.assertEqual(ins[0].args[1][3], "D")
        self.assertEqual(ins[0].args[1][4], 2)
        sl.assert_not_called()

    def test_pg_down_raises(self):
        with patch.object(fs.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), \
                self.assertRaises(psycopg2.OperationalError):
            fs.main()


class TestFrame(unittest.TestCase):
    def test_compiles_and_entrypoints(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for n in ("main", "_connect", "summarize", "extract_names"):
            self.assertTrue(callable(getattr(fs, n)))


if __name__ == "__main__":
    unittest.main()
