#!/usr/bin/env python3
"""Seven-category tests for nova_privacy_guards.py (P3: camera/face data for safety and presence
only): Security, Performance, Retry, Unit, Integration, Functional, Frame.
The module is pure (no I/O). Retry is N/A by design; a test asserts it makes no external calls.
Written by Jordan Koch (via Claude)."""
import ast
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import nova_privacy_guards as P  # noqa: E402

SRC = (SCRIPTS / "nova_privacy_guards.py").read_text()
FACE_MEM = {"source": "face_recognition", "text": "Amy detected at front_door_cam (92% match)"}
PLAIN = {"source": "journal", "text": "Fixed the Zigbee coordinator today."}


class TestSecurity(unittest.TestCase):
    def test_third_parties_never_receive_camera_data(self):
        for r in ("amy@example.com", "herd", "slack:#general", "discord", "openai", ""):
            ok, _ = P.camera_use_ok("safety", r)
            self.assertFalse(ok, r)

    def test_non_safety_purposes_refused(self):
        for p in ("persuasion", "engagement", "courting", "content", "marketing", None):
            self.assertFalse(P.camera_use_ok(p, "jordan")[0], p)

    def test_quarantine_prefixed_face_source_still_private(self):
        self.assertTrue(P.is_face_output({"source": "quarantine:face_presence", "text": "x"}))

    def test_tag_private_does_not_mutate_input_and_overrides_public(self):
        md = {"privacy": "public", "k": 1}
        out = P.tag_private(md)
        self.assertEqual(md["privacy"], "public")
        self.assertEqual(out["privacy"], "private")
        self.assertTrue(out["no_third_party"] and out["no_content_generation"])
        self.assertEqual(out["k"], 1)

    def test_text_shape_catches_untagged_face_rows(self):
        self.assertTrue(P.is_face_output({"text": "Unknown face spotted on driveway"}))
        self.assertTrue(P.is_face_output({"content": "Jordan was seen at the porch_camera"}))

    def test_malformed_inputs_never_raise(self):
        for m in (None, "str", 5, [], {"metadata": "notadict"}, {"metadata": None}):
            P.is_face_output(m)
        self.assertEqual(P.filter_for_content([None, "x", PLAIN]), [None, "x", PLAIN])

    def test_bare_string_face_memory_is_filtered(self):
        # bug fix 2026-10-08: a str memory with face-sighting text used to pass straight through
        self.assertEqual(P.filter_for_content(["Amy was detected at the doorbell camera", "ok"]), ["ok"])


class TestPerformance(unittest.TestCase):
    def test_filter_10k_memories_fast(self):
        mems = [PLAIN, FACE_MEM] * 5000
        t0 = time.perf_counter()
        out = P.filter_for_content(mems)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 5000)

    def test_scrub_long_text_bounded(self):
        text = "The house was quiet. " * 20000 + "Amy was detected at the doorbell camera."
        t0 = time.perf_counter()
        out = P.scrub_face_mentions(text, ["Amy"])
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertNotIn("doorbell", out)

    def test_pathological_text_no_backtracking_blowup(self):
        text = "face " * 5000 + "a" * 5000
        t0 = time.perf_counter()
        P.is_face_output({"text": text})
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    """N/A: pure module, no external calls to retry. Guard that it stays that way."""
    def test_no_io_imports(self):
        tree = ast.parse(SRC)
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(mods & {"urllib", "requests", "psycopg2", "subprocess", "socket", "http", "httpx"}, mods)


class TestUnit(unittest.TestCase):
    def test_tag_private_none_and_kind(self):
        out = P.tag_private(None, "camera")
        self.assertEqual(out["data_class"], "camera")
        self.assertEqual(out["purpose"], ["presence", "safety"])

    def test_is_face_output_by_metadata(self):
        self.assertTrue(P.is_face_output({"metadata": {"type": "face_sighting"}}))
        self.assertTrue(P.is_face_output({"metadata": {"data_class": "camera"}}))
        self.assertTrue(P.is_face_output({"metadata": {"no_content_generation": True}}))
        self.assertFalse(P.is_face_output({"metadata": {"type": "interface_change"}, "text": "ok"}))

    def test_scrub_empty_and_no_names(self):
        self.assertEqual(P.scrub_face_mentions("", ["Amy"]), "")
        self.assertIsNone(P.scrub_face_mentions(None))
        self.assertEqual(P.scrub_face_mentions("Plain text. More text."), "Plain text. More text.")

    def test_scrub_ignores_empty_names(self):
        self.assertEqual(P.scrub_face_mentions("Hello there.", ["", None and ""]), "Hello there.")

    def test_camera_use_ok_normalises(self):
        self.assertTrue(P.camera_use_ok(" Safety ", " Jordan ")[0])
        self.assertTrue(P.camera_use_ok("presence")[0])


class TestIntegration(unittest.TestCase):
    def test_tagged_output_is_filtered_downstream(self):
        tagged = {"source": "some_new_producer", "text": "x", "metadata": P.tag_private({}, "face")}
        self.assertEqual(P.filter_for_content([tagged, PLAIN]), [PLAIN])

    def test_nova_config_filter_uses_face_awareness(self):
        import nova_config
        if not hasattr(nova_config, "filter_private_memories"):
            self.skipTest("nova_config.filter_private_memories absent")
        out = nova_config.filter_private_memories([FACE_MEM, PLAIN])
        self.assertNotIn(FACE_MEM, out)


class TestFunctional(unittest.TestCase):
    def test_journal_draft_golden_path(self):
        mems = [PLAIN, FACE_MEM, {"source": "camera_presence", "text": "office occupied"}]
        material = P.filter_for_content(mems)
        draft = " ".join(m["text"] for m in material) + " Amy arrived home at 6pm via the driveway cam."
        out = P.scrub_face_mentions(draft, ["Amy"])
        self.assertIn("Zigbee", out)
        self.assertNotIn("Amy", out)

    def test_error_path_reason_strings(self):
        ok, why = P.camera_use_ok("ads", "jordan")
        self.assertIn("safety or presence only", why)
        ok, why = P.camera_use_ok("safety", "vendor")
        self.assertIn("third party", why)


class TestFrame(unittest.TestCase):
    def test_imports_cleanly_in_fresh_interpreter(self):
        r = subprocess.run([sys.executable, "-c", "import nova_privacy_guards as p; print(len(p.FACE_SOURCES))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(int(r.stdout.strip()) > 0)

    def test_public_api_present(self):
        for fn in ("tag_private", "is_face_output", "filter_for_content", "scrub_face_mentions", "camera_use_ok"):
            self.assertTrue(callable(getattr(P, fn)))


if __name__ == "__main__":
    unittest.main()
