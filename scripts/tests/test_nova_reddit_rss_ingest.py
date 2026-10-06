#!/usr/bin/env python3
"""Tests for nova_reddit_rss_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import gc
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
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reddit_rss_ingest.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="reddit-rss-test-"))


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_FEED = "C_FEED"
    cfg.post_both = MagicMock()
    return {"nova_config": cfg}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):
        spec.loader.exec_module(mod)
    return mod


rr = _load("reddit_rss_under_test", SCRIPT)
# Module-level stubs: no Reddit, no memory server, no PG, no Slack, no 22 s sleeps, no /tmp state or lock files.
rr.STATE_FILE = str(TMP / "nova_reddit_rss.offset")
rr.tempfile = types.SimpleNamespace(gettempdir=lambda: str(TMP))
rr.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")))
rr.time = types.SimpleNamespace(time=time.time, sleep=MagicMock())
rr.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=rr.urllib.request.Request,
                                                                urlopen=MagicMock(side_effect=OSError("offline: urlopen stubbed"))),
                                  error=urllib.error)


def _entry(pid, title, author="/u/someone", content="<p>Hello &amp; welcome</p>"):
    return (f"<entry><id>{pid}</id><title>{title}</title><author><name>{author}</name></author>"
            f"<published>2026-10-05T10:00:00+00:00</published><content type=\"html\">{content}</content></entry>")


FEED = "<feed>" + _entry("t3_aaa", "First post") + _entry("t3_bbb", "Second &lt;b&gt;post&lt;/b&gt;", content="") + "</feed>"
COMMENTS = "<feed>" + _entry("t1_c1", "c", author="/u/alice", content="nice") + _entry("t1_c2", "c", author="/u/bob", content="") + "</feed>"


class _Resp:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _http_error(code, headers=None):
    import email.message
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://www.reddit.com/x", code, "err", h, io.BytesIO(b""))


class _Cur:
    """Cursor stub: `seen` maps subreddit -> post ids; `state` maps key -> value; records every statement."""
    def __init__(self, seen=None, state=None):
        self.seen, self.state = dict(seen or {}), dict(state or {})
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append(s); self.params.append(params); self._last = (s, params)
        if s.startswith("INSERT INTO reddit_rss_state"):
            self.state[params[0]] = params[1]
        elif s.startswith("INSERT INTO reddit_rss_seen"):
            self.seen.setdefault(params[0], set()).add(params[1])

    def fetchone(self):
        s, p = self._last
        return (self.state[p[0]],) if "reddit_rss_state" in s and p[0] in self.state else None

    def fetchall(self):
        s, p = self._last
        return [(x,) for x in self.seen.get(p[0], ())]

    def ran(self, frag): return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


def _urlopen(routes):
    """urlopen stub keyed by URL substring -> body str | Exception | list of those (consumed in order)."""
    def uo(req, timeout=None):
        url = req.full_url
        for key, val in routes.items():
            if key in url:
                v = val.pop(0) if isinstance(val, list) else val
                if isinstance(v, Exception):
                    raise v
                return _Resp(v)
        raise OSError(f"no route for {url}")
    return MagicMock(side_effect=uo)


def _run_main(argv, cur, routes, start_ts=None):
    gc.collect()   # a prior main()'s lock-file handle can linger in a traceback cycle and hold the flock
    conn = types.SimpleNamespace(cursor=lambda: cur, close=MagicMock(), autocommit=False)
    rr.nova_config.post_both = MagicMock()
    uo = _urlopen(routes); buf = io.StringIO()
    with patch.object(sys, "argv", ["nova_reddit_rss_ingest.py", *argv]), patch.object(rr.psycopg2, "connect", MagicMock(return_value=conn)) as pg, \
         patch.object(rr.urllib.request, "urlopen", uo), patch.object(rr, "START_TS", start_ts if start_ts is not None else time.time()), redirect_stdout(buf):
        rr.main()
    return cur, pg, uo, conn, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", rr.DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        evil = "x'); DROP TABLE reddit_rss_seen; --"
        cur = _Cur()
        with patch.object(rr.urllib.request, "urlopen", _urlopen({"/r/": "<feed>" + _entry(evil, "t") + "</feed>"})):
            rr.crawl_sub(cur, evil, "reddit")
        for s, p in cur.sql and zip(cur.sql, cur.params):
            self.assertNotIn("DROP", s)
        self.assertEqual(cur.ran("INSERT INTO reddit_rss_seen")[0][1], (evil, evil))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"reddit_rss_seen", "reddit_rss_state"})

    def test_memories_are_marked_private_and_async(self):
        uo = MagicMock(return_value=_Resp("{}"))
        with patch.object(rr.urllib.request, "urlopen", uo):
            self.assertTrue(rr.remember("text", {"vector": "fishbowl", "type": "reddit"}))
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, rr.MEMORY_URL + "?async=1")
        payload = json.loads(req.data)
        self.assertEqual(payload["metadata"]["privacy"], "private")
        self.assertEqual((payload["source"], payload["tier"]), ("fishbowl", "long_term"))

    def test_argv_subreddit_is_normalized_never_a_path(self):
        cur = _Cur(state={"cooldown_until": "0"})
        _run_main(["r/burbank,https://reddit.com/r/glendale/", "local"], cur, {"/r/": ""})
        selects = [p[0] for s, p in cur.ran("SELECT post_id FROM reddit_rss_seen")]
        self.assertEqual(selects, ["burbank", "glendale"])


class TestPerformance(unittest.TestCase):
    def test_parse_entries_fast_on_10k(self):
        xml = "<feed>" + "".join(_entry(f"t3_{i}", f"post {i}") for i in range(10_000)) + "</feed>"
        t0 = time.perf_counter()
        out = rr.parse_entries(xml)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(out), 10_000)
        self.assertEqual(out[5]["content"], "Hello & welcome")

    def test_rotation_is_bounded_per_run(self):
        n = 0
        for _ in range(10_000 // len(rr.SUBS)):
            t = rr.rotating_targets(); n += 1
            self.assertEqual(len(t), len(rr.FISHBOWL_SUBS) + rr.ROTATE_BATCH)
        self.assertGreater(n, 0)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_transient_errors_twice_then_succeeds(self):
        uo = _urlopen({"/r/x": [OSError("reset"), OSError("reset"), "<feed></feed>"]})
        rr.time.sleep.reset_mock()
        with patch.object(rr.urllib.request, "urlopen", uo):
            self.assertEqual(rr.fetch("https://www.reddit.com/r/x/.rss"), "<feed></feed>")
        self.assertEqual(uo.call_count, 3)
        self.assertEqual(rr.time.sleep.call_args_list[0][0], (10,))

    def test_fetch_gives_up_after_three_and_returns_none(self):
        uo = _urlopen({"/r/x": [OSError("a"), OSError("b"), OSError("c"), "never"]})
        with patch.object(rr.urllib.request, "urlopen", uo):
            self.assertIsNone(rr.fetch("https://www.reddit.com/r/x/.rss"))
        self.assertEqual(uo.call_count, 3)

    def test_429_aborts_immediately_and_other_http_errors_fail_open(self):
        uo = _urlopen({"/r/x": [_http_error(429, {"Retry-After": "120"})]})
        with patch.object(rr.urllib.request, "urlopen", uo):
            with self.assertRaises(rr.RateLimited) as cm:
                rr.fetch("https://www.reddit.com/r/x/.rss")
        self.assertEqual(cm.exception.retry_after, 120); self.assertEqual(uo.call_count, 1)
        with patch.object(rr.urllib.request, "urlopen", _urlopen({"/r/x": [_http_error(403)]})), redirect_stdout(io.StringIO()):
            self.assertIsNone(rr.fetch("https://www.reddit.com/r/x/.rss"))

    def test_remember_and_slack_fail_open(self):
        # RETRY GAP: remember — one POST; failure returns False and the post is still marked seen by the caller
        # RETRY GAP: slack — one post_both; failure is logged, never raised
        with patch.object(rr.urllib.request, "urlopen", MagicMock(side_effect=OSError("memory down"))):
            self.assertFalse(rr.remember("t", {"vector": "v"}))
        rr.nova_config.post_both = MagicMock(side_effect=OSError("slack 500"))
        with redirect_stdout(io.StringIO()) as out:
            rr.slack("hi")
        self.assertIn("slack failed", out.getvalue())

    def test_deadline_aborts_the_run_cleanly(self):
        cur = _Cur(state={"cooldown_until": "0"})
        with redirect_stdout(io.StringIO()):
            with patch.object(rr, "START_TS", time.time() - rr.DEADLINE_S - 1):
                with self.assertRaises(rr.Deadline):
                    rr.check_deadline()
        cur, pg, uo, conn, out = _run_main(["burbank", "burbank"], cur, {"/r/": FEED}, start_ts=time.time() - rr.DEADLINE_S - 1)
        self.assertIn("deadline reached", out); uo.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_parse_entries_strips_tags_and_unescapes(self):
        e = rr.parse_entries(FEED)
        self.assertEqual([x["id"] for x in e], ["t3_aaa", "t3_bbb"])
        self.assertEqual(e[1]["title"], "Second <b>post</b>")   # tags stripped first, entities unescaped after: escaped text survives
        self.assertEqual(e[0]["author"], "/u/someone"); self.assertEqual(e[0]["pub"], "2026-10-05T10:00:00+00:00")
        self.assertEqual(e[1]["content"], "")
        self.assertEqual(rr.parse_entries(""), [])

    def test_rotating_targets_persists_offset_and_wraps(self):
        Path(rr.STATE_FILE).write_text("junk")
        t1 = rr.rotating_targets()
        self.assertEqual(list(t1)[:2], rr.FISHBOWL_SUBS)
        self.assertEqual(len(t1), 2 + rr.ROTATE_BATCH)
        others = [s for s in rr.SUBS if s not in rr.FISHBOWL_SUBS]
        self.assertEqual(Path(rr.STATE_FILE).read_text(), str(rr.ROTATE_BATCH % len(others)))
        Path(rr.STATE_FILE).write_text(str(len(others) - 1))
        t2 = rr.rotating_targets()
        self.assertEqual(list(t2)[2], others[-1]); self.assertEqual(list(t2)[3], others[0])   # wraps

    def test_state_helpers(self):
        cur = _Cur()
        self.assertEqual(rr.get_state(cur, "cooldown_until", "0"), "0")
        rr.set_state(cur, "throttle_streak", 3)
        self.assertEqual(cur.ran("ON CONFLICT (key) DO UPDATE")[0][1], ("throttle_streak", "3"))
        self.assertEqual(rr.get_state(cur, "throttle_streak"), "3")

    def test_comments_formats_authors_and_skips_empty(self):
        with patch.object(rr.urllib.request, "urlopen", _urlopen({"/comments/abc/": COMMENTS})):
            self.assertEqual(rr.comments("sub", "abc"), "u/alice: nice")
        with patch.object(rr.urllib.request, "urlopen", _urlopen({"/comments/abc/": [OSError(), OSError(), OSError()]})):
            self.assertEqual(rr.comments("sub", "abc"), "")

    def test_rate_limited_carries_retry_after(self):
        self.assertEqual(rr.RateLimited(5).retry_after, 5)


class TestIntegration(unittest.TestCase):
    def test_slack_goes_through_nova_config_post_both_to_the_feed(self):
        self.assertIn("import nova_config", SRC)
        rr.nova_config.post_both = MagicMock()
        rr.slack("m")
        rr.nova_config.post_both.assert_called_once_with("m", slack_channel="C_FEED", discord_channel=None)

    def test_first_seed_skips_comments_and_chunks_long_bodies(self):
        cur = _Cur()
        long_feed = "<feed>" + _entry("t3_long", "Long", content="x" * (rr.CHUNK * 2 + 10)) + "</feed>"
        calls = []
        with patch.object(rr.urllib.request, "urlopen", _urlopen({"/r/sub/.rss": long_feed, "/remember": "{}"})) as uo:
            new, sample, first = rr.crawl_sub(cur, "sub", "reddit")
        self.assertEqual((new, first), (1, True))
        remembers = [json.loads(c[0][0].data) for c in uo.call_args_list if "/remember" in c[0][0].full_url]
        self.assertEqual([m["metadata"]["idx"] for m in remembers], [0, 1, 2])
        self.assertTrue(all(len(m["text"]) <= rr.CHUNK for m in remembers))
        self.assertEqual(remembers[0]["metadata"]["author"], "fishbowl")           # authors are never persisted
        self.assertFalse(any("/comments/" in c[0][0].full_url for c in uo.call_args_list))

    def test_incremental_run_fetches_capped_comments_and_dedupes(self):
        cur = _Cur(seen={"sub": {"t3_aaa"}})
        routes = {"/r/sub/.rss": FEED, "/comments/bbb/": COMMENTS, "/remember": "{}"}
        with patch.object(rr.urllib.request, "urlopen", _urlopen(routes)) as uo:
            new, sample, first = rr.crawl_sub(cur, "sub", "fishbowl")
        self.assertEqual((new, first), (1, False))
        self.assertTrue(sample.startswith("[r/sub post by /u/someone] Second <b>post</b>"))
        self.assertIn("--- comments ---\nu/alice: nice", json.loads([c for c in uo.call_args_list if "/remember" in c[0][0].full_url][0][0][0].data)["text"])
        self.assertEqual(cur.ran("INSERT INTO reddit_rss_seen")[0][1], ("sub", "t3_bbb"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_rolls_up_incremental_and_pings_fishbowl(self):
        cur = _Cur(seen={"burbank": {"old"}, "WatchesCirclejerk": {"old"}}, state={"cooldown_until": "0", "throttle_streak": "2"})
        routes = {"/r/burbank/.rss": FEED, "/r/WatchesCirclejerk/.rss": FEED, "/r/fresh/.rss": FEED, "/comments/": COMMENTS, "/remember": "{}"}
        cur, pg, uo, conn, out = _run_main(["burbank,WatchesCirclejerk,fresh", "reddit"], cur, routes)
        pg.assert_called_once_with(rr.DSN)
        self.assertTrue(cur.sql[0].startswith("CREATE TABLE IF NOT EXISTS reddit_rss_seen"))
        msgs = [c[0][0] for c in rr.nova_config.post_both.call_args_list]
        self.assertEqual(len(msgs), 2)   # fishbowl pings key off the VECTOR; with vector 'reddit' everything rolls up
        self.assertEqual(msgs[0], ":mag: *Reddit RSS* — 4 new post(s) across 2 sub(s): r/burbank (2), r/WatchesCirclejerk (2)")
        self.assertTrue(msgs[1].startswith(":seedling: *Reddit RSS restored* — first-seed pass ingested: r/fresh→reddit (2)"))
        self.assertEqual(cur.state["throttle_streak"], "0"); self.assertEqual(cur.state["cooldown_until"], "0")
        conn.close.assert_called_once()
        self.assertIn("r/fresh: SEEDED 2 posts (comments skipped)", out)

    def test_fishbowl_vector_gets_its_own_sampled_ping(self):
        cur = _Cur(seen={"TheTpGentleman": {"old"}}, state={"cooldown_until": "0"})
        cur, pg, uo, conn, out = _run_main(["TheTpGentleman", "fishbowl"], cur, {"/r/": FEED, "/comments/": COMMENTS, "/remember": "{}"})
        msg = rr.nova_config.post_both.call_args_list[0][0][0]
        self.assertIn("r/TheTpGentleman → fishbowl: 2 new post(s).\n*Sample:*\n> [r/TheTpGentleman post by /u/someone] First post", msg)

    def test_429_sets_cooldown_with_growing_streak_and_posts_nothing(self):
        cur = _Cur(state={"cooldown_until": "0", "throttle_streak": "1"})
        t0 = time.time()
        cur, pg, uo, conn, out = _run_main(["burbank", "burbank"], cur, {"/r/": [_http_error(429, {"Retry-After": "5"})]})
        self.assertEqual(cur.state["throttle_streak"], "2")
        self.assertGreaterEqual(float(cur.state["cooldown_until"]) - t0, 1800 - 2)          # 900 * 2**(2-1)
        rr.nova_config.post_both.assert_not_called(); conn.close.assert_called_once()
        self.assertIn("429 from Reddit — aborting pass, cooldown 30m (streak 2)", out)

    def test_active_cooldown_and_held_lock_skip_the_pass(self):
        cur = _Cur(state={"cooldown_until": str(time.time() + 3600)})
        cur, pg, uo, conn, out = _run_main(["burbank", "burbank"], cur, {})
        uo.assert_not_called(); self.assertIn("cooldown active", out)
        import fcntl
        with patch.object(rr.fcntl, "flock", MagicMock(side_effect=OSError("locked"))), redirect_stdout(io.StringIO()) as buf, \
             patch.object(rr.psycopg2, "connect", MagicMock()) as pg2, patch.object(sys, "argv", ["x", "burbank", "burbank"]):
            rr.main()
        pg2.assert_not_called(); self.assertIn("another crawl is already running", buf.getvalue())
        self.assertIs(rr.fcntl, fcntl)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_reddit_rss_ingest as m; print('IMPORT-OK', m.DELAY)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT-OK 22")


if __name__ == "__main__":
    unittest.main()
