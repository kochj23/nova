#!/usr/bin/env python3
"""nova_vault_to_keychain.py — on a Mac, ADD every item of the 1Password vault "Nova" that is not
yet in /Library/Keychains/System.keychain (service = item title, account = username or "nova"),
so `security find-generic-password -s NAME -w` works for root daemons and users with no GUI login.
NEVER modifies an existing item: updating a System keychain item pops a macOS authorization
dialog per item (2026-10-03, 15 prompts on Jordan's screen). Adding as root is silent.
Rotation = delete the item by hand once (`security delete-generic-password -s NAME System.keychain`)
and the next run re-adds it. Runs as root from a LaunchDaemon hourly. Never prints a value."""
import json, os, subprocess, sys
SYS = "/Library/Keychains/System.keychain"
def sh(*a, **k): return subprocess.run(a, capture_output=True, text=True, **k)
tok = sh("security", "find-generic-password", "-s", "nova-op-token", "-w", SYS).stdout.strip()
if not tok: sys.exit("no nova-op-token in System keychain")
env = {"OP_SERVICE_ACCOUNT_TOKEN": tok, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/var/root"}
r = sh("op", "item", "list", "--vault", "Nova", "--format=json", env=env, timeout=90)
if r.returncode != 0: sys.exit("op item list failed: " + r.stderr.strip()[:120])
items = json.loads(r.stdout)
trust = [a for p in ("/usr/bin/security", "/usr/bin/python3", "/opt/homebrew/bin/python3") if os.path.exists(p) for a in ("-T", p)]
added = present = failed = 0; err = ""
for it in items:
    if sh("security", "find-generic-password", "-s", it["title"], SYS).returncode == 0:
        present += 1; continue                                   # exists -> leave it alone, no prompt
    full = json.loads(sh("op", "item", "get", it["id"], "--vault", "Nova", "--format=json", env=env, timeout=60).stdout)
    fields = full.get("fields", [])
    pw = next((x["value"] for x in fields if x.get("type") == "CONCEALED" and x.get("value")), None)
    if not pw: continue
    acct = next((x["value"] for x in fields if x.get("id") == "username" and x.get("value")), "nova")
    r = sh("security", "add-generic-password", "-a", acct, "-s", full["title"], *trust, "-w", pw, SYS)
    if r.returncode == 0: added += 1
    else:
        failed += 1; err = err or (r.stderr or r.stdout).strip()[:120]
print(f"vault_to_keychain: {len(items)} vault items; {present} already present, {added} added, {failed} failed" + (f" | first error: {err}" if err else ""))
