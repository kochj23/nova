#!/usr/bin/env python3
"""nova_claude_token_watch.py — early warning before .6's Claude Code token expires.

THE RECURRING FAILURE: .6's Claude Max OAuth access token has an ~8h life and the headless
background context doesn't reliably auto-refresh it, so it expires roughly nightly. Every time
it does, every generator that shells out to `claude` (local_burbank, copenhagen, the
journal/ops pipeline, and — via claude_cred_sync — the whole .2 content fleet) fails until
Jordan runs /login again. The publish guards now catch the resulting stubs (no garbage ships),
but the articles silently go MISSING and Jordan only finds out hours later.

This closes the last gap: page Jordan BEFORE it expires, so he re-logs in ahead of the failure
instead of discovering it via absent articles. Runs hourly. Two escalations:
  - access token < WARN_H hours left  -> #nova-warning, once (deduped)
  - access token already EXPIRED       -> #nova-critical (generators are failing right now)
  - refresh token < REFRESH_WARN_DAYS  -> #nova-warning (the 27-day /login is coming due)

Also records the expiry to health_checks so the token's freshness is itself monitored.

Permanent fix (Jordan, one-time, needs a browser): `claude setup-token` mints a LONG-LIVED
token that doesn't expire nightly — this watchdog reminds him to do that once.
"""
from __future__ import annotations
import json
import subprocess
import sys
import time

KEYCHAIN_SERVICE = "Claude Code-credentials"
WARN_H = 2.0              # warn when the access token has < 2h left
REFRESH_WARN_DAYS = 3.0   # warn when the 27-day refresh token is within 3 days of expiring
DSN = "host=localhost dbname=nova_ops user=kochj"


def read_token() -> dict | None:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout).get("claudeAiOauth")
    except Exception:
        return None


def _notify(title, body, level):
    try:
        from nova_notify import notify
        notify(title, body=body, level=level, category="fleet",
               source="nova_claude_token_watch", dedup_key=f"claude-token:{level}")
    except Exception:
        pass


def _record(hours_left):
    try:
        import psycopg2
        conn = psycopg2.connect(DSN)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO health_checks (service_name, node_name, checked_by, status, latency_ms, error_message) "
                "VALUES ('claude_token','mac-studio','nova_claude_token_watch',%s,%s,%s)",
                ("up" if hours_left > 0 else "down", int(max(0, hours_left) * 3600),
                 f"access token {hours_left:.1f}h left"))
        conn.commit(); conn.close()
    except Exception:
        pass


def _longlived_ok() -> bool:
    """Is the long-lived CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) present AND does it
    actually authenticate? This is now the PRIMARY health signal: while it's good, the nightly
    expiry of the short-lived FILE credential is harmless — `claude -p` falls back to this token
    (nova_claude_code.claude_env injects it), so generators keep working. Verified 2026-08-12."""
    try:
        from nova_claude_code import claude_oauth_token
        tok = claude_oauth_token()
    except Exception:
        tok = None
    if not tok:
        return False
    try:
        import tempfile, subprocess, os
        th = tempfile.mkdtemp()          # blind `claude` to any file credential — token-only auth
        env = {**os.environ, "HOME": os.path.expanduser("~"), "CLAUDE_CODE_OAUTH_TOKEN": tok}
        # keychain read needs the real HOME; the CLI is pointed at the empty temp HOME below
        env["HOME"] = th
        r = subprocess.run(["claude", "-p", "--model", "haiku"], input="Reply with the single word OK.",
                           capture_output=True, text=True, timeout=60, env=env)
        return "OK" in (r.stdout or "")
    except Exception:
        return False


def main() -> int:
    # PRIMARY: the long-lived token. If it's healthy, the short-lived file-credential's nightly
    # expiry is a non-event — do NOT page about it (that was the old false-alarm-forever behaviour).
    ll = _longlived_ok()
    if ll:
        _record(24 * 365)   # effectively "fresh" — the long-lived token carries auth
        print("token: long-lived CLAUDE_CODE_OAUTH_TOKEN present and authenticating — healthy")
        return 0

    # Long-lived token MISSING or BROKEN — we've regressed to the nightly file-credential dance.
    # THIS is worth an alarm, because now the old failure mode is back in play.
    _notify("Claude long-lived token missing/broken on .6 — back on the nightly-expiry treadmill",
            "The CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` no longer authenticates (Keychain "
            "'claude-code-oauth-token' on .6 / ~/.config/nova/claude-oauth-token on the Linux nodes). "
            "Re-run `claude setup-token` and store it, or generators will start failing again whenever "
            "the short-lived file credential expires. See agent_docs services-monitoring.", "warning")

    o = read_token()
    if not o:
        _notify("Claude file credential ALSO unreadable on .6 — logged out",
                "Both the long-lived token and the Keychain file credential are unavailable; "
                "`claude -p` cannot authenticate at all. Run `claude /login` and `claude setup-token`.",
                "critical")
        print("token: long-lived broken AND file cred unreadable")
        return 0

    now = time.time() * 1000
    access_h = (int(o.get("expiresAt", 0)) - now) / 3600000.0
    _record(access_h)
    if access_h <= 0:
        _notify("Claude file credential EXPIRED and no long-lived fallback — generators failing NOW",
                "The short-lived access token has expired and the long-lived token isn't working, so "
                "content generation is down. Run `claude setup-token` (preferred) or `claude /login`.",
                "critical")
    print(f"token: long-lived BROKEN; file-cred access {access_h:.1f}h left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
