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
    CAMS = {"front_door": "Exterior - Front Right"}

    def ev(self, h, cam="front_door", label="person"):
        return {"camera": cam, "label": label, "data": {"box": [0.5, 0.3, 0.1, h]}}

    def test_person_close_to_the_door_alerts(self):
        self.assertEqual(dc.classify(self.ev(0.55), self.CAMS, 0.4), "person")

    def test_far_sidewalk_passer_by_does_not(self):
        # measured 2026-11-02: sidewalk walkers are 0.23-0.26 of the frame tall
        self.assertIsNone(dc.classify(self.ev(0.26), self.CAMS, 0.4))

    def test_other_cameras_and_labels_ignored(self):
        self.assertIsNone(dc.classify(self.ev(0.9, cam="exterior_front_right"), self.CAMS, 0.4))  # = Interior - Garage
        self.assertIsNone(dc.classify(self.ev(0.9, label="dog"), self.CAMS, 0.4))
        self.assertIsNone(dc.classify({"camera": "front_door", "label": "person", "data": {}}, self.CAMS, 0.4))


class TestGates(unittest.TestCase):
    def test_quiet_window_wraps_midnight(self):
        q = {"start": 22, "end": 7}
        self.assertTrue(all(dc.in_quiet(h, q) for h in (22, 23, 0, 3, 6)))
        self.assertFalse(any(dc.in_quiet(h, q) for h in (7, 12, 21)))

    def test_cooldown(self):
        self.assertFalse(dc.should_alert("person", 1000, 950, 120))
        self.assertTrue(dc.should_alert("person", 1000, 800, 120))


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
        # Frigate's exterior_front_right is really the Interior - Garage camera: never watched
        self.assertNotIn("exterior_front_right", dc.DEFAULTS["cameras"])
        self.assertTrue(all(v.startswith("Exterior") for v in dc.DEFAULTS["cameras"].values()))


if __name__ == "__main__":
    unittest.main()
