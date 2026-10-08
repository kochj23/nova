#!/usr/bin/env python3
"""7-category tests for nova_yt_subs_audio.py (Security, Performance, Retry, Unit, Integration,
Functional, Frame): anonymous-first download, daily gate behind the baseline, 30/h rate limit,
shared whisper lock, LLM topic classifier with retry, listing retry, baseline retry lap.
Offline: yt-dlp, PostgreSQL, Ollama, Slack and Whisper are all mocked. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_yt_subs_audio_7cat.py
"""
import contextlib
import itertools
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
with patch.dict(sys.modules, {"nova_ingest": types.ModuleType("nova_ingest")}):
    import nova_yt_subs_audio as m  # noqa: E402

SRC = (SCRIPTS / "nova_yt_subs_audio.py").read_text()


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["yt-dlp"], rc, out, err)


class FakeCur:
    """Answers the handful of queries the module makes; records every execute."""

    def __init__(self, done=False, recent=0, had=()):
        self.done, self.recent, self.had = done, recent, list(had)
        self.calls, self._one, self._all = [], None, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM service_config WHERE service = %s" in sql:
            self._one = (1,) if self.done else None
        elif "count(*), min(seen_at)" in sql:
            self._one = (self.recent, None)
        elif "greatest(5" in sql:
            self._one = (10,)
            self.recent = 0     # the hour rolls over while we "sleep"
        elif "SELECT DISTINCT substr" in sql:
            self._all = [(c,) for c in self.had]
        elif "count(*) FILTER" in sql:
            self._one = (1, 0)
        elif "SELECT 1 FROM yt_ingest_seen" in sql:
            self._one = None
        else:
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def inserts(self):
        return [p for s, p in self.calls if s.startswith("INSERT INTO yt_ingest_seen")]


def fake_ni(text="hello world " * 50, audio_ok=True):
    ni = types.SimpleNamespace()
    ni._shutdown = False
    ni.WORK_DIR = Path(tempfile.mkdtemp())
    ni._audio = lambda a, w: (w.write_text("wav"), audio_ok)[1]
    ni._transcribe_dispatch = MagicMock(return_value=text)
    ni.clean_text = lambda t: t
    ni.chunk_words = lambda t: [t[:100], t[100:200]] if t else []
    ni.is_garbage = lambda c: False
    ni.remember = MagicMock(return_value=True)
    ni.get_existing_vectors = lambda: set()
    return ni


@contextlib.contextmanager
def no_lock():
    yield


class TestSecurity(unittest.TestCase):
    def test_channel_folder_cannot_escape_audio_dir(self):
        d = Path(tempfile.mkdtemp())
        with patch.object(m, "AUDIO_DIR", d):
            f = m.channel_folder("UCabc", "../../../etc/passwd")
        self.assertEqual(f.parent, d)
        self.assertNotIn("/", f.name)

    def test_download_is_anonymous_first(self):
        d = Path(tempfile.mkdtemp())
        seen = []

        def run(args, **kw):
            seen.append(args)
            folder = next(d.iterdir())
            (folder / "t [v1].m4a").write_text("a")
            return _cp(0)
        with patch.object(m, "AUDIO_DIR", d), patch.object(m.subprocess, "run", side_effect=run):
            got = m.download_audio("UCabc", "v1", "Chan")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("--cookies", seen[0])
        self.assertTrue(got.name.endswith("[v1].m4a"))

    def test_subprocess_uses_arg_list_never_shell(self):
        with patch.object(m.subprocess, "run", return_value=_cp()) as run:
            m._yt(["x; rm -rf /"])
        args, kw = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kw.get("shell", False))

    def test_transcript_and_classifier_stay_local(self):
        self.assertTrue(m.CLASSIFIER_URL.startswith("http://127.0.0.1"))
        self.assertIn("local_only=True", SRC)

    def test_seen_rows_written_with_bound_parameters(self):
        self.assertNotRegex(SRC, r'execute\(f"')

    def test_no_user_home_paths_or_secrets(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]")
        self.assertNotRegex(SRC.lower(), r"password\s*=|xox[bp]-")


