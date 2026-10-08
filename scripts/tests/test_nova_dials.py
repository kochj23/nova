"""Tests for nova_dials.py + nova_voice dials + nova_care_checkin.py — the 7 house categories
(Security, Performance, Retry, Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_care_checkin as cc  # noqa: E402
import nova_dials  # noqa: E402
import nova_voice  # noqa: E402


def _pg_rows(rows):
    """psycopg2.connect mock whose cursor returns `rows`."""
    cur = mock.MagicMock()
    cur.fetchall.return_value = rows
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value = cur
    cur.__enter__.return_value = cur
    return mock.patch("psycopg2.connect", return_value=conn), cur


def _fresh():
    nova_voice._DIAL_CACHE.update(at=0.0, vals=None)


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_parameterized_sql(self):
        for f in ("nova_dials.py", "nova_care_checkin.py"):
            src = (SCRIPTS / f).read_text()
            self.assertNotRegex(src, r"xox[bap]-[0-9A-Za-z-]{10,}")
            self.assertNotRegex(src, r"password\s*=\s*['\"]")
            self.assertNotRegex(src, r"execute\(f[\"']")
            self.assertNotIn("/Users/", src)

    def test_set_rejects_out_of_range(self):
        for bad in (("humor", "-1"), ("snark", "999"), ("x; DROP", "1"), ("profanity", "sure")):
            with self.assertRaises(ValueError):
                nova_dials.parse_set(*bad)


class TestPerformance(unittest.TestCase):
    def test_render_and_parse_fast(self):
        t = time.time()
        for _ in range(10000):
            nova_voice.render_dials(dict(nova_voice.DIAL_DEFAULTS))
        cc.parse_reply("1 yes 2 no 3 meh 4 yes " * 200, 4)
        self.assertLess(time.time() - t, 3.0)

    def test_dials_cached(self):
        _fresh()
        p, cur = _pg_rows([])
        with p as conn:
            nova_voice.dials(); nova_voice.dials(); nova_voice.dial("humor")
            self.assertEqual(conn.call_count, 1)


class TestRetry(unittest.TestCase):
    # RETRY GAP: nova_voice.dials / nova_care_checkin.remember — no retry; they fail open instead.
    def test_dials_fail_open_to_defaults(self):
        _fresh()
        with mock.patch("psycopg2.connect", side_effect=Exception("pg down")):
            self.assertEqual(nova_voice.dials(), nova_voice.DIAL_DEFAULTS)
            self.assertIn("humor 90/100", nova_voice.system_prompt_short())

    def test_remember_fails_open(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(cc.remember("x", {}))


class TestUnit(unittest.TestCase):
    def test_defaults_restate_todays_voice(self):
        r = nova_voice.render_dials(dict(nova_voice.DIAL_DEFAULTS))
        self.assertIn("as described above", r)
        self.assertIn("profanity ON", r)
        self.assertIn("CUE LIGHT", r)

    def test_bands_and_profanity_off(self):
        v = dict(nova_voice.DIAL_DEFAULTS, humor=10, snark=50, profanity=False, verbosity=90)
        r = nova_voice.render_dials(v)
        self.assertIn("mostly straight", r)
        self.assertIn("profanity OFF", r)
        self.assertIn("expansive", r)
        self.assertNotIn("CUE LIGHT", r)

    def test_dial_scale_default_is_identity(self):
        _fresh()
        with mock.patch.object(nova_voice, "dials", return_value=dict(nova_voice.DIAL_DEFAULTS)):
            self.assertAlmostEqual(nova_voice.dial_scale("proactivity", 0.9, 0.6, 0.3), 0.6)
        with mock.patch.object(nova_voice, "dials", return_value=dict(nova_voice.DIAL_DEFAULTS, proactivity=100)):
            self.assertAlmostEqual(nova_voice.dial_scale("proactivity", 0.9, 0.6, 0.3), 0.3)

    def test_parse_reply_and_week(self):
        self.assertEqual(cc.parse_reply("1: yes, 2) no 3 meh", 3), {"1": "helped", "2": "noise", "3": "meh"})
        self.assertEqual(cc.parse_reply("", 3), {})
        self.assertEqual(cc.week_of(date(2026, 10, 10)), date(2026, 10, 4))

    def test_selftests(self):
        with mock.patch("builtins.print"):
            self.assertEqual(cc.selftest(), 0)
            self.assertEqual(nova_dials.main(["--selftest"]), 0)


class TestIntegration(unittest.TestCase):
    def test_pg_values_flow_into_prompt(self):
        _fresh()
        p, _ = _pg_rows([("humor", 20), ("profanity", False), ("bogus", 5)])
        with p:
            v = nova_voice.dials()
        self.assertEqual(v["humor"], 20)
        self.assertFalse(v["profanity"])
        self.assertNotIn("bogus", v)
        _fresh()

    def test_reuses_shared_helpers(self):
        src = (SCRIPTS / "nova_care_checkin.py").read_text()
        self.assertIn("nsa.read_answer", src)
        self.assertIn('"jordan_feedback"', src)
        self.assertIn("nova_care_checkins", src)
        self.assertIn('SERVICE = "nova_dials"', (SCRIPTS / "nova_dials.py").read_text())


class TestFunctional(unittest.TestCase):
    def test_gather_compose(self):
        ops = mock.MagicMock()
        ops.fetchall.return_value = [("🆕 New device", "warning", 11)]
        ops.fetchone.return_value = ("rail radio", "I found a detail")
        mem = mock.MagicMock()
        mem.fetchall.return_value = [("local", "🕯️ The Watchman"), ("local", "B")]
        items = cc.gather(ops, mem)
        self.assertEqual([i["kind"] for i in items], ["alert", "articles", "reach"])
        self.assertIn('"New device"', items[0]["text"])
        msg = cc.compose(items, date(2026, 10, 11))
        self.assertIn("3. reach-out on rail radio", msg)

    def test_set_writes_service_config(self):
        p, cur = _pg_rows([])
        with p, mock.patch("builtins.print"):
            self.assertEqual(nova_dials.main(["set", "humor", "60"]), 0)
        sql, params = cur.execute.call_args_list[0][0]
        self.assertIn("INSERT INTO service_config", sql)
        self.assertEqual(params[:3], ("nova_dials", "humor", "60"))

    def test_set_bad_value_errors(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(nova_dials.main(["set", "humor", "loud"]), 2)


class TestFrame(unittest.TestCase):
    def test_selftest_subprocess(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for f in ("nova_dials.py", "nova_care_checkin.py"):
            r = subprocess.run([sys.executable, str(SCRIPTS / f), "--selftest"], env=env,
                               capture_output=True, text=True, timeout=30, cwd=SCRIPTS)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_has_no_side_effects(self):
        for f in ("nova_dials.py", "nova_care_checkin.py"):
            self.assertTrue(re.search(r'if __name__ == "__main__":\n    sys.exit\(main\(\)\)',
                                      (SCRIPTS / f).read_text()))


if __name__ == "__main__":
    unittest.main()
