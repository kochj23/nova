#!/usr/bin/env python3
"""Tests for nova_broadcastify_calls.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import base64
import hashlib
import hmac
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_broadcastify_calls.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="bcfy-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sec = types.ModuleType("nova_secrets")
    sec.get_secret = MagicMock(side_effect=lambda n: {"nova-broadcastify-calls-kid": "KID1",
                                                      "nova-broadcastify-calls-secret": "s3cr3t-from-vault",
                                                      "nova-broadcastify-calls-iss": "iss-1"}[n])
    corr = types.ModuleType("nova_scanner_correct"); corr.correct = MagicMock(side_effect=lambda t, d: (t, None))
    with patch.dict(sys.modules, {"nova_secrets": sec, "nova_scanner_correct": corr}):   # bound at import; restored after
        spec.loader.exec_module(mod)
    return mod


bc = _load("bcfy_under_test", SCRIPT)
CORRECT = bc.correct
bc.STATE = TMP / "state" / "bcfy_calls_pos.json"          # never touch ~/.openclaw/state


class _Stop(Exception):
    """Raised from the mocked sleep to leave main()'s forever loop after one pass."""


def _resp(data):
    r = MagicMock(); r.read.return_value = data if isinstance(data, bytes) else json.dumps(data).encode()
    return r


def _run(calls_by_group, audio=b"x" * 5000, transcribe="unit 12 code 3 at Glenoaks and Olive", archives_err=None, pos=None,
         transcribe_exc=None):
    """One pass of main() with the API, audio fetch, whisper, correction, memory and sleep mocked."""
    bc.STATE.parent.mkdir(parents=True, exist_ok=True)
    if pos is not None:
        bc.STATE.write_text(json.dumps(pos))
    elif bc.STATE.exists():
        bc.STATE.unlink()
    remembered, fetched = [], []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.startswith(bc.MEM):
            remembered.append(json.loads(req.data)); return _resp({"ok": True})
        if "/group_archives/" in url:
            gid = url.split("/group_archives/")[1].split("/")[0]
            if archives_err and gid in archives_err:
                raise archives_err[gid]
            return _resp({"calls": calls_by_group.get(gid, [])})
        fetched.append(url); return _resp(audio)
    sleep = MagicMock(side_effect=_Stop())
    tr = MagicMock(return_value=transcribe, side_effect=transcribe_exc)
    with patch.object(bc.urllib.request, "urlopen", urlopen), patch.object(bc.time, "sleep", sleep), \
         patch.object(bc, "_transcribe", tr), redirect_stdout(io.StringIO()) as out:
        with self_assert_stop():
            bc.main()
    return remembered, fetched, tr, out.getvalue(), sleep


