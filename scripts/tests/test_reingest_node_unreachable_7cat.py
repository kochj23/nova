#!/usr/bin/env python3
"""7-category tests for the 2026-10-20 re-ingest fix: nova-core7 heartbeated over Wi-Fi while its registered
Ethernet IP was dead, so every file sent to it failed in seconds (ssh 255) and 5,553 good files spent all
their attempts and landed in 'error'. Now an unreachable minion raises NodeUnreachable: the file goes back to
pending without spending an attempt and that node's loop stops. Reuses the Popen fake from
test_nova_media_reingest. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_reingest_node_unreachable_7cat.py
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_nova_media_reingest import EP, _Popen, mr  # noqa: E402


def _popen(dst_rc):
    return lambda cmd, **kw: _Popen(cmd, rc=dst_rc if "cat >" in cmd[-1] else 0, **kw)


def _do_item(exc):
    stats, calls = {"error": 0}, []
    with mock.patch.object(mr, "process", side_effect=exc), \
            mock.patch.object(mr, "finish", side_effect=lambda oc, fp, st, **kw: calls.append((st, kw))):
        oc = mock.MagicMock()
        oc.fetchone.return_value = (0,)
        cont = mr.do_item(oc, None, (EP, "Show", "Title"), 60, stats, tag="[nova-core7]")
    return cont, calls, stats


class TestSecurity(unittest.TestCase):
    def test_error_text_carries_no_command_line(self):
        with self.assertRaises(mr.NodeUnreachable) as cm:
            mr.stream_pcm(EP, "kochj@192.168.1.125", "x.pcm", 60, _popen=_popen(255))
        self.assertNotIn("ffmpeg", str(cm.exception))


class TestPerformance(unittest.TestCase):
    def test_classification_is_instant(self):
        t = time.time()
        for _ in range(500):
            try:
                mr.stream_pcm(EP, "h", "x.pcm", 60, _popen=_popen(255))
            except mr.NodeUnreachable:
                pass
        self.assertLess(time.time() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_unreachable_node_spends_no_attempt(self):
        cont, calls, stats = _do_item(mr.NodeUnreachable("minion h unreachable (ssh 255)"))
        self.assertEqual(calls[0][0], "pending")
        self.assertNotIn("attempts", calls[0][1])
        self.assertEqual(stats["error"], 0)


class TestUnit(unittest.TestCase):
    def test_255_from_the_copy_is_unreachable_other_failures_are_not(self):
        with self.assertRaises(mr.NodeUnreachable):
            mr.stream_pcm(EP, "h", "x.pcm", 60, _popen=_popen(255))
        with self.assertRaises(RuntimeError) as cm:
            mr.stream_pcm(EP, "h", "x.pcm", 60, _popen=_popen(1))
        self.assertNotIsInstance(cm.exception, mr.NodeUnreachable)


class TestIntegration(unittest.TestCase):
    def test_node_loop_stops_on_unreachable(self):
        cont, _, _ = _do_item(mr.NodeUnreachable("x"))
        self.assertFalse(cont)


class TestFunctional(unittest.TestCase):
    def test_a_real_file_error_still_counts_an_attempt(self):
        cont, calls, stats = _do_item(RuntimeError("audio extraction on nova-core failed (ffmpeg 1, copy 0): bad"))
        self.assertTrue(cont)
        self.assertEqual(calls[0][1]["attempts"], 1)
        self.assertEqual(stats["error"], 1)


class TestFrame(unittest.TestCase):
    def test_unreachable_is_a_runtime_error_subclass(self):
        self.assertTrue(issubclass(mr.NodeUnreachable, RuntimeError))   # older broad handlers still catch it


if __name__ == "__main__":
    unittest.main()
