#!/usr/bin/env python3
"""Tests for nova_slack_watch.py — focus on the DATA-SAFETY guarantees:
secrets are redacted before they reach the model, inference is loopback-only,
and the offline heuristic still flags real trouble when the LLM is down."""
import importlib

import pytest

w = importlib.import_module("nova_slack_watch")


# Secret-shaped fixtures are ASSEMBLED at runtime from fragments so no live
# credential pattern sits in the committed source (keeps push-protection happy)
# while still exercising the redactor against a realistic string.
def _slack_tok():  return "xox" + "b-" + "1" * 12 + "-" + "a" * 16
def _openai_key(): return "sk" + "-" + "a" * 16
def _aws_key():    return "AKIA" + "Z" * 14


# ── redaction: no live credential shape survives to the model ─────────────────
def test_redact_masks_slack_token():
    out = w._redact(f"token is {_slack_tok()} here")
    assert "xox" + "b-" not in out and "[slack-token]" in out


def test_redact_masks_api_keys_and_bearer():
    assert "[api-key]" in w._redact(f"key={_openai_key()}")
    assert "AKIA" not in w._redact(f"{_aws_key()} creds")
    assert "Bearer [redacted]" in w._redact("Authorization: Bearer abcDEF123._-tok")


def test_redact_masks_password_pairs_and_hex():
    assert w._redact("password: hunter2swordfish").endswith("=[redacted]")
    assert "[hex-secret]" in w._redact("sig " + "a" * 40)


def test_redact_leaves_normal_text_intact():
    msg = "mac-mini (.190) ollama unreachable — service down"
    assert w._redact(msg) == msg


# ── loopback-only: never ship data off-box ───────────────────────────────────
def test_assert_local_allows_loopback():
    for u in ("http://127.0.0.1:11434/api/chat",
              "http://localhost:11434/api/chat"):
        w._assert_local(u)  # must not raise


def test_assert_local_refuses_remote():
    for u in ("http://192.168.1.6:11434/api/chat",
              "https://api.openai.com/v1/chat",
              "http://ollama.example.com/api/chat"):
        with pytest.raises(RuntimeError):
            w._assert_local(u)


def test_default_endpoint_is_loopback():
    w._assert_local(w.OLLAMA_URL)  # the shipped default must pass the guard


# ── heuristic fallback: still catches trouble with no LLM ─────────────────────
def test_heuristic_flags_severe_channels_and_alarm_words():
    msgs = [
        {"channel": "#nova-info",     "text": "calendar: dentist at 3pm"},
        {"channel": "#nova-info",     "text": "PostgreSQL replica is DOWN"},
        {"channel": "#nova-warning",  "text": "anything at all here"},
        {"channel": "#nova-chat",     "text": "good morning"},
    ]
    flagged = w.heuristic_flags(msgs)
    texts = {m["text"] for m in flagged}
    assert "PostgreSQL replica is DOWN" in texts      # alarm word
    assert "anything at all here" in texts            # severe channel
    assert "calendar: dentist at 3pm" not in texts    # routine
    assert "good morning" not in texts


# ── digest formatting carries the self-skip marker ───────────────────────────
def test_build_digest_tags_with_watch_marker():
    a = {"severity": "critical", "headline": "X happened",
         "items": [{"channel": "#nova-critical", "what": "y", "why": "z"}]}
    out = w.build_digest(a)
    assert w.WATCH_MARKER in out          # so our own posts are skipped next run
    assert "X happened" in out and "`#nova-critical`" in out
