#!/usr/bin/env python3
"""7-category gap tests for nova_fellowship_daily.py — the 2026-10-06 franchise additions (M*A*S*H
plus 15 TV/film casts), the corrected host/IP mapping (nova-core3 = .5, mac-mini = .77), and the
LLM / PG retries added here. PG, the LLM, image generation and publishing are mocked.
Base suite: test_nova_fellowship_daily.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_fellowship_daily_7cat.py
"""
import importlib.util
import io
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fellowship_daily.py"
NEW = ["M*A*S*H (1972-83)", "Friday the 13th", "Halloween", "Hellraiser", "The Conjuring", "Scream",
       "The Evil Dead", "21 Jump Street", "Magnum, P.I. (1980-88)", "Miami Vice", "The A-Team",
       "Mr. Belvedere", "Good Times", "Alice (1976-85)", "Hawaii Five-O (1968-80)", "CHiPs"]
FLEET = ("mac-studio", "nova-core (", "nova-core2", "nova-core3", "nova-core4", "nova-core5", "tv-movies-mini", "mac-mini")


def _load():
    nj = types.ModuleType("nova_journal")
    for f in ("publish_hugo", "git_push", "notify_slack", "get_image_prompt", "generate_image"):
        setattr(nj, f, MagicMock())
    nj.today_str = lambda: "2026-10-08"
    nv = types.ModuleType("nova_voice"); nv.CONTEXT_JOURNAL_OPS = "OPS:"; nv.system_prompt = lambda s: s
    rd = types.ModuleType("nova_rando_daily_ops"); rd.call_llm = MagicMock()
    spec = importlib.util.spec_from_file_location("fellow_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": nv, "nova_rando_daily_ops": rd}):
        spec.loader.exec_module(mod)
    mod.ROTATION_STATE = Path(tempfile.mkdtemp()) / "rotation.json"
    return mod


fd = _load()
AH = types.ModuleType("nova_article_history"); AH.recent_articles_context = lambda s: ""


def _conn():
    cur = MagicMock(); cur.fetchall.side_effect = [[("nova-core", "running", 3)], [("nova-core", 5.0, 2.0)]]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn


def run_main(llm):
    fd.call_llm = llm
    fd.nj.publish_hugo.reset_mock()
    with patch.object(fd.psycopg2, "connect", return_value=_conn()), patch.object(fd.time, "sleep") as sl, \
            patch.dict(sys.modules, {"nova_article_history": AH}), redirect_stdout(io.StringIO()) as out:
        rc = fd.main()
    return rc, sl, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_new_casts_carry_no_secrets_or_private_ips_beyond_last_octet(self):
        for f in fd.FRANCHISES:
            self.assertNotRegex(f["cast"], r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", f["name"])
            self.assertNotRegex(f["cast"].lower(), r"password|api[_-]?key|token")

    def test_read_only_sql(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET)\b", SCRIPT.read_text()))


class TestPerformance(unittest.TestCase):
    def test_retry_backoff_bounded(self):
        rc, sl, _ = run_main(MagicMock(return_value=""))
        self.assertLessEqual(sum(c.args[0] for c in sl.call_args_list), 20)

    def test_casts_reasonably_sized_for_prompt(self):
        self.assertTrue(all(len(f["cast"]) < 12000 for f in fd.FRANCHISES))


class TestRetry(unittest.TestCase):
    def test_llm_exception_then_success_publishes(self):
        rc, sl, _ = run_main(MagicMock(side_effect=[RuntimeError("claude -p flake"), "TITLE: Fine\n\nbody"]))
        self.assertEqual(rc, 0)
        sl.assert_called_once_with(5)
        self.assertEqual(fd.nj.publish_hugo.call_args.args[0], "Fine")

    def test_llm_final_failure_logged(self):
        rc, _, out = run_main(MagicMock(side_effect=RuntimeError("down")))
        self.assertEqual(rc, 1)
        self.assertIn("LLM failed after 3 tries", out)
        fd.nj.publish_hugo.assert_not_called()

    def test_pg_connect_retried(self):
        conn = _conn()
        with patch.object(fd.psycopg2, "connect", side_effect=[OSError("blip"), conn]), patch.object(fd.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            status, threats = fd.gather_today_status()
        self.assertIn("nova-core: 3 running", status)

    def test_pg_unreachable_raises(self):
        with patch.object(fd.psycopg2, "connect", side_effect=OSError("down")), patch.object(fd.time, "sleep"), \
                redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            fd.gather_today_status()


class TestUnit(unittest.TestCase):
    def test_new_franchises_present_once(self):
        names = [f["name"] for f in fd.FRANCHISES]
        for n in NEW:
            self.assertEqual(names.count(n), 1, n)

    def test_every_franchise_complete(self):
        for f in fd.FRANCHISES:
            self.assertTrue(all(f.get(k) for k in ("cast", "emoji", "image_style", "name", "tag")), f["name"])

    def test_every_cast_maps_the_whole_fleet(self):
        for f in fd.FRANCHISES:
            for host in FLEET:
                self.assertIn(host, f["cast"], f"{f['name']}: {host}")

    def test_corrected_octets(self):
        for f in fd.FRANCHISES:
            for host, octet in (("nova-core3", "(.5)"), ("mac-mini", "(.77)")):
                m = re.search(rf"- {re.escape(host)} (\(\.\d+\))", f["cast"])
                if m:
                    self.assertEqual(m.group(1), octet, f"{f['name']}: {host}")


class TestIntegration(unittest.TestCase):
    def test_rotation_reaches_every_new_franchise(self):
        fd.ROTATION_STATE.unlink(missing_ok=True)
        seen = {fd.next_franchise()["name"] for _ in range(len(fd.FRANCHISES))}
        self.assertTrue(set(NEW) <= seen)


class TestFunctional(unittest.TestCase):
    def test_golden_publishes_with_franchise_tag(self):
        rc, _, _ = run_main(MagicMock(return_value="TITLE: Quiet\n\nAll green."))
        self.assertEqual(rc, 0)
        tags = fd.nj.publish_hugo.call_args.args[3]
        self.assertIn("operations", tags)


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(callable(fd.main) and callable(fd._retry))


if __name__ == "__main__":
    unittest.main()
