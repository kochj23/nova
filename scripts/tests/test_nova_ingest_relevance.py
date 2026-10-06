"""page_relevance_check: short seed topics (2026-10-06 — the 'Car' crawl stored nothing)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_ingest as ni  # noqa: E402

CAR = "A car, or an automobile, is a motor vehicle with wheels. Most definitions of cars state that they run primarily on roads. " * 5


def test_three_letter_seed_matches_whole_word():
    assert ni.page_relevance_check(CAR, "Car", "automotive")


def test_three_letter_seed_does_not_match_inside_words():
    assert not ni.page_relevance_check("A woolen scarf and a scary scarecrow sat in the barn by the river. " * 10, "Car", "automotive")


def test_long_seed_behaviour_unchanged():
    assert ni.page_relevance_check("Zigbee is a low-power mesh networking standard used in home devices. " * 5, "Zigbee", "home_automation")
    assert not ni.page_relevance_check("The opera opened in Vienna to great acclaim from the critics present. " * 5, "Zigbee", "home_automation")
