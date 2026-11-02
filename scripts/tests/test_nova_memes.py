"""Tests for nova_memes.py — Memes and Friends (privacy + public-only rules, Jordan 2026-11-02)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_memes as nm  # noqa: E402


def test_selftest():
    assert nm.selftest() == 0


def test_only_meme_platforms():
    assert nm.key_for("https://www.instagram.com/reel/ABCdef123/?igsh=1")[0] == "ig"
    assert nm.key_for("https://crutchfield.com/p/x") is None
    assert nm.key_for("https://votenoon40.org/") is None


def test_window_rules():
    assert nm.window(None, None) == (0, nm.STILL_S)
    assert nm.window(60, None) == (0, 60)
    assert nm.window(61, 10) == (10, 10 + nm.CLIP_S)


def _meme(**text):
    return {"key": "meme-ig-X", "platform_name": "Instagram", "creator": "@acct", "reach": "5,000 likes",
            "source": "https://www.instagram.com/p/X/", "jpg": "x.jpg", "a": 0, "b": 6,
            "text": {"title": "T", "what": "w", "analysis": ["a1"], "rating": 5, "rating_line": "r", **text}}


def test_compose_scrubs_contact_details():
    body, _ = nm.compose([_meme(what="Text me at (818) 555-1212 or bob@example.com")], "today")
    assert "555-1212" not in body and "bob@example.com" not in body


def test_compose_never_names_chat():
    body, desc = nm.compose([_meme()], "in the last 24 hours")
    assert "sent by" not in body.lower() and "@acct on Instagram" in body


def test_meme_before_analysis():
    body, _ = nm.compose([_meme(analysis=["ANALYSIS"])], "today")
    assert body.index("{{< meme") < body.index("ANALYSIS")


def test_ig_parse_private_post_is_empty():
    e = nm.parse_ig_embed("<html><body>This content isn't available</body></html>")
    assert not e["creator"] and not e["image"] and e["likes"] == 0
