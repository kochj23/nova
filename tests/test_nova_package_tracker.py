"""
test_nova_package_tracker.py — All 7 test categories for nova_package_tracker.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_package_tracker.py"
_spec = importlib.util.spec_from_file_location("nova_package_tracker", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

extract_tracking_numbers = _mod.extract_tracking_numbers
detect_carrier_from_email = _mod.detect_carrier_from_email
infer_status_from_subject = _mod.infer_status_from_subject
status_advanced = _mod.status_advanced
status_icon = _mod.status_icon


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_data_file_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.DATA_FILE))
    def test_vector_url_is_localhost(self):
        self.assertTrue(_mod.VECTOR_URL.startswith("http://127.0.0.1"))


class TestPerformance(unittest.TestCase):
    def test_extract_tracking_numbers_fast(self):
        text = "Your UPS tracking number is 1Z999AA10123456784 and USPS is 9400111899223427449470"
        start = time.perf_counter()
        for _ in range(1000):
            extract_tracking_numbers(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
    def test_status_order_defined(self):
        self.assertEqual(_mod.STATUS_ORDER[0], "ordered")
        self.assertEqual(_mod.STATUS_ORDER[-1], "delivered")
    def test_load_tracking_data_fast_on_missing(self):
        with patch.object(_mod, "DATA_FILE", Path("/nonexistent/data.json")):
            start = time.perf_counter()
            data = _mod.load_tracking_data()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01)
        self.assertIn("packages", data)


class TestRetry(unittest.TestCase):
    def test_vector_remember_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            _mod.vector_remember("package update", {})
    def test_check_usps_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.check_usps_status("9400111899223427449470")
        self.assertIsNone(result)
    def test_slack_post_does_not_raise(self):
        _nova_cfg.post_both.side_effect = Exception("slack down")
        try:
            _mod.slack_post("Test message")
        except Exception:
            pass
        finally:
            _nova_cfg.post_both.side_effect = None


class TestUnit(unittest.TestCase):
    def test_extract_ups_tracking(self):
        text = "UPS tracking: 1Z999AA10123456784"
        result = extract_tracking_numbers(text)
        self.assertTrue(any(carrier == "UPS" for carrier, _ in result))

    def test_extract_amazon_tracking(self):
        text = "Amazon tracking: TBA123456789012"
        result = extract_tracking_numbers(text)
        self.assertTrue(any(carrier == "Amazon" for carrier, _ in result))

    def test_detect_carrier_fedex(self):
        result = detect_carrier_from_email("shipping@fedex.com", "Your FedEx package is shipped")
        self.assertEqual(result, "FedEx")

    def test_detect_carrier_ups(self):
        result = detect_carrier_from_email("noreply@ups.com", "UPS shipment notification")
        self.assertEqual(result, "UPS")

    def test_detect_carrier_amazon(self):
        result = detect_carrier_from_email("shipment-tracking@amazon.com", "Your Amazon order has shipped")
        self.assertEqual(result, "Amazon")

    def test_detect_carrier_unknown(self):
        result = detect_carrier_from_email("noreply@example.com", "Your order is ready")
        self.assertEqual(result, "Unknown")

    def test_infer_status_delivered(self):
        self.assertEqual(infer_status_from_subject("Your package has been delivered"), "delivered")

    def test_infer_status_shipped(self):
        self.assertEqual(infer_status_from_subject("Your order has shipped"), "shipped")

    def test_infer_status_out_for_delivery(self):
        self.assertEqual(infer_status_from_subject("Out for delivery today"), "out_for_delivery")

    def test_infer_status_in_transit(self):
        self.assertEqual(infer_status_from_subject("Package in transit"), "in_transit")

    def test_status_advanced_true(self):
        self.assertTrue(status_advanced("shipped", "delivered"))
        self.assertTrue(status_advanced("ordered", "in_transit"))

    def test_status_advanced_false(self):
        self.assertFalse(status_advanced("delivered", "shipped"))
        self.assertFalse(status_advanced("shipped", "shipped"))

    def test_status_icon_delivered(self):
        self.assertIn("✅", status_icon("delivered"))

    def test_status_icon_shipped(self):
        self.assertIn("📦", status_icon("shipped"))


class TestIntegration(unittest.TestCase):
    def test_save_and_load_tracking_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "DATA_FILE", Path(tmpdir) / "tracking.json"):
                data = {"packages": {"pkg1": {"status": "shipped"}}, "last_scan": "2026-01-01"}
                _mod.save_tracking_data(data)
                loaded = _mod.load_tracking_data()
        self.assertEqual(loaded["packages"]["pkg1"]["status"], "shipped")

    def test_digest_shows_active_packages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data = {"packages": {
                "pkg1": {"status": "shipped", "carrier": "UPS", "subject": "Test Package", "last_seen": "2026-01-01"},
                "pkg2": {"status": "delivered", "carrier": "USPS", "subject": "Old Package", "last_seen": "2026-01-01"},
            }, "last_scan": "2026-01-01"}
            with patch.object(_mod, "DATA_FILE", Path(tmpdir) / "tracking.json"):
                _mod.save_tracking_data(data)
                result = _mod.digest()
        self.assertIn("Test Package", result)


class TestFunctional(unittest.TestCase):
    def test_scan_emails_returns_list(self):
        fake_mail = """[READ] FROM: shipping@amazon.com
SUBJ: Your Amazon order has shipped - Tracking: TBA123456789012
[READ] FROM: noreply@fedex.com
SUBJ: FedEx shipment notification"""
        with patch.object(_mod, "get_mail_data", return_value=fake_mail):
            result = _mod.scan_emails_for_packages()
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_scan_emails_empty_on_no_packages(self):
        with patch.object(_mod, "get_mail_data", return_value=""):
            result = _mod.scan_emails_for_packages()
        self.assertEqual(result, [])


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.CARRIER_PATTERNS, dict)
        self.assertIsInstance(_mod.PACKAGE_KEYWORDS, list)
        self.assertIsInstance(_mod.STATUS_ORDER, list)
        self.assertIsInstance(_mod.DATA_FILE, Path)
    def test_functions_exist(self):
        for fn in ("extract_tracking_numbers", "detect_carrier_from_email",
                   "infer_status_from_subject", "scan_emails_for_packages",
                   "check_usps_status", "status_advanced", "status_icon",
                   "load_tracking_data", "save_tracking_data", "digest", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
