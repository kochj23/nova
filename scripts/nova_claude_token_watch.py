#!/usr/bin/env python3
"""nova_claude_token_watch.py — early warning before .6's Claude Code token expires.

THE RECURRING FAILURE: .6's Claude Max OAuth access token has an ~8h life and the headless
background context doesn't reliably auto-refresh it, so it expires roughly nightly. Every time
it does, every generator that shells out to `claude` (local_burbank, overnight_review, the
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


def main() -> int:
    o = read_token()
    if not o:
        _notify("Claude token unreadable on .6",
                "Keychain 'Claude Code-credentials' missing/unreadable — .6 may be logged out. "
                "Run `claude /login` (or `claude setup-token` for a long-lived token).", "critical")
        print("token: unreadable")
        return 0

    now = time.time() * 1000
    access_h = (int(o.get("expiresAt", 0)) - now) / 3600000.0
    refresh_d = (int(o.get("refreshTokenExpiresAt", 0)) - now) / 86400000.0
    _record(access_h)

    if access_h <= 0:
        _notify("Claude token EXPIRED on .6 — content generators are failing NOW",
                "The access token has expired; local_burbank / overnight_review / the journal "
                "pipeline (and the .2 fleet via cred-sync) will fail until you re-auth. "
                "Run `claude /login`. Permanent fix: `claude setup-token` (long-lived).", "critical")
    elif access_h < WARN_H:
        _notify(f"Claude token on .6 expires in {access_h:.1f}h",
                "Re-auth soon to avoid overnight article failures: `claude /login`. "
                "Permanent fix so this stops recurring: `claude setup-token` (long-lived token).",
                "warning")

    if 0 < refresh_d < REFRESH_WARN_DAYS:
        _notify(f"Claude REFRESH token on .6 expires in {refresh_d:.1f} days",
                "The 27-day refresh token is nearly up — a `claude /login` will be required soon "
                "regardless of the nightly access-token dance.", "warning")

    print(f"token: access {access_h:.1f}h left, refresh {refresh_d:.1f}d left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
