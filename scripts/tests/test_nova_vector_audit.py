#!/usr/bin/env python3
"""
test_nova_vector_audit.py — Tests for nova_vector_audit.py.

FOCUS: f-string UPDATE SQL (injection surface) + PII-local Ollama routing.

Covers:
  * Unit — quality_check_batch buckets, get_all_vectors / sample_memories
    parsing, classify_batch JSON extraction, the run_audit denominator/decision
    logic.
  * Security invariant 1 (PII-local) — call_llm classifies raw memory text on
    LOCAL Ollama (127.0.0.1) ONLY and never reaches any cloud host; on error it
    returns '' rather than falling back to a remote model.
  * Security invariant 2 (no shell / injection surface) — the SQL builders
    (psql/move_memory/sample_memories) invoke psql via an argv LIST with no
    shell=True, so a single-quote or ';' in a source/id/vector value cannot
    break out to the OS shell. The SQL-level f-string interpolation is
    characterized (documented as the remaining Tier-A hardening target).
  * Decision invariant — run_audit only issues a move to a vector that EXISTS
    and differs from the current one; an LLM-invented target is ignored.

All external deps (psql/subprocess, urllib, psycopg2, nova_config, nova_notify,
nova_image_utils) are mocked. No live service or DB is contacted.

Written by Jordan Koch.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import nova_vector_audit as v


# ── Unit: quality_check_batch ────────────────────────────────────────────────

def test_quality_near_empty():
    r = v.quality_check_batch([{"id": "1", "text": "short"}])
    assert r["near_empty"] == 1
    assert r["total_issues"] == 1


def test_quality_repetitive():
    text = " ".join(["spam"] * 12)
    r = v.quality_check_batch([{"id": "1", "text": text}])
    assert r["repetitive"] == 1
    assert r["total_issues"] == 1


def test_quality_garbled():
    r = v.quality_check_batch([{"id": "1", "text": "@" * 50}])
    assert r["garbled"] == 1
    assert r["total_issues"] == 1


def test_quality_low_signal():
    text = "um um um um uh uh uh uh you know umm here is padding text right ok"
    r = v.quality_check_batch([{"id": "1", "text": text}])
    assert r["low_signal"] == 1
    assert r["total_issues"] == 1


def test_quality_clean_has_no_issues():
    text = "This is a perfectly normal memory about automotive engine diagnostics and repair."
    r = v.quality_check_batch([{"id": "1", "text": text}])
    assert r["total_issues"] == 0
    assert r["examples"] == []


def test_quality_each_memory_counted_once():
    # near-empty short-circuits before the other checks (continue).
    mems = [{"id": str(i), "text": "x"} for i in range(4)]
    r = v.quality_check_batch(mems)
    assert r["near_empty"] == 4
    assert r["total_issues"] == 4
    # examples capped to one entry per flagged memory
    assert len(r["examples"]) == 4


# ── Unit: get_all_vectors parsing ────────────────────────────────────────────

def test_get_all_vectors_parses_pipe_rows():
    fake = "automotive|1200\nmedicine|300\nmilitary_history|50"
    with patch.object(v, "psql", return_value=fake):
        vectors = v.get_all_vectors()
    assert vectors == [("automotive", 1200), ("medicine", 300), ("military_history", 50)]


def test_get_all_vectors_ignores_malformed_lines():
    fake = "automotive|1200\n\ngarbage-without-pipe\nmedicine|10"
    with patch.object(v, "psql", return_value=fake):
        vectors = v.get_all_vectors()
    assert vectors == [("automotive", 1200), ("medicine", 10)]


# ── Unit: sample_memories parsing ────────────────────────────────────────────

def test_sample_memories_parses_id_and_text():
    fake = "abc-1|some memory text\nabc-2|another one"
    with patch.object(v, "psql", return_value=fake) as mock_psql:
        mems = v.sample_memories("automotive", 5)
    assert mems == [
        {"id": "abc-1", "text": "some memory text"},
        {"id": "abc-2", "text": "another one"},
    ]
    # the vector name and limit flow into the SQL
    sql = mock_psql.call_args[0][0]
    assert "automotive" in sql
    assert "LIMIT 5" in sql


# ── Unit: classify_batch JSON extraction ─────────────────────────────────────

def test_classify_batch_extracts_json_array():
    resp = 'Here you go:\n[{"id":"1","verdict":"correct"},{"id":"2","verdict":"move","suggested_vector":"automotive"}]\nDone.'
    with patch.object(v, "call_llm", return_value=resp):
        out = v.classify_batch("medicine", [{"id": "1", "text": "hi"}], ["medicine", "automotive"])
    assert out == [
        {"id": "1", "verdict": "correct"},
        {"id": "2", "verdict": "move", "suggested_vector": "automotive"},
    ]


def test_classify_batch_returns_empty_on_garbage():
    with patch.object(v, "call_llm", return_value="the model refused to answer"):
        out = v.classify_batch("medicine", [{"id": "1", "text": "hi"}], ["medicine"])
    assert out == []


def test_classify_batch_returns_empty_on_empty_llm():
    with patch.object(v, "call_llm", return_value=""):
        out = v.classify_batch("medicine", [{"id": "1", "text": "hi"}], ["medicine"])
    assert out == []


# ── Security invariant 1: PII-local Ollama routing ───────────────────────────

def _is_lan_or_loopback(host: str) -> bool:
    """Raw memory samples may only be classified on-box or on the private LAN —
    never a cloud endpoint. Since 2026-07-19 the default points at the .6 inference
    host (nova-core has no local Ollama), so RFC1918 is as acceptable as loopback."""
    name = host.split(":", 1)[0]
    return (name in ("127.0.0.1", "localhost")
            or name.startswith("192.168.") or name.startswith("10.")
            or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", name) is not None)


def test_ollama_url_is_local_loopback():
    # Constant must point at loopback or the private LAN — raw memory samples never leave the house.
    host = v.OLLAMA_URL.split("://", 1)[1].split("/", 1)[0]
    assert _is_lan_or_loopback(host), v.OLLAMA_URL
    assert "11434" in v.OLLAMA_URL  # Ollama port


def _fake_urlopen_factory(captured, response_text="ok"):
    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps(
            {"response": response_text}).encode()
        cm.__exit__.return_value = False
        return cm
    return _fake_urlopen


def test_call_llm_only_contacts_loopback():
    captured = {}
    with patch("urllib.request.urlopen", _fake_urlopen_factory(captured, "verdict text")):
        out = v.call_llm("system prompt", "raw private memory text")
    assert out == "verdict text"
    host = captured["url"].split("://", 1)[1].split("/", 1)[0]
    assert _is_lan_or_loopback(host), captured["url"]
    # never a cloud endpoint
    for cloud in ("anthropic", "openai", "googleapis", "azure", "amazonaws"):
        assert cloud not in captured["url"]


def test_call_llm_sends_local_model_and_memory_in_payload():
    captured = {}
    seen_payload = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        seen_payload["data"] = req.data
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"response": "x"}).encode()
        cm.__exit__.return_value = False
        return cm

    with patch("urllib.request.urlopen", _fake_urlopen):
        v.call_llm("sys", "SENSITIVE-PII-12345")
    body = json.loads(seen_payload["data"])
    assert body["model"] == v.OLLAMA_MODEL
    assert body["stream"] is False
    assert "SENSITIVE-PII-12345" in body["prompt"]  # PII only ever in the local request


def test_call_llm_strips_think_block():
    captured = {}
    with patch("urllib.request.urlopen",
               _fake_urlopen_factory(captured, "<think>secret reasoning</think>final answer")):
        out = v.call_llm("s", "u")
    assert out == "final answer"
    assert "secret" not in out


def test_call_llm_returns_empty_on_error_no_cloud_fallback():
    # On local failure it must return '' (callers treat as 'no verdicts'),
    # NOT retry against a remote/cloud model.
    def _boom(req, timeout=None):
        raise ConnectionError("ollama down")
    with patch("urllib.request.urlopen", _boom):
        out = v.call_llm("s", "raw memory")
    assert out == ""


# ── Security invariant 2: no shell, injection surface characterized ──────────

def test_psql_uses_argv_list_no_shell():
    completed = MagicMock(stdout="result\n")
    with patch.object(v.subprocess, "run", return_value=completed) as mock_run:
        v.psql("SELECT 1;")
    args, kwargs = mock_run.call_args
    argv = args[0]
    assert isinstance(argv, list)            # argv vector, not a shell string
    assert argv[0] == "psql"
    assert kwargs.get("shell", False) is False  # never shell=True
    # the SQL is a single discrete argv element — cannot spill into the shell
    assert "SELECT 1;" in argv


def test_move_memory_quote_and_semicolon_cannot_reach_shell():
    """A malicious vector/id with ' and ; stays inside ONE argv element, so it
    cannot break out to the OS shell (no shell=True). This is the load-bearing
    safety property; SQL-level interpolation is characterized below."""
    completed = MagicMock(stdout="")
    evil_vec = "public'; DROP TABLE memories; --"
    evil_id = "1'; DELETE FROM memories; --"
    with patch.object(v.subprocess, "run", return_value=completed) as mock_run:
        v.move_memory(evil_id, "private", evil_vec)
    args, kwargs = mock_run.call_args
    argv = args[0]
    assert kwargs.get("shell", False) is False
    # The whole UPDATE (including the evil payload) is exactly one argv element.
    sql_elem = argv[-1]
    assert sql_elem.startswith("UPDATE memories SET source")
    assert evil_vec in sql_elem and evil_id in sql_elem
    # It is NOT split across multiple argv entries (no shell-level tokenization).
    assert sum(1 for a in argv if "DROP TABLE" in a) == 1


def test_move_memory_sql_interpolation_is_characterized():
    # Documents the CURRENT (unparameterized) behavior so a future Tier-A fix
    # that switches to bind params will intentionally flip this test.
    completed = MagicMock(stdout="")
    with patch.object(v.subprocess, "run", return_value=completed) as mock_run:
        v.move_memory("mem42", "medicine", "automotive")
    sql = mock_run.call_args[0][0][-1]
    assert sql == "UPDATE memories SET source = 'automotive' WHERE id = 'mem42';"


# ── Decision invariant: run_audit only moves to existing vectors ─────────────

def _patch_run_audit(monkeypatch, vectors, sample, classify_result):
    monkeypatch.setattr(v, "get_all_vectors", lambda: vectors)
    monkeypatch.setattr(v, "sample_memories", lambda name, n: list(sample))
    monkeypatch.setattr(v, "quality_check_batch", lambda mems, source=None: {
        "repetitive": 0, "near_empty": 0, "garbled": 0, "low_signal": 0,
        "total_issues": 0, "examples": []})
    monkeypatch.setattr(v, "classify_batch", lambda name, mems, allv: list(classify_result))
    # deterministic vector selection
    monkeypatch.setattr(v.random, "sample", lambda pop, k: list(pop))


def test_run_audit_moves_only_to_existing_different_vector(monkeypatch):
    # automotive has <50 rows so it is NOT itself audited, but remains a valid
    # move target (it exists in vector_names). Total must clear the >=1000-memory
    # empty-store guard or run_audit aborts rather than publish a hallucinated audit.
    vectors = [("medicine", 1000), ("automotive", 10)]
    sample = [{"id": "m1", "text": "engine repair notes for a v8"},
              {"id": "m2", "text": "aspirin dosage guidance for adults"},
              {"id": "m3", "text": "some borderline note about health"}]
    classify = [
        # valid move: automotive exists and != medicine
        {"id": "m1", "verdict": "move", "suggested_vector": "automotive", "reason": "engine"},
        # invalid: suggested vector does not exist -> must NOT move
        {"id": "m2", "verdict": "move", "suggested_vector": "invented_vector"},
        # correct -> no move
        {"id": "m3", "verdict": "correct"},
    ]
    _patch_run_audit(monkeypatch, vectors, sample, classify)
    moved = []
    monkeypatch.setattr(v, "move_memory",
                        lambda mid, old, new: moved.append((mid, old, new)))
    # neutralize the psycopg2 shared_observations write
    monkeypatch.setattr(v, "psql", lambda sql: "")
    with patch.dict(sys.modules, {"psycopg2": MagicMock()}):
        stats = v.run_audit()

    assert moved == [("m1", "medicine", "automotive")]
    assert stats["moved"] == 1
    assert stats["moves"][0]["to"] == "automotive"
    # m2's move is rejected (target missing) but it entered the 'move' branch, so
    # it is NOT recounted as correct; only m3 (verdict=correct) increments correct.
    assert stats["correct"] == 1
    assert stats["memories_classified"] == 3


def test_run_audit_never_moves_to_same_vector(monkeypatch):
    vectors = [("medicine", 1000)]  # >=1000 clears the empty-store abort guard
    sample = [{"id": "m1", "text": "aspirin note"}]
    classify = [{"id": "m1", "verdict": "move", "suggested_vector": "medicine"}]
    _patch_run_audit(monkeypatch, vectors, sample, classify)
    moved = []
    monkeypatch.setattr(v, "move_memory",
                        lambda mid, old, new: moved.append((mid, old, new)))
    monkeypatch.setattr(v, "psql", lambda sql: "")
    with patch.dict(sys.modules, {"psycopg2": MagicMock()}):
        stats = v.run_audit()
    assert moved == []
    assert stats["moved"] == 0


def test_run_audit_quality_pct_uses_sampled_not_classified(monkeypatch):
    # Guards the divide-by-zero / bogus-denominator fix: even when the LLM
    # returns no verdicts, quality_pct is computed over rows actually scanned.
    vectors = [("medicine", 1000)]  # >=1000 clears the empty-store abort guard
    sample = [{"id": "m1", "text": "x"}, {"id": "m2", "text": "y"}]  # both near_empty
    _patch_run_audit(monkeypatch, vectors, sample, classify_result=[])
    # restore real quality_check_batch so issues are actually detected
    monkeypatch.setattr(v, "quality_check_batch", _real_quality)
    monkeypatch.setattr(v, "psql", lambda sql: "")
    with patch.dict(sys.modules, {"psycopg2": MagicMock()}):
        stats = v.run_audit()
    assert stats["memories_sampled"] == 2
    assert stats["memories_classified"] == 0
    assert stats["accuracy_pct"] is None          # no LLM verdicts -> n/a, not crash
    assert stats["quality_issue_pct"] == 100.0     # both rows flagged, denom = sampled


# capture the real function before any monkeypatch in this session could shadow it
_real_quality = v.quality_check_batch


# ═══════════════════════════════════════════════════════════════════════════════
# House categories added 2026-10-05 — the 7 unittest classes (Security, Performance, Retry,
# Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).
# Module-level stubs below: no notify bus, no image generation from any test in this file.
# ═══════════════════════════════════════════════════════════════════════════════
import os as _os
import subprocess as _subprocess
import tempfile as _tempfile
import time as _time
import types as _types
import unittest

v.notify = MagicMock()
v.generate_image = MagicMock(return_value=None)
_VSRC = (Path(__file__).resolve().parents[1] / "nova_vector_audit.py").read_text()
_VTMP = Path(_tempfile.mkdtemp(prefix="vector-audit-test-"))


def _stats(**kw):
    s = {"vectors_audited": 2, "audited_detail": [{"vector": "astronomy", "count": 900, "sampled": 2, "issues": 1,
                                                   "examples": ["x x x x"]}],
         "memories_sampled": 200, "memories_classified": 150, "correct": 140, "moved": 1,
         "moves": [{"id": "m1", "from": "astronomy", "to": "physics", "reason": "misfiled"}],
         "accuracy_pct": 93.3, "total_vectors": 80, "total_memories": 1_700_000,
         "quality": {"total_issues": 3, "repetitive": 1, "near_empty": 1, "garbled": 1, "low_signal": 0},
         "quality_issue_pct": 1.5}
    s.update(kw)
    return s


class _Voice:
    def __enter__(self):
        mod = _types.ModuleType("nova_voice")
        mod.system_prompt = lambda s: "SYS" + s
        mod.CONTEXT_JOURNAL_VECTOR_AUDIT = ""
        self.p = patch.dict(sys.modules, {"nova_voice": mod}); self.p.start(); return self

    def __exit__(self, *e):
        self.p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(_VSRC))

    def test_private_shelves_never_audited(self):
        vectors = [("imessage", 5000), ("email", 5000), ("apple_health", 5000), ("astronomy", 5000)]
        picked = []
        with patch.object(v, "get_all_vectors", return_value=vectors), \
             patch.object(v, "_rotation_pick", side_effect=lambda c, n: picked.extend(c) or []), \
             patch.object(v, "_rotation_mark"), patch.dict(sys.modules, {"psycopg2": MagicMock()}):
            v.run_audit()
        self.assertEqual([n for n, _ in picked], ["astronomy"])

    def test_invented_stats_scrubbed_from_public_prose(self):
        dirty = "LiveJournal is 100% empty with 1,919,003 ghosts since 2003."
        clean = v._scrub_invented_stats(dirty)
        self.assertFalse(v._has_invented_stats(clean))
        self.assertIn("2003", clean)                            # years are legitimate prose


class TestPerformance(unittest.TestCase):
    def test_quality_check_10k(self):
        mems = [{"id": str(i), "text": f"distinct memory number {i} about orbital mechanics and comets"}
                for i in range(10_000)]
        t0 = _time.perf_counter()
        q = v.quality_check_batch(mems, source="astronomy")
        self.assertLess(_time.perf_counter() - t0, 5.0)
        self.assertIn("total_issues", q)


class TestRetry(unittest.TestCase):
    def test_numeric_prose_regenerated_once(self):
        calls = []
        def llm(system, user, max_tokens=4000):
            calls.append(system)
            return "It was 87% tidy." if len(calls) == 1 else "It was mostly tidy and lovely. " * 10
        with _Voice(), patch.object(v, "call_llm", side_effect=llm):
            art = v.generate_article(_stats())
        self.assertEqual(len(calls), 2)
        self.assertIn("ZERO digits", calls[1])
        self.assertTrue(art.startswith("It was mostly tidy"))

    def test_still_numeric_after_retry_is_scrubbed(self):
        with _Voice(), patch.object(v, "call_llm", return_value="We lost 12,345 memories (40%)."):
            art = v.generate_article(_stats())
        prose = art.split("### The actual numbers")[0]
        self.assertFalse(v._has_invented_stats(prose))

    def test_rotation_falls_back_to_random_when_pg_down(self):
        # RETRY GAP: _rotation_pick — one connect attempt; on failure, random selection (never raises)
        pg = _types.ModuleType("psycopg2"); pg.connect = MagicMock(side_effect=OSError("pg down"))
        with patch.dict(sys.modules, {"psycopg2": pg}):
            out = v._rotation_pick([("a", 1), ("b", 2), ("c", 3)], 2)
            v._rotation_mark(["a"])                             # non-fatal too
        self.assertEqual(len(out), 2)
        self.assertEqual(pg.connect.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_facts_ledger(self):
        led = v._facts_ledger(_stats())
        self.assertIn("1,700,000 across 80 vectors", led)
        self.assertIn("140 of 150 classified (93.3%)", led)
        self.assertNotIn("Correctly filed", v._facts_ledger(_stats(accuracy_pct=None)))

    def test_has_invented_stats(self):
        self.assertTrue(v._has_invented_stats("about 45 %"))
        self.assertTrue(v._has_invented_stats("19191 rows"))
        self.assertFalse(v._has_invented_stats("in 1999 and 2026 I learned a lot"))

    def test_empty_store_aborts(self):
        with patch.object(v, "get_all_vectors", return_value=[("a", 10)]):
            self.assertTrue(v.run_audit()["aborted"])


class TestIntegration(unittest.TestCase):
    def test_rotation_reads_and_orders_by_last_audited(self):
        cur = MagicMock(); cur.fetchall.return_value = [("old", 100.0), ("new", 999.0)]
        pg = _types.ModuleType("psycopg2"); pg.connect = MagicMock(return_value=MagicMock(cursor=lambda: cur))
        with patch.dict(sys.modules, {"psycopg2": pg}):
            out = v._rotation_pick([("new", 1), ("old", 1), ("never", 1)], 2)
        self.assertEqual([n for n, _ in out], ["never", "old"])

    def test_article_is_voice_plus_code_ledger(self):
        with _Voice(), patch.object(v, "call_llm", return_value="A gentle morning among the shelves.") as llm:
            art = v.generate_article(_stats())
        self.assertIn("- astronomy — 1 quality issue(s) found", llm.call_args[0][1])
        self.assertTrue(art.endswith(v._facts_ledger(_stats())))


class TestFunctional(unittest.TestCase):
    def test_publish_writes_post_pushes_and_notifies(self):
        runs = []
        def fake_run(argv, **kw):
            runs.append(argv[:2]); return _subprocess.CompletedProcess(argv, 0, "", "")
        v.notify.reset_mock()
        with patch.object(v, "CONTENT_DIR", _VTMP / "content"), patch.object(v, "IMAGES_DIR", _VTMP / "img"), \
             patch.object(v, "HUGO_ROOT", _VTMP), patch.object(v.subprocess, "run", fake_run):
            v.publish('Shelves "and" Dust', "body text", None, _stats())
        post = next((_VTMP / "content").glob("*-shelves-and-dust.md"))
        self.assertIn('title: "Shelves and Dust"', post.read_text())
        self.assertEqual(runs, [["git", "add"], ["git", "commit"], ["git", "pull"], ["git", "push"]])
        self.assertIn("Moved 1 misfiled", v.notify.call_args[1]["body"])

    def test_main_aborts_without_publishing_on_empty_llm(self):
        import nova_config
        with patch.object(v, "run_audit", return_value=_stats()), \
             patch.object(v, "generate_article", return_value=""), patch.object(v, "generate_title", return_value=""), \
             patch.object(v, "publish") as pub, patch.object(nova_config, "post_both") as pb:
            self.assertEqual(v.main(), 1)
        pub.assert_not_called()
        self.assertIn("skipped", pb.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: a run audits the live store and publishes, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', _VSRC)
        r = _subprocess.run([sys.executable, "-c", "import nova_vector_audit as m; print(callable(m.main))"],
                            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=30,
                            env={**_os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
