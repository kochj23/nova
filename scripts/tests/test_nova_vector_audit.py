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
