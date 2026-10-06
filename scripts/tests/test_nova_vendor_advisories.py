#!/usr/bin/env python3
"""Tests for nova_vendor_advisories.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import urllib.request          # imported BEFORE the patch.dict load so the module and the tests share one urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_vendor_advisories.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.SLACK_BB = "C_CRITICAL"
    with patch.dict(sys.modules, {"nova_config": cfg}):
        spec.loader.exec_module(mod)
    return mod


va = _load("va", SCRIPT)
KEV = {"vulnerabilities": [
    {"cveID": "CVE-2026-50746", "vendorProject": "Ubiquiti", "product": "UniFi Network", "vulnerabilityName": "Auth bypass"},
    {"cveID": "CVE-2026-1", "vendorProject": "Acme", "product": "Widget", "vulnerabilityName": "Nothing we run"},
]}


def _rss(*items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description></item>" for t, l, d in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


RSS1 = _rss(("Synology patches DSM flaw", "https://b.example/1", "critical &amp; exploited"),
            ("Celebrity gossip", "https://b.example/2", "nothing"))
RSS2 = _rss(("Grafana zero-day", "https://h.example/1", "update now"))


def _fetch(kev=KEV, rss=(RSS1, RSS2), fail=()):
    """urllib.request.urlopen stand-in keyed by URL; URLs in `fail` raise."""
    feeds = dict(zip(va.RSS, rss))

    def urlopen(req, timeout=None):
        url = req.full_url
        urlopen.calls.append((url, req.get_header("User-agent"), timeout))
        if url in fail:
            raise urllib.error.URLError("down")
        if url == va.KEV_URL:
            return io.BytesIO(json.dumps(kev).encode())
        return io.BytesIO(feeds[url])
    urlopen.calls = []
    return urlopen


def _main(state, fetch=None):
    va.nova_config.post_both.reset_mock()
    out = io.StringIO()
    with patch.object(va, "STATE", state), patch("urllib.request.urlopen", fetch or _fetch()), redirect_stdout(out):
        va.main()
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_no_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("Authorization", SRC)
        self.assertIn("stdlib only, no API key", SRC)

    def test_feed_text_is_matched_not_executed_and_alerts_go_to_critical_only(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "s.json")
            json.dump({"seen": []}, open(state, "w"))
            hostile = _rss(("&lt;script&gt;alert(1)&lt;/script&gt; UniFi", "javascript:evil()", "$(rm -rf /) &lt;b&gt;"))
            _main(state, _fetch(kev={"vulnerabilities": []}, rss=(hostile, RSS2)))
        msg, kw = va.nova_config.post_both.call_args.args[0], va.nova_config.post_both.call_args.kwargs
        self.assertEqual(kw["slack_channel"], "C_CRITICAL")
        self.assertIn("UniFi", msg)
        self.assertEqual(SRC.count("post_both("), 1)

    def test_fetch_identifies_itself(self):
        f = _fetch()
        with patch("urllib.request.urlopen", f):
            va._get(va.KEV_URL)
        self.assertEqual(f.calls[0][1], "nova-advisory-watch/1.0 (+fleet security)")
        self.assertEqual(f.calls[0][2], 25)


class TestPerformance(unittest.TestCase):
    def test_kev_scan_on_10k_entries(self):
        big = {"vulnerabilities": [{"cveID": f"CVE-{i}", "vendorProject": "Ubiquiti" if i % 4 == 0 else "Other",
                                    "product": "p", "vulnerabilityName": "n"} for i in range(10_000)]}
        with patch("urllib.request.urlopen", _fetch(kev=big)):
            t0 = time.perf_counter()
            hits = va.check_kev()
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(hits), 2_500)

    def test_fleet_regex_on_10k_blobs(self):
        blobs = [f"item {i} about " + ("Synology DSM" if i % 2 else "pancakes") for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(1 for b in blobs if va.FLEET_RE.search(b))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(n, 5_000)


class TestRetry(unittest.TestCase):
    def test_kev_fetch_failure_fails_open_to_no_hits(self):
        # RETRY GAP: check_kev/_get — one GET; a URLError is printed and [] returned
        f = _fetch(fail=(va.KEV_URL,))
        with patch("urllib.request.urlopen", f), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(va.check_kev(), [])
        self.assertEqual(len(f.calls), 1)
        self.assertIn("KEV fetch failed", out.getvalue())

    def test_one_dead_feed_does_not_sink_the_other(self):
        # RETRY GAP: check_rss/_get — per-feed single attempt; the failing feed is skipped, the other still parsed
        f = _fetch(fail=(va.RSS[0],))
        with patch("urllib.request.urlopen", f), redirect_stdout(io.StringIO()) as out:
            hits = va.check_rss()
        self.assertEqual([h[2] for h in hits], ["Grafana zero-day"])
        self.assertIn(f"RSS {va.RSS[0]} failed", out.getvalue())

    def test_malformed_xml_is_contained(self):
        with patch("urllib.request.urlopen", _fetch(rss=(b"<rss><item><title>UniFi", RSS2))), redirect_stdout(io.StringIO()):
            self.assertEqual(len(va.check_rss()), 1)


class TestUnit(unittest.TestCase):
    def test_fleet_regex_word_boundaries(self):
        self.assertTrue(va.FLEET_RE.search("Synology DSM 7.2 patched"))
        self.assertIsNone(va.FLEET_RE.search("sdsms are not a thing"))
        self.assertTrue(va.FLEET_RE.search("Chef Infra Server"))
        self.assertIsNone(va.FLEET_RE.search("chefs cook"))
        self.assertEqual(va.FLEET_RE.search("Running on macOS 26").group(1).lower(), "macos")

    def test_kev_hit_shape(self):
        with patch("urllib.request.urlopen", _fetch()):
            hits = va.check_kev()
        self.assertEqual(len(hits), 1)
        src, cve, text, link, key = hits[0]
        self.assertEqual((cve, key), ("CVE-2026-50746", "CVE-2026-50746"))
        self.assertEqual(link, "https://nvd.nist.gov/vuln/detail/CVE-2026-50746")
        self.assertIn("actively-exploited", src); self.assertIn("Ubiquiti UniFi Network — Auth bypass", text)

    def test_rss_hits_unescape_and_key_on_link(self):
        with patch("urllib.request.urlopen", _fetch()):
            hits = va.check_rss()
        self.assertEqual([(h[0], h[2], h[4]) for h in hits],
                         [("news", "Synology patches DSM flaw", "https://b.example/1"), ("news", "Grafana zero-day", "https://h.example/1")])
        self.assertEqual(hits[0][1].lower(), "synology")

    def test_empty_feeds_yield_nothing(self):
        with patch("urllib.request.urlopen", _fetch(kev={}, rss=(_rss(), _rss()))):
            self.assertEqual(va.check_kev() + va.check_rss(), [])


class TestIntegration(unittest.TestCase):
    def test_first_run_baselines_without_alerting(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "sub", "s.json")
            out = _main(state)
            seen = json.load(open(state))["seen"]
        va.nova_config.post_both.assert_not_called()
        self.assertIn("baselined 3 current fleet-relevant advisories", out)
        self.assertEqual(set(seen), {"CVE-2026-50746", "https://b.example/1", "https://h.example/1"})

    def test_seen_set_is_capped_and_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "s.json")
            json.dump({"seen": [f"old{i}" for i in range(3_500)]}, open(state, "w"))
            _main(state)
            self.assertEqual(len(json.load(open(state))["seen"]), 3_000)

    def test_alert_channel_and_message_come_from_nova_config(self):
        self.assertIn("slack_channel=nova_config.SLACK_BB", SRC)
        self.assertIn("import nova_config", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_second_run_alerts_only_the_new_items(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "s.json")
            json.dump({"seen": ["https://b.example/1"]}, open(state, "w"))
            out = _main(state)
            seen = json.load(open(state))["seen"]
        va.nova_config.post_both.assert_called_once()
        msg = va.nova_config.post_both.call_args.args[0]
        self.assertTrue(msg.startswith(":shield: *Fleet security advisory* — 2 new item(s)"))
        self.assertIn("*[CISA KEV ⚠️ actively-exploited]* Ubiquiti UniFi Network — Auth bypass\n  https://nvd.nist.gov/vuln/detail/CVE-2026-50746", msg)
        self.assertIn("*[news]* Grafana zero-day\n  https://h.example/1", msg)
        self.assertNotIn("Synology", msg)
        self.assertIn("2 new advisories alerted (3 matched total)", out)
        self.assertIn("CVE-2026-50746", seen)

    def test_error_path_all_sources_down_alerts_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "s.json")
            json.dump({"seen": ["x"]}, open(state, "w"))
            out = _main(state, _fetch(fail=(va.KEV_URL, *va.RSS)))
            self.assertEqual(json.load(open(state))["seen"], ["x"])
        va.nova_config.post_both.assert_not_called()
        self.assertIn("0 new advisories alerted (0 matched total)", out)


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_main_is_guarded(self):
        # no --help; running main() fetches CISA + two feeds, so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_vendor_advisories"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
