#!/usr/bin/env python3
"""nova_claude_cred_sync.py — keep the Linux fleet's Claude Code CLI logged in.

ROOT CAUSE (2026-08-07): the headless Claude CLI on Linux nodes does NOT auto-refresh its
OAuth access token — there's no interactive session to trigger a refresh — so ~8h after each
login the access token expires and the node goes "Not logged in · Please run /login". That
silently kills every task that shells out to `claude` (journal_*, fishbowl_*,
operations_security, local_airwaves, tech_today, dream, ...) with a bare exit 1. A one-time
credential copy always decays.

.6 (macOS) keeps its token fresh automatically (the app/CLI refreshes it), so this pushes .6's
fresh Keychain credential to each Linux node on a schedule that beats the access-token
lifetime. Only ever pushes a currently-valid token. Never logs the secret.

=== 2026-10-21 (node-parity session) — three changes, all load-bearing ===

(1) ACCESS TOKEN ONLY — the refreshToken is deliberately STRIPPED before the push.
    Previously the whole Keychain blob went to every node, so six Linux boxes each held a
    5-day refresh token that could mint fresh access tokens indefinitely; one compromised node
    meant a durable foothold on Jordan's Max account. Shipping only
    {accessToken, expiresAt, scopes, subscriptionType} is enough: VERIFIED on nova-core4 (.250)
    on 2026-10-21 — a credentials.json with those four fields and NO refreshToken, with
    CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY unset, ran `claude -p "say ok"` -> "ok".
    So the CLI does not require a refreshToken to be present; it just can't renew, which is
    this script's whole job. A node compromise now leaks at most one short-lived token.

(2) IT WAS SWITCHED OFF. The scheduler task `claude_cred_sync` was enabled:false, which is why
    on 2026-10-21 EVERY node's ~/.claude/.credentials.json was an empty shell — accessToken "",
    expiresAt 0, refreshToken "" — and nova-core2/.86 had been failing its claude-dependent
    tasks since 2026-10-03. Re-enabled at `every 1h` (was 4h): the access token lives ~8h, so
    1h gives ~8x margin, keeps a rebooted node current within the hour, and costs nothing on
    the common path because of (3).

(3) IDEMPOTENT AND QUIET. Each push is skipped when the node already holds this exact token
    (compared by sha256 of the sanitized payload, computed remotely — the digest, not the
    secret, crosses the wire). Since .6's token only changes when it actually refreshes,
    almost every hourly run is a no-op. Per-node results land in health_checks
    (service_name='claude_cred'), and a node that has not synced for STALE_WARN_H / been
    unreachable escalates through nova_notify — so this can never silently rot again.

LAYERING: this is the FALLBACK path. The primary is the long-lived `claude setup-token` token
in the fleet secret store (1Password vault "Nova" item `claude-code-oauth-token`), which
~/.config/nova/cli-auth.sh exports as CLAUDE_CODE_OAUTH_TOKEN and nova_claude_code.py reads
directly. The env token overrides the file credential, so the two are independent: either one
alone keeps a node working.

Run on .6. Tests: test_nova_claude_cred_sync.py. See agent_docs services-monitoring.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys
import time

KEYCHAIN_SERVICE = "Claude Code-credentials"
# Every core Linux node with a claude CLI (2026-10-21). .125 was dropped: unreachable
# (ssh timeout) and no longer in the fleet. .197 = nova-core7, reached by IP because its
# NIC is dead and it rides Wi-Fi.
HOSTS = [
    "kochj@192.168.1.2",    # nova-core    — scheduler-core leader
    "kochj@192.168.1.5",    # nova-core3   — scheduler-core standby
    "kochj@192.168.1.10",   # nova-core5   — PG, scheduler standby (installed, disabled)
    "kochj@192.168.1.86",   # nova-core2   — cold scheduler standby
    "kochj@192.168.1.250",  # nova-core4   — resilience node
    "kochj@192.168.1.197",  # nova-core7   — Wi-Fi uplink, dead NIC
]
REMOTE_PATH = "~/.claude/.credentials.json"
MIN_REMAINING_MS = 30 * 60 * 1000   # don't push a token with <30m left — it would just re-break
STALE_WARN_H = 6.0                  # a node unsynced this long is drifting toward expiry (<8h life)
STALE_CRIT_H = 12.0                 # ...this long and its token is certainly dead

# The only fields a node needs. refreshToken / refreshTokenExpiresAt are deliberately absent:
# see (1) above. subscriptionTypeSource is .6-local bookkeeping and is not shipped either.
SHIP_FIELDS = ("accessToken", "expiresAt", "scopes", "subscriptionType")


# ── pure, testable core ───────────────────────────────────────────────────────
def access_remaining_ms(blob: dict, now_ms: int) -> int:
    """Milliseconds until the access token expires (negative if already expired)."""
    return int(blob.get("claudeAiOauth", {}).get("expiresAt", 0)) - now_ms


def is_syncable(blob: dict, now_ms: int, min_remaining_ms: int = MIN_REMAINING_MS) -> bool:
    """True only if this credential is worth pushing: a real oauth blob whose access token has
    comfortably more than `min_remaining_ms` left. We never push a dead/near-dead token — that
    would just re-break the remote.

    NOTE: a refreshToken is NOT required. It used to be, which meant that the moment .6's
    5-day refresh token lapsed this script stopped syncing anything — exactly when the fleet
    needed it most. Whether .6 can still renew is .6's problem (nova_claude_token_watch pages
    about that); all this function asks is "is the access token I'm about to ship any good?".
    """
    o = blob.get("claudeAiOauth")
    if not isinstance(o, dict) or not o.get("accessToken"):
        return False
    return access_remaining_ms(blob, now_ms) >= min_remaining_ms


def sanitize(blob: dict) -> dict:
    """The payload actually shipped: the access token and the metadata the CLI reads, and
    nothing else. Strips refreshToken/refreshTokenExpiresAt so a node cannot mint new tokens.
    Preserves the file's existing shape ({"claudeAiOauth": {...}}) so the CLI reads it
    unchanged."""
    o = blob.get("claudeAiOauth", {})
    return {"claudeAiOauth": {k: o[k] for k in SHIP_FIELDS if k in o}}


def payload_bytes(blob: dict) -> bytes:
    """Canonical serialization — stable key order so the digest only changes when the token
    does, not when Keychain happens to reorder its JSON."""
    return json.dumps(sanitize(blob), sort_keys=True, separators=(",", ":")).encode()


def payload_digest(blob: dict) -> str:
    return hashlib.sha256(payload_bytes(blob)).hexdigest()


def is_stale(last_sync_age_h: float | None,
             warn_h: float = STALE_WARN_H, crit_h: float = STALE_CRIT_H) -> str:
    """Staleness verdict for one node: 'ok', 'warn' or 'crit'. A node that has never synced
    (None) is 'crit' — that is the .86 failure mode, silent since 2026-10-03."""
    if last_sync_age_h is None or last_sync_age_h >= crit_h:
        return "crit"
    if last_sync_age_h >= warn_h:
        return "warn"
    return "ok"


# ── IO ────────────────────────────────────────────────────────────────────────
def _log(msg: str, level: str = "info") -> None:
    try:
        from nova_logger import log
        log(msg, level=level, source="nova_claude_cred_sync")
    except Exception:
        print(msg, file=sys.stderr if level != "info" else sys.stdout)


def local_credential() -> str | None:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def remote_digest(host: str) -> str | None:
    """sha256 of the node's current credentials file, computed ON the node. Only the digest
    travels, so nothing secret is in argv, stdout or this process's memory."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
             f"test -f {REMOTE_PATH} && sha256sum < {REMOTE_PATH} | cut -d' ' -f1"],
            capture_output=True, text=True, timeout=20)
        d = (r.stdout or "").strip()
        return d if len(d) == 64 else None
    except Exception:
        return None


