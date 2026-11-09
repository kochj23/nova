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


class TestMailbox(unittest.TestCase):
    Z = dc.DEFAULTS["zones"]
    CAMS = dc.DEFAULTS["cameras"]

    def ev(self, box):
        return {"camera": "front_door", "label": "person", "data": {"box": box}}

    def test_carrier_at_mailbox_alerts_even_if_small(self):
        self.assertEqual(dc.classify(self.ev([0.68, 0.2, 0.08, 0.3]), self.CAMS, 0.4, self.Z), "person")

    def test_far_sidewalk_behind_mailbox_does_not(self):
        # real 2026-11-02 09:28 sidewalk walker
        self.assertIsNone(dc.classify(self.ev([0.59, 0.42, 0.06, 0.26]), self.CAMS, 0.4, self.Z))


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


class TestSpeakers(unittest.TestCase):
    def test_only_the_quiet_rooms(self):
        # Jordan 2026-11-09: dogs -> garage, outdoor, master bath, guest bath only
        names = {s["name"] for s in dc.DEFAULTS["speakers"]}
        self.assertEqual(names, {"GarPod", "Outpod", "MBath", "GBath"})
        self.assertTrue(all(s["entity"].startswith("media_player.") for s in dc.DEFAULTS["speakers"]))

    def test_chime_restores_each_speakers_volume(self):
        from unittest import mock
        calls = []
        state = {"media_player.garpod": 0.84, "media_player.gbath": None}
        def fake_ha(path, body=None):
            calls.append((path, body))
            if body is None:
                return {"attributes": {"volume_level": state[path.rsplit("/", 1)[1]]}}
        async def fake_all(speakers, volume, path):
            return [s["name"] for s in speakers], []
        cfg = {"speakers": [{"name": "GarPod", "entity": "media_player.garpod"},
                            {"name": "GBath", "entity": "media_player.gbath"}], "volume": 50}
        with mock.patch.object(dc, "_ha", fake_ha), mock.patch.object(dc, "_chime_all", fake_all), \
             mock.patch.object(dc, "clip", lambda k: SCRIPT):
            ok, bad = dc.chime(cfg)
        self.assertEqual(ok, ["GarPod", "GBath"])
        sets = [b for p, b in calls if b]
        # known level restored; unknown (None) left alone rather than guessed
        self.assertEqual(sets, [{"entity_id": "media_player.garpod", "volume_level": 0.84}])

    def test_restore_runs_even_if_the_stream_fails(self):
        from unittest import mock
        sets = []
        async def boom(*a):
            raise RuntimeError("airplay down")
        with mock.patch.object(dc, "_ha", lambda p, b=None: sets.append(b) if b else {"attributes": {"volume_level": 0.5}}), \
             mock.patch.object(dc, "_chime_all", boom), mock.patch.object(dc, "clip", lambda k: SCRIPT):
            with self.assertRaises(RuntimeError):
                dc.chime({"speakers": [{"name": "MBath", "entity": "media_player.mbath"}], "volume": 50})
        self.assertEqual(sets, [{"entity_id": "media_player.mbath", "volume_level": 0.5}])


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_exterior_only(self):
        src = SCRIPT.read_text()
        self.assertNotRegex(src, r"(?i)(password|token)\s*=\s*['\"][^'\"]{8,}")
        # Frigate's exterior_front_right is really the Interior - Garage camera: never watched
        self.assertNotIn("exterior_front_right", dc.DEFAULTS["cameras"])
        self.assertTrue(all(v.startswith("Exterior") for v in dc.DEFAULTS["cameras"].values()))


if __name__ == "__main__":
    unittest.main()
