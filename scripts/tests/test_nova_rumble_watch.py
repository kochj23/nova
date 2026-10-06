#!/usr/bin/env python3
"""Tests for nova_rumble_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rumble_watch.py"
SRC = SCRIPT.read_text()
try:
    import psycopg2  # noqa: F401  real module locked in before any stubbing
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rw = _load("rumble_under_test", SCRIPT)


def _vid(vid, slug, by):
    return ('{"url":"https://rumble.com/%s-%s.html","title":"t","by":{"id":"1","name":"%s"}}' % (vid, slug, by))


PAGE = "<html>" + ",".join([_vid("v4abc1", "forgotten-weapons-fg42", "Forgotten Weapons"),
                             _vid("v4abc2", "recommended-thing", "Some Other Channel"),
                             _vid("v4abc1", "forgotten-weapons-fg42", "Forgotten Weapons"),
                             _vid("v4abc3", "mg42-at-the-range", "forgotten weapons")]) + "</html>"


def _resp(text):
    r = MagicMock(); r.read.return_value = text.encode()
    return r


def _run(pages, feed_new=None, notify=None):
    """Run main() with the scrape, PG, the creator-feed helpers and nova_notify mocked."""
    conn = MagicMock()
    connect = MagicMock(return_value=conn)
    processed = []

    def process(conn_, platform, name, ups):
        processed.append((platform, name, ups))
        return list(ups) if feed_new is None else feed_new(name, ups)
    nn = types.ModuleType("nova_notify"); nn.notify = notify or MagicMock()
    with patch.object(rw.urllib.request, "urlopen", side_effect=lambda req, timeout=None: _resp(pages.get(req.full_url, ""))), \
         patch.dict(sys.modules, {"psycopg2": types.SimpleNamespace(connect=connect), "nova_notify": nn}), \
         patch.object(rw.feed, "ensure_schema", MagicMock()) as es, patch.object(rw.feed, "process_creator", process), \
         redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        rc = rw.main()
    return rc, processed, out.getvalue(), err.getvalue(), nn.notify, connect, es


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("keychain", SRC.lower().replace("no auth needed", ""))   # public pages, no secret pulled

    def test_no_sql_here_all_persistence_goes_through_creator_feed(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|SELECT )", SRC))
        self.assertNotIn("shell=True", SRC)

    def test_hostile_markup_cannot_inject_into_titles_or_ids(self):
        evil = _vid("v4evil", 'x" onload="alert(1)', "Forgotten Weapons")
        with patch.object(rw.urllib.request, "urlopen", return_value=_resp(evil)):
            ups = rw.fetch_recent("Forgotten Weapons", "https://rumble.com/c/ForgottenWeapons")
        self.assertEqual(ups, [])                                        # the strict regex refuses it
        for v in rw.CREATORS.values():
            self.assertTrue(v.startswith("https://rumble.com/"))


class TestPerformance(unittest.TestCase):
    def test_scrape_regex_over_a_10k_video_page_under_1s(self):
        page = ",".join(_vid(f"v{i:06x}", f"slug-{i}", "Forgotten Weapons" if i % 2 else "Other") for i in range(10_000))
        with patch.object(rw.urllib.request, "urlopen", return_value=_resp(page)):
            t0 = time.perf_counter()
            ups = rw.fetch_recent("Forgotten Weapons", "https://rumble.com/c/ForgottenWeapons")
            dt = time.perf_counter() - t0
        self.assertLess(dt, 1.0)
        self.assertEqual(len(ups), rw.RECENT)                           # bounded: stops at RECENT, never the whole page

    def test_norm_fast_on_10k_names(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            rw._norm(f"The Bearded Mechanic #{i}")
        self.assertLess(time.perf_counter() - t0, 0.2)


class TestRetry(unittest.TestCase):
    def test_fetch_is_one_shot_and_fails_open_to_empty(self):
        # RETRY GAP: fetch_recent()/urllib.request.urlopen — one attempt; a failed channel yields [] and a stderr line
        with patch.object(rw.urllib.request, "urlopen", side_effect=OSError("503")) as u, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(rw.fetch_recent("Dark5", "https://rumble.com/c/Dark5"), [])
        self.assertEqual(u.call_count, 1)
        self.assertIn("rumble: fetch Dark5 failed: 503", err.getvalue())

    def test_all_channels_down_fires_one_deduped_canary_and_still_exits_zero(self):
        rc, processed, out, err, notify, _, _ = _run({})                 # every page empty -> 0 videos
        self.assertEqual(rc, 0)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[1]["dedup_key"], "rumble-scraper-broken")
        self.assertEqual(notify.call_args[1]["level"], "warning")
        self.assertIn("0 videos across all channels", out)

    def test_canary_notify_failure_is_swallowed(self):
        rc, _, out, _, notify, _, _ = _run({}, notify=MagicMock(side_effect=RuntimeError("bus down")))
        self.assertEqual(rc, 0)
        self.assertIn("8 creators checked, 0 new upload(s)", out)


class TestUnit(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(rw._norm("Forgotten Weapons"), "forgottenweapons")
        self.assertEqual(rw._norm(" The-Bearded_Mechanic! "), "thebeardedmechanic")
        self.assertEqual(rw._norm(""), ""); self.assertEqual(rw._norm(None), "")

    def test_fetch_recent_filters_dedups_and_titles(self):
        with patch.object(rw.urllib.request, "urlopen", return_value=_resp(PAGE)) as u:
            ups = rw.fetch_recent("Forgotten Weapons", "https://rumble.com/c/ForgottenWeapons")
        self.assertEqual([u_["video_id"] for u_ in ups], ["v4abc1", "v4abc3"])     # recommended + duplicate dropped
        self.assertEqual(ups[0], {"video_id": "v4abc1", "title": "Forgotten Weapons Fg42",
                                  "url": "https://rumble.com/v4abc1-forgotten-weapons-fg42.html", "published_at": None})
        req = u.call_args[0][0]
        self.assertEqual(req.get_header("User-agent"), rw.UA["User-Agent"])
        self.assertEqual(u.call_args[1]["timeout"], 15)

    def test_creators_are_the_verified_eight(self):
        self.assertEqual(len(rw.CREATORS), 8)
        self.assertEqual(rw.PLATFORM, "rumble")


class TestIntegration(unittest.TestCase):
    def test_persistence_is_delegated_to_nova_creator_feed(self):
        import nova_creator_feed
        self.assertIs(rw.feed, nova_creator_feed)
        for fn in ("ensure_schema", "process_creator", "DSN"):
            self.assertTrue(hasattr(rw.feed, fn))
        pages = {"https://rumble.com/c/ForgottenWeapons": PAGE}
        rc, processed, out, _, _, connect, es = _run(pages)
        connect.assert_called_once_with(rw.feed.DSN)
        es.assert_called_once()
        self.assertEqual(len(processed), 8)
        fw = [p for p in processed if p[1] == "Forgotten Weapons"][0]
        self.assertEqual(fw[0], "rumble"); self.assertEqual(len(fw[2]), 2)


class TestFunctional(unittest.TestCase):
    def test_golden_path_announces_new_uploads_and_no_canary(self):
        pages = {"https://rumble.com/c/ForgottenWeapons": PAGE}
        rc, processed, out, err, notify, _, _ = _run(pages, feed_new=lambda n, ups: ups[:1])
        self.assertEqual(rc, 0)
        self.assertIn("rumble: Forgotten Weapons -> 1 new", out)
        self.assertIn("rumble: 8 creators checked, 1 new upload(s)", out)
        notify.assert_not_called()
        self.assertEqual(err, "")

    def test_pg_outage_escapes_before_any_scrape(self):
        u = MagicMock()
        with patch.dict(sys.modules, {"psycopg2": types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("pg down")))}), \
             patch.object(rw.urllib.request, "urlopen", u):
            with self.assertRaises(RuntimeError):
                rw.main()
        u.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would open PG and scrape Rumble, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_rumble_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
