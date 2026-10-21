#!/usr/bin/env python3
"""Tests for nova_claude_cred_sync.py — focus on the two things that have actually broken the
fleet: shipping the WRONG SHAPE of credential, and letting a node rot unnoticed.

Run: python3 -m pytest tests/test_nova_claude_cred_sync.py
  or: python3 tests/test_nova_claude_cred_sync.py   (plain asserts, no pytest needed)
"""
import importlib.util
import sys
import time
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_claude_cred_sync.py"
_spec = importlib.util.spec_from_file_location("nova_claude_cred_sync", _SCRIPT)
s = importlib.util.module_from_spec(_spec)
sys.modules["nova_claude_cred_sync"] = s
_spec.loader.exec_module(s)

NOW = int(time.time() * 1000)
H = 3600 * 1000


def _blob(access="sk-ant-oat01-aaa", expires_in_h=8.0, refresh="sk-ant-ort01-bbb"):
    o = {
        "accessToken": access,
        "expiresAt": NOW + int(expires_in_h * H),
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x",
        "subscriptionTypeSource": "local",
    }
    if refresh is not None:
        o["refreshToken"] = refresh
        o["refreshTokenExpiresAt"] = NOW + 5 * 24 * H
    return {"claudeAiOauth": o}


# ── the shape we ship ─────────────────────────────────────────────────────────
def test_refresh_token_is_never_shipped():
    """THE security property. Six Linux nodes used to each hold a 5-day refresh token that
    could mint new access tokens indefinitely; one compromised node was a durable foothold on
    Jordan's Max account. Verified 2026-10-21 on .250 that the CLI authenticates without it."""
    out = s.sanitize(_blob())
    assert "refreshToken" not in out["claudeAiOauth"]
    assert "refreshTokenExpiresAt" not in out["claudeAiOauth"]
    # and the secret must not survive anywhere in the serialized payload
    assert b"sk-ant-ort01-bbb" not in s.payload_bytes(_blob())


def test_sanitize_keeps_exactly_what_the_cli_reads():
    o = s.sanitize(_blob())["claudeAiOauth"]
    assert set(o) == {"accessToken", "expiresAt", "scopes", "subscriptionType"}
    assert o["accessToken"] == "sk-ant-oat01-aaa"
    assert o["subscriptionType"] == "max"


def test_sanitize_preserves_the_file_envelope():
    """The node's file shape must stay {"claudeAiOauth": {...}} or the CLI won't read it."""
    assert list(s.sanitize(_blob())) == ["claudeAiOauth"]


def test_sanitize_tolerates_missing_optional_fields():
    out = s.sanitize({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1}})
    assert out["claudeAiOauth"] == {"accessToken": "t", "expiresAt": 1}
    assert s.sanitize({}) == {"claudeAiOauth": {}}


# ── idempotency ───────────────────────────────────────────────────────────────
def test_digest_is_stable_across_key_order_and_dropped_fields():
    """Almost every hourly run must be a no-op, so the digest may only change when the token
    does — not when Keychain reorders its JSON or .6's refresh token rotates."""
    a = _blob(refresh="sk-ant-ort01-FIRST")
    b = _blob(refresh="sk-ant-ort01-ROTATED")
    assert s.payload_digest(a) == s.payload_digest(b)
    assert s.payload_digest(_blob()) != s.payload_digest(_blob(access="sk-ant-oat01-zzz"))


# ── what we refuse to ship ────────────────────────────────────────────────────
def test_expired_or_nearly_expired_tokens_are_not_syncable():
    assert not s.is_syncable(_blob(expires_in_h=-1), NOW)      # already dead
    assert not s.is_syncable(_blob(expires_in_h=0.1), NOW)     # <30m left: would re-break the node
    assert s.is_syncable(_blob(expires_in_h=8), NOW)


def test_empty_shell_credential_is_not_syncable():
    """Exactly what every node held on 2026-10-21: accessToken "", expiresAt 0."""
    shell = {"claudeAiOauth": {"accessToken": "", "expiresAt": 0, "refreshToken": "",
                               "subscriptionType": "max"}}
    assert not s.is_syncable(shell, NOW)
    assert not s.is_syncable({}, NOW)
    assert not s.is_syncable({"claudeAiOauth": "nope"}, NOW)


def test_a_missing_refresh_token_does_not_block_the_sync():
    """Regression guard: is_syncable used to require a refreshToken, so the moment .6's 5-day
    refresh token lapsed the whole fleet stopped being synced — precisely when it mattered."""
    assert s.is_syncable(_blob(refresh=None), NOW)


# ── staleness / rot detection ─────────────────────────────────────────────────
def test_staleness_thresholds():
    assert s.is_stale(0.5) == "ok"
    assert s.is_stale(5.9) == "ok"
    assert s.is_stale(6.0) == "warn"     # drifting: token lives ~8h
    assert s.is_stale(11.9) == "warn"
    assert s.is_stale(12.0) == "crit"    # certainly dead


def test_never_synced_node_is_critical_not_ok():
    """The .86 failure mode: silently broken since 2026-10-03 because nothing treated
    "no record at all" as an alarm."""
    assert s.is_stale(None) == "crit"


def test_access_remaining_ms_sign():
    assert s.access_remaining_ms(_blob(expires_in_h=2), NOW) > 0
    assert s.access_remaining_ms(_blob(expires_in_h=-2), NOW) < 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
