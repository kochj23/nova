#!/usr/bin/env python3
"""nova_hear.py + nova_hear_listen.py — 7 house categories (Security, Performance, Retry, Unit, Integration,
Functional, Frame). YouTube, the audio analysis, the writer and the Qwen3-Omni model are always mocked: nothing is
downloaded, nothing loads MLX, nothing posts. Written by Jordan Koch (via Claude)."""
import json
import re
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_hear as h  # noqa: E402
import nova_hear_listen as hl  # noqa: E402

URL = "https://www.youtube.com/watch?v=vZexE7wAvBE"
FAKE = {"duration_s": 254.9, "tempo": {"bpm": 86.1, "half": 43.1, "double": 172.3, "candidates": [86.1, 172.3]},
        "key": {"key": "D# major", "runner_up": "D# minor", "margin": 0.016, "correlation": 0.39},
        "loudness": {"integrated_lufs": -7.8}, "sections": [],
        "clap": {"speech_fraction": 0.0, "vocals": [{"label": "rap vocals", "prob": 0.95}], "genre": []}}
NOTE = "Loud, steady and euphoric with a dense mix. " * 6
QUIET = lambda m: None  # noqa: E731


def fake_download(cmd, **k):
    out = re.sub(r"%\(ext\)s$", "wav", cmd[cmd.index("-o") + 1])
    Path(out).write_bytes(b"RIFF")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def search_result(n_noise=0):
    rows = [{"id": f"n{i}", "title": f"Hide U reaction {i}", "channel": "Random Fan", "duration": 250.0}
            for i in range(n_noise)]
    rows.append({"id": "vZexE7wAvBE", "title": "Kosheen - Hide U (Official Video)", "channel": "KosheenAtMoksha",
                 "duration": 255.0})
    return rows


class TestSecurity(unittest.TestCase):
    def test_listen_service_accepts_only_youtube_watch_urls(self):
        for bad in ("https://evil.example/watch?v=abcdefg", "file:///etc/passwd", "https://www.youtube.com/watch?v=abc",
                    "https://www.youtube.com/watch?v=abcdefg; rm -rf /", "https://www.youtube.com/watch?v=abcdefg&list=x",
                    "http://youtu.be/abcdefg"):
            self.assertIsNone(hl.YT_RE.match(bad), bad)
        self.assertTrue(hl.YT_RE.match(URL) and hl.YT_RE.match("https://youtu.be/vZexE7wAvBE"))

    def test_unlisted_caller_is_refused_and_allowlist_uses_dns_names(self):
        self.assertTrue(all(n.endswith(".digitalnoise.net") or n == "localhost" for n in hl.ALLOWED_HOSTS))
        with patch.object(hl.socket, "getaddrinfo", side_effect=OSError), patch.dict(hl._allow, {"t": 0, "ips": set()}):
            self.assertFalse(hl.allowed("192.168.1.86"))
            self.assertTrue(hl.allowed("127.0.0.1"))

    def test_no_hardcoded_ips_or_shell_strings(self):
        for src in ((SCRIPTS / "nova_hear.py").read_text(), (SCRIPTS / "nova_hear_listen.py").read_text()):
            self.assertNotRegex(src, r"\b192\.168\.\d+\.\d+\b")
            self.assertNotIn("shell=True", src)

    def test_writer_never_receives_vocal_type_labels(self):
        self.assertNotIn("vocals", h._for_writer(FAKE)["clap"])
        self.assertIn("vocals", FAKE["clap"])                     # input left untouched (deep copy)


class TestPerformance(unittest.TestCase):
    def test_find_official_scans_1000_results_fast(self):
        t = time.perf_counter()
        got = h.find_official("Kosheen", "Hide U", QUIET, search=lambda q: search_result(1000))
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertEqual(got, URL)

    def test_majority_and_clip_starts_are_cheap(self):
        votes = [{"lead_vocal": True, "gender": "female", "style": "melodic singing"}] * 5000
        t = time.perf_counter()
        for d in range(31, 3000, 7):
            hl.clip_starts(float(d))
        self.assertEqual(hl.majority(votes)["gender"], "female")
        self.assertLess(time.perf_counter() - t, 0.5)


class TestRetry(unittest.TestCase):
    def test_download_retried_three_times_then_note_skipped(self):
        calls = []

        def no_file(cmd, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP Error 403")
        logs = []
        with patch.object(h.subprocess, "run", side_effect=no_file), patch("time.sleep"):
            self.assertIsNone(h.describe_track(URL, log=logs.append))
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("no audio" in m for m in logs))

    def test_listen_service_timeout_or_refusal_returns_none(self):
        for exc in (urllib.error.URLError("refused"), TimeoutError("slow")):
            with patch.object(urllib.request, "urlopen", side_effect=exc):
                self.assertIsNone(h.vocal_guess(URL, log=QUIET, timeout=1))

    def test_busy_service_answers_503_instead_of_queueing(self):
        srv, port = _serve()
        try:
            with patch.object(hl, "allowed", return_value=True):
                hl.LOCK.acquire()
                try:
                    code, body = _post(port, {"url": URL})
                finally:
                    hl.LOCK.release()
            self.assertEqual((code, body["error"]), (503, "busy"))
        finally:
            srv.shutdown()


