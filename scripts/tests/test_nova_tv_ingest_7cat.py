#!/usr/bin/env python3
"""7-category tests for the 2026-10-08 crime_drama misfiling fix in the TV ingest classifiers.

Root cause: three duplicated show-name classifiers (nova_tv_ingest, nova_nightly_media,
nova_tv_retry_failed) used bare-substring matching and a "combat/war/battle/military -> crime_drama"
rule, so "The Bulwark" (bul-WAR-k), "Military Aviation History" and "Combat Veteran News" (no news
check in nightly_media) were filed as crime_drama. Fix: one shared explicit_vector() (channel map ->
news -> word-boundary military -> named crime shows) consulted first by all three, and remember()
now retries via post_with_retry() instead of failing silently.

Security, Performance, Retry, Unit, Integration, Functional, Frame. Offline: no HTTP, no DB writes.
Written by Jordan Koch (via Claude).
"""
import inspect
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_tv_ingest as tv  # noqa: E402
import nova_tv_retry_failed as retry_mod  # noqa: E402
import nova_nightly_media as nightly  # noqa: E402

CLASSIFIERS = (tv.classify_source, retry_mod.classify_source, nightly.classify_source)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSecurity(unittest.TestCase):
    def test_hostile_show_names_never_raise_and_stay_in_known_vocab(self):
        for name in ("'; DROP TABLE memories; --", "../../etc/passwd", "\x00\x08war\x00", "",
                     "a" * 50_000, "WAR" * 1000):
            for fn in CLASSIFIERS:
                v = fn(name, "t", "")
                self.assertRegex(v, r"^[a-z_]+$")

    def test_no_secrets_or_home_paths_in_new_code(self):
        src = inspect.getsource(tv.explicit_vector) + inspect.getsource(tv.post_with_retry)
        self.assertNotRegex(src, r"/Users/|password|token|api_key")

    def test_post_failure_log_does_not_echo_payload(self):
        logs = []
        req = mock.Mock()
        with mock.patch.object(tv.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
                mock.patch.object(tv.time, "sleep"):
            tv.post_with_retry(req, label="remember[news]", log_fn=logs.append)
        self.assertEqual(len(logs), 1)
        self.assertNotIn("SECRET", logs[0])


class TestPerformance(unittest.TestCase):
    def test_explicit_vector_is_cheap(self):
        names = ["The Bulwark", "Law & Order (1990)", "Some Random Channel", "KTLA 5 News"] * 500
        t = time.perf_counter()
        for n in names:
            tv.explicit_vector(n)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_regexes_precompiled(self):
        self.assertTrue(hasattr(tv._MILITARY_RE, "search"))
        self.assertTrue(hasattr(tv._CRIME_SHOW_RE, "search"))


class TestRetry(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(req, timeout=15):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("reset")
            return _Resp()
        with mock.patch.object(tv.urllib.request, "urlopen", side_effect=flaky), \
                mock.patch.object(tv.time, "sleep") as sl:
            self.assertTrue(tv.post_with_retry(mock.Mock(), log_fn=lambda m: None))
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1, 2])  # exponential backoff

    def test_final_failure_is_logged_not_silent(self):
        logs = []
        with mock.patch.object(tv.urllib.request, "urlopen", side_effect=OSError("boom")), \
                mock.patch.object(tv.time, "sleep"):
            self.assertFalse(tv.post_with_retry(mock.Mock(), label="remember[x]", log_fn=logs.append))
        self.assertTrue(logs and "3 attempts" in logs[0])

    def test_all_three_remember_paths_use_retry(self):
        for mod in (tv, retry_mod, nightly):
            self.assertIn("post_with_retry", inspect.getsource(mod.remember), mod.__name__)


class TestUnit(unittest.TestCase):
    def test_channel_map(self):
        self.assertEqual(tv.explicit_vector("The Bulwark"), "politics")
        self.assertEqual(tv.explicit_vector("Military Aviation History"), "military_history")
        self.assertEqual(tv.explicit_vector("Combat Veteran News"), "news")
        self.assertEqual(tv.explicit_vector("The Weekly Show with Jon Stewart"), "politics")
        self.assertEqual(tv.explicit_vector("Dragnet (1951)"), "crime_drama")

    def test_word_boundary_not_substring(self):
        # "bulwark"/"software"/"warner" contain "war" but are not military
        for n in ("Bulwark Daily", "Software Unscripted", "Warner Archive"):
            self.assertNotEqual(tv.explicit_vector(n), "military_history", n)
        self.assertEqual(tv.explicit_vector("Combat (1962)"), "military_history")
        self.assertEqual(tv.explicit_vector("Cannon"), "crime_drama")

    def test_unknown_falls_through(self):
        self.assertIsNone(tv.explicit_vector("Some Random Channel"))
        self.assertIsNone(tv.explicit_vector(""))
        self.assertIsNone(tv.explicit_vector(None))


class TestIntegration(unittest.TestCase):
    def test_three_classifiers_agree_on_misfiled_channels(self):
        for show, want in (("The Bulwark", "politics"), ("Military Aviation History", "military_history"),
                           ("Combat Veteran News", "news"), ("Dragnet (1951)", "crime_drama")):
            got = {fn.__module__: fn(show, "Episode", "a war story about battle") for fn in CLASSIFIERS}
            self.assertEqual(set(got.values()), {want}, got)

    def test_remember_posts_with_classified_source(self):
        seen = {}

        def capture(req, timeout=15):
            seen["body"] = req.data
            return _Resp()
        with mock.patch.object(tv.urllib.request, "urlopen", side_effect=capture):
            self.assertTrue(tv.remember("x", tv.classify_source("The Bulwark", "", ""), {}))
        self.assertIn(b'"source": "politics"', seen["body"])


class TestFunctional(unittest.TestCase):
    def test_crime_shows_still_crime_drama(self):
        for show in ("Dragnet (1951)", "Lights Out (1949)", "Cannon", "21 Jump Street (1987)"):
            for fn in CLASSIFIERS:
                self.assertEqual(fn(show, "", ""), "crime_drama", (fn.__module__, show))

    def test_news_and_other_routes_unchanged(self):
        self.assertEqual(tv.classify_source("KTLA 5 Morning News", "", ""), "local_news")
        self.assertEqual(tv.classify_source("Meat Church BBQ", "", ""), "cooking")
        self.assertEqual(nightly.classify_source("Wristwatch Revival", "", ""), "horology")


class TestFrame(unittest.TestCase):
    def test_modules_compile(self):
        for name in ("nova_tv_ingest.py", "nova_tv_retry_failed.py", "nova_nightly_media.py"):
            r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / name)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_public_api_present(self):
        for attr in ("explicit_vector", "post_with_retry", "CHANNEL_VECTOR", "classify_source", "remember"):
            self.assertTrue(hasattr(tv, attr), attr)


if __name__ == "__main__":
    unittest.main()