class TestPerformance(unittest.TestCase):
    def test_ytdlp_calls_have_socket_and_process_timeouts(self):
        with patch.object(m.subprocess, "run", return_value=_cp()) as run:
            m._yt(["a"], timeout=7)
        self.assertIn("--socket-timeout", run.call_args[0][0])
        self.assertEqual(run.call_args[1]["timeout"], 7)

    def test_overlong_replays_skipped(self):
        out = f"long\tnot_live\t{m.MAX_SECONDS + 1}\tauction\nok\tnot_live\t60\tshort\n"
        with patch.object(m, "_yt", return_value=_cp(0, out)):
            self.assertEqual(m.latest_video("UCx"), ("ok", "short"))

    def test_rate_wait_sleep_is_bounded(self):
        cur = FakeCur(recent=m.RATE_PER_HOUR)
        with patch.object(m, "ni", fake_ni()), patch.object(m.time, "sleep") as sl:
            m.wait_for_rate(cur)
        self.assertTrue(all(c.args[0] <= 300 for c in sl.call_args_list))

    def test_uncovered_filter_scales(self):
        subs = [(f"UC{i:022d}", f"@h{i}", f"n{i}") for i in range(20000)]
        t = time.perf_counter()
        out = m.uncovered(subs, {f"h{i}" for i in range(0, 20000, 2)}, set())
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(len(out), 10000)

    def test_baseline_time_slice_exits(self):
        cur = FakeCur()
        todo = [("UC1", "@a", "A")]
        with patch.object(m, "ni", fake_ni()), patch.object(m, "post_info"), \
                patch.object(m, "latest_video") as lv, patch.object(m.time, "time", side_effect=itertools.chain([0, 0, 0], itertools.repeat(10 ** 6))):
            m.baseline(cur, todo, set(), max_minutes=1)
        lv.assert_not_called()


class TestRetry(unittest.TestCase):
    def test_listing_retries_then_succeeds(self):
        rs = [_cp(1, err="HTTP 500"), _cp(0, "v1\tnot_live\t60\tT\n")]
        with patch.object(m, "_yt", side_effect=rs) as yt, patch.object(m.time, "sleep") as sl:
            self.assertEqual(m.latest_video("UCx"), ("v1", "T"))
        self.assertEqual(yt.call_count, 2)
        sl.assert_called_once()

    def test_listing_gives_up_after_three_and_logs(self):
        with patch.object(m, "_yt", return_value=_cp(1, err="blocked")) as yt, \
                patch.object(m.time, "sleep"), patch.object(m, "log") as lg:
            self.assertIsNone(m.latest_video("UCx"))
        self.assertEqual(yt.call_count, 3)
        self.assertTrue(any("failed after 3" in c.args[0] for c in lg.call_args_list))

    def test_classifier_retries_with_backoff(self):
        ok = types.SimpleNamespace(read=lambda: json.dumps({"message": {"content": "music"}}).encode())
        with patch("urllib.request.urlopen", side_effect=[OSError("swap"), ok]), \
                patch.object(m.time, "sleep") as sl:
            self.assertEqual(m.classify("Beatles", "Abbey Road"), "music")
        sl.assert_called_once()

    def test_classifier_final_failure_logged_not_silent(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as u, \
                patch.object(m.time, "sleep"), patch.object(m, "log") as lg:
            self.assertEqual(m.classify("x", "y"), m.FALLBACK_VECTOR)
        self.assertEqual(u.call_count, 3)
        self.assertTrue(lg.called)

    def test_download_falls_back_to_cookies(self):
        d = Path(tempfile.mkdtemp())
        seen = []

        def run(args, **kw):
            seen.append(args)
            if "--cookies" not in args:
                return _cp(1, err="403")
            (next(d.iterdir()) / "t [v1].m4a").write_text("a")
            return _cp(0)
        with patch.object(m, "AUDIO_DIR", d), patch.object(m.subprocess, "run", side_effect=run):
            self.assertIsNotNone(m.download_audio("UCabc", "v1", "C"))
        self.assertEqual(len(seen), 2)
        self.assertIn("--cookies", seen[1])

    def test_download_both_fail_logged_returns_none(self):
        d = Path(tempfile.mkdtemp())
        with patch.object(m, "AUDIO_DIR", d), patch.object(m.subprocess, "run", return_value=_cp(1, err="x\nERROR: gone")), \
                patch.object(m, "log") as lg:
            self.assertIsNone(m.download_audio("UCabc", "v1", "C"))
        self.assertIn("ERROR: gone", lg.call_args[0][0])

    def test_baseline_retry_lap_retries_failed_channel_once(self):
        cur = FakeCur()
        calls = []

        def dl(cid, vid, name):
            calls.append(cid)
            return None   # always fails
        with patch.object(m, "ni", fake_ni()), patch.object(m, "post_info"), \
                patch.object(m, "latest_video", return_value=("v", "t")), patch.object(m, "download_audio", side_effect=dl):
            m.baseline(cur, [("UC1", "@a", "A")], set())
        self.assertEqual(calls, ["UC1", "UC1"])

    def test_subscriptions_fall_back_to_cached_list(self):
        cur = MagicMock()
        cur.fetchone.return_value = ([["UC1", "@a", "A"]],)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(m, "_yt", return_value=_cp(1)), patch.object(m.psycopg2, "connect", return_value=conn):
            self.assertEqual(m.subscriptions(), [("UC1", "@a", "A")])

    def test_stalled_ytdlp_becomes_failure(self):
        with patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired("yt", 1)):
            self.assertEqual(m._yt(["a"]).returncode, 1)


