"""Tests for nova_whole_picture: the digest formatting is pure, so no database is touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova_whole_picture as W  # noqa: E402


def test_digest_names_conflicts_and_gaps():
    lines = W.digest_lines([("rule a", "rule b", "they disagree")], "all rooms reporting", (10, 4), 0)
    text = "\n".join(lines)
    assert "Rule conflicts to review: 1" in text and "rule a <-> rule b: they disagree" in text
    assert "10, of which 4 have no recorded rationale" in text


def test_digest_says_none_when_clear():
    text = "\n".join(W.digest_lines([], "all rooms reporting", (0, 0), 0))
    assert "Rule conflicts to review: none" in text and "Actions in window: none" in text


def test_digest_reports_failed_checks_separately():
    text = "\n".join(W.digest_lines([], "all rooms reporting", (0, 0), 3))
    assert "Checks that failed (not counted as conflicts): 3" in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