def push(host: str, blob_str: str) -> tuple[bool, str]:
    """Write the credential to the remote node's .credentials.json (mode 600, owner = the ssh
    user). The secret is piped over stdin — never in argv, never in a log line."""
    try:
        cmd = ("umask 077; mkdir -p ~/.claude && cat > " + REMOTE_PATH +
               " && chmod 600 " + REMOTE_PATH + " && echo OK")
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, cmd],
                           input=blob_str, capture_output=True, text=True, timeout=25)
        return (r.returncode == 0 and "OK" in r.stdout), r.stderr.strip()[:120]
    except Exception as e:
        return False, str(e)[:120]


def _dsn() -> str:
    try:
        from nova_dsn import dsn  # type: ignore
        return dsn()
    except Exception:
        return os.environ.get("NOVA_PG_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")


def record(results: dict[str, tuple[bool, str]]) -> dict[str, float | None]:
    """Write one health_checks row per node and return each node's pre-run sync age in hours
    (None = never synced), so staleness is judged on history, not on this single run."""
    ages: dict[str, float | None] = {h: None for h in results}
    try:
        import psycopg2
        with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
            for host in results:
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM (now() - max(checked_at)))/3600.0 "
                    "FROM health_checks WHERE service_name='claude_cred' "
                    "AND node_name=%s AND status='up'", (host,))
                row = cur.fetchone()
                ages[host] = float(row[0]) if row and row[0] is not None else None
            for host, (ok, err) in results.items():
                cur.execute(
                    "INSERT INTO health_checks (service_name, node_name, checked_by, status, "
                    "error_message) VALUES ('claude_cred', %s, 'nova_claude_cred_sync', %s, %s)",
                    (host, "up" if ok else "down", err[:200]))
            conn.commit()
    except Exception as e:
        _log(f"cred-sync: could not record health_checks ({type(e).__name__})", level="warning")
    return ages


