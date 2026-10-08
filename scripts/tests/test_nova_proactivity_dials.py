#!/usr/bin/env python3
"""The proactivity / verbosity dials keep today's behaviour at their defaults, and move it when
turned. Each module is re-imported under a patched nova_voice.dials(). Written by Jordan Koch
(via Claude)."""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_voice  # noqa: E402

ENV = ("NOVA_REACH_DAILY_CAP", "NOVA_REACH_DIRECT_COOLDOWN_H", "NOVA_REACH_THRESHOLD")


def _load(name, **dial):
    vals = {**nova_voice.DIAL_DEFAULTS, **dial}
    with mock.patch.object(nova_voice, "dials", return_value=vals), \
         mock.patch.dict(os.environ, {k: "" for k in ENV}):
        sys.modules.pop(name, None)
        return importlib.import_module(name)


class TestDefaultsUnchanged(unittest.TestCase):
    def test_reach(self):
        r = _load("nova_reach")
        self.assertEqual((r.CARE_THRESHOLD, r.DAILY_CAP, r.DIRECT_COOLDOWN_HOURS), (0.6, 1, 6))

    def test_notify(self):
        self.assertEqual(_load("nova_notify_jordan").MAX_ITEMS, 6)

    def test_slack_answers(self):
        self.assertEqual(_load("nova_slack_answers").PROPOSALS_PER_DAY, 3)

    def test_aspirations(self):
        a = _load("nova_aspirations")
        self.assertEqual((a.MAX_OPEN_WISHES, a.WISH_COOLDOWN_HRS), (6, 8))

    def test_journal_lengths(self):
        with mock.patch.object(nova_voice, "dials", return_value=dict(nova_voice.DIAL_DEFAULTS)):
            import nova_journal as nj
            self.assertEqual(nj.article_length("essay"), (2500, 4000, "grounded"))


class TestDialsMove(unittest.TestCase):
    def test_low_proactivity_is_quieter(self):
        r = _load("nova_reach", proactivity=0)
        self.assertEqual((r.CARE_THRESHOLD, r.DAILY_CAP, r.DIRECT_COOLDOWN_HOURS), (0.9, 0, 24))
        self.assertEqual(_load("nova_notify_jordan", proactivity=0).MAX_ITEMS, 2)
        self.assertEqual(_load("nova_slack_answers", proactivity=0).PROPOSALS_PER_DAY, 1)

    def test_high_proactivity_is_chattier(self):
        r = _load("nova_reach", proactivity=100)
        self.assertAlmostEqual(r.CARE_THRESHOLD, 0.3)
        self.assertEqual(r.DAILY_CAP, 3)
        self.assertEqual(_load("nova_aspirations", proactivity=100).WISH_COOLDOWN_HRS, 4)

    def test_verbosity_scales_journal(self):
        import nova_journal as nj
        with mock.patch.object(nova_voice, "dials", return_value={**nova_voice.DIAL_DEFAULTS, "verbosity": 0}):
            self.assertEqual(nj.article_length("essay"), (1500, 2400, "grounded"))

    @classmethod
    def tearDownClass(cls):
        for m in ("nova_reach", "nova_notify_jordan", "nova_slack_answers", "nova_aspirations"):
            sys.modules.pop(m, None)


if __name__ == "__main__":
    unittest.main()
