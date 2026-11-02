"""Tests for nova_top_lessons.py (daily 06:00 top-10 lessons article). Pure logic only; no DB, no model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_top_lessons as t  # noqa: E402


def test_selftest_passes():
    assert t.selftest() == 0


def test_scrub_hides_internal_addresses_hosts_and_paths():
    out = t.scrub("pg on nova-core1 at 192.168.1.2:5434 wrote /Users/someone/logs/x.log")
    assert "192.168" not in out and "nova-core1" not in out and "/Users" not in out


def test_pq_notice_is_not_an_error_but_a_real_line_is():
    tail = ("** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            "** This session may be vulnerable to \"store now, decrypt later\" attacks.\n")
    assert t.error_gist(tail) == ""
    assert t.error_gist(tail + "psycopg2.OperationalError: timed out\n").startswith("psycopg2.OperationalError")


def test_noise_rule_overrides_a_booked_belief():
    c = {"origin": "ops", "cause": [f"Scheduled job `nas` failed 20 of 64 finished runs — {t.NO_ERROR}"]}
    assert t.unsupported(c)
    assert t.combine(("Yes", "booked as belief #1"), None, t.unsupported(c))[0] == "Questionable"


def test_review_disagreement_is_visible():
    assert t.combine(("Yes", "booked"), {"belief": False, "why": "fiction"})[0] == "Questionable"
    assert t.combine(("No", "scores 0.5"), {"belief": True, "why": "sound"})[0] == "No, but close"
    assert t.combine(("Yes", "booked"), {"belief": True, "why": "sound"}) == ("Yes", "sound (pipeline: booked)")


def test_fold_merges_paraphrases_and_keeps_best_first():
    f = t.fold([{"stance": "B.", "score": 0.7, "embedding": [0, 1], "evidence": []},
                {"stance": "A.", "score": 0.9, "embedding": [1, 0], "evidence": ["a"]},
                {"stance": "A again.", "score": 0.8, "embedding": [0.995, 0.05], "evidence": ["b"]}])
    assert [x["stance"] for x in f] == ["A.", "B."] and f[0]["seen"] == 2


def test_review_prompt_differs_by_origin():
    ops = t.review_prompt({"origin": "ops", "stance": "s", "cause": ["c"]})
    work = t.review_prompt({"origin": "work_lessons", "stance": "s", "cause": ["c"]})
    assert "OPERATIONS" in ops and "READ" in work and "JSON" in ops and "JSON" in work
