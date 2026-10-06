#!/usr/bin/env python3
"""Tests for nova_package_tracker.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_package_tracker.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("npt_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pt = _load()
# stub every outbound side effect at module load; tracking state in a tempdir; never read the real mailbox cache
pt.notify = MagicMock()
pt.vector_remember = MagicMock()
pt.DATA_FILE = Path(_TMP.name) / "tracking.json"
pt.get_mail_data = MagicMock(return_value="")

USPS = "9400111899223344556677"
MAIL = f"""[UNREAD] FROM: USPS <auto@usps.com>
SUBJ: Your package {USPS} is in transit
[UNREAD] FROM: Amazon <ship@amazon.com>
SUBJ: Your order has shipped
[READ] FROM: Newsletter <news@example.com>
SUBJ: Weekly deals
"""


class _Base(unittest.TestCase):
    def setUp(self):
        pt.notify.reset_mock(); pt.vector_remember.reset_mock()
        pt.DATA_FILE.unlink(missing_ok=True)
        pt.get_mail_data.return_value = MAIL


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_tracking_lookup_is_https_and_numeric_only(self):
        self.assertIn("https://tools.usps.com/", SRC)
        # extracted numbers are regex-validated digits/alnum, so nothing hostile reaches the URL
        found = pt.extract_tracking_numbers("x 9400111899223344556677&evil=1 <script>")
        self.assertEqual(found, [("USPS", USPS)])

    def test_stable_key_not_salted_hash(self):
        self.assertIn("hashlib.sha1", SRC)
        self.assertNotIn("hash(pkg", SRC)


class TestPerformance(_Base):
    def test_scan_10k_emails_fast(self):
        pt.get_mail_data.return_value = "\n".join(
            f"[UNREAD] FROM: Shop{i} <s@example.com>\nSUBJ: Your order {i} has shipped" for i in range(10_000))
        t0 = time.perf_counter()
        pkgs = pt.scan_emails_for_packages()
        self.assertEqual(len(pkgs), 10_000)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(_Base):
    def test_usps_lookup_fails_open(self):
        # RETRY GAP: check_usps_status()/urlopen — one attempt; failure -> None (falls back to email status)
        with patch.object(pt.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertIsNone(pt.check_carrier_status("USPS", USPS))
        self.assertEqual(u.call_count, 1)
        self.assertIsNone(pt.check_carrier_status("UPS", "1Z999"))

    def test_corrupt_state_resets(self):
        pt.DATA_FILE.write_text("{nope")
        self.assertEqual(pt.load_tracking_data(), {"packages": {}, "last_scan": ""})


class TestUnit(unittest.TestCase):
    def test_extract_and_detect(self):
        got = dict((n, c) for c, n in pt.extract_tracking_numbers("1Z999AA10123456784 and TBA123456789012"))
        self.assertEqual(got, {"1Z999AA10123456784": "UPS", "TBA123456789012": "Amazon"})
        self.assertEqual(pt.extract_tracking_numbers(""), [])
        self.assertEqual(pt.detect_carrier_from_email("x@fedex.com", ""), "FedEx")
        self.assertEqual(pt.detect_carrier_from_email("a", "b"), "Unknown")

    def test_infer_status_and_advance(self):
        self.assertEqual(pt.infer_status_from_subject("It was DELIVERED"), "delivered")
        self.assertEqual(pt.infer_status_from_subject("Out for delivery today"), "out_for_delivery")
        self.assertEqual(pt.infer_status_from_subject("Order confirmed"), "ordered")
        self.assertEqual(pt.infer_status_from_subject("whatever"), "shipped")
        self.assertTrue(pt.status_advanced("shipped", "delivered"))
        self.assertFalse(pt.status_advanced("delivered", "shipped"))
        self.assertTrue(pt.status_advanced("weird", "shipped"))
        self.assertEqual(pt.status_icon("nope"), "📦")


class TestIntegration(_Base):
    def test_scan_shapes_packages(self):
        pkgs = pt.scan_emails_for_packages()
        self.assertEqual(len(pkgs), 2)
        self.assertEqual((pkgs[0]["carrier"], pkgs[0]["tracking"], pkgs[0]["status"]), ("USPS", USPS, "in_transit"))
        self.assertEqual((pkgs[1]["carrier"], pkgs[1]["tracking"]), ("Amazon", None))

    def test_digest_reads_saved_state(self):
        pt.save_tracking_data({"packages": {"a": {"status": "in_transit", "carrier": "UPS", "subject": "Box"},
                                            "b": {"status": "delivered", "carrier": "USPS", "subject": "Mail",
                                                  "last_seen": pt.TODAY + "T10:00"}}})
        d = pt.digest()
        self.assertIn("1 active, 1 delivered today", d)
        self.assertIn("🚚 [UPS] Box", d)


class TestFunctional(_Base):
    def test_first_run_posts_new_then_status_change(self):
        with patch.object(pt.urllib.request, "urlopen", side_effect=OSError("offline")):
            pt.main()
        title = pt.notify.call_args[0][0]
        self.assertTrue(title.startswith("*Package Update"))
        self.assertIn("New packages detected", pt.notify.call_args.kwargs["body"])
        self.assertEqual(len(json.loads(pt.DATA_FILE.read_text())["packages"]), 2)
        pt.notify.reset_mock()
        resp = MagicMock(); resp.__enter__.return_value.read.return_value = b"<html>Delivered</html>"
        with patch.object(pt.urllib.request, "urlopen", return_value=resp):
            pt.main()
        body = pt.notify.call_args.kwargs["body"]
        self.assertIn("🚚 -> ✅ [USPS]", body)
        self.assertIn("in_transit -> delivered", pt.vector_remember.call_args[0][0])

    def test_no_mail_no_post_and_old_delivered_pruned(self):
        old = (datetime.now() - timedelta(days=30)).isoformat()
        pt.save_tracking_data({"packages": {"x": {"status": "delivered", "last_seen": old},
                                            "y": {"status": "shipped", "last_seen": old}}})
        pt.get_mail_data.return_value = ""
        pt.main()
        pt.notify.assert_not_called()
        self.assertEqual(list(json.loads(pt.DATA_FILE.read_text())["packages"]), ["y"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--digest", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_package_tracker"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
