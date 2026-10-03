#!/usr/bin/env python3
"""Nova fleet secret store — PG + pgcrypto, app-side key, ciphertext-only in DB.

Security model:
  * The DB holds ONLY pgp_sym ciphertext. The master key never enters the DB
    as stored data and never appears in SQL statement text (passed as a bound
    parameter, so it is absent from pg_stat_activity query text and PG logs).
  * Master key + DB password come from the environment, populated by systemd
    LoadCredential (TPM/host-sealed via systemd-creds). They are never typed on
    a command line and never printed.
  * `set` reads the plaintext from STDIN, never argv — so it stays out of the
    process table, shell history, and any transcript.

Env (injected by systemd from $CREDENTIALS_DIRECTORY, or exported for a shell):
  NOVA_SECRET_KEY        pgcrypto master passphrase
  NOVA_SECRETS_DB_PASS   password for the nova_secrets PG role
  NOVA_SECRETS_DSN       e.g. "host=127.0.0.1 port=5432 dbname=nova_ops user=nova_secrets sslmode=prefer"
"""
import os, sys

def _keychain(name):
    """macOS fallback: read a credential from the login Keychain (service = kebab-cased name,
    e.g. NOVA_SECRET_KEY -> nova-secret-key). Returns None off-macOS or if absent."""
    if sys.platform != "darwin":
        return None
    import subprocess
    r = subprocess.run(["security", "find-generic-password", "-s", name.lower().replace("_", "-"), "-w"],
                       capture_output=True, text=True)
    out = r.stdout.rstrip("\n")
    return out if r.returncode == 0 and out else None

def _load(name):
    """Resolve a credential across the fleet: systemd cred dir -> env -> macOS Keychain."""
    cd = os.environ.get("CREDENTIALS_DIRECTORY")           # Linux: systemd LoadCredential(Encrypted)
    if cd:
        p = os.path.join(cd, name)
        if os.path.exists(p):
            with open(p) as f:
                return f.read().rstrip("\n")
    v = os.environ.get(name)                               # Linux: EnvironmentFile (e.g. nova-core5)
    if v:
        return v
    if sys.platform != "darwin":
        return _linux_sealed(name)                         # Linux, outside systemd: sealed cred via sudo
    return _keychain(name)                                 # macOS: Keychain (login or System)


