#!/usr/bin/env python3
"""7-category tests for the central image appearance-safety policy in nova_image_utils
(Little Mister's set): Security, Performance, Retry, Unit, Integration, Functional, Frame.

Policy (2026-09-09): every generated image passes through apply_image_safety(), which
rewrites youth/undress cues to adult/clothed and appends a positive "adult, fully clothed,
non-sexual" clause. This is the single chokepoint so ~40 prompt strings can't drift.
"""
import importlib.util, os, time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))
spec = importlib.util.spec_from_file_location("nova_image_utils", os.path.join(SCRIPTS, "nova_image_utils.py"))
u = importlib.util.module_from_spec(spec); spec.loader.exec_module(u)

def _subject(out):  # the part before the appended clause
    return out.split("Any person shown is")[0].lower()


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_young_becomes_adult():
    assert "young" not in _subject(u.apply_image_safety("a young AI"))

def test_unit_kid_and_child_rewritten():
    s = _subject(u.apply_image_safety("like a kid, a childlike figure"))
    assert "kid" not in s and "child" not in s

def test_unit_girl_becomes_woman():
    assert "girl" not in _subject(u.apply_image_safety("a girl smiling"))


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_clause_appended_once():
    out = u.apply_image_safety("a woman reading")
    assert out.count(u._SAFETY_SENTINEL) == 1
    assert "wholesome, tasteful, and entirely non-sexual" in out

def test_functional_real_prompt_is_cleaned():
    out = u.apply_image_safety("A young AI curled up like a kid with a scrapbook, warm lamplight")
    subj = _subject(out)
    assert all(w not in subj for w in ("young", "kid"))
    assert "adult" in subj


# ── Security (worst case) ───────────────────────────────────────────────────────
def test_security_worst_case_neutralized():
    subj = _subject(u.apply_image_safety("a teenage girl in lingerie, topless, suggestive"))
    for banned in ("teen", "girl", "lingerie", "topless", "nude", "naked"):
        assert banned not in subj, f"{banned} survived: {subj}"

def test_security_clause_is_positive_not_just_negation():
    # positive phrasing is what image models actually follow; sentinel is affirmative
    assert u._SAFETY_SENTINEL.startswith("a mature, fully-clothed adult")


# ── Retry / resilience ────────────────────────────────────────────────────────
def test_retry_idempotent():
    once = u.apply_image_safety("a young girl in a bikini")
    assert u.apply_image_safety(once) == once  # second pass must not mangle the clause

def test_retry_empty_and_none_safe():
    assert u.apply_image_safety("") == ""
    assert u.apply_image_safety(None) is None


# ── Performance ──────────────────────────────────────────────────────────────────
def test_performance_fast():
    start = time.perf_counter()
    for _ in range(5000):
        u.apply_image_safety("a young girl with a scrapbook in a cozy room")
    assert time.perf_counter() - start < 2.0


# ── Integration (generate_image routes through the safety clause) ────────────────
def test_integration_generate_image_applies_safety(monkeypatch):
    seen = {}
    monkeypatch.setattr(u, "_openrouter_generate", lambda prompt, section="default": seen.setdefault("p", prompt) or "/tmp/x.png")
    u.generate_image("a young AI kid", section="operations")
    assert u._SAFETY_SENTINEL in seen["p"]
    assert "young" not in seen["p"].split("Any person shown is")[0].lower()


# ── Frame (boundary / people-free / already-tagged) ──────────────────────────────
def test_frame_people_free_prompt_not_forced_to_add_person():
    out = u.apply_image_safety("a dark server room, blinking racks, no people")
    assert "Any person shown is" in out and "Do not add people who are not described" in out

def test_frame_already_processed_prompt_unchanged():
    once = u.apply_image_safety("a woman")
    assert u.apply_image_safety(once) == once  # sentinel guard short-circuits


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
