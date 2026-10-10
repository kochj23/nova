"""Tests for nova_action_why: formatting is pure, so no database is touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova_action_why as W  # noqa: E402


def test_explain_shows_recorded_rationale():
    out = W.explain({"ts": "2026-10-09 13:00:00", "description": "restarted scheduler", "target": "scheduler",
                     "outcome": "success", "rationale": "config reload failed"})
    assert "why: config reload failed" in out and "on scheduler" in out and "(success)" in out


def test_explain_says_so_when_rationale_missing():
    out = W.explain({"ts": "2026-10-09 13:00:00", "description": "ran a command", "rationale": "  "})
    assert W.NO_RATIONALE in out, out


def test_summary_counts_missing_reasons():
    rows = [{"rationale": "x"}, {"rationale": None}, {"rationale": ""}]
    assert W.summarise(rows) == "3 actions shown, 2 with no recorded rationale"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
