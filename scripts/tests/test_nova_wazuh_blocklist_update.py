#!/usr/bin/env python3
"""Tests for nova_wazuh_blocklist_update.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This script MUTATES the world: it ssh/docker-pushes CDB blocklists to the Wazuh manager and restarts it.
Every outbound call (urlopen, ssh/docker subprocess) is mocked in every test; the restart guard
(push nothing / restart nothing when feeds are empty) is proven explicitly."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_wazuh_blocklist_update.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("wazuh_blocklist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wz = _load()
IP_FEED = "1.2.3.4\n5.6.7.8 extra\n# comment\nnot-an-ip\n"
HASH = "a" * 64
DOMAIN_FEED = "evil.example\n// note\nbad.test\n"


def _feeds(mapping):
    return lambda url, timeout=30: mapping.get(url, [])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_feeds_need_no_api_key(self):
        self.assertNotIn("Authorization", SRC)
        for lst in wz.FEEDS.values():
            for url, _ in lst:
                self.assertTrue(url.startswith("https://"))

    def test_entries_are_shape_validated(self):
        with patch.object(wz, "fetch_feed", _feeds({u: IP_FEED.splitlines() for u, _ in wz.FEEDS["ip-blocklist"]})), \
             redirect_stdout(io.StringIO()):
            content, count = wz.build_cdb_content("ip-blocklist")
        self.assertIn("1.2.3.4:", content)
        self.assertNotIn("not-an-ip", content)       # non-IP shape rejected


class TestPerformance(unittest.TestCase):
    def test_build_caps_at_max_ips(self):
        many = [f"10.0.{i // 256}.{i % 256}" for i in range(20_000)]
        with patch.object(wz, "fetch_feed", return_value=many), redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            content, count = wz.build_cdb_content("ip-blocklist")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(count, wz.MAX_IPS)


class TestRetry(unittest.TestCase):
    def test_fetch_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: fetch_feed/urlopen — one attempt, [] on failure
        with patch.object(wz.urllib.request, "urlopen", side_effect=OSError("feed down")) as u, redirect_stdout(io.StringIO()):
            self.assertEqual(wz.fetch_feed("https://x"), [])
        self.assertEqual(u.call_count, 1)

    def test_push_failure_returns_false_not_raise(self):
        # RETRY GAP: push_to_wazuh/ssh — one attempt; a failure is reported False, never raised
        with patch.object(wz.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ssh")) as run, \
             redirect_stdout(io.StringIO()):
            self.assertFalse(wz.push_to_wazuh("ip-blocklist", "1.2.3.4:x\n"))
        self.assertEqual(run.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_fetch_feed_strips_comments(self):
        resp = MagicMock(); resp.read.return_value = IP_FEED.encode()
        with patch.object(wz.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(wz.fetch_feed("https://x"), ["1.2.3.4", "5.6.7.8 extra", "not-an-ip"])

    def test_hash_list_requires_64_chars(self):
        with patch.object(wz, "fetch_feed", return_value=[HASH, "tooshort", HASH.upper()[:63]]), redirect_stdout(io.StringIO()):
            content, count = wz.build_cdb_content("hash-blocklist")
        self.assertEqual(count, 1)
        self.assertIn(f"{HASH}:", content)

    def test_domain_list_keeps_first_token(self):
        with patch.object(wz, "fetch_feed", return_value=["evil.example tag", "bad.test"]), redirect_stdout(io.StringIO()):
            content, count = wz.build_cdb_content("suspicious-domains")
        self.assertEqual(count, 2)
        self.assertIn("evil.example:", content)


class TestIntegration(unittest.TestCase):
    def test_push_targets_the_manager_container_list_path(self):
        with patch.object(wz.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            self.assertTrue(wz.push_to_wazuh("ip-blocklist", "1.2.3.4:x\n"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("/var/ossec/etc/lists/ip-blocklist", argv[-1])
        self.assertEqual(run.call_args.kwargs["input"], b"1.2.3.4:x\n")


class TestFunctional(unittest.TestCase):
    def test_golden_path_pushes_all_lists_and_restarts_once(self):
        feeds = {u: (IP_FEED.splitlines() if "ip" in name or "compromised" in u or "feodo" in u else
                     [HASH] if name == "hash-blocklist" else DOMAIN_FEED.splitlines())
                 for name, lst in wz.FEEDS.items() for u, _ in lst}
        calls = []
        with patch.object(wz, "fetch_feed", _feeds(feeds)), \
             patch.object(wz.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0]) or MagicMock(returncode=0)), \
             redirect_stdout(io.StringIO()) as out:
            wz.main()
        restarts = [c for c in calls if any("wazuh-control restart" in str(x) for x in c)]
        self.assertEqual(len(restarts), 1)
        self.assertIn("total entries", out.getvalue())

    def test_empty_feeds_push_nothing_and_never_restart(self):
        with patch.object(wz, "fetch_feed", return_value=[]), patch.object(wz, "push_to_wazuh") as push, \
             patch.object(wz.subprocess, "run") as run, redirect_stdout(io.StringIO()) as out:
            wz.main()
        push.assert_not_called()
        run.assert_not_called()                       # the restart guard: no entries -> world untouched
        self.assertIn("Done — 0 total entries", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_wazuh_blocklist_update as m; print(m.MAX_IPS)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "5000")


if __name__ == "__main__":
    unittest.main()
