#!/usr/bin/env python3
"""nova_keychain_to_vault.py — make the 1Password vault "Nova" the source of truth for every
secret the Nova code reads from the macOS Keychain or the fleet store.

Collects secret names from: (1) every `security find-generic-password -s <name>` in the scripts
and Claude hooks/MCP, (2) every login-Keychain item with account "nova", (3) nova.secrets names.
For each name that is NOT already a vault item: reads the value (Keychain first, fleet store
second) and creates a Password item titled <name>. Needs the desktop-app CLI session (writes);
the read-only service account cannot create items. Never prints a value. Run on .6, once;
afterwards nova_op_sync.py mirrors vault -> nova.secrets hourly.
"""
import json, os, re, subprocess, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nova_secrets import get_secret, list_secrets  # noqa: E402

VAULT = "Nova"
HOME = os.path.expanduser("~")
SCAN = glob.glob(f"{HOME}/.openclaw/scripts/*.py") + glob.glob(f"{HOME}/.openclaw/scripts/*.sh") \
     + glob.glob(f"{HOME}/.claude/hooks/*") + glob.glob(f"{HOME}/.claude/mcp-servers/*/server.py")
RX = re.compile(r'find-generic-password[^\n]*?-s["\',\s]+([A-Za-z0-9._-]+)')

def keychain_names():
    names = set()
    for f in SCAN:
        try: names |= set(RX.findall(open(f, errors="ignore").read()))
        except OSError: pass
    dump = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True).stdout
    cur = None
    for line in dump.splitlines():
        m = re.search(r'"svce"<blob>="([^"]+)"', line)
        if m: cur = m.group(1)
        if '"acct"<blob>="nova"' in line and cur: names.add(cur)
    return {n for n in names if not n.startswith("$")}

def keychain_value(name):
    r = subprocess.run(["security", "find-generic-password", "-s", name, "-w"], capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout.strip() else None

def vault_titles():
    r = subprocess.run(["op", "item", "list", "--vault", VAULT, "--format=json"], capture_output=True, text=True, timeout=90)
    return {i["title"] for i in json.loads(r.stdout)} if r.returncode == 0 else set()

def create(name, value, source):
    r = subprocess.run(["op", "item", "create", "--vault", VAULT, "--category=Password", "--title", name,
                        f"password={value}", "--tags", f"nova,{source}", "--format=json"],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0

def main():
    have = vault_titles()
    names = keychain_names()
    fleet = {row[0] for row in list_secrets()}
    todo = sorted((names | fleet) - have)
    created, missing, failed = [], [], []
    for n in todo:
        v = keychain_value(n); src = "keychain"
        if v is None and n in fleet:
            try: v = get_secret(n); src = "fleet-store"
            except Exception: v = None
        if v is None: missing.append(n); continue
        (created if create(n, v, src) else failed).append(n)
    print(f"vault had {len(have)}; keychain names {len(names)}, fleet names {len(fleet)}; "
          f"created {len(created)}, no value anywhere {len(missing)}, failed {len(failed)}")
    if missing: print("no value anywhere (referenced in code but absent from Keychain and fleet store):", ", ".join(missing))
    if failed: print("FAILED to create:", ", ".join(failed))

if __name__ == "__main__":
    main()
