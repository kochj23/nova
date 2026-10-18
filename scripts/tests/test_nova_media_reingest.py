#!/usr/bin/env python3
"""Tests for nova_media_reingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure functions on fixtures; PG through fake cursors; ffmpeg / ssh /
mlx_whisper through a mocked subprocess runner; the memory server through an injected post().
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_media_reingest.py"
spec = importlib.util.spec_from_file_location("media_reingest_under_test", SCRIPT)
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
SRC = SCRIPT.read_text()
ROOT = mr.VIDEO_ROOT
EP = ROOT + "TVShows/Engine Masters/Season 05/Engine Masters_S05E02_Cheap Carb Showdown.mp4"
SPEECH = " ".join(f"the carb on dyno pull {i} made more torque than we thought" for i in range(120))


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last, self.rowcount = [], [], "", 0

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql
        self.rowcount = len(params[1]) if params and "superseded_by = %s" in sql else 1

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


def _writes(cur):
    return [(q, p) for q, p in zip(cur.sql, cur.params) if re.match(r"\s*(INSERT|UPDATE|DELETE)", q)]


def _fake_run(transcript=SPEECH, fail=None):
    """subprocess.run stand-in: ffmpeg writes a 60 s wav, mlx_whisper writes transcript.txt."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if fail and fail(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr=b"boom")
        if cmd[0] == mr.FFMPEG_BIN:
            Path(cmd[-1]).write_bytes(b"\0" * (44 + 60 * mr.WAV_BYTES_PER_SEC))
        elif cmd[0] == mr.WHISPER_BIN:
            out = Path(cmd[cmd.index("--output-dir") + 1]) / "transcript.txt"
            out.write_text(transcript)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    run.calls = calls
    return run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_personal_data(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|secret|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotIn("kochj23" + "@", SRC)
        self.assertNotIn(str(Path.home()), SRC)

    def test_sql_is_parameterized(self):
        for m in re.finditer(r'execute\(f"([^"]*)"', SRC):
            self.assertNotRegex(m.group(1), r"\{(?!cols\})", "only the fixed column list may be interpolated")
        self.assertIn("WHERE file_path = %s", SRC)

    def test_remote_path_is_shell_quoted(self):
        nasty = ROOT + "TVShows/x/$(rm -rf ~) `id`.mp4"
        run = _fake_run()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(mr, "local_readable", return_value=False):
            mr.extract_audio(nasty, Path(d), _run=run)
        ssh = next(c for c in run.calls if c[0] == "ssh")
        self.assertIn("'/external/videos/TVShows/x/$(rm -rf ~) `id`.mp4'", ssh[-1])

    def test_never_deletes_memories(self):
        self.assertNotRegex(SRC, r"(?i)DELETE\s+FROM\s+memories")
        self.assertIn("superseded_by = %s, valid_to = now()", SRC)

    def test_local_only_transcription(self):
        self.assertNotIn("openrouter", SRC.lower())
        self.assertIn("whisper-large-v3-turbo", mr.WHISPER_MODEL)


class TestPerformance(unittest.TestCase):
    def test_pure_functions_on_10k_items(self):
        paths = [ROOT + f"TVShows/Show {i % 50}/Season 01/ep {i} (19{i % 100:02d}).mkv" for i in range(10000)]
        t = time.perf_counter()
        for i, p in enumerate(paths):
            mr.eligible(p, 50_000_000)
            mr.priority(i % 3, i % 7, i, i % 40)
        target, sup = mr.plan_supersede([f"o{i}" for i in range(10000)], [f"o{i}" for i in range(0, 10000, 2)])
        self.assertEqual(len(sup), 5000)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_batch_loop_is_bounded_by_the_deadline(self):
        oc = _Cur([("RETURNING file_path", (EP, "Engine Masters", "ep", "trash"))])
        with mock.patch.object(mr, "process") as proc:
            st = mr.run_batch(oc, _Cur(), datetime.now() + timedelta(seconds=60), None)
        proc.assert_not_called()                      # < 300 s left: never starts a file it cannot finish
        self.assertEqual(st["done"], 0)


class TestRetry(unittest.TestCase):
    def test_remember_post_retries_with_backoff(self):
        import nova_skipping_needle as sn
        attempts = []

        def flaky(req, timeout=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("down")
            return io.BytesIO(b'{"id": "new-1", "status": "stored"}')
        with mock.patch("urllib.request.urlopen", side_effect=flaky), \
                mock.patch.object(sn, "remember_url", return_value="http://memory.invalid/remember"):
            ids = mr.store(["chunk one"], "Show", "T", EP, "automotive", "tv_transcript",
                           post=lambda *a: sn.post_remember(*a, _sleep=lambda s: None))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(ids, ["new-1"])

    def test_failed_store_leaves_old_memories_untouched(self):
        # RETRY GAP: none — a chunk that still fails after post_remember's retries aborts the item before supersession
        oc = _Cur([("source_vector FROM media_ingest_state", ("automotive",))])
        mc = _Cur([("FROM memories WHERE metadata->>'show'", [("old-1", "automotive", "tv_transcript", 400, True)])])
        with mock.patch.object(mr, "extract_audio", side_effect=lambda p, t: _wav(t)), \
                mock.patch.object(mr, "transcribe_local", return_value=SPEECH):
            with self.assertRaises(RuntimeError):
                mr.process(oc, mc, (EP, "Engine Masters", "ep", "ingested"), 600, post=lambda *a: None)
        self.assertFalse([q for q, _ in _writes(mc)])

    def test_kill_switch_unreadable_fails_closed(self):
        with mock.patch.dict(sys.modules, {"nova_autonomy_safety": None}):
            self.assertTrue(mr.kill_switch(_Cur()))


def _wav(tmp):
    w = Path(tmp) / "audio.wav"
    w.write_bytes(b"\0" * (44 + 600 * mr.WAV_BYTES_PER_SEC))
    return w


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mr.selftest(), 0)

    def test_eligibility(self):
        self.assertEqual(mr.eligible(ROOT + "Ripped Movies/A/A.mp4", 10**9), "excluded dir")
        self.assertEqual(mr.eligible(ROOT + "Liked/clip.mp4", 9_999_999), "small")
        self.assertEqual(mr.eligible(ROOT + "Liked/notes.txt", 10**9), "not video")
        self.assertEqual(mr.eligible("/elsewhere/a.mp4", 10**9), "outside VIDEO_ROOT")
        self.assertEqual(mr.eligible(ROOT + "Documentary/Nanook of the North (1922).mkv", 10**9), "silent era")
        self.assertIsNone(mr.eligible(ROOT + "Documentary/Freaks (1932).mkv", 10**9))

    def test_priority_order(self):
        watched = mr.priority(1, 0, 0, 1)
        series = mr.priority(0, 3, 100, 50)
        recall = mr.priority(0, 0, 500, 50)
        count = mr.priority(0, 0, 0, 50)
        self.assertGreater(watched[0], series[0])
        self.assertGreater(series[0], recall[0])
        self.assertGreater(recall[0], count[0])
        self.assertEqual([x[1] for x in (watched, series, recall, count)],
                         ["plex_file", "plex_show", "memory_recall", "episode_count"])

    def test_plan_supersede_keeps_deduplicated_rows_live(self):
        self.assertEqual(mr.plan_supersede(["a", "b"], ["n1", "a"]), ("n1", ["b"]))
        self.assertEqual(mr.plan_supersede([], ["n1"]), ("n1", []))

    def test_deadline_and_eta(self):
        self.assertEqual(mr.batch_deadline(datetime(2026, 10, 16, 4, 0), 7), datetime(2026, 10, 16, 6, 0))
        self.assertEqual(mr.batch_deadline(datetime(2026, 10, 16, 7, 0), 7), datetime(2026, 10, 16, 14, 0))
        self.assertIsNone(mr.eta_days(100, 0, 7))
        self.assertAlmostEqual(mr.eta_days(3600 * 70, 2.0, 7), 5.0)

    def test_source_choice_and_paths(self):
        self.assertEqual(mr.choose_source(None, ["quarantine:x"], "television", "comedy"), "television")
        self.assertEqual(mr.choose_source(None, [], None, "comedy"), "comedy")
        self.assertIsNone(mr.majority([]))
        self.assertEqual(mr.plex_to_local("/elsewhere/x"), None)
        with self.assertRaises(ValueError):
            mr.to_remote("/tmp/x.mp4")
        self.assertIn("nights left", mr.report_line({"done": 1, "total": 2, "eta_days": 3.0}))


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_helpers(self):
        self.assertIn("tv.chunk_text(text)", SRC)                         # loop collapse + junk-share filter
        self.assertIn("nova_skipping_needle.post_remember", SRC)          # sync /remember with retry
        self.assertIn("nova_autonomy_safety.kill_switch_engaged", SRC)
        self.assertNotRegex(SRC, r"def chunk_text|def is_trash_chunk|def post_remember")
        self.assertIn("tv.explicit_vector(show)", SRC)                     # same vector authority as the ingest
        self.assertIn("coalesce(metadata->>'reclass_from', source)", SRC)  # the ingest's source, not a re-route

    def test_queue_and_config_keys(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS media_reingest_queue", mr.SCHEMA)
        oc = _Cur()
        mr.set_cfg(oc, "latest", {"line": "x"})
        self.assertEqual(oc.params[0][:2], ("media_reingest", "latest"))
        oc = _Cur([("FROM service_config", ("false",))])
        self.assertFalse(mr.enabled(oc))
        self.assertTrue(mr.enabled(_Cur()))

    def test_build_queue_filters_and_ranks(self):
        rows = [(EP, "Engine Masters", "ep", "trash", None),
                (ROOT + "TVShows/Engine Masters/Season 01/gone.mp4", "Engine Masters", "gone", "ingested", None),
                (ROOT + "Ripped Movies/A/A.mp4", "Ripped Movies", "A", "ingested", None),
                (ROOT + "Liked/b.mp4", "Liked", "b", "ingested", "reingest-2026-10: local MLX whisper")]
        oc = _Cur([("FROM media_ingest_state", rows), ("RETURNING (xmax = 0)", (True,))])
        mc = _Cur([("sum(access_count)", [("Engine Masters", 52)])])
        files = {EP: 2 * 10**8, ROOT + "Ripped Movies/A/A.mp4": 10**9, ROOT + "Liked/b.mp4": 10**9}
        with mock.patch.object(mr, "ensure_schema"), redirect_stdout(io.StringIO()):
            r = mr.build_queue(oc, mc, files=files, plex={EP: (2, 1440.0)})
        self.assertEqual(r["added"], 1)
        self.assertEqual(r["skipped"], {"missing": 1, "excluded dir": 1, "already reingested": 1})
        ins = next(p for q, p in _writes(oc) if "INSERT INTO media_reingest_queue" in q)
        self.assertEqual(ins[4], "plex_file")
        self.assertEqual(ins[7], 1440.0)


class TestFunctional(unittest.TestCase):
    def _process(self, post):
        oc = _Cur([("source_vector FROM media_ingest_state", ("automotive",))])
        old = [("old-1", "automotive", "tv_transcript", 400, True), ("old-2", "sci_fi", None, 380, None)]
        mc = _Cur([("FROM memories WHERE metadata->>'show'", old)])
        run = _fake_run()
        with mock.patch.object(mr, "local_readable", return_value=True), \
                mock.patch.object(subprocess, "run", run), redirect_stdout(io.StringIO()):
            r = mr.process(oc, mc, (EP, "Engine Masters", "Cheap Carb Showdown", "trash"), 600, post=post)
        return r, oc, mc, run

    def test_golden_path_stores_then_supersedes(self):
        stored = []

        def post(text, source, meta):
            stored.append((text, source, meta))
            return {"id": f"new-{len(stored)}", "status": "stored"}
        with mock.patch.object(mr, "extract_audio", side_effect=lambda p, t: _wav(t)), \
                mock.patch.object(mr, "transcribe_local", return_value=SPEECH):
            r, oc, mc, _ = self._process(post)
        self.assertEqual(r["status"], "done")
        self.assertGreater(len(stored), 0)
        text, source, meta = stored[0]
        self.assertTrue(text.startswith("[Engine Masters] "))
        self.assertEqual(source, "automotive")
        self.assertEqual((meta["pipeline"], meta["chunk"], meta["type"], meta["source_file"]),
                         ("reingest-2026-10", 1, "tv_transcript", EP))
        sup = [p for q, p in _writes(mc) if "superseded_by" in q]
        self.assertEqual(sup, [("new-1", ["old-1", "old-2"])])
        state = [p for q, p in _writes(oc) if "UPDATE media_ingest_state" in q][0]
        self.assertEqual(state[0], len(stored))
        self.assertEqual(r["superseded"], 2)

    def test_empty_transcript_keeps_old_memories(self):
        with mock.patch.object(mr, "extract_audio", side_effect=lambda p, t: _wav(t)), \
                mock.patch.object(mr, "transcribe_local", return_value="♪ ♪ ♪"):
            r, oc, mc, _ = self._process(lambda *a: self.fail("nothing should be stored"))
        self.assertEqual(r["status"], "empty")
        self.assertFalse(_writes(mc))

    def test_scheduled_stands_down_for_the_movie_job_and_the_lock(self):
        oc = _Cur()
        with mock.patch.object(mr, "ensure_schema"), mock.patch.object(mr, "kill_switch", return_value=False), \
                mock.patch.object(mr, "movie_job_running", return_value=True), \
                mock.patch.object(mr, "run_batch") as rb, redirect_stdout(io.StringIO()):
            self.assertEqual(mr.scheduled(oc, _Cur(), 7, None, _movie_running=lambda: False), 0)
        rb.assert_not_called()
        oc = _Cur([("pg_try_advisory_lock", (False,))])
        with mock.patch.object(mr, "ensure_schema"), mock.patch.object(mr, "kill_switch", return_value=False), \
                mock.patch.object(mr, "movie_job_running", return_value=False), \
                mock.patch.object(mr, "run_batch") as rb, redirect_stdout(io.StringIO()):
            mr.scheduled(oc, _Cur(), 7, None, _movie_running=lambda: False)
        rb.assert_not_called()

    def test_error_requeues_then_gives_up(self):
        oc = _Cur([("RETURNING file_path", (EP, "Engine Masters", "ep", "trash")),
                   ("SELECT attempts", (2,))])
        with mock.patch.object(mr, "process", side_effect=RuntimeError("ffmpeg died")), \
                redirect_stdout(io.StringIO()):
            st = mr.run_batch(oc, _Cur(), datetime.now() + timedelta(hours=1), 1)
        self.assertEqual(st["error"], 1)
        upd = [p for q, p in _writes(oc) if q.startswith("UPDATE media_reingest_queue SET status = %s")]
        self.assertEqual(upd[-1][:3], ("error", 3, "ffmpeg died"))

    def test_movie_job_detected_by_cwd(self):
        def run(cmd, **kw):
            if cmd[0].endswith("ps"):
                return subprocess.CompletedProcess(cmd, 0, stdout="  101 /usr/bin/Python -I run.py\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="p101\nfcwd\nn/tmp/x/movie-ingest\n", stderr="")
        self.assertTrue(mr.movie_job_running(_run=run))
        self.assertFalse(mr.movie_job_running(
            _run=lambda c, **k: subprocess.CompletedProcess(c, 0, stdout="  9 /bin/zsh\n", stderr="")))


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for flag in ("--selftest", "--help"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True,
                               timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(mr.main))


if __name__ == "__main__":
    unittest.main()



class TestWaitsForMovieJob(unittest.TestCase):
    def test_waits_inside_window_then_proceeds(self):
        calls = {"n": 0}
        def running():
            calls["n"] += 1
            return calls["n"] < 3          # running twice, then gone
        slept = []
        with mock.patch.object(mr, "guards", return_value="stop here"), mock.patch.object(mr, "ensure_schema"):
            mr.scheduled(mock.Mock(), mock.Mock(), 7, None, _sleep=slept.append, _movie_running=running)
        self.assertEqual(slept, [300, 300])


class TestTranscribeRemote(unittest.TestCase):
    """2026-10-16: minion whisper — copy the WAV, run the minion script, read stdout, always clean up."""

    def _run(self, fail_at=None, out=b"  hello world  "):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if fail_at and fail_at in " ".join(cmd):
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
        return calls, run

    def test_copies_runs_reads_and_cleans(self):
        calls, run = self._run()
        txt = mr.transcribe_remote("nova-core9", Path("/tmp/a.wav"), 600, _run=run)
        self.assertEqual(txt, "hello world")
        self.assertEqual(calls[0][0], "scp")
        self.assertTrue(calls[0][-1].startswith("nova-core9:.nova-minion/reingest-"))
        name = calls[0][-1].rsplit("/", 1)[1]
        self.assertIn(f"nova_minion_whisper.py {name}", calls[1][-1])
        self.assertIn(f"-- {name}", calls[2][-1])                 # the WAV is removed on the minion

    def test_cleans_up_when_whisper_fails(self):
        calls, run = self._run(fail_at="nova_minion_whisper.py")
        with self.assertRaises(subprocess.CalledProcessError):
            mr.transcribe_remote("nova-core6", Path("/tmp/a.wav"), 600, _run=run)
        self.assertEqual(len(calls), 3)                           # scp, whisper (failed), cleanup still ran


# ---------------------------------------------------------------- 2026-10-18: continuous, media-host extraction
class _Popen:
    """subprocess.Popen stand-in for stream_pcm: records argv, yields fixed PCM bytes, given return codes."""
    made = []

    def __init__(self, cmd, stdout=None, stdin=None, stderr=None, rc=0, data=b"\1\0" * 16000):
        self.cmd, self.rc = cmd, rc
        self.stdout = io.BytesIO(data) if stdout is not None else None
        self.stderr = io.BytesIO(b"") if stderr is not None else None
        _Popen.made.append(self)

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        pass


class TestMinionsSecurity(unittest.TestCase):
    def test_extract_cmd_quotes_the_path_and_runs_at_idle_priority(self):
        nasty = ROOT + "TVShows/x/a'; touch /tmp/pwned #.mp4"
        cmd = mr.extract_cmd(nasty)
        self.assertIn("'\"'\"'", cmd)                              # the quote is escaped, not closed
        self.assertTrue(cmd.startswith("nice -n 19 ionice -c3 ffmpeg"))
        self.assertTrue(cmd.endswith("-f s16le -"))

    def test_minion_cleanup_only_removes_its_own_clip(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"text": "hi", "audio_s": 3.0}', stderr=b"")
        fetch = mr.minion_fetch("nova-core3", "kochj@192.168.1.5", 12, "Linux", _run=run, _stream=lambda *a: None)
        self.assertEqual(fetch(EP, 600), ("hi", 3.0))
        self.assertIn("--threads 12", calls[0][-1])
        self.assertRegex(calls[-1][-1], r"/bin/rm -f -- reingest-[0-9a-f]{12}\.pcm$")


class TestMinionsPerformance(unittest.TestCase):
    def test_plan_workers_scales(self):
        rows = [(f"n{i}", f"kochj@10.0.{i // 250}.{i % 250}", 2, 8, "Linux") for i in range(5000)]
        t0 = time.monotonic()
        plan = mr.plan_workers(rows, {f"n{i}": {0} for i in range(0, 5000, 2)})
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(len(plan), 2500 * 1 + 2500 * 2)


class TestMinionsRetry(unittest.TestCase):
    def test_clip_removed_when_whisper_fails(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if "nova_minion_whisper.py" in cmd[-1]:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        fetch = mr.minion_fetch("nova-core2", "kochj@192.168.1.86", 0, "Linux", _run=run, _stream=lambda *a: None)
        with self.assertRaises(subprocess.CalledProcessError):
            fetch(EP, 600)
        self.assertIn("/bin/rm -f --", calls[-1][-1])

    def test_failed_extraction_raises_with_reason(self):
        _Popen.made = []

        def popen(cmd, **kw):
            return _Popen(cmd, rc=1 if "ffmpeg" in cmd[-1] else 0, **kw)
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
            mr.stream_pcm(EP, None, os.path.join(d, "x.pcm"), 60, _popen=popen)


class TestMinionsUnit(unittest.TestCase):
    def test_studio_load_and_budget(self):
        cpu, mem = mr.studio_load("  10.0\n 290.5\n abc\n 0.0\n", 4, "System-wide memory free percentage: 87%")
        self.assertAlmostEqual(cpu, 0.751, places=3)
        self.assertEqual(mem, 0.87)
        self.assertTrue(mr.over_budget(cpu, mem))                 # 75.1% CPU > 75%
        self.assertFalse(mr.over_budget(0.5, 0.30))
        self.assertTrue(mr.over_budget(0.5, 0.20))                # under 25% free memory
        self.assertEqual(mr.studio_load("", 8, "")[1], 1.0)       # unreadable pressure never blocks

    def test_plan_workers_respects_alive_slots_and_studio_cap(self):
        rows = [("mac-studio", None, 2, 0, "Darwin"), ("nova-core9", "kochj@192.168.1.7", 2, 0, "Darwin")]
        plan = mr.plan_workers(rows, {"nova-core9": {0}}, studio_n=lambda w: w - 1)
        self.assertEqual(plan, [("mac-studio", None, 0, "Darwin", 0), ("nova-core9", "kochj@192.168.1.7", 0, "Darwin", 1)])

    def test_whisper_minions_maps_hosts(self):
        cur = _Cur([("FROM minion_nodes", [("mac-studio", "192.168.1.6", 2, 0, "Darwin", 1, 10),
                                           ("nova-core3", "192.168.1.5", 2, 12, "Linux", 0, 0), ("x", None, 1, 0, "", 0, 0)])])
        rows = mr.whisper_minions(cur)
        self.assertEqual([r[1] for r in rows], [None, "kochj@192.168.1.5", "x"])
        self.assertEqual(rows[0][5:], (1, 10))
        self.assertIn("exclude_nodes", cur.sql[-1])                # a node can be benched without touching it
        self.assertEqual(cur.params[-1][1], mr.SERVICE)

    def test_cpu_slots_beside_gpu_slots(self):
        rows = [("nova-core10", "kochj@192.168.1.77", 2, 0, "Darwin", 1, 8)]
        plan = mr.plan_workers(rows, {"nova-core10": {0, 1}})
        self.assertEqual(plan, [("nova-core10", "kochj@192.168.1.77", 8, "Darwin", mr.CPU_SLOT0)])
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"text": "x", "audio_s": 1.0}', stderr=b"")
        mr.minion_fetch("nova-core10", "kochj@192.168.1.77", 8, "Darwin", _run=run, _stream=lambda *a: None,
                        cpu=True)(EP, 60)
        self.assertIn("--json --cpu --threads 8", calls[0][-1])
        self.assertIn("nice -n 15", calls[0][-1])


class TestMinionsIntegration(unittest.TestCase):
    def test_process_with_fetch_stores_and_supersedes(self):
        oc = _Cur([("source_vector FROM media_ingest_state", ("automotive",))])
        mc = _Cur([("FROM memories WHERE metadata->>'show'", [("old-1", "automotive", "tv_transcript", 400, True)])])
        stored = []

        def post(text, source, meta):
            stored.append(meta)
            return {"id": f"new-{len(stored)}", "status": "stored"}
        with mock.patch.object(mr, "extract_audio", side_effect=AssertionError("the Studio must not extract")), \
                redirect_stdout(io.StringIO()):
            r = mr.process(oc, mc, (EP, "Engine Masters", "Cheap Carb Showdown", "trash"), 600, post=post,
                           fetch=lambda p, t: (SPEECH, 1800.0))
        self.assertEqual((r["status"], r["audio_s"], r["superseded"]), ("done", 1800.0, 1))
        state = [p for q, p in _writes(oc) if "UPDATE media_ingest_state" in q][0]
        self.assertIn("minion whisper", state[3])

    def test_stream_pcm_pipes_media_host_into_the_minion(self):
        _Popen.made = []
        mr.stream_pcm(EP, "kochj@192.168.1.5", "reingest-abc.pcm", 60, _popen=lambda cmd, **kw: _Popen(cmd, **kw))
        src, dst = _Popen.made
        self.assertEqual(src.cmd[-2], mr.MEDIA_HOST)
        self.assertEqual(dst.cmd[-2], "kochj@192.168.1.5")
        self.assertEqual(dst.cmd[-1], "cat > .nova-minion/reingest-abc.pcm")


class TestMinionsFunctional(unittest.TestCase):
    def test_studio_local_path_wraps_pcm_and_uses_local_whisper(self):
        def stream(path, host, dest, timeout):
            self.assertIsNone(host)
            Path(dest).write_bytes(b"\0\0" * 16000 * 3)
        with mock.patch.object(mr, "transcribe_local", return_value="local text") as tl:
            text, audio_s = mr.minion_fetch("mac-studio", None, _stream=stream)(EP, 60)
        self.assertEqual((text, audio_s), ("local text", 3.0))
        self.assertTrue(tl.call_args[0][0].name.endswith(".wav"))

    def test_supervisor_starts_each_slot_and_stops_when_queue_is_empty(self):
        started = []
        pend = iter([(1,), None])
        cur = _Cur([("status = 'pending' LIMIT 1", lambda c: next(pend))])
        conn = mock.Mock()
        conn.cursor.return_value = cur
        rows = [("nova-core3", "kochj@192.168.1.5", 2, 12, "Linux"), ("nova-core4", "kochj@192.168.1.250", 1, 6, "Linux")]
        with mock.patch.object(mr, "connect", return_value=conn), mock.patch.object(mr, "guards", return_value=None), \
                mock.patch.object(mr, "revert_stale"), mock.patch.object(mr, "whisper_minions", return_value=rows), \
                mock.patch.object(mr, "minion_worker", side_effect=lambda *a: started.append((a[0], a[-1]))), \
                redirect_stdout(io.StringIO()):
            mr.run_minions(5, _sleep=lambda s: None)
        self.assertEqual(sorted(started), [("nova-core3", 0), ("nova-core3", 1), ("nova-core4", 0)])


class TestMinionsFrame(unittest.TestCase):
    def test_studio_heavy_work_runs_at_utility_qos(self):
        if sys.platform == "darwin":
            self.assertEqual(mr.QOS[:3], ["/usr/sbin/taskpolicy", "-c", "utility"])   # not -b: 6x slower Whisper
        self.assertIn("*QOS, WHISPER_BIN", SRC)
        self.assertIn("*QOS, FFMPEG_BIN", SRC)
        self.assertEqual(mr.STUDIO_BUDGET, 0.75)

    def test_scheduler_runs_the_dispatchers_back_to_back(self):
        yml = SCRIPTS.parent / "config" / "scheduler.yaml"
        if not yml.exists():
            self.skipTest("no scheduler.yaml next to scripts")
        txt = yml.read_text()
        for task in ("media_reingest_minions", "work_lessons"):
            block = txt.split(f"\n  {task}:", 1)[1][:900]
            self.assertRegex(block, r"schedule: every [15]m")
            self.assertIn("overlap: skip", block)


class TestInterruptedAttempt(unittest.TestCase):
    """2026-10-18: chunks an interrupted attempt stored (pipeline already reingest) are retired on the retry."""

    def test_old_memories_includes_own_pipeline_rows_as_legacy(self):
        mc = _Cur([("FROM memories", [])])
        mr.old_memories(mc, "Engine Masters", EP)
        sql, params = mc.sql[-1], mc.params[-1]
        self.assertNotIn("<> %s AND (metadata->>'source_file'", sql)
        self.assertIn("metadata->>'source_file' = %s AND coalesce(metadata->>'pipeline', '') <> %s", sql)
        self.assertEqual(params[:3], (EP, mr.PIPELINE, "Engine Masters"))
