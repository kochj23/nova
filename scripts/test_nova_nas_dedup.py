#!/usr/bin/env python3
"""Tests for nova_nas_dedup.py — focus on the SAFETY guarantees, since this tool
deletes files and must NEVER delete from the Google-synced folders."""
import importlib

import pytest

d = importlib.import_module("nova_nas_dedup")

GD = "/Volumes/NAS/GoogleDriveBackups"
GD2 = "/Volumes/NAS/Google-Drive-kochjpar"


# ── pure helpers ──────────────────────────────────────────────────────────────
def test_protected_matches_both_google_folders():
    assert d.protected(f"{GD}/mail/a.pst")
    assert d.protected(f"{GD2}/x/y.mbox")
    assert not d.protected("/Volumes/NAS/mail/a.pst")
    assert not d.protected("/Volumes/NAS/GoogleStuffNope/a.pst")  # substring, not a path segment


def test_itunes_is_never_deletable():
    # Hard rule (Jordan, 2026-06-23): NEVER delete anything under /Volumes/NAS/iTunes.
    assert d.protected("/Volumes/NAS/iTunes/Music/Artist/track.mp3")
    assert d.protected("/Volumes/NAS/iTunes/anything/at/all.m4a")
    assert d.protected("/Volumes/NAS/iTunes")  # the dir itself
    assert "/Volumes/NAS/iTunes" in d.NEVER_DELETE_PREFIXES


def test_keeper_prefers_non_copy_name():
    paths = ["/Volumes/NAS/docs/report copy.pdf", "/Volumes/NAS/docs/report.pdf"]
    assert d._keeper(paths, [1.0, 2.0]) == "/Volumes/NAS/docs/report.pdf"


# ── propose(): the core safety net ───────────────────────────────────────────
class FakeCursor:
    def __init__(self, sets): self._sets = sets; self.inserts = []
    def execute(self, q, p=None):
        if p and "INSERT INTO nas_dedup_proposals" in q:
            self.inserts.append(p)            # (path, keeper, hash, size, set_size, status)
    def fetchall(self): return self._sets
    def fetchone(self): return (0, 0)
    def __enter__(self): return self
    def __exit__(self, *a): pass


class FakeConn:
    autocommit = True
    def __init__(self, sets): self.cur = FakeCursor(sets)
    def cursor(self): return self.cur
    def close(self): pass


def _run_propose(monkeypatch, sets):
    monkeypatch.setattr(d, "notify", None)
    conn = FakeConn(sets)
    monkeypatch.setattr(d.psycopg2, "connect", lambda dsn: conn)
    d.propose()
    return conn.cur.inserts


def test_propose_never_targets_a_protected_path(monkeypatch):
    # dup spanning Google + elsewhere
    sets = [("h1", [f"{GD}/a.pst", "/Volumes/NAS/mail/a.pst"], [1.0, 2.0], 100, 2)]
    inserts = _run_propose(monkeypatch, sets)
    deletes = [i[0] for i in inserts]
    assert all(not d.protected(p) for p in deletes)        # NEVER delete a Google file
    assert "/Volumes/NAS/mail/a.pst" in deletes            # the non-Google copy IS proposed
    assert all(i[5] == "proposed" for i in inserts)


def test_propose_all_google_is_manual_review_not_deletion(monkeypatch):
    sets = [("h2", [f"{GD}/a.pst", f"{GD}/backup/a.pst"], [1.0, 2.0], 100, 2)]
    inserts = _run_propose(monkeypatch, sets)
    assert inserts, "should record the Google-internal dup"
    assert all(i[5] == "manual_review" for i in inserts)   # flagged, never 'proposed'
    assert not any(i[5] == "proposed" for i in inserts)


def test_propose_normal_dup_keeps_canonical(monkeypatch):
    sets = [("h3", ["/Volumes/NAS/x/a copy.pst", "/Volumes/NAS/x/a.pst"], [1.0, 2.0], 100, 2)]
    inserts = _run_propose(monkeypatch, sets)
    assert len(inserts) == 1
    assert inserts[0][0] == "/Volumes/NAS/x/a copy.pst"    # delete the copy
    assert inserts[0][1] == "/Volumes/NAS/x/a.pst"         # keep the canonical
    assert inserts[0][5] == "proposed"


# ── apply(): belt-and-suspenders ─────────────────────────────────────────────
def test_apply_skips_protected_even_if_marked_approved(monkeypatch):
    monkeypatch.setattr(d, "notify", None)
    removed = []
    monkeypatch.setattr(d.os, "remove", lambda p: removed.append(p))

    class ApplyCursor(FakeCursor):
        def fetchall(self): return [(f"{GD}/should-never-delete.pst", 100)]
    class ApplyConn(FakeConn):
        def __init__(self): self.cur = ApplyCursor([])
    monkeypatch.setattr(d.psycopg2, "connect", lambda dsn: ApplyConn())
    d.apply()
    assert removed == []                                   # a protected path is NEVER os.remove'd
