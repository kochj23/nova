#!/usr/bin/env python3
"""Tests for nova_yt_backfill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_yt_backfill.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="yt-backfill-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _stubs():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_FEED = "C_FEED"; cfg.post_both = MagicMock()
    w = types.ModuleType("nova_yt_ingest_watch")
    w.CHANNELS = [{"key": "fishbowl", "url": "https://www.youtube.com/@fishbowl/streams", "vector": "fishbowl"},
                  {"key": "other", "url": "https://www.youtube.com/@other/streams", "vector": "other_vec"}]
    w.recent_ids = MagicMock(return_value=[])
    w.vid_live_status = MagicMock(return_value="was_live")
    w.PY = "/opt/homebrew/bin/python3"; w.CAPTURE = str(SCRIPTS / "nova_yt_capture.py")
    return {"nova_config": cfg, "nova_yt_ingest_watch": w}


def _load(argv):
    spec = importlib.util.spec_from_file_location("yt_backfill_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with _stub_modules(_stubs()), patch.object(sys, "argv", argv):
        spec.loader.exec_module(mod)
    return mod


yb = _load(["nova_yt_backfill.py", "2"])


class _Cur:
    """Answers `SELECT status` with the per-video status map; records every statement."""
    def __init__(self, status):
        self.status = dict(status); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        sql = " ".join(sql.split()); self.sql.append((sql, params))
        if sql.startswith("SELECT status"):
            st = self.status.get((params[0], params[1])); self._last = (st,) if st else None
        elif sql.startswith("INSERT"):
            self.status[(params[0], params[1])] = "capturing"
        elif sql.startswith("UPDATE"):
            self.status[(params[0], params[1])] = "failed"

    def fetchone(self):
        return self._last

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(vids, status=None, live="was_live", capture=None, only=None, n=2):
    cur = _Cur(status or {})
    yb.recent_ids = MagicMock(return_value=vids)
    yb.vid_live_status = MagicMock(return_value=live)
    yb.nova_config.post_both = MagicMock()

    def _cap(args, **kw):
        ch = next(c["key"] for c in yb.CHANNELS if c["vector"] == args[3])
        cur.status[(ch, args[2])] = "ingested"
        return types.SimpleNamespace(returncode=0)
    with patch.object(yb.psycopg2, "connect", return_value=_Conn(cur)), patch.object(yb, "ONLY", only), \
         patch.object(yb, "N", n), patch.object(yb.subprocess, "run", capture or MagicMock(side_effect=_cap)) as sp, \
         redirect_stdout(io.StringIO()) as out:
        yb.main()
    return cur, sp, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_sql_is_parameterized_and_titles_truncated(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        evil = "t'); DROP TABLE yt_ingest_seen; --" + "x" * 300
        cur, sp, _ = _run([("vid1", 0, evil)])
        sql, params = cur.ran("INSERT INTO yt_ingest_seen")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[2], evil[:200])

    def test_capture_runs_as_argv_with_the_video_id_as_one_token(self):
        cur, sp, _ = _run([("v; rm -rf /", 0, "t")], only="fishbowl")
        argv = sp.call_args[0][0]
        self.assertEqual(argv, [yb.PY, yb.CAPTURE, "v; rm -rf /", "fishbowl"])


class TestPerformance(unittest.TestCase):
    def test_10k_already_ingested_candidates_are_skipped_fast(self):
        vids = [(f"v{i}", 0, f"t{i}") for i in range(10_000)]
        status = {("fishbowl", f"v{i}"): "ingested" for i in range(10_000)}
        t0 = time.perf_counter()
        cur, sp, out = _run(vids, status=status, n=10_000, only="fishbowl")
        self.assertLess(time.perf_counter() - t0, 3.0)
        sp.assert_not_called()
        self.assertIn("10000 already had", yb.nova_config.post_both.call_args[0][0])


class TestRetry(unittest.TestCase):
    def test_capture_failure_marks_the_row_failed_and_continues(self):
        # RETRY GAP: subprocess.run(capture) — one attempt per video; an exception marks status='failed'
        cur, sp, out = _run([("v1", 0, "a"), ("v2", 0, "b")],
                            capture=MagicMock(side_effect=[subprocess.TimeoutExpired("c", 1), types.SimpleNamespace(returncode=0)]),
                            only="fishbowl")
        self.assertEqual(sp.call_count, 2)
        self.assertEqual(cur.ran("SET status='failed'")[0][1], ("fishbowl", "v1"))
        self.assertIn("capture failed v1", out)
        self.assertIn("2 failed/empty", yb.nova_config.post_both.call_args[0][0])   # v1 raised, v2 never reached "ingested"

    def test_slack_failure_never_breaks_the_backfill(self):
        # RETRY GAP: slack()/post_both — single call, exceptions swallowed and logged
        cur = _Cur({})
        yb.recent_ids = MagicMock(return_value=[])
        yb.nova_config.post_both = MagicMock(side_effect=RuntimeError("slack down"))
        with patch.object(yb.psycopg2, "connect", return_value=_Conn(cur)), patch.object(yb, "ONLY", None), \
             redirect_stdout(io.StringIO()) as out:
            yb.main()
        self.assertEqual(yb.nova_config.post_both.call_count, 2)
        self.assertIn("slack: slack down", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_argv_parsing_count_and_channel_filter(self):
        self.assertEqual(yb.N, 2); self.assertIsNone(yb.ONLY)
        other = _load(["nova_yt_backfill.py", "7", "fishbowl"])
        self.assertEqual((other.N, other.ONLY), (7, "fishbowl"))
        default = _load(["nova_yt_backfill.py"])
        self.assertEqual((default.N, default.ONLY), (10, None))

    def test_slack_routes_to_the_feed_without_discord(self):
        yb.nova_config.post_both = MagicMock()
        yb.slack("hi")
        yb.nova_config.post_both.assert_called_once_with("hi", slack_channel="C_FEED", discord_channel=None)

    def test_log_prefix(self):
        with redirect_stdout(io.StringIO()) as out:
            yb.log("x")
        self.assertEqual(out.getvalue(), "[yt-backfill] x\n")


class TestIntegration(unittest.TestCase):
    def test_reuses_the_watchers_channel_list_and_helpers(self):
        self.assertIn("from nova_yt_ingest_watch import CHANNELS, recent_ids, vid_live_status, PY, CAPTURE", SRC)
        self.assertNotIn("def recent_ids", SRC); self.assertNotIn("def vid_live_status", SRC)

    def test_upcoming_streams_are_skipped_and_live_ones_get_the_flag(self):
        cur, sp, _ = _run([("up", 0, "t")], live="is_upcoming")
        sp.assert_not_called(); self.assertEqual(cur.ran("INSERT"), [])
        cur, sp, _ = _run([("lv", 0, "t")], live="is_live")
        self.assertEqual(sp.call_args[0][0][-1], "--live")

    def test_channel_filter_limits_work_to_one_key(self):
        cur, sp, _ = _run([("v", 0, "t")], only="other")
        self.assertEqual(sp.call_args[0][0][3], "other_vec")
        self.assertEqual(sp.call_count, 1)


class TestFunctional(unittest.TestCase):
    def test_golden_path_captures_new_and_skips_seen(self):
        cur, sp, out = _run([("new1", 0, "Fresh stream"), ("seen", 0, "Old")],
                            status={("fishbowl", "seen"): "ingested", ("other", "seen"): "ingested"})
        self.assertEqual(sp.call_count, 2)                                    # one per channel (both see the list)
        self.assertEqual(cur.ran("INSERT INTO yt_ingest_seen")[0][1], ("fishbowl", "new1", "Fresh stream"))
        self.assertEqual(sp.call_args_list[0][0][0], [yb.PY, yb.CAPTURE, "new1", "fishbowl"])
        self.assertEqual(sp.call_args_list[0][1]["timeout"], 12 * 3600)
        msgs = [c[0][0] for c in yb.nova_config.post_both.call_args_list]
        self.assertTrue(msgs[0].startswith(":rewind: *Fishbowl YT backfill started*"))
        self.assertIn("2 captured, 0 failed/empty, 2 already had", msgs[-1])
        self.assertIn("capturing new1 (vod) Fresh stream", out)

    def test_capture_that_leaves_no_ingested_row_counts_as_failed(self):
        cur, sp, out = _run([("v", 0, "t")], capture=MagicMock(return_value=types.SimpleNamespace(returncode=1)), only="fishbowl")
        self.assertIn("0 captured, 1 failed/empty", yb.nova_config.post_both.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, types, unittest.mock as um, psycopg2, subprocess, runpy; "
                "sys.modules['nova_config'] = um.MagicMock(); sys.modules['nova_yt_ingest_watch'] = um.MagicMock(); "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "subprocess.run = um.MagicMock(side_effect=AssertionError('subprocess at import')); "
                "sys.argv = [sys.argv[1]]; runpy.run_path(sys.argv[0], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