class self_assert_stop:
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError("main() returned instead of looping")
        return et is _Stop


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_secrets_come_from_the_fleet_store(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for n in ("nova-broadcastify-calls-kid", "nova-broadcastify-calls-secret", "nova-broadcastify-calls-iss"):
            self.assertIn(f'get_secret("{n}")', SRC)
        self.assertNotIn("s3cr3t-from-vault", SRC)

    def test_jwt_is_a_valid_hs256_signature_over_header_and_payload(self):
        bc._creds = None; bc.nova_secrets.get_secret.reset_mock()
        tok = bc._jwt()
        h, p, s = tok.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(h))), {"alg": "HS256", "typ": "JWT", "kid": "KID1"})
        payload = json.loads(base64.urlsafe_b64decode(pad(p)))
        self.assertEqual(payload["iss"], "iss-1"); self.assertEqual(payload["exp"] - payload["iat"], 300)
        want = base64.urlsafe_b64encode(hmac.new(b"s3cr3t-from-vault", f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=")
        self.assertEqual(s, want.decode())
        self.assertEqual(bc.nova_secrets.get_secret.call_count, 3)     # credentials are cached after one lookup
        bc._jwt(); self.assertEqual(bc.nova_secrets.get_secret.call_count, 3)

    def test_no_shell_and_state_stays_under_openclaw(self):
        self.assertNotIn("shell=True", SRC); self.assertNotIn("os.system", SRC)
        self.assertIn('Path.home() / ".openclaw/state/bcfy_calls_pos.json"', SRC)


class TestPerformance(unittest.TestCase):
    def test_jwt_minting_10k_under_1s(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            bc._jwt()
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_one_pass_over_10k_tiny_calls_under_2s(self):
        now = int(time.time())
        calls = [{"ts": now - 10_000 + i, "url": f"https://cdn.bcfy.io/{i}.mp3"} for i in range(10_000)]
        t0 = time.perf_counter()
        remembered, fetched, tr, out, _ = _run({"7095-2101": calls}, audio=b"tiny")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(fetched), 10_000); tr.assert_not_called()  # <4000 bytes: fetched, never transcribed
        self.assertEqual(json.loads(bc.STATE.read_text())["7095-2101"], now - 1)


class TestRetry(unittest.TestCase):
    def test_archives_error_is_one_shot_and_the_loop_continues(self):
        # RETRY GAP: _archives()/urllib.request.urlopen — one attempt per poll; the group is skipped (cursor untouched)
        # and every other group still runs, so one bad group never stalls the ingest.
        now = int(time.time())
        remembered, fetched, tr, out, _ = _run({"7095-2161": [{"ts": now - 5, "url": "https://cdn/a.mp3"}]},
                                               archives_err={"7095-2101": RuntimeError("HTTP 500")}, pos={"7095-2101": now - 100})
        self.assertIn("[bcfy-calls] 7095-2101 archives error: HTTP 500", out)
        pos = json.loads(bc.STATE.read_text())
        self.assertEqual(pos["7095-2101"], now - 100)                   # not advanced, not widened
        self.assertEqual(pos["7095-2161"], now - 5); self.assertEqual(len(remembered), 1)

    def test_per_call_audio_failure_is_swallowed_and_cursor_still_advances(self):
        # RETRY GAP: main()/audio urlopen + _transcribe — one attempt per call; the error is logged, the call skipped
        now = int(time.time())
        remembered, fetched, tr, out, _ = _run({"7095-2101": [{"ts": now - 5, "url": "https://cdn/a.m4a"}]},
                                               transcribe_exc=RuntimeError("whisper died"))
        self.assertEqual(tr.call_count, 1)
        self.assertEqual(remembered, [])
        self.assertIn(f"[bcfy-calls] 7095-2101 call {now - 5} err: whisper died", out)
        self.assertEqual(json.loads(bc.STATE.read_text())["7095-2101"], now - 5)

    def test_remember_is_best_effort(self):
        # RETRY GAP: _remember()/memory server — one attempt, any failure is swallowed
        with patch.object(bc.urllib.request, "urlopen", side_effect=OSError("memory down")) as u:
            bc._remember("t", "fire", "L", "7095-2101", 1)
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_pos_roundtrip_and_missing_state(self):
        if bc.STATE.exists():
            bc.STATE.unlink()
        self.assertEqual(bc._load_pos(), {})
        bc._save_pos({"g": 5})
        self.assertEqual(bc._load_pos(), {"g": 5})
        self.assertTrue(str(bc.STATE).startswith(str(TMP)))

    def test_groups_map_to_blotter_sources(self):
        self.assertEqual(len(bc.GROUPS), 7)
        for gid, (source, label) in bc.GROUPS.items():
            self.assertIn(source, ("scanner", "fire", "rail", "chp"))
            self.assertTrue(gid.startswith("7095-")); self.assertTrue(label)
        self.assertGreater(bc.MAX_LOOKBACK, bc.POLL_SECS)

    def test_stale_cursor_is_clamped_to_max_lookback(self):
        now = int(time.time())
        seen = {}

        def urlopen(req, timeout=None):
            url = req.full_url
            if "/group_archives/" in url:
                gid, start, end = url.split("/group_archives/")[1].split("/")
                seen[gid] = (int(start), int(end))
            return _resp({"calls": []})
        pos = {g: now - 12 * 86400 for g in bc.GROUPS}
        bc.STATE.parent.mkdir(parents=True, exist_ok=True); bc.STATE.write_text(json.dumps(pos))
        with patch.object(bc.urllib.request, "urlopen", urlopen), patch.object(bc.time, "sleep", MagicMock(side_effect=_Stop())), \
             redirect_stdout(io.StringIO()) as out:
            with self_assert_stop():
                bc.main()
        for gid, (start, end) in seen.items():
            self.assertGreaterEqual(start, end - bc.MAX_LOOKBACK)
        self.assertIn("cursor 12.0d stale — clamping", out.getvalue())
        self.assertIn("SKIPPED, not recoverable", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_correction_pass_is_the_shared_scanner_corrector(self):
        self.assertIn("from nova_scanner_correct import correct", SRC)
        self.assertNotIn("def correct(", SRC)                             # one corrector, shared with the scanner ingest
        import nova_scanner_correct as sc
        self.assertTrue(callable(sc.correct))
        self.assertIs(bc.correct, CORRECT)                               # the name bound at import is what main() calls

    def test_remember_posts_the_blotter_shape_to_the_memory_server(self):
        with patch.object(bc.urllib.request, "urlopen") as u:
            bc._remember("engine 12 responding", "fire", "Verdugo Fire — Red-1 Dispatch", "7095-2101", 1700000000, correction_confidence=0.9)
        req = u.call_args[0][0]
        self.assertEqual(req.full_url, bc.MEM + "?async=1")
        body = json.loads(req.data)
        self.assertEqual(body["text"], "[Verdugo Fire — Red-1 Dispatch] engine 12 responding")
        self.assertEqual(body["source"], "fire")
        meta = body["metadata"]
        self.assertEqual(meta["source_feed"], "bcfy-calls/7095-2101"); self.assertEqual(meta["call_ts"], 1700000000)
        self.assertTrue(meta["corrected"]); self.assertEqual(meta["correction_confidence"], 0.9)
        self.assertEqual(meta["receiver"], "broadcastify-calls")


class TestFunctional(unittest.TestCase):
    def test_golden_path_transcribes_corrects_remembers_and_saves_cursor(self):
        now = int(time.time())
        calls = [{"ts": now - 30, "url": "https://cdn.bcfy.io/one.mp3"}, {"ts": now - 20, "url": "https://cdn.bcfy.io/two.m4a"}]
        bc.correct.reset_mock(); bc.correct.side_effect = lambda t, d: (t.replace("glen oaks", "Glenoaks"), 0.8)
        try:
            remembered, fetched, tr, out, sleep = _run({"7095-2161": calls}, transcribe="unit 12 at glen oaks and olive")
        finally:
            bc.correct.side_effect = lambda t, d: (t, None)
        self.assertEqual(fetched, ["https://cdn.bcfy.io/one.mp3", "https://cdn.bcfy.io/two.m4a"])
        self.assertEqual(tr.call_args_list[0][0][1], ".mp3"); self.assertEqual(tr.call_args_list[1][0][1], ".m4a")
        self.assertEqual(bc.correct.call_args[0], ("unit 12 at glen oaks and olive", "scanner"))
        self.assertEqual(len(remembered), 2)
        self.assertEqual(remembered[0]["text"], "[Burbank PD — Dispatch] unit 12 at Glenoaks and olive")
        self.assertEqual(remembered[0]["metadata"]["correction_confidence"], 0.8)
        self.assertEqual(json.loads(bc.STATE.read_text())["7095-2161"], now - 20)
        self.assertIn("Burbank PD — Dispatch (c=0.8) :: unit 12 at Glenoaks and olive", out)
        sleep.assert_called_once_with(bc.POLL_SECS)

    def test_already_seen_calls_are_skipped_and_short_text_is_not_stored(self):
        now = int(time.time())
        calls = [{"ts": now - 50, "url": "https://cdn/old.mp3"}, {"ts": now - 10, "url": "https://cdn/new.mp3"}]
        remembered, fetched, tr, out, _ = _run({"7095-2101": calls}, transcribe="10-4", pos={"7095-2101": now - 50})
        self.assertEqual(fetched, ["https://cdn/new.mp3"])              # ts <= cursor never re-fetched (re-billed)
        self.assertEqual(remembered, [])                                # len("10-4") <= MINLEN
        self.assertEqual(json.loads(bc.STATE.read_text())["7095-2101"], now - 10)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would start the forever poll loop, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_broadcastify_calls"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
