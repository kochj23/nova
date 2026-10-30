#!/usr/bin/env python3
"""Tests for nova_sdr_actuator.py deep health (plan #6, 2026-10-15): stream heartbeat + live SNR,
output rate vs the channel's own history, passive SDRplay API liveness, and how they fold into ok/sick."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_sdr_actuator as a  # noqa: E402

NOW = 1_800_000_000.0


class TestStream(unittest.TestCase):
    def _st(self, **kw):
        d = tempfile.mkdtemp()
        st = {"ts": NOW - 10, "samples": 1000, "snr_db": 21.5}
        st.update(kw)
        json.dump(st, open(os.path.join(d, "BUR_Tower.json"), "w"))
        return d

    def test_label_maps_like_the_capture_args(self):
        self.assertEqual(a.lab('BUR "Tower": x'), "BUR_Tower_x")

    def test_flowing(self):
        ok, detail, snr = a.stream_health("BUR Tower", NOW, self._st())
        self.assertTrue(ok); self.assertEqual(snr, 21.5); self.assertIn("SNR 21.5", detail)

    def test_zero_samples_is_a_stalled_stream(self):
        ok, detail, _ = a.stream_health("BUR Tower", NOW, self._st(samples=0))
        self.assertFalse(ok); self.assertIn("stalled", detail)

    def test_stale_heartbeat_is_hung(self):
        ok, detail, _ = a.stream_health("BUR Tower", NOW, self._st(ts=NOW - 600))
        self.assertFalse(ok); self.assertIn("stale", detail)

    def test_missing_file_is_not_judged(self):
        self.assertEqual(a.stream_health("Nope", NOW, tempfile.mkdtemp()), (None, "", None))


class TestOutput(unittest.TestCase):
    def test_busy_channel_silent_for_hours_is_sick(self):
        # 819 calls over ~24 h, silent 7 h (LAPD North Hollywood on 2026-10-15)
        rates = {"LAPD North Hollywood": (819, 0, NOW - 7 * 3600, NOW - 31 * 3600)}
        ok, detail, n1h = a.output_health("LAPD North Hollywood", rates, NOW - 86400, NOW)
        self.assertFalse(ok); self.assertIn("no output for 7.0 h", detail); self.assertEqual(n1h, 0)

    def test_quiet_channel_is_never_judged_sick(self):
        rates = {"KBUR Ground": (30, 0, NOW - 10 * 3600, NOW - 6 * 86400)}
        self.assertIsNot(a.output_health("KBUR Ground", rates, None, NOW)[0], False)

    def test_short_silence_on_busy_channel_is_fine(self):
        rates = {"LAPD Northeast": (12000, 70, NOW - 600, NOW - 7 * 86400)}
        self.assertTrue(a.output_health("LAPD Northeast", rates, None, NOW)[0])

    def test_silence_counts_from_a_recent_reassignment(self):
        rates = {"X": (5000, 0, NOW - 10 * 3600, NOW - 7 * 86400)}
        self.assertTrue(a.output_health("X", rates, NOW - 600, NOW)[0])

    def test_unknown_channel_not_judged(self):
        self.assertEqual(a.output_health("new", {}, None, NOW), (None, "", None))

    def test_memory_db_down_is_empty_not_fatal(self):
        with patch("psycopg2.connect", side_effect=RuntimeError("down")):
            self.assertEqual(a.output_rates("dbname=nova_memories"), {})


class TestApi(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(a.api_health(journal="", ps="Ssl", active="active"), (True, "API responding"))

    def test_service_down_or_missing(self):
        self.assertFalse(a.api_health(journal="", ps="", active="failed")[0])
        self.assertFalse(a.api_health(journal="", ps="", active="active")[0])
        self.assertFalse(a.api_health(journal="", ps="Zs", active="active")[0])

    def test_wedge_signatures(self):
        self.assertFalse(a.api_health(journal="sdrplay_api_Open: ServiceNotResponding", ps="Ssl", active="active")[0])
        own = "[sdr-actuator] sdrplay api: SICK API error: [ERROR] ApiVersion Error: sdrplay_api_ServiceNotResponding"
        self.assertTrue(a.api_health(journal=own, ps="Ssl", active="active")[0])   # our own verdict is not evidence
        seg = "sdrplay_apiServ[1]: segfault at 7dcb ip 5a2a sp 7dcb error 4 in sdrplay_apiService[18a6a,5a2a+4a000]"
        self.assertFalse(a.api_health(journal=seg, ps="Ssl", active="active")[0])

    def test_submiturb_needs_a_burst(self):
        one = "libusb: error [submit_iso_transfer] submiturb failed, errno=12"
        self.assertTrue(a.api_health(journal=one, ps="Ssl", active="active")[0])
        self.assertFalse(a.api_health(journal="\n".join([one] * 3), ps="Ssl", active="active")[0])


class TestCombine(unittest.TestCase):
    def test_any_judged_sick_makes_sick_unknown_is_ignored(self):
        self.assertEqual(a.combine((True, "capture opened"), (None, ""), (True, "3 calls/h")),
                         (True, "capture opened; 3 calls/h"))
        ok, detail = a.combine((True, "bridge listening on :7355"), (None, ""), (False, "no output for 7.3 h"))
        self.assertFalse(ok); self.assertEqual(detail, "bridge listening on :7355; no output for 7.3 h")

    def test_unit_down_stays_down(self):
        self.assertFalse(a.combine((False, "unit failed"), (True, "IQ flowing"))[0])


class TestPerformance(unittest.TestCase):
    def test_health_math_is_cheap(self):
        rates = {f"c{i}": (1000, 5, NOW - 100, NOW - 86400) for i in range(1000)}
        t = time.perf_counter()
        for i in range(1000):
            a.output_health(f"c{i}", rates, None, NOW)
        self.assertLess(time.perf_counter() - t, 0.5)


if __name__ == "__main__":
    unittest.main()
