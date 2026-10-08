#!/usr/bin/env python3
"""7-category gap tests for nova_speaks_upload.py — the 2026-10-07 cookie guard ("never replace a working
YouTube jar with a logged-out export"), the private raw Safari export, cookie-export retry and the
retire() retry. Safari/yt-dlp, YouTube and PG are mocked; nothing is uploaded.
Base suite: test_nova_speaks_upload.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_speaks_upload_7cat.py
"""
import io
import os
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
import nova_speaks_upload as up  # noqa: E402

LOGGED_IN = ("# Netscape HTTP Cookie File\n"
             ".google.com\tTRUE\t/\tTRUE\t0\tSAPISID\ts1\n"
             ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tli\n"
             ".bank.example\tTRUE\t/\tTRUE\t0\tsession\tBANKSECRET\n")
LOGGED_OUT = ("# Netscape HTTP Cookie File\n"
              ".youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tv\n"
              ".youtube.com\tTRUE\t/\tTRUE\t0\tYSC\ty\n")


class _Env(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.raw = self.d / "raw.txt"
        self.jar = self.d / "jar.txt"
        self.jar.write_text("GOOD-OLD-JAR")
        self.out = io.StringIO()
        for p in (patch.object(up, "COOKIES", self.jar), patch("tempfile.mktemp", return_value=str(self.raw)),
                  patch("time.sleep"), redirect_stdout(self.out)):
            p.__enter__(); self.addCleanup(p.__exit__, None, None, None)

    def export(self, *results):
        """subprocess.run stub: each result is (rc, raw_text or None) or an exception."""
        it = iter(results)

        def run(argv, **kw):
            r = next(it)
            if isinstance(r, BaseException):
                raise r
            rc, text = r
            if text is not None:
                self.raw.write_text(text)
                self.umask_seen = os.umask(0); os.umask(self.umask_seen)
            return subprocess.CompletedProcess(argv, rc, "", "err")
        return patch("subprocess.run", side_effect=run)


class TestSecurity(_Env):
    def test_raw_safari_export_written_private_and_always_removed(self):
        with self.export((0, LOGGED_IN)):
            up.refresh_cookies()
        self.assertEqual(self.umask_seen, 0o077)
        self.assertFalse(self.raw.exists())
        self.assertNotIn("BANKSECRET", self.jar.read_text())
        self.assertEqual(self.jar.stat().st_mode & 0o777, 0o600)

    def test_raw_export_removed_even_when_logged_out(self):
        with self.export((0, LOGGED_OUT)):
            up.refresh_cookies()
        self.assertFalse(self.raw.exists())

    def test_umask_restored(self):
        before = os.umask(0o022); os.umask(before)
        with self.export((0, LOGGED_IN)):
            up.refresh_cookies()
        after = os.umask(0o022); os.umask(after)
        self.assertEqual(before, after)

    def test_retire_rejects_non_video_ids(self):
        with patch.object(sys, "argv", ["x", "--retire", "../../x;rm"]), patch.object(up, "session") as s:
            self.assertEqual(up.main(), 2)
        s.assert_not_called()


class TestPerformance(_Env):
    def test_export_has_timeout_and_bounded_attempts(self):
        with self.export((1, None), (1, None)) as run:
            up.refresh_cookies()
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(c.kwargs["timeout"] == 120 for c in run.call_args_list))

    def test_large_export_filtered_fast(self):
        big = LOGGED_IN + "".join(f".site{i}.com\tTRUE\t/\tTRUE\t0\tc{i}\tv\n" for i in range(50000))
        with self.export((0, big)):
            t = time.perf_counter(); up.refresh_cookies()
        self.assertLess(time.perf_counter() - t, 2.0)


