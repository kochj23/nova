#!/usr/bin/env python3
"""Tests for nova_kev_gear.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The KEV fetch (_get/urlopen), psycopg2.connect, notify and triage are mocked; nothing pages."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_kev_gear.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_kev_gear_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kg = _load()
kg.notify = MagicMock()      # module-level stubs: never page
kg.triage = MagicMock(return_value={"annotation": "", "level": "warning"})
kg.log = lambda m: None


def _v(cve, vp, product, ransom="Unknown", due="2026-01-01"):
    return {"cveID": cve, "vendorProject": vp, "product": product, "vulnerabilityName": f"{product} bug",
            "dateAdded": "2025-12-01", "dueDate": due, "knownRansomwareCampaignUse": ransom}


KEV = {"vulnerabilities": [
    _v("CVE-1", "Synology", "DiskStation Manager"),
    _v("CVE-2", "Cisco", "IOS XE"),
    _v("CVE-3", "Apple", "iOS", ransom="Known"),
    _v("CVE-4", "Samsung", "Mobile Devices"),
    _v("CVE-5", "Samsung", "Tizen TV"),
    _v("CVE-6", "Bose", "SoundTouch"),
]}


def _conn(devices=(), baseline_count=5, inserted=True):
    c = MagicMock()
    cur = c.cursor.return_value
    cur.fetchall.return_value = [(d,) for d in devices]
    cur.fetchone.side_effect = lambda: (baseline_count,) if "count(*)" in cur.execute.call_args[0][0] else ((1,) if inserted else None)
    return c, cur


def _main(argv, conn, kev=KEV, get_exc=None):
    with patch.object(kg.psycopg2, "connect", return_value=conn), \
         patch.object(kg, "_get", side_effect=get_exc, return_value=json.dumps(kev).encode()), \
         patch.object(kg, "notify") as n, patch.object(sys, "argv", ["x", *argv]), patch("builtins.print"):
        rc = kg.main()
    return rc, n


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized_insert(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token)\s*=\s*['\"]", SRC, re.I))
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", SRC)
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))

    def test_token_regex_escaped_against_hostile_device_names(self):
        inv = kg.build_inventory(_conn(devices=["bose (.*) [x"])[0])
        list(kg.match_kev(KEV, inv))      # must not raise re.error
        self.assertIn("bose", inv)

    def test_dry_run_writes_and_pages_nothing(self):
        c, cur = _conn()
        rc, n = _main(["--dry-run"], c)
        self.assertEqual(rc, 0)
        n.assert_not_called()
        self.assertFalse(any("INSERT INTO kev_matches" in x[0][0] for x in cur.execute.call_args_list))


class TestPerformance(unittest.TestCase):
    def test_match_10k_kev_entries_fast(self):
        kev = {"vulnerabilities": [_v(f"CVE-{i}", f"Vendor{i % 97}", f"Product {i}") for i in range(10_000)]}
        t0 = time.perf_counter()
        out = list(kg.match_kev(kev, dict(kg.FLEET_SOFTWARE)))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(out, [])


class TestRetry(unittest.TestCase):
    def test_kev_fetch_failure_warns_once_and_returns_1(self):
        # RETRY GAP: main()/_get — one KEV fetch; failure -> deduped warning, exit 1, nightly re-run
        c, cur = _conn()
        rc, n = _main([], c, get_exc=OSError("cisa down"))
        self.assertEqual(rc, 1)
        self.assertEqual(n.call_args.kwargs["dedup_key"], "kev-gear-fetch-fail")
        c.close.assert_called_once()

    def test_triage_failure_still_pages(self):
        c, cur = _conn()
        with patch.object(kg, "triage", side_effect=RuntimeError("llm")):
            rc, n = _main([], c)
        self.assertEqual(rc, 0)
        self.assertEqual(n.call_args.kwargs["level"], "warning")


class TestUnit(unittest.TestCase):
    def test_apple_is_vendor_scoped_and_cisco_ios_is_not_apple(self):
        got = {m[0]: m[4] for m in kg.match_kev(KEV, dict(kg.FLEET_SOFTWARE))}
        self.assertEqual(got["CVE-3"], "apple")
        self.assertNotIn("CVE-2", got)

    def test_samsung_requires_tv_word(self):
        got = {m[0] for m in kg.match_kev(KEV, dict(kg.FLEET_SOFTWARE))}
        self.assertIn("CVE-5", got)
        self.assertNotIn("CVE-4", got)

    def test_build_inventory_only_trusts_listed_brands(self):
        inv = kg.build_inventory(_conn(devices=["sonos-kitchen", "hikvision cam", "office", None, "12345"])[0])
        self.assertIn("sonos", inv)
        self.assertNotIn("hikvision", inv)
        self.assertNotIn("office", inv)
        self.assertEqual(inv["synology"], kg.FLEET_SOFTWARE["synology"])

    def test_empty_kev(self):
        self.assertEqual(list(kg.match_kev({}, dict(kg.FLEET_SOFTWARE))), [])


class TestIntegration(unittest.TestCase):
    def test_inventory_query_and_triage_wiring(self):
        c, cur = _conn()
        kg.build_inventory(c)
        self.assertIn("telemetry.known_devices", cur.execute.call_args[0][0])
        c2, _ = _conn()
        with patch.object(kg, "triage", return_value={"annotation": "ANN", "level": "critical"}) as t:
            _, n = _main([], c2)
        self.assertEqual(t.call_args.kwargs["source"], "kev_gear")
        self.assertEqual(n.call_args.kwargs["level"], "critical")
        self.assertIn("ANN", n.call_args.kwargs["body"])


class TestFunctional(unittest.TestCase):
    def test_new_matches_page_once_ransomware_first(self):
        c, cur = _conn()
        rc, n = _main([], c)
        self.assertEqual(rc, 0)
        n.assert_called_once()
        body = n.call_args.kwargs["body"]
        self.assertTrue(body.split("\n")[1].startswith("• *CVE-3*"))
        self.assertIn("ransomware", body)
        self.assertIn("alerted=true WHERE alerted=false", cur.execute.call_args_list[-1][0][0])

    def test_first_run_baselines_silently(self):
        c, cur = _conn(baseline_count=0)
        rc, n = _main([], c)
        self.assertEqual(rc, 0)
        n.assert_not_called()
        ins = [x for x in cur.execute.call_args_list if "INSERT INTO kev_matches" in x[0][0]]
        self.assertTrue(ins and all(x[0][1][-1] is True for x in ins))

    def test_no_new_rows_no_page(self):
        c, cur = _conn(inserted=False)
        _, n = _main([], c)
        n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_is_clean(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)
        r = subprocess.run([sys.executable, "-c", "import nova_kev_gear"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
