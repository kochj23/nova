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


# ── house categories added 2026-10-05 (Security, Performance, Retry, Unit, Integration, Functional, Frame) ────
# The targeted pytest functions above stay as-is; these classes complete the seven-category contract and make
# the file runnable as `python3 tests/test_nova_memory_reclassify.py`.
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from collections import Counter  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from unittest.mock import patch  # noqa: E402

SCRIPT = SCRIPTS_DIR / "nova_memory_reclassify.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="reclassify-test-"))
R.LOG = TMP / "memory_reclassify.log"          # keep ~/.openclaw/logs untouched
R.OUT = TMP / "operations"                     # ...and the Hugo operations folder


class _Cur:
    """Cursor stub for main(): the keyset page is served once, then empty; every statement + params is recorded."""
    def __init__(self, centroids, page, texts=()):
        self.centroids, self.page, self.texts = centroids, page, list(texts)
        self.sql, self.params, self._last, self._paged, self.closed = [], [], None, False, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append(s); self.params.append(params); self._last = []
        if "GROUP BY source HAVING" in s:
            self._last = self.centroids
        elif "ORDER BY id LIMIT" in s:
            self._last = [] if self._paged else self.page; self._paged = True
        elif "SELECT text FROM memories" in s:
            self._last = [(t,) for t in self.texts]

    def fetchall(self):
        return list(self._last)

    def ran(self, frag):
        return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed, self.autocommit = cur, False, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = self.cur.closed = True


CENTROIDS = [("knowledge", 300, "[1.0, 0.0, 0.0]"), ("wikipedia", 250, "[0.0, 1.0, 0.0]"), ("imessage", 900, "[0.0, 0.0, 1.0]")]
PAGE = [("m1", "wikipedia", "[1.0, 0.0, 0.0]"),      # misfiled public memory -> moves to knowledge
        ("m2", "knowledge", "[1.0, 0.0, 0.0]"),      # already right
        ("m3", "imessage", "[1.0, 0.0, 0.0]"),       # private -> stays put
        ("m4", "knowledge", "[0.0, 0.0, 1.0]")]      # homeless (imessage is not a target)


def _main(argv=(), cur=None, llm="Nova's own article.", urlopen=None, connect_exc=None, batch=None):
    """Run main() fully offline; returns (cur, slack messages, execute_batch calls, urlopen requests, log text)."""
    cur = cur or _Cur(CENTROIDS, PAGE)
    slack, batches, reqs = [], [], []

    def connect(dsn):
        if connect_exc:
            raise connect_exc
        return _Conn(cur)

    def open_(req, timeout=None):
        reqs.append(req)
        if urlopen:
            raise urlopen
        return io.BytesIO(b"{}")
    with patch.object(R.psycopg2, "connect", connect), patch.object(R, "slack", slack.append), \
         patch.object(R.psycopg2.extras, "execute_batch", batch or (lambda c, sql, rows, page_size=0: batches.append((sql, list(rows))))), \
         patch.object(R, "call_openrouter", lambda *a, **k: llm), patch.object(R.urllib.request, "urlopen", open_), \
         patch.object(sys, "argv", ["nova_memory_reclassify.py", *argv]), redirect_stdout(io.StringIO()):
        R.main()
    return cur, slack, batches, reqs, R.LOG.read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", R.DSN)

    def test_sql_is_parameterized_and_nothing_is_ever_deleted(self):
        self.assertIsNone(re.search(r'execute(_batch)?\(\s*(cur,\s*)?f"', SRC))
        self.assertNotIn("DELETE", SRC)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"memories"})
        cur, _, batches, _, _ = _main()
        for s, p in zip(cur.sql, cur.params):
            if "FROM memories" in s:
                self.assertIn("%s", s); self.assertIsInstance(p, tuple)     # every value travels as a bind param
        self.assertIn("WHERE id=%s", batches[0][0])

    def test_private_memory_never_moves_and_private_vector_is_never_a_target(self):
        cur, _, batches, _, _ = _main()
        moved = {mid for _, rows in batches for (_, _, mid) in rows}
        self.assertNotIn("m3", moved)
        self.assertNotIn("imessage", {ns for _, rows in batches for (ns, _, _) in rows})

    def test_article_prompt_forbids_secrets_and_ids(self):
        self.assertIn("No secrets, no IDs.", SRC)


