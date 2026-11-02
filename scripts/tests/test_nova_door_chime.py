"""Tests for nova_door_chime.py — pure logic only (no Protect, no AirPlay, no Slack)."""
import importlib.util
import sys
import tempfile
import unittest
import wave
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "nova_door_chime.py"
spec = importlib.util.spec_from_file_location("nova_door_chime_t", SCRIPT)
dc = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPT.parent))
spec.loader.exec_module(dc)


class TestClassify(unittest.TestCase):
    F = {"cam"}

    def test_ring_person_package(self):
        self.assertEqual(dc.classify({"camera": "cam", "type": "ring"}, self.F), "ring")
        self.assertEqual(dc.classify({"camera": "cam", "smartDetectTypes": ["person"]}, self.F), "person")
        self.assertEqual(dc.classify({"camera": "cam", "smartDetectTypes": ["person", "package"]}, self.F), "package")

    def test_ignores_vehicles_motion_and_other_cameras(self):
        self.assertIsNone(dc.classify({"camera": "cam", "smartDetectTypes": ["vehicle"]}, self.F))
        self.assertIsNone(dc.classify({"camera": "cam", "type": "motion", "smartDetectTypes": []}, self.F))
        self.assertIsNone(dc.classify({"camera": "backyard", "type": "ring"}, self.F))


class TestGates(unittest.TestCase):
    def test_quiet_window_wraps_midnight(self):
        q = {"start": 22, "end": 7}
        self.assertTrue(all(dc.in_quiet(h, q) for h in (22, 23, 0, 3, 6)))
        self.assertFalse(any(dc.in_quiet(h, q) for h in (7, 12, 21)))

    def test_cooldown_but_ring_always(self):
        self.assertFalse(dc.should_alert("person", 1000, 950, 120))
        self.assertTrue(dc.should_alert("person", 1000, 800, 120))
        self.assertTrue(dc.should_alert("ring", 1000, 999, 120))


class TestSound(unittest.TestCase):
    def test_tone_is_soft_and_short(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.wav"
            dc.make_chime(p)
            with wave.open(str(p)) as w:
                self.assertEqual((w.getnchannels(), w.getframerate()), (2, 44100))
                secs = w.getnframes() / w.getframerate()
                import array
                a = array.array("h", w.readframes(w.getnframes()))
            self.assertLess(secs, 3)
            self.assertLess(max(abs(x) for x in a), 0.55 * 32767)   # never full-scale

    def test_every_kind_has_an_announcement_addressed_to_jordan(self):
        for k in ("person", "package", "ring"):
            self.assertIn("Little Mister", dc.ANNOUNCE[k])


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_exterior_only(self):
        src = SCRIPT.read_text()
        self.assertNotRegex(src, r"(?i)(password|token)\s*=\s*['\"][^'\"]{8,}")
        self.assertTrue(all(c.startswith("Exterior") for c in dc.FRONT))


if __name__ == "__main__":
    unittest.main()