class TestUnit(unittest.TestCase):
    def test_vocal_line_variants(self):
        self.assertEqual(h.vocal_line(None), "")
        self.assertIn("no lead vocal", h.vocal_line({"lead_vocal": False}))
        self.assertEqual(h.vocal_line({"lead_vocal": True, "gender": "unsure", "style": "unsure"}), "")
        self.assertIn("female, melodic singing", h.vocal_line({"lead_vocal": True, "gender": "female",
                                                               "style": "melodic singing"}))

    def test_parse_answer_rejects_garbage_and_unknown_values(self):
        self.assertIsNone(hl.parse_answer("no json here"))
        self.assertIsNone(hl.parse_answer("{not: json}"))
        a = hl.parse_answer('{"lead_vocal": "yes", "gender": "robot", "style": "yodel"}')
        self.assertEqual(a, {"lead_vocal": None, "gender": "unsure", "style": "unsure"})

    def test_majority_tie_is_unsure(self):
        v = [{"lead_vocal": True, "gender": "female", "style": "rapping"},
             {"lead_vocal": True, "gender": "male", "style": "melodic singing"}]
        m = hl.majority(v)
        self.assertEqual((m["gender"], m["style"], m["lead_vocal"]), ("unsure", "unsure", True))

    def test_clip_starts_never_run_past_the_end(self):
        self.assertEqual(hl.clip_starts(10), [0.0])
        for d in (31, 60, 255, 900):
            self.assertTrue(all(0 <= s and s + hl.CLIP_S <= d for s in hl.clip_starts(d)), d)

    def test_weak_key_becomes_ambiguous_strong_key_kept(self):
        self.assertEqual(h._for_writer(FAKE)["key"]["key"], "ambiguous")
        strong = json.loads(json.dumps(FAKE)); strong["key"]["margin"] = 0.2
        self.assertEqual(h._for_writer(strong)["key"]["key"], "D# major")


class TestIntegration(unittest.TestCase):
    def test_listen_endpoint_round_trip_over_http(self):
        srv, port = _serve()
        res = {"lead_vocal": True, "gender": "female", "style": "melodic singing", "votes": [], "seconds": 1.0,
               "peak_gb": 20.7}
        try:
            with patch.object(hl, "allowed", return_value=True), patch.object(hl, "listen", return_value=res):
                code, body = _post(port, {"url": URL})
                bad, _ = _post(port, {"url": "https://evil.example/x"})
            self.assertEqual((code, body["gender"]), (200, "female"))
            self.assertEqual(bad, 400)
            with patch.object(hl, "allowed", return_value=False):
                self.assertEqual(_post(port, {"url": URL})[0], 403)
        finally:
            srv.shutdown()

    def test_reaction_swapped_for_official_upload_through_add_sound_note(self):
        reaction = "https://www.youtube.com/watch?v=SjS3miaTvlE"
        talky = json.loads(json.dumps(FAKE)); talky["clap"]["speech_fraction"] = 0.56
        block = f"Track of the Day: Kosheen — Hide U. [Watch]({reaction})"
        with patch.object(h.subprocess, "run", side_effect=fake_download), \
             patch.object(h, "hear", side_effect=[talky, FAKE]), patch.object(h, "vocal_guess", return_value=None), \
             patch.object(h, "_yt_search", return_value=search_result(3)), \
             patch("nova_journal.call_openrouter",
                   side_effect=lambda s, u, **k: '{"artist": "Kosheen", "title": "Hide U"}' if "ONE song" in s else NOTE):
            out = h.add_sound_note(block, [{"url": reaction}], log=QUIET)
        self.assertIn("[Listen on YouTube](https://www.youtube.com/watch?v=vZexE7wAvBE)", out)
        self.assertTrue(out.startswith(block))


class TestFunctional(unittest.TestCase):
    def test_block_without_a_video_link_is_returned_unchanged(self):
        self.assertEqual(h.add_sound_note("Track of the Day: something", [{"url": URL}], log=QUIET),
                         "Track of the Day: something")

    def test_note_failure_never_breaks_the_edition(self):
        with patch.object(h, "describe_track", side_effect=RuntimeError("boom")):
            self.assertEqual(h.add_sound_note(f"see {URL}", [{"url": URL}], log=QUIET), f"see {URL}")

    def test_full_note_shape(self):
        with patch.object(h.subprocess, "run", side_effect=fake_download), patch.object(h, "hear", return_value=FAKE), \
             patch.object(h, "vocal_guess", return_value={"lead_vocal": True, "gender": "female",
                                                          "style": "melodic singing"}), \
             patch("nova_journal.call_openrouter", return_value=NOTE):
            note = h.describe_track(URL, log=QUIET)
        self.assertTrue(note.startswith("**🎧 What it sounds like:**"))
        self.assertIn("a listening model, not measured", note)
        self.assertTrue(note.endswith(f"[Listen on YouTube]({URL})"))


class TestFrame(unittest.TestCase):
    def test_listen_selftest_passes(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_hear_listen.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])

    def test_hear_cli_without_args_errors_cleanly(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_hear.py")], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--selftest", p.stderr)

    def test_imports_never_start_a_server_or_load_models(self):
        self.assertNotIn("mlx", sys.modules.get("nova_hear_listen").__dict__)
        self.assertFalse(hl.LOCK.locked())


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hl.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _post(port, obj):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/vocals", data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


if __name__ == "__main__":
    unittest.main()