def main() -> int:
    raw = local_credential()
    if not raw:
        _log("cred-sync: no local Claude credential in Keychain — skipping", level="warning")
        return 0
    try:
        blob = json.loads(raw)
    except Exception:
        _log("cred-sync: local credential is not JSON — skipping", level="warning")
        return 0

    now = int(time.time() * 1000)
    if not is_syncable(blob, now):
        # .6's own token is stale/near-dead — nothing good to push; let it refresh first.
        # nova_claude_token_watch is what pages Jordan about that.
        _log("cred-sync: local token not fresh enough to sync — skipping this cycle")
        return 0

    want = payload_digest(blob)
    ship = json.dumps(sanitize(blob), sort_keys=True, separators=(",", ":"))

    results: dict[str, tuple[bool, str]] = {}
    pushed, skipped, failed = [], [], []
    for host in HOSTS:
        have = remote_digest(host)
        if have == want:
            skipped.append(host)
            results[host] = (True, "already current")
            continue
        ok, err = push(host, ship)
        results[host] = (ok, err)
        if ok:
            pushed.append(host)
        else:
            failed.append(f"{host} ({err or 'unreachable'})")

    ages = record(results)

    # Rot detection: a node that FAILED this run and was already stale is the .86 pattern.
    rotting = {lvl: [] for lvl in ("warn", "crit")}
    for host, (ok, _) in results.items():
        if ok:
            continue
        lvl = is_stale(ages.get(host))
        if lvl in rotting:
            rotting[lvl].append(host)

    if failed:
        try:
            from nova_notify import notify
            if rotting["crit"]:
                notify(f"Claude credential sync: {', '.join(rotting['crit'])} has no valid "
                       f"credential and has not synced in >{STALE_CRIT_H:.0f}h — its `claude` "
                       f"tasks are failing now",
                       level="critical", category="fleet", source="nova_claude_cred_sync",
                       dedup_key="claude-cred-sync-rot-crit")
            elif rotting["warn"]:
                notify(f"Claude credential sync: {', '.join(rotting['warn'])} unsynced for "
                       f">{STALE_WARN_H:.0f}h (access token lives ~8h)",
                       level="warning", category="fleet", source="nova_claude_cred_sync",
                       dedup_key="claude-cred-sync-rot-warn")
            else:
                notify(f"Claude credential sync failed for: {', '.join(failed)}",
                       level="warning", category="fleet", source="nova_claude_cred_sync",
                       dedup_key="claude-cred-sync-fail")
        except Exception:
            pass

    hrs = access_remaining_ms(blob, now) / 3600000.0
    _log(f"cred-sync: {len(pushed)} pushed, {len(skipped)} already current, "
         f"{len(failed)} failed of {len(HOSTS)}; token good for ~{hrs:.1f}h. "
         f"failed={failed or 'none'}",
         level="warning" if failed else "info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