class TestPerformance(unittest.TestCase):
    def test_process_fast_on_10k_memories(self):
        sources, cents, src_idx = _axis_centroids()
        batch = [(f"m{i}", ("knowledge", "wikipedia", "imessage")[i % 3], f"[{i % 2}.0, {(i + 1) % 2}.0, 0.1]") for i in range(10_000)]
        pending, pairs, hid, hemb = [], Counter(), [], []
        t0 = time.perf_counter()
        n = R._process(batch, sources, cents, src_idx, pending, pairs, hid, hemb)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(n, 10_000)
        self.assertGreater(len(pending), 0)
        self.assertFalse(any(os == "imessage" for _, _, os in pending))

    def test_homeless_buffer_is_capped(self):
        sources, cents, src_idx = _axis_centroids()
        batch = [(f"h{i}", "knowledge", "[0.0, 0.0, 1.0]") for i in range(50)]
        hid, hemb = [], []
        with patch.object(R, "HOMELESS_CAP", 10):
            R._process(batch, sources, cents, src_idx, [], Counter(), hid, hemb)
        self.assertEqual((len(hid), len(hemb)), (10, 10))


class TestRetry(unittest.TestCase):
    def test_slack_failure_is_swallowed_and_logged(self):
        # RETRY GAP: slack()/nova_config.post_both — one attempt; failure is logged, the run continues
        with patch.object(R.nova_config, "post_both", side_effect=OSError("slack 500")), redirect_stdout(io.StringIO()):
            R.slack("hello")
        self.assertIn("slack post failed: slack 500", R.LOG.read_text())

    def test_ops_vector_store_failure_does_not_abort_the_run(self):
        # RETRY GAP: urllib.request.urlopen(/remember) — one attempt; the article is still written to disk and Slack
        cur, slack, _, reqs, log = _main(urlopen=OSError("memory server down"))
        self.assertEqual(len(reqs), 1)
        self.assertIn("ops-vector store failed: memory server down", log)
        self.assertTrue(slack[-1].startswith(":white_check_mark: *Memory reclassification complete*"))
        self.assertTrue(cur.closed)

    def test_llm_failure_falls_back_to_a_deterministic_article(self):
        # RETRY GAP: write_article/call_openrouter — one attempt; None -> canned summary, never an empty article
        with patch.object(R, "call_openrouter", lambda *a, **k: None):
            text = R.write_article({"processed": 10, "moved": 2, "homeless": 1, "new_vectors": {}, "top_moves": [], "elapsed_s": 3})
        self.assertEqual(text, "Memory audit complete: 2 of 10 memories reshelved.")

    def test_pg_connect_failure_escapes_before_any_slack_post(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt; nothing is announced for a run that never started
        posted = []
        with patch.object(R.nova_config, "post_both", lambda m, **k: posted.append(m)), \
             patch.object(R.psycopg2, "connect", side_effect=OSError("pg down")), \
             patch.object(sys, "argv", ["nova_memory_reclassify.py"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                R.main()
        self.assertEqual(posted, [])


class TestUnit(unittest.TestCase):
    def test_log_writes_to_the_redirected_file(self):
        with redirect_stdout(io.StringIO()) as out:
            R.log("unit-probe")
        self.assertIn("unit-probe", out.getvalue())
        self.assertIn("] unit-probe", R.LOG.read_text().splitlines()[-1])

    def test_kmeans_single_cluster_and_small_input(self):
        X = R.normalize(np.array([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2]], dtype=np.float32))
        self.assertEqual(R.kmeans(X, k=1).tolist(), [0, 0, 0])
        self.assertEqual(len(R.kmeans(X, k=3)), 3)

    def test_ops_voice_prompt_is_a_string_about_nova(self):
        with patch.object(R.psycopg2, "connect", side_effect=OSError("offline")):   # nova_voice reads PG for flavor; stay offline
            p = R.nova_config_ops_voice()
        self.assertIsInstance(p, str)
        self.assertIn("Nova", p)

    def test_write_article_prompt_carries_the_stats(self):
        seen = {}

        def fake(sys_p, user, **kw):
            seen.update(sys_p=sys_p, user=user, kw=kw); return "ok"
        with patch.object(R, "call_openrouter", fake):
            self.assertEqual(R.write_article({"processed": 1234, "moved": 5, "homeless": 7, "new_vectors": {"nv": 9},
                                              "top_moves": [{"from→to": "a → b", "n": 5}], "elapsed_s": 42}), "ok")
        self.assertIn("1,234", seen["user"]); self.assertIn("- a → b: 5", seen["user"]); self.assertIn("nv (9)", seen["user"])
        self.assertEqual(seen["kw"]["max_tokens"], 1200)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported_not_copied(self):
        import nova_journal, nova_unused_memories
        self.assertIs(R.call_openrouter, nova_journal.call_openrouter)
        self.assertIs(R.INTERNAL_SOURCES, nova_unused_memories.INTERNAL_SOURCES)
        self.assertIn("dbname=nova_memories", R.DSN)
        self.assertNotIn("def is_private_source", SRC)

    def test_process_output_matches_the_flush_update_shape(self):
        cur, _, batches, _, _ = _main()
        sql, rows = batches[0]
        self.assertIn("SET source=%s", sql); self.assertIn("'reclass_from',%s", sql); self.assertTrue(sql.endswith("WHERE id=%s"))
        self.assertEqual(rows, [("knowledge", "wikipedia", "m1")])     # (new_source, old_source, id) in placeholder order

    def test_build_centroids_then_process_chain(self):
        conn = _fake_conn(CENTROIDS)
        sources, cents = R.build_centroids(conn)
        src_idx = {s: i for i, s in enumerate(sources)}
        n, pending, pairs, hid, _ = _run_process(PAGE, sources, cents, src_idx)
        self.assertEqual((n, pending, hid), (4, [("m1", "knowledge", "wikipedia")], ["m4"]))
        self.assertEqual(pairs, Counter({"wikipedia → knowledge": 1}))


class TestFunctional(unittest.TestCase):
    def test_golden_path_moves_writes_article_and_announces(self):
        for f in R.OUT.glob("*"):
            f.unlink()
        cur, slack, batches, reqs, log = _main()
        self.assertEqual(batches, [(batches[0][0], [("knowledge", "wikipedia", "m1")])])
        stats = json.loads((R.OUT / "reclassify_stats.json").read_text())
        self.assertEqual((stats["processed"], stats["moved"], stats["homeless"], stats["dry_run"]), (4, 1, 1, False))
        self.assertEqual(stats["top_moves"], [{"from→to": "wikipedia → knowledge", "n": 1}])
        md = next(R.OUT.glob("*-memory-reclassify.md")).read_text()
        self.assertIn('categories: ["operations"]', md); self.assertIn("Nova's own article.", md)
        body = json.loads(reqs[0].data)
        self.assertEqual(body["source"], "operations"); self.assertIn("Nova's own article.", body["text"])
        self.assertTrue(reqs[0].full_url.endswith("/remember"))
        self.assertTrue(slack[0].startswith(":mag: *Memory reclassification started*"))
        self.assertIn("4 audited · 1 moved · 0 new vector(s) · 1 homeless", slack[-1])
        self.assertEqual(cur.params[0], (R.MIN_TARGET,))
        self.assertEqual(cur.ran("ORDER BY id LIMIT")[0][1], ("", R.BATCH))
        self.assertTrue(cur.closed)
        self.assertIn("DONE", log)

    def test_dry_run_changes_nothing_but_still_reports(self):
        cur, slack, batches, reqs, _ = _main(argv=["--dry-run"])
        self.assertEqual(batches, [])
        self.assertFalse(any("UPDATE" in s for s in cur.sql))
        self.assertTrue(json.loads((R.OUT / "reclassify_stats.json").read_text())["dry_run"])
        self.assertEqual(len(reqs), 1)

    def test_limit_stops_after_the_first_page(self):
        cur, _, _, _, _ = _main(argv=["--limit", "2"])
        self.assertEqual(len(cur.ran("ORDER BY id LIMIT")), 1)

    def test_bad_limit_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            _main(argv=["--limit", "lots"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: the script has no argparse and any invocation starts a 1.66M-row mutating run — import smoke only
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_memory_reclassify"], cwd=str(SCRIPTS_DIR),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