class TestUnit(unittest.TestCase):
    def test_covered_keys(self):
        h, i = m.covered_keys("https://www.youtube.com/@Foo.Bar UC" + "a" * 22)
        self.assertEqual(h, {"foo.bar"})
        self.assertEqual(i, {"UC" + "a" * 22})

    def test_laps_yields_retry_lap_after_pending(self):
        failed, lap = [], []
        out = []
        for x in m._laps([1, 2], failed, lap):
            out.append(x)
            if x == 1 and len(out) == 1:
                failed.append(1)
        self.assertEqual(out, [1, 2, 1])

    def test_keep_only_logs_unlink_errors(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.m4a").write_text("1"); (d / "b.m4a").write_text("2")
        with patch.object(Path, "unlink", side_effect=OSError("EBUSY")), patch.object(m, "log") as lg:
            m.keep_only(d, d / "b.m4a")
        self.assertIn("EBUSY", lg.call_args[0][0])

    def test_classifier_unknown_label_falls_back(self):
        r = types.SimpleNamespace(read=lambda: json.dumps({"message": {"content": "gardening"}}).encode())
        with patch("urllib.request.urlopen", return_value=r):
            self.assertEqual(m.classify("a", "b"), m.FALLBACK_VECTOR)


class TestIntegration(unittest.TestCase):
    def test_transcribe_stores_chunks_under_classified_vector_with_lock(self):
        ni = fake_ni()
        lock = MagicMock()
        lock.return_value.__enter__ = MagicMock(); lock.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(m, "ni", ni), patch("nova_yt_capture.whisper_lock", lock), \
                patch.object(m, "classify", return_value="horology"):
            n = m.transcribe_and_remember(Path("a.m4a"), "Chan", "Omega", "v1", set(), False)
        self.assertEqual(n, 2)
        lock.assert_called_once()
        self.assertEqual(ni.remember.call_args[0][1], "horology")
        self.assertEqual(ni.remember.call_args[0][2]["pipeline"], "yt_subs_audio")
        self.assertEqual(list(ni.WORK_DIR.glob("*.wav")), [])   # temp wav removed

    def test_no_speech_is_none_not_failure(self):
        ni = fake_ni(text="")
        with patch.object(m, "ni", ni), patch("nova_yt_capture.whisper_lock", no_lock):
            self.assertIsNone(m.transcribe_and_remember(Path("a"), "C", "T", "v", set(), False))

    def test_audio_extract_failure_is_zero(self):
        with patch.object(m, "ni", fake_ni(audio_ok=False)):
            self.assertEqual(m.transcribe_and_remember(Path("a"), "C", "T", "v", set(), False), 0)


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur, subs=(("UC1", "@a", "A"),), mounted=True):
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(sys, "argv", ["x", *argv]), patch.object(m, "subscriptions", return_value=list(subs)), \
                patch.object(m, "ni", fake_ni()), patch.object(m.psycopg2, "connect", return_value=conn), \
                patch.object(m.time, "sleep"), patch.object(m.os.path, "ismount", return_value=mounted), \
                patch.object(m, "latest_video", return_value=("v9", "Title")) as lv, \
                patch.object(m, "download_audio", return_value=Path("a.m4a")) as dl, \
                patch.object(m, "transcribe_and_remember", return_value=3), patch.object(m, "post_info") as pi:
            m.main()
        return lv, dl, pi

    def test_daily_update_waits_for_baseline(self):
        cur = FakeCur(done=False)
        lv, dl, _ = self._main([], cur)
        lv.assert_not_called(); dl.assert_not_called()

    def test_daily_update_golden_path(self):
        cur = FakeCur(done=True)
        _, dl, _ = self._main(["--max", "1"], cur)
        dl.assert_called_once()
        self.assertEqual(cur.inserts()[0][3], "ingested")

    def test_dry_run_downloads_nothing(self):
        cur = FakeCur(done=True)
        _, dl, _ = self._main(["--dry-run"], cur)
        dl.assert_not_called()
        self.assertEqual(cur.inserts(), [])

    def test_baseline_golden_marks_done_and_reports(self):
        cur = FakeCur(done=False)
        _, dl, pi = self._main(["--baseline"], cur)
        dl.assert_called_once()
        self.assertTrue(any(s.startswith("INSERT INTO service_config") for s, _ in cur.calls))
        self.assertIn("baseline done", pi.call_args[0][0])

    def test_baseline_refuses_unmounted_share(self):
        with self.assertRaises(SystemExit):
            self._main(["--baseline"], FakeCur(), mounted=False)

    def test_empty_subscription_list_exits_nonzero(self):
        with self.assertRaises(SystemExit) as e:
            self._main([], FakeCur(), subs=())
        self.assertEqual(e.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_module_shape(self):
        for name in ("main", "baseline", "download_audio", "classify", "wait_for_rate", "latest_video"):
            self.assertTrue(callable(getattr(m, name)))

    def test_compiles(self):
        compile(SRC, "nova_yt_subs_audio.py", "exec")

    def test_help_runs_without_side_effects(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_yt_subs_audio.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--baseline", r.stdout)


if __name__ == "__main__":
    unittest.main()
