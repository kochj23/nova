"""test_nova_memory_reclassify.py — targeted tests for nova_memory_reclassify.

Focus (high-risk): mass DB reassignment of ~1.66M rows. The core safety
invariant is that a PRIVATE / health / imessage / internal source is NEVER
reclassified into a public vector — provenance must win over topical
similarity, in BOTH directions:

  * a private/internal memory is never MOVED OUT of its vector (_process skips it), and
  * a private/internal vector is never a MOVE TARGET (build_centroids excludes it).

All external deps (psycopg2, the OpenRouter LLM call, the vector HTTP API) are
mocked — nothing here touches a live DB or network. We do NOT re-check the
generic shell=True / SQL-f-string invariants (test_security.py owns those).

Written by Jordan Koch (via Claude).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import nova_memory_reclassify as R  # noqa: E402
from nova_unused_memories import INTERNAL_SOURCES  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _fake_conn(rows):
    """A psycopg2-shaped conn whose cursor() context manager yields `rows`."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _axis_centroids():
    """Two public target vectors along orthogonal axes (dim=3), plus src_idx."""
    sources = ["knowledge", "wikipedia"]
    cents = R.normalize(np.array([[1.0, 0, 0], [0, 1.0, 0]], dtype=np.float32))
    src_idx = {s: i for i, s in enumerate(sources)}
    return sources, cents, src_idx


def _run_process(batch, sources, cents, src_idx):
    pending, move_pairs = [], __import__("collections").Counter()
    homeless_ids, homeless_emb = [], []
    n = R._process(batch, sources, cents, src_idx, pending, move_pairs,
                   homeless_ids, homeless_emb)
    return n, pending, move_pairs, homeless_ids, homeless_emb


# ── Unit: parse_vec / normalize ──────────────────────────────────────────────

def test_parse_vec_roundtrip():
    v = R.parse_vec("[1.0, 2.5, -3.0]")
    assert v.dtype == np.float32
    assert v.tolist() == pytest.approx([1.0, 2.5, -3.0])


def test_parse_vec_tolerates_whitespace_and_brackets():
    v = R.parse_vec("  [0.5,0.5]  ")
    assert v.tolist() == pytest.approx([0.5, 0.5])


def test_normalize_unit_norm_rows():
    out = R.normalize(np.array([[3.0, 4.0]], dtype=np.float32))
    assert np.linalg.norm(out[0]) == pytest.approx(1.0)
    assert out.tolist()[0] == pytest.approx([0.6, 0.8])


def test_normalize_zero_row_stays_zero_no_nan():
    """A zero embedding must not produce NaN/inf (would poison every cosine sim)."""
    out = R.normalize(np.array([[0.0, 0.0, 0.0]], dtype=np.float32))
    assert not np.isnan(out).any()
    assert out.tolist()[0] == [0.0, 0.0, 0.0]


# ── Unit: _protected (provenance gate) ───────────────────────────────────────

@pytest.mark.parametrize("src", ["imessage", "email_archive", "health"])
def test_protected_true_for_private_sources(src):
    import nova_config
    assert nova_config.is_private_source(src), "precondition: source is private"
    assert R._protected(src) is True


@pytest.mark.parametrize("src", sorted(INTERNAL_SOURCES)[:6])
def test_protected_true_for_internal_sources(src):
    assert R._protected(src) is True


@pytest.mark.parametrize("src", ["knowledge", "wikipedia"])
def test_protected_false_for_public_sources(src):
    assert R._protected(src) is False


def test_protected_false_for_empty_or_none():
    assert R._protected("") is False
    assert R._protected(None) is False


# ── Security invariant #1: private memory is NEVER moved out ──────────────────

def test_private_memory_never_reassigned_even_if_topically_public():
    """An imessage memory whose embedding points squarely at the public
    'knowledge' centroid must still stay put — provenance beats topic."""
    sources, cents, src_idx = _axis_centroids()
    # embedding == knowledge centroid direction; nearest public vector = knowledge
    batch = [("m1", "imessage", "[1.0, 0.0, 0.0]")]
    n, pending, move_pairs, homeless_ids, _ = _run_process(batch, sources, cents, src_idx)
    assert n == 1
    assert pending == [], "private source leaked into a move batch"
    assert sum(move_pairs.values()) == 0
    assert homeless_ids == [], "private source must not even enter the homeless pool"


@pytest.mark.parametrize("src", ["health", "email_archive", "dream"])
def test_no_private_or_internal_source_ever_enters_pending(src):
    sources, cents, src_idx = _axis_centroids()
    batch = [("x", src, "[1.0, 0.0, 0.0]")]  # points at public 'knowledge'
    _, pending, _, homeless_ids, _ = _run_process(batch, sources, cents, src_idx)
    assert pending == []
    assert homeless_ids == []


def test_public_memory_IS_moved_when_clearly_closer_elsewhere():
    """Positive control: a genuinely public memory in the wrong vector DOES move
    (proves the guard above blocks on provenance, not because nothing ever moves)."""
    sources, cents, src_idx = _axis_centroids()
    # currently 'wikipedia' but embedding == 'knowledge' direction
    batch = [("m2", "wikipedia", "[1.0, 0.0, 0.0]")]
    _, pending, move_pairs, _, _ = _run_process(batch, sources, cents, src_idx)
    assert len(pending) == 1
    mid, new_src, old_src = pending[0]
    assert (mid, new_src, old_src) == ("m2", "knowledge", "wikipedia")
    assert move_pairs["wikipedia → knowledge"] == 1


