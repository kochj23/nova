#!/usr/bin/env python3
"""7-category tests for nova_politics_column (Little Mister's set): Security, Performance,
Retry, Unit, Integration, Functional, Frame. DB, LLM, image, and git are all mocked —
the focus is the SAFETY GUARDS: never fabricate on a thin week, never publish an
ungrounded (hallucinated) draft."""
import importlib.util, os, sys, time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))
spec = importlib.util.spec_from_file_location("nova_politics_column", os.path.join(SCRIPTS, "nova_politics_column.py"))
pc = importlib.util.module_from_spec(spec); spec.loader.exec_module(pc)

ITEMS = [{"outlet": "Techdirt", "headline": "DOJ blocked charges against an ICE officer"},
         {"outlet": "New Voice of Ukraine", "headline": "Ukrainian drones reach 3,000 km inside Russia"},
         {"outlet": "EFF", "headline": "Drone-as-first-responder programs expanding"}]


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_build_source_block():
    b = pc.build_source_block(ITEMS)
    assert "- [Techdirt] DOJ blocked" in b and b.count("\n") == 2

def test_unit_verify_grounded_true_with_two_outlets():
    art = "As Techdirt reported... and the EFF warned..."
    assert pc.verify_grounded(art, ITEMS) is True

def test_unit_verify_grounded_false_with_one_outlet():
    art = "Techdirt reported a thing, and I have opinions about it."
    assert pc.verify_grounded(art, ITEMS) is False


# ── Security (the load-bearing guarantees) ──────────────────────────────────────
def test_security_thin_week_never_fabricates(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS[:2])  # below MIN_BRIEF_ITEMS
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not generate")))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 0
    assert published["n"] == 0  # nothing published, nothing generated

def test_security_ungrounded_draft_never_published(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS + ITEMS + ITEMS)  # >= MIN
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k: "A"*2000 + " I made all this up with no sources.")
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    rc = pc.main([])
    assert rc == 1 and published["n"] == 0  # grounding gate blocked it


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_happy_path_publishes(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS*4)  # >= MIN
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k:
                        "Per Techdirt, the DOJ blocked charges. The EFF warned about drones. " + "x"*1000)
    monkeypatch.setattr(pc, "generate_title", lambda *a, **k: "A Test Column")
    monkeypatch.setattr(pc, "make_cover", lambda *a, **k: None)
    calls = {}
    monkeypatch.setattr(pc, "publish", lambda t, b, c: calls.setdefault("t", t) or "http://x")
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 0 and calls["t"] == "A Test Column"

def test_functional_short_article_aborts(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS*4)
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k: "too short")
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 1 and published["n"] == 0


# ── Retry / resilience ────────────────────────────────────────────────────────
def test_retry_log_action_failure_is_swallowed(monkeypatch):
    # _log_action swallows DB errors internally; calling with a broken DSN must not raise
    monkeypatch.setenv("NOVA_OPS_DSN", "host=256.256.256.256 dbname=x user=y")
    pc._log_action("test", "test")  # no exception


# ── Performance ──────────────────────────────────────────────────────────────
def test_performance_verify_grounded_fast():
    art = "Techdirt and the EFF and New Voice of Ukraine " * 200
    start = time.perf_counter()
    for _ in range(2000):
        pc.verify_grounded(art, ITEMS)
    assert time.perf_counter() - start < 2.0


# ── Integration ──────────────────────────────────────────────────────────────
def test_integration_thresholds_and_sources_sane():
    assert pc.MIN_BRIEF_ITEMS >= 5 and pc.MIN_ARTICLE_CHARS >= 500
    # private/personal shelves must never be in the political source set
    for banned in ("imessage", "email_archive", "personal", "scanner"):
        assert banned not in pc.POLITICAL_SOURCES

def test_integration_dry_run_flag_recognized(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS*4)
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k:
                        "Per Techdirt and the EFF, things happened. " + "x"*1000)
    monkeypatch.setattr(pc, "generate_title", lambda *a, **k: "Dry Title")
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main(["--dry-run"]) == 0 and published["n"] == 0  # dry-run never publishes


# ── Frame (boundary) ──────────────────────────────────────────────────────────
def test_frame_empty_brief_aborts(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: [])
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 0  # graceful skip, no crash

def test_frame_verify_grounded_empty_items():
    assert pc.verify_grounded("some article text", []) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
