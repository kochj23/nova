"""Tests for nova_liked_playlist.py — "5 stars - Youtube" (Jordan 2026-11-02)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_liked_playlist as p  # noqa: E402


def _rec(artist, releases, score=100):
    return {"score": score, "artist-credit": [{"name": artist}], "releases": releases}


def _rel(title, date, ptype="Album", sec=None, artist=None):
    r = {"title": title, "date": date, "status": "Official",
         "release-group": {"id": title, "primary-type": ptype, **({"secondary-types": sec} if sec else {})}}
    if artist:
        r["artist-credit"] = [{"name": artist}]
    return r


def test_tidy_strips_youtube_packaging():
    assert p.tidy("More Than This (Lyrics)") == "More Than This"
    assert p.tidy("It's My Life [Official 4K Video]") == "It's My Life"
    assert p.tidy("Allstars MIC | DnB Allstars") == "Allstars MIC"


def test_safe_name():
    assert p.safe_name('AC/DC - Back In Black') == "AC DC - Back In Black"
    assert p.safe_name('What? "Yes"') == "What Yes"


def test_pick_release_prefers_original_studio_album():
    recs = [_rec("Roxy Music", [_rel("Hits Zum Abfahren", "1990", sec=["Compilation"]),
                                _rel("Now 80s", "1985", artist="Various Artists")]),
            _rec("Roxy Music", [_rel("Avalon", "2003-07-04"), _rel("Avalon", "1982-05-28")]),
            _rec("Roxy Music", [_rel("Live at Wembley", "1983", sec=["Live"])])]
    credit, rel = p.pick_release(recs, "Roxy Music")
    assert rel["title"] == "Avalon" and rel["date"] == "1982-05-28"


def test_pick_release_falls_back_to_single_and_rejects_other_artists():
    assert p.pick_release([_rec("Somebody Else", [_rel("X", "2000")])], "Cutty Ranks") is None
    credit, rel = p.pick_release([_rec("Cutty Ranks", [_rel("A Who Say Me Dun", "1992", ptype="Single")])], "Cutty Ranks")
    assert rel["title"] == "A Who Say Me Dun"


def test_tags_for_uses_youtube_music_metadata_first():
    t = p.tags_for({"artist": "Talk Talk", "track": "It's My Life", "album": "It's My Life", "release_year": 1984,
                    "title": "Talk Talk - It's My Life (Official Video)"}, {"kind": "song"})
    assert (t["artist"], t["title"], t["album"], t["year"]) == ("Talk Talk", "It's My Life", "It's My Life", 1984)
