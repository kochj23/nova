#!/usr/bin/env python3
"""nova_op_sync.py — mirror the 1Password vault "Nova" into the fleet secret store (nova.secrets).

1Password is where humans create/rotate secrets; nova.secrets is what the fleet reads so Nova
keeps running when the WAN or 1Password is down. Runs on .6 hourly (scheduler task op_sync).
Auth: 1Password service account token (read-only, Nova vault only) from the System keychain
item nova-op-token on macOS, or OP_SERVICE_ACCOUNT_TOKEN / systemd cred op-token on Linux.
Naming: each item's TITLE is the secret name. One concealed field -> stored under the title;
several -> stored as "<title>/<field label>". Never prints a value.
ponytail: full re-sync every run (47 items) instead of change detection; add `op item list`
updated_at diffing if the vault grows past a few hundred items.
"""
import json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nova_secrets import set_secret, _load  # noqa: E402

VAULT = "Nova"

def _token():
    t = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN") or _load("OP_TOKEN")
    if not t and sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", "nova-op-token", "-w"],
                           capture_output=True, text=True)
        t = r.stdout.strip() if r.returncode == 0 else ""
    if not t:
        sys.exit("[op_sync] no 1Password service-account token (nova-op-token / OP_SERVICE_ACCOUNT_TOKEN)")
    return t

def _op(args, tok):
    r = subprocess.run(["op", *args, "--format=json"], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": tok})
    if r.returncode != 0:
        raise RuntimeError(f"op {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)

def main():
    tok = _token()
    items = _op(["item", "list", "--vault", VAULT], tok)
    stored = skipped = 0
    for it in items:
        full = _op(["item", "get", it["id"], "--vault", VAULT], tok)
        secret_fields = [f for f in full.get("fields", []) if f.get("type") == "CONCEALED" and f.get("value")]
        if not secret_fields:
            skipped += 1
            continue
        title = full["title"].strip()
        for f in secret_fields:
            name = title if len(secret_fields) == 1 else f"{title}/{f.get('label') or f['id']}"
            set_secret(name, f["value"], note=f"1Password:{VAULT}/{it['id']} ({full.get('category','')})")
            stored += 1
    print(f"op_sync: {len(items)} items in vault {VAULT}; {stored} secrets stored, {skipped} items without concealed fields")

if __name__ == "__main__":
    main()