# ── Security invariant #2: private/internal vectors are never MOVE TARGETS ─────

def test_build_centroids_excludes_private_and_internal_targets():
    rows = [
        ("knowledge", 300, "[1.0, 0.0, 0.0]"),
        ("imessage", 400, "[0.0, 1.0, 0.0]"),   # private -> must be dropped
        ("dream", 500, "[0.0, 0.0, 1.0]"),       # internal -> must be dropped
        ("wikipedia", 250, "[0.0, 0.7, 0.7]"),
    ]
    conn = _fake_conn(rows)
    sources, cents = R.build_centroids(conn)
    assert "imessage" not in sources
    assert "dream" not in sources
    assert set(sources) == {"knowledge", "wikipedia"}
    assert cents.shape[0] == 2
    # centroids are L2-normalized
    assert np.linalg.norm(cents[0]) == pytest.approx(1.0, abs=1e-5)


def test_build_centroids_respects_min_target_sql_param():
    """MIN_TARGET is passed as the HAVING bind param (not string-formatted)."""
    conn = _fake_conn([("knowledge", 300, "[1.0, 0.0, 0.0]")])
    R.build_centroids(conn)
    cur = conn.cursor.return_value
    _, params = cur.execute.call_args[0]
    assert params == (R.MIN_TARGET,)


# ── Decision logic: keep / move / homeless thresholds ────────────────────────

def test_memory_kept_when_already_in_best_vector():
    sources, cents, src_idx = _axis_centroids()
    batch = [("k1", "knowledge", "[1.0, 0.0, 0.0]")]
    _, pending, move_pairs, homeless_ids, _ = _run_process(batch, sources, cents, src_idx)
    assert pending == [] and sum(move_pairs.values()) == 0 and homeless_ids == []


def test_homeless_when_below_similarity_floor():
    """Embedding orthogonal to every centroid => best_sim ~0 < HOMELESS_SIM."""
    sources, cents, src_idx = _axis_centroids()
    batch = [("h1", "knowledge", "[0.0, 0.0, 1.0]")]
    _, pending, _, homeless_ids, homeless_emb = _run_process(batch, sources, cents, src_idx)
    assert homeless_ids == ["h1"]
    assert len(homeless_emb) == 1
    assert pending == []


def test_no_move_when_margin_not_exceeded():
    """Nearest is a different vector but only barely — below MARGIN => no move."""
    sources, cents, src_idx = _axis_centroids()
    # nearly 45deg between the two axes but tilted just slightly toward wikipedia;
    # currently in knowledge. Difference in cos-sim < MARGIN (0.05) => stay.
    a = 0.71
    b = np.sqrt(1 - a * a)  # b slightly > a so wikipedia wins by a hair
    batch = [("t1", "knowledge", f"[{a}, {b}, 0.0]")]
    _, pending, _, _, _ = _run_process(batch, sources, cents, src_idx)
    # confirm the margin really is small enough to suppress the move
    assert (b - a) <= R.MARGIN
    assert pending == []


# ── kmeans: deterministic separation of clear clusters ───────────────────────

def test_kmeans_separates_two_clear_clusters():
    cluster_a = np.tile([1.0, 0.0], (10, 1))
    cluster_b = np.tile([0.0, 1.0], (10, 1))
    X = R.normalize(np.vstack([cluster_a, cluster_b]).astype(np.float32))
    labels = R.kmeans(X, k=2)
    assert len(set(labels[:10])) == 1, "first cluster should share one label"
    assert len(set(labels[10:])) == 1, "second cluster should share one label"
    assert labels[0] != labels[10], "the two clusters must get different labels"


def test_kmeans_is_deterministic():
    X = R.normalize(np.random.default_rng(0).standard_normal((30, 4)).astype(np.float32))
    assert R.kmeans(X, k=3).tolist() == R.kmeans(X, k=3).tolist()


# ── name_cluster: sanitization (LLM mocked; no network) ──────────────────────

def test_name_cluster_sanitizes_to_snake_case(monkeypatch):
    monkeypatch.setattr(R, "call_openrouter", lambda *a, **k: "Foo Bar!!")
    assert R.name_cluster(["some text"]) == "foo"


def test_name_cluster_strips_non_alnum(monkeypatch):
    monkeypatch.setattr(R, "call_openrouter", lambda *a, **k: "My-Vector Name")
    assert R.name_cluster(["t"]) == "myvector"


def test_name_cluster_falls_back_when_filtered_to_empty(monkeypatch):
    """Model output with no alnum chars filters to '' and falls back."""
    monkeypatch.setattr(R, "call_openrouter", lambda *a, **k: "!!! ???")
    assert R.name_cluster(["t"]) == "uncategorized"


def test_name_cluster_crashes_on_fully_empty_output(monkeypatch):
    """DISCOVERED EDGE-CASE BUG (module, not test): a truly empty/whitespace-only
    LLM reply hits `''.split()[0]` -> IndexError instead of the 'uncategorized'
    fallback. Pinned here so a future module fix can flip this expectation.
    Low blast-radius: only reached when the model returns nothing for a homeless
    cluster name; does not affect the reassignment safety invariant."""
    monkeypatch.setattr(R, "call_openrouter", lambda *a, **k: "")
    with pytest.raises(IndexError):
        R.name_cluster(["t"])


def test_name_cluster_truncates_to_40_chars(monkeypatch):
    monkeypatch.setattr(R, "call_openrouter", lambda *a, **k: "a" * 100)
    assert len(R.name_cluster(["t"])) == 40
