#!/usr/bin/env python3
"""Tests for nova_sdr_actuator_mac.py (2026-10-30): launchd plist rendering for nova_fm_capture and the
heal order that never restarts the SDRplay API under an open stream."""
import plistlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_sdr_actuator_mac as m  # noqa: E402

META = {"serial": "24051E8734", "outdir": "/Users/kochj23/nrsp_calls"}


def tgt(label, hz, demod="AM"):
    return {"id": label, "label": label, "freq_hz": hz, "demod": demod, "meta": {}}


class TestCaptureArgs(unittest.TestCase):
    def test_no_target_means_stop(self):
        self.assertIsNone(m.capture_args("fm_dt", [("1", None, META), ("2", None, META)]))

    def test_dual_tuner_orders_by_port(self):
        args = m.capture_args("fm_dt", [("2", tgt("BUR Ground", 123900000), META),
                                        ("1", tgt("BUR Interop", 155760000, "NFM"), META)])
        self.assertEqual(args[2:4], ["24051E8734", "/Users/kochj23/nrsp_calls"])
        self.assertEqual(args[4:], ["BUR_Interop:155760000:1:NFM", "BUR_Ground:123900000:2:AM"])

    def test_single_dt_channel_names_the_tuner_port(self):
        args = m.capture_args("fm_dt", [("1", None, META), ("2", tgt("x", 118700000), META)])
        self.assertEqual(args[-1], "x:118700000:Tuner 2 50 ohm:AM")

    def test_labels_are_cleaned(self):
        args = m.capture_args("fm_master", [("Tuner 1 50 ohm", tgt('a:b "c"', 1), META)])
        self.assertEqual(args[-1], "ab_c:1:Tuner 1 50 ohm:AM")


class TestPlist(unittest.TestCase):
    def test_plist_is_stable_and_keepalive(self):
        args = ["py", "cap.py", "s", "o", "x:1:1:AM"]
        p1, p2 = m.plist_for("nova-fm-duo3", args), m.plist_for("nova-fm-duo3", args)
        self.assertEqual(p1, p2)                  # byte-identical -> no reload churn every minute
        d = plistlib.loads(p1)
        self.assertEqual(d["Label"], "net.digitalnoise.nova-fm-duo3")
        self.assertTrue(d["KeepAlive"])
        self.assertEqual(d["ProgramArguments"], args)
        self.assertEqual(d["EnvironmentVariables"]["NOVA_SDR_STATS_DIR"], m.STATS_DIR)


class TestHeal(unittest.TestCase):
    def test_capture_out_before_api_and_never_kickstart(self):
        calls = []
        with patch.object(m, "sh", side_effect=lambda *c: calls.append(c) or ""), \
             patch.object(m.time, "sleep"), patch.object(m.os.path, "exists", return_value=True):
            m.heal(["nova-fm-duo3"], 501)
        flat = [" ".join(c) for c in calls]
        cap_out = flat.index("launchctl bootout gui/501/net.digitalnoise.nova-fm-duo3")
        api_out = flat.index("sudo -n launchctl bootout system/com.sdrplay.service")
        api_in = next(i for i, c in enumerate(flat) if c.startswith("sudo -n launchctl bootstrap system"))
        cap_in = next(i for i, c in enumerate(flat) if c.startswith("launchctl bootstrap gui/501"))
        self.assertLess(cap_out, api_out)
        self.assertLess(api_out, api_in)
        self.assertLess(api_in, cap_in)
        self.assertFalse(any("kickstart" in c or "pkill" in c for c in flat))


if __name__ == "__main__":
    unittest.main()
