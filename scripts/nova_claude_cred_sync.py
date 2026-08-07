#!/usr/bin/env python3
"""nova_claude_cred_sync.py — keep the Linux fleet's Claude Code CLI logged in.

ROOT CAUSE (2026-08-07): the headless Claude CLI on Linux nodes (nova-core .2 runs the
scheduler-core content tasks) does NOT auto-refresh its OAuth access token — there's no
interactive session to trigger a refresh — so ~12h after each login the access token expires
and the node goes "Not logged in · Please run /login". That silently kills every task that
shells out to `claude` (journal_*, fishbowl_*, operations_security, local_airwaves, journal_
research/tech_today/dream, ...) with a bare exit 1. A one-time credential copy always decays.

.6 (macOS) keeps its token fresh automatically (the app/CLI refreshes it), so this pushes .6's
fresh Keychain credential to each Linux node on a schedule that BEATS the ~12h access-token
lifetime (runs every 6h). Only ever pushes a currently-valid credential. Never logs the secret.

Run on .6. See agent_docs services-monitoring (Claude Code CLI logout runbook).
"""
from __future__ import annotations
import json
import subprocess
import sys
import time

KEYCHAIN_SERVICE = "Claude Code-credentials"
HOSTS = ["kochj@192.168.1.2"]          # Linux nodes whose claude CLI needs keeping-alive
REMOTE_PATH = "~/.claude/.credentials.json"
MIN_REMAINING_MS = 30 * 60 * 1000      # don't bother pushing a token with <30m left


# ── pure, testable core ───────────────────────────────────────────────────────
def access_remaining_ms(blob: dict, now_ms: int) -> int:
    """Milliseconds until the access token expires (negative if already expired)."""
    return int(blob.get("claudeAiOauth", {}).get("expiresAt", 0)) - now_ms


def is_syncable(blob: dict, now_ms: int, min_remaining_ms: int = MIN_REMAINING_MS) -> bool:
    """True only if this credential is worth pushing: a real oauth blob whose access token has
    comfortably more than `min_remaining_ms` left and whose refresh token is still valid. We
    never push a dead/near-dead token — that would just re-break the remote."""
    o = blob.get("claudeAiOauth")
    if not isinstance(o, dict) or not o.get("accessToken") or not o.get("refreshToken"):
        return False
    if access_remaining_ms(blob, now_ms) < min_remaining_ms:
        return False
    if int(o.get("refreshTokenExpiresAt", 0)) <= now_ms:
        return False
    return True


# ── IO ────────────────────────────────────────────────────────────────────────
def local_credential() -> str | None:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def push(host: str, blob_str: str) -> tuple[bool, str]:
    """Write the credential to the remote node's .credentials.json (mode 600). Secret is piped
    over stdin — never appears in argv or logs."""
    try:
        cmd = ("umask 077; mkdir -p ~/.claude && cat > " + REMOTE_PATH +
               " && chmod 600 " + REMOTE_PATH + " && echo OK")
        r = subprocess.run(["ssh", host, cmd], input=blob_str,
                           capture_output=True, text=True, timeout=25)
        return (r.returncode == 0 and "OK" in r.stdout), r.stderr.strip()[:120]
    except Exception as e:
        return False, str(e)[:120]


def main() -> int:
    raw = local_credential()
    if not raw:
        print("cred-sync: no local Claude credential in Keychain — skipping", file=sys.stderr)
        return 0
    try:
        blob = json.loads(raw)
    except Exception:
        print("cred-sync: local credential is not JSON — skipping", file=sys.stderr)
        return 0
    now = int(time.time() * 1000)
    if not is_syncable(blob, now):
        # .6's own token is stale/near-dead — nothing good to push; let it refresh first.
        print("cred-sync: local token not fresh enough to sync — skipping this cycle")
        return 0

    ok_hosts, failed = [], []
    for host in HOSTS:
        ok, err = push(host, raw)
        (ok_hosts if ok else failed).append(host if ok else f"{host} ({err})")

    if failed:
        try:
            from nova_notify import notify
            notify(f"Claude credential sync failed for: {', '.join(failed)}",
                   level="warning", category="fleet", source="nova_claude_cred_sync",
                   dedup_key="claude-cred-sync-fail")
        except Exception:
            pass
    hrs = access_remaining_ms(blob, now) / 3600000.0
    print(f"cred-sync: pushed to {len(ok_hosts)}/{len(HOSTS)} host(s); "
          f"token good for ~{hrs:.1f}h. failed={failed or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
