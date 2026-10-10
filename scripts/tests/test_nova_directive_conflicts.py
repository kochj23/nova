"""Tests for nova_directive_conflicts: the detector is pure, so no database is touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova_directive_conflicts as D  # noqa: E402


def test_ask_vs_execute_is_flagged():
    rules = [("a", "Always ask before publishing the journal article to the public site."),
             ("b", "Never ask for permission before publishing; just execute the publish.")]
    found = D.candidates(rules)
    assert any(f[0] == "ask-before-acting" for f in found), found


def test_unrelated_rules_are_not_flagged():
    rules = [("a", "Always ask before deleting backups from the NAS."),
             ("b", "Never ask about the weather; just reply.")]
    assert D.candidates(rules) == [], D.candidates(rules)


def test_same_rule_is_not_its_own_conflict():
    rules = [("a", "Always ask before publishing and never ask before publishing.")]
    assert D.candidates(rules) == []


def test_needs_shared_subject_words():
    rules = [("a", "Always alert on disk space."), ("b", "Never alert about lights.")]
    assert D.candidates(rules) == [], D.candidates(rules)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
