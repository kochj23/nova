#!/usr/bin/env python3
"""nova_seed_fleet_secrets.py — one-shot: copy secrets from the macOS Keychain (.6)
into the fleet pgcrypto store (nova.secrets on the PG PRIMARY) so the Linux cluster
nodes can read them via nova_secrets.get_secret(). #502 migration enabler.

SAFETY: token plaintext NEVER leaves this process and NEVER enters a transcript.
It goes  Keychain -> in-process memory -> pgp_sym_encrypt (bound param) -> ciphertext
in PG.  The only things printed are the secret NAME and a stored/verified status —
never a value.  Verification compares in memory (got == source), it does not echo.

Run this ON .6 (macOS): it reads the source tokens from the Keychain, and
nova_secrets resolves NOVA_SECRET_KEY from the Keychain too (service 'nova-secret-key').

Usage:  python3 nova_seed_fleet_secrets.py             # seed the gateway wave-1 set
        python3 nova_seed_fleet_secrets.py a-svc b-svc # seed specific service names
Written by Jordan Koch (via Claude).
"""
import os
import subprocess
import sys

# Target the PG PRIMARY so seeded secrets replicate to the whole fleet. Run from .6
# (source Keychain) but WRITE to .2 — 127.0.0.1 on .6 is a local, non-replicating store.
# ponytail: .2 hardcoded = current primary; override both envs if the primary moves.
os.environ.setdefault("NOVA_SECRETS_ADMIN_DSN",
                      "host=192.168.1.2 port=5432 dbname=nova_ops user=kochj sslmode=prefer")
os.environ.setdefault("NOVA_SECRETS_DSN",
                      "host=192.168.1.2 port=5432 dbname=nova_ops user=nova_secrets sslmode=prefer")

import nova_secrets  # noqa: E402  (after DSN defaults so it picks them up)

# Gateway wave-1 credential set. Keychain service name == fleet store name, so the
# gateway's keychain("nova-slack-bot-token") resolves the same key in either place.
GATEWAY_SECRETS = [
    "nova-slack-bot-token",
    "nova-slack-app-token",
    "nova-discord-token",
    "nova-openrouter-api-key",
]


def keychain_read(service, account="nova"):
    """Read a secret from the login Keychain (tries -a nova, then no-account)."""
    for args in (["-a", account, "-s", service, "-w"], ["-s", service, "-w"]):
        r = subprocess.run(["security", "find-generic-password", *args],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def seed(names):
    if sys.platform != "darwin":
        sys.exit("[seed] run this on .6 (macOS) — it reads the source tokens from the Keychain")
    ok = fail = 0
    for name in names:
        val = keychain_read(name)
        if not val:
            print(f"  SKIP  {name:26} (not in Keychain)")
            fail += 1
            continue
        nova_secrets.set_secret(name, val, note="seeded from .6 Keychain (#502)")
        verified = nova_secrets.get_secret(name) == val   # compared, never printed
        print(f"  {'OK   ' if verified else 'BAD  '}{name:26} {'stored + verified' if verified else 'MISMATCH'}")
        ok += verified
        fail += (not verified)
    print(f"[seed] {ok} stored+verified, {fail} skipped/failed, of {len(names)}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(seed(sys.argv[1:] or GATEWAY_SECRETS))