def _linux_sealed(name):
    """Linux fallback for processes NOT started by systemd with LoadCredential (cron, ssh shells,
    the `security` shim, Claude hooks): decrypt /etc/nova/<kebab>.cred with systemd-creds, or read
    /etc/nova/<kebab>.env. Both are root-only; kochj has passwordless sudo fleet-wide, so this grants
    nothing sudo did not already grant. 2026-10-03."""
    import subprocess
    kebab = name.lower().replace("_", "-")
    cred = f"/etc/nova/{kebab}.cred"
    for embedded in (name, kebab):            # .2/.86 were sealed with --name=NOVA_SECRET_KEY, newer nodes with the kebab
        r = subprocess.run(["sudo", "-n", "systemd-creds", "decrypt", f"--name={embedded}", cred, "-"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout.rstrip("\n")
    for env in (f"/etc/nova/{kebab}.env", "/etc/nova/nova-secret.env"):
        r = subprocess.run(["sudo", "-n", "cat", env], capture_output=True, text=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    return None

def _env(name):
    v = _load(name)
    if not v:
        sys.exit(f"[nova_secrets] missing credential: {name}")
    return v

def _optional_env(name):
    """Like _env but returns None instead of exiting when absent."""
    return _load(name)

def _connect(admin=False):
    import psycopg2
    # Services READ via the least-privilege nova_secrets role (SELECT only).
    # Admin ops (set/delete/rotate) use a superuser DSN — never the service role.
    # Default to the primary BY NAME, not 127.0.0.1. The old localhost default silently died
    # when the DB moved off .6 in the DNS cutover: on .6 port 5432 is now a retired pgbouncer
    # shim that answers and then rejects with 'trust authentication failed'. Every consumer of
    # this module lost its credentials on 2026-07-16 and none of them said so — the Burbank PD
    # and Verdugo Fire scanner feeds simply stopped producing while the journal kept publishing
    # from a different city's radio. Resolving by name is also correct ON .2, where the name
    # points at the local machine anyway.
    if admin:
        dsn = os.environ.get("NOVA_SECRETS_ADMIN_DSN",
                             "host=pg-primary.digitalnoise.net port=5432 dbname=nova_ops "
                             "user=kochj sslmode=prefer")
    else:
        dsn = os.environ.get("NOVA_SECRETS_DSN",
                             "host=pg-primary.digitalnoise.net port=5432 dbname=nova_ops "
                             "user=nova_secrets sslmode=prefer")
    # DB password only if pg_hba requires it; local/LAN trust needs none.
    # (Protection is the master key, which is NOT in the DB — not the DB role.)
    pw = _optional_env("NOVA_SECRETS_ADMIN_PASS" if admin else "NOVA_SECRETS_DB_PASS")
    return psycopg2.connect(dsn, **({"password": pw} if pw else {}))

# ── public API (import and call from services) ─────────────────────────────────

def get_secret(name):
    """Return plaintext for `name`, decrypted in-process. Raises on unknown name/bad key."""
    key = _env("NOVA_SECRET_KEY")
    with _connect() as c, c.cursor() as cur:
        # key is a BOUND PARAMETER -> never in logged statement text / pg_stat_activity value
        cur.execute("SELECT pgp_sym_decrypt(ciphertext, %(k)s) FROM nova.secrets WHERE name=%(n)s",
                    {"k": key, "n": name})
        row = cur.fetchone()
    if not row:
        raise KeyError(f"secret not found: {name}")
    return row[0]

def _vault_token_rw():
    """Read-WRITE 1Password service-account token (vault Nova only). Present on .6 (System keychain
    item nova-op-token-rw) so Nova can create/rotate secrets herself; absent elsewhere -> no vault write."""
    t = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN_RW") or _load("OP_TOKEN_RW")
    if not t and sys.platform == "darwin":
        t = _keychain("nova-op-token-rw")
    return t


def _vault_put(name, value, note=None):
    """Mirror a secret INTO the 1Password vault "Nova" (item title = name) so 1Password stays the
    source of truth when Nova writes a secret. Silently no-op without the rw token or `op`.
    ponytail: value crosses argv on `op item edit` (brief, local); use --template if that ever matters."""
    import json, shutil, subprocess, tempfile
    tok = _vault_token_rw()
    if not tok or not shutil.which("op"):
        return False
    env = {**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": tok}
    r = subprocess.run(["op", "item", "get", name, "--vault", "Nova", "--format=json"],
                       capture_output=True, text=True, env=env, timeout=60)
    if r.returncode == 0:
        item_id = json.loads(r.stdout)["id"]
        r = subprocess.run(["op", "item", "edit", item_id, "--vault", "Nova", f"password={value}"],
                           capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
    else:
        tpl = {"title": name, "category": "PASSWORD", "tags": ["nova", "nova-written"],
               "fields": [{"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": value}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            os.chmod(f.name, 0o600); json.dump(tpl, f); path = f.name
        try:
            r = subprocess.run(["op", "item", "create", "--vault", "Nova", "--template", path],
                               capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)  # op refuses template+piped stdin
        finally:
            os.unlink(path)
    if r.returncode != 0:
        sys.stderr.write(f"[nova_secrets] vault write failed for {name}: {(r.stderr or r.stdout).strip()[:200]}\n")
    return r.returncode == 0


def set_secret(name, value, note=None):
    """Upsert ciphertext for `name` in nova.secrets AND mirror it into the 1Password vault (if this
    host holds the rw token). `value` is encrypted in the query via bound params."""
    key = _env("NOVA_SECRET_KEY")
    with _connect(admin=True) as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO nova.secrets(name, ciphertext, note) "
            "VALUES(%(n)s, pgp_sym_encrypt(%(v)s, %(k)s), %(note)s) "
            "ON CONFLICT (name) DO UPDATE SET ciphertext=EXCLUDED.ciphertext, "
            "note=COALESCE(EXCLUDED.note, nova.secrets.note), updated_at=now(), updated_by=current_user",
            {"n": name, "v": value, "k": key, "note": note})
        c.commit()
    if not (note or "").startswith("1Password:"):      # don't echo the vault->store mirror back into the vault
        _vault_put(name, value, note)

def list_secrets():
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT name, note, updated_at, updated_by FROM nova.secrets ORDER BY name")
        return cur.fetchall()

def delete_secret(name):
    with _connect(admin=True) as c, c.cursor() as cur:
        cur.execute("DELETE FROM nova.secrets WHERE name=%s", (name,))
        c.commit()

# ── CLI ────────────────────────────────────────────────────────────────────────

def _selftest():
    """End-to-end round trip with a THROWAWAY secret; prints only MATCH/FAIL, never a real value."""
    import base64, hashlib
    probe = "__selftest_probe__"
    # deterministic throwaway plaintext derived from the key hash — not a real secret
    val = "throwaway-" + hashlib.sha256(_env("NOVA_SECRET_KEY").encode()).hexdigest()[:16]
    set_secret(probe, val, note="selftest — safe to delete")
    got = get_secret(probe)
    delete_secret(probe)
    print("SELFTEST: MATCH ✓" if got == val else "SELFTEST: FAIL ✗")
    return 0 if got == val else 1

def main():
    if len(sys.argv) < 2:
        sys.exit("usage: nova_secrets.py get <name> | set <name> [note] | list | delete <name> | selftest")
    cmd = sys.argv[1]
    if cmd == "get":
        sys.stdout.write(get_secret(sys.argv[2]))          # for service use; no newline noise
    elif cmd == "set":
        note = sys.argv[3] if len(sys.argv) > 3 else None
        val = sys.stdin.read().rstrip("\n")                # plaintext via STDIN, never argv
        set_secret(sys.argv[2], val, note)
        print(f"stored: {sys.argv[2]}")                    # name only, never the value
    elif cmd == "list":
        for n, note, ts, by in list_secrets():
            print(f"  {n:30} {str(ts)[:19]}  {by:12} {note or ''}")
    elif cmd == "delete":
        delete_secret(sys.argv[2]); print(f"deleted: {sys.argv[2]}")
    elif cmd == "selftest":
        sys.exit(_selftest())
    else:
        sys.exit(f"unknown command: {cmd}")

if __name__ == "__main__":
    main()