class TestRetry(_Env):
    def test_export_retried_then_succeeds(self):
        with self.export((1, None), (0, LOGGED_IN)) as run:
            up.refresh_cookies()
        self.assertEqual(run.call_count, 2)
        self.assertIn("LOGIN_INFO", self.jar.read_text())

    def test_export_timeout_is_a_failure_not_a_crash(self):
        with self.export(subprocess.TimeoutExpired("yt-dlp", 120), subprocess.TimeoutExpired("yt-dlp", 120)):
            up.refresh_cookies()
        self.assertEqual(self.jar.read_text(), "GOOD-OLD-JAR")
        self.assertIn("timed out", self.out.getvalue())

    def test_retire_retried_after_transient_error(self):
        cur = MagicMock(); cur.fetchone.return_value = (str(self._article()), "u", "/x.mp4", "OLDOLDOLD11", "OLDOLDOLD11")
        cur.rowcount = 1
        conn = MagicMock(); conn.cursor.return_value = cur
        yt = self._yt()
        with patch.object(up.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x", "--slug", "s"]), \
                patch.dict(sys.modules, {"youtube_up": yt}), patch.object(up, "session", return_value=MagicMock(upload=lambda *a, **k: "NEWNEWNEW22")), \
                patch.object(up, "retire", side_effect=[ConnectionError("reset"), True]) as rt:
            self.assertEqual(up.main(), 0)
        self.assertEqual(rt.call_count, 2)
        final = cur.execute.call_args_list[-1].args[1]
        self.assertTrue(final[0])
        self.assertIn("set PRIVATE", final[1])

    def test_retire_gives_up_after_three_and_flags_manual_hide(self):
        cur = MagicMock(); cur.fetchone.return_value = (str(self._article()), "u", "/x.mp4", "OLDOLDOLD11", "OLDOLDOLD11")
        cur.rowcount = 1
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(up.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x", "--slug", "s"]), \
                patch.dict(sys.modules, {"youtube_up": self._yt()}), \
                patch.object(up, "session", return_value=MagicMock(upload=lambda *a, **k: "NEWNEWNEW22")), \
                patch.object(up, "retire", side_effect=ConnectionError("down")) as rt:
            up.main()
        self.assertEqual(rt.call_count, 3)
        self.assertIn("NEEDS MANUAL HIDE", cur.execute.call_args_list[-1].args[1][1])

    def _article(self):
        d = self.d / "content" / "local"; d.mkdir(parents=True, exist_ok=True)
        p = d / "a.md"; p.write_text('---\ntitle: "Heat"\ndate: 2026-10-05\ntags: ["x"]\n---\nbody')
        return p

    def _yt(self):
        m = types.ModuleType("youtube_up")
        m.Metadata = lambda **k: k
        m.PrivacyEnum = {"PUBLIC": "PUBLIC", "PRIVATE": "PRIVATE", "UNLISTED": "UNLISTED"}
        m.CategoryEnum = types.SimpleNamespace(SCIENCE_TECH="st")
        return m


class TestUnit(_Env):
    def test_logged_out_export_never_replaces_working_jar(self):
        with self.export((0, LOGGED_OUT)):
            up.refresh_cookies()
        self.assertEqual(self.jar.read_text(), "GOOD-OLD-JAR")
        self.assertIn("no login cookies", self.out.getvalue())

    def test_google_login_cookie_redomained_to_youtube(self):
        with self.export((0, LOGGED_IN)):
            up.refresh_cookies()
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\ts1", self.jar.read_text())


class TestIntegration(_Env):
    def test_session_loads_the_refreshed_jar(self):
        yt = types.ModuleType("youtube_up")
        yt.YTUploaderSession = lambda jar: jar
        with self.export((0, LOGGED_IN)), patch.dict(sys.modules, {"youtube_up": yt}):
            jar = up.session()
        self.assertIn("LOGIN_INFO", {c.name for c in jar})

    def test_subs_audio_reads_the_jar_this_script_keeps_fresh(self):
        for f in ("nova_yt_subs_audio.py", "nova_speaks_upload.py"):
            self.assertIn('".openclaw/cache/yt_cookies_youtube.txt"', (SCRIPTS / f).read_text())


class TestFunctional(_Env):
    def test_check_reports_invalid_session(self):
        sess = MagicMock(); sess.has_valid_cookies.return_value = False
        with patch.object(sys, "argv", ["x", "--check"]), patch.object(up, "session", return_value=sess):
            self.assertEqual(up.main(), 1)

    def test_logged_out_then_logged_in_sequence(self):
        with self.export((0, LOGGED_OUT)):
            up.refresh_cookies()
        self.assertEqual(self.jar.read_text(), "GOOD-OLD-JAR")
        with self.export((0, LOGGED_IN)):
            up.refresh_cookies()
        self.assertIn("LOGIN_INFO", self.jar.read_text())


class TestFrame(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_speaks_upload.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--retire", r.stdout)

    def test_entrypoints(self):
        for n in ("main", "refresh_cookies", "_store_jar", "session", "retire", "build"):
            self.assertTrue(callable(getattr(up, n)))


if __name__ == "__main__":
    unittest.main()
