#!/usr/bin/env python3
"""nova_threshold.py — THE THRESHOLD LEDGER: every invitation into the house, written down.

From Stoker's Dracula: Van Helsing tells the others the Count cannot first enter a place
unless someone of the household bids him in, but once bidden he may come and go as he
likes (Ch. XVIII). Later Renfield, the household's own inmate, admits that he raised the
sash and called the Count in himself (Ch. XXI). The danger is not the door; it is the
standing invitation that nobody remembers giving, and the trusted insider who gives it.
Nova's version: every key, token and allowlist entry that lets someone into or over Nova is
one row, with blank owner, purpose and expiry columns that only a human fills. A row with no
purpose is an invitation nobody can account for, and the ledger lists every one of them.

Minimal first version:
  * ssh_key      authorized_keys on every node in node_status (local read, or read-only SSH):
                 fingerprint, comment, forced command, from= (sender-bound), expiry-time=
  * slack_token  name and last-rotated time of Slack tokens in the fleet secret store
                 (metadata only: the value column is never selected)
  * herd_sender  the herd email allowlist (herd_config.HERD)
Public keys and fingerprints only; no secret value is ever read, printed or stored.
Each --run upserts the ledger (owner / purpose / expiry are never overwritten), prints every
current row with no purpose, and files one claude_queue item a week proposing that Little
Mister give them a purpose, an owner and an expiry. Revocation stays his decision.

CLI:   --run [--dry-run]   --show   --selftest
Table: threshold_ledger (PK kind, host, ident)
Config: service_config threshold/rotation_days (default 180)
Schedule: weekly, Tuesday 04:15.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

OPENCLAW = Path(__file__).resolve().parents[1]          # herd_config.py lives here
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
QUEUE_SESSION = "nova-threshold"
QUEUE_DESC = "Threshold Ledger: invitations into Nova with no stated purpose"
RESURFACE_DAYS = 7
CURRENT_DAYS = 8          # a row seen within this window is a live invitation

SCHEMA = """
CREATE TABLE IF NOT EXISTS threshold_ledger (
  kind text NOT NULL, host text NOT NULL DEFAULT '', ident text NOT NULL,
  label text, fingerprint text, sender_bound boolean, detail jsonb,
  invited_by text, owner text, purpose text, expires_at timestamptz, last_rotated timestamptz,
  first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, host, ident));
"""


def log(m: str) -> None:
    print(f"[threshold {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── authorized_keys (pure) ──────────────────────────────────────────────────

# ponytail: finds the first "<keytype> <base64>" pair; a forced command that itself embeds a
# public key would confuse it. A full sshd option tokenizer is the ceiling.
KEY_RE = re.compile(r"(?:^|\s)((?:ssh|ecdsa|sk)-[\w@.-]+)\s+([A-Za-z0-9+/]+={0,2})(?:\s+(.*))?$")
QUOTED = r'"((?:[^"\\]|\\.)*)"'


def fingerprint(blob_b64: str) -> str | None:
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except ValueError:
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def parse_expiry(s: str | None):
    """sshd expiry-time YYYYMMDD[HHMM[SS]] -> aware datetime (read as UTC), or None."""
    # ponytail: sshd reads expiry-time in the server's local zone (or a trailing Z); UTC is close enough for a ledger.
    s = (s or "").rstrip("Zz")
    fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(s))
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc) if fmt and s.isdigit() else None
    except ValueError:
        return None


def parse_authorized_keys(text: str, host: str) -> list:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        m = KEY_RE.search(line) if line and not line.startswith("#") else None
        fp = fingerprint(m[2]) if m else None
        if not fp:
            continue
        opts = line[:m.start(1)].strip()
        cmd = re.search(r"command=" + QUOTED, opts)
        exp = re.search(r"expiry-time=" + QUOTED, opts)
        rows.append({"kind": "ssh_key", "host": host, "ident": fp, "label": (m[3] or "").strip() or None,
                     "fingerprint": fp, "sender_bound": bool(re.search(r"(^|,)from=", opts)),
                     "detail": {"type": m[1], "forced_command": cmd[1] if cmd else None,
                                "options": sorted(set(re.findall(r"(?:^|,)([a-z-]+)", re.sub(QUOTED, "", opts))))},
                     "expires_at": parse_expiry(exp[1]) if exp else None, "last_rotated": None})
    return rows


# ── sources ─────────────────────────────────────────────────────────────────

def _ssh_cat(ip: str):
    r = subprocess.run(["ssh", *SSH_OPTS, f"{getpass.getuser()}@{ip}",
                        "cat ~/.ssh/authorized_keys 2>/dev/null; true"],
                       capture_output=True, text=True, timeout=30)
    return ("ok", r.stdout) if r.returncode == 0 else None


def host_keys(name: str, ip: str, _sleep=None) -> list | None:
    """Rows for one node, or None if it could not be read (its rows are then left as they were)."""
    from nova_fleet_exec import is_local
    if is_local(ip):
        p = Path.home() / ".ssh" / "authorized_keys"
        return parse_authorized_keys(p.read_text() if p.exists() else "", name)
    res = W.retry(_ssh_cat, ip, attempts=3, delay=3.0, tag="threshold", _sleep=_sleep)
    return parse_authorized_keys(res[1], name) if res else None


def fleet_keys(cur, _sleep=None) -> list:
    out = []
    for name, ip in _q(cur, "SELECT node_name, host(node_ip) FROM node_status ORDER BY node_name"):
        rows = host_keys(name, ip, _sleep=_sleep)
        if rows is None:
            log(f"{name}: unreachable, keys not refreshed")
        out += rows or []
    return out


def slack_tokens(cur) -> list:
    # metadata only: name + updated_at. The value column is never selected.
    return [{"kind": "slack_token", "host": "", "ident": n, "label": n, "fingerprint": None,
             "sender_bound": False, "detail": {"store": "nova.secrets"}, "expires_at": None,
             "last_rotated": ts}
            for n, ts in _q(cur, "SELECT name, updated_at FROM nova.secrets WHERE name ILIKE %s ORDER BY name",
                            ("%slack%",))]


def herd_senders() -> list:
    if str(OPENCLAW) not in sys.path:
        sys.path.append(str(OPENCLAW))
    try:
        from herd_config import HERD
    except Exception as e:  # noqa: BLE001 — no herd config means no herd rows, not an error
        log(f"herd_config unavailable: {e}")
        return []
    return [{"kind": "herd_sender", "host": "", "ident": (m.get("email") or "").lower(), "label": m.get("name"),
             "fingerprint": None, "sender_bound": False, "detail": {"profile": m.get("profile")},
             "expires_at": None, "last_rotated": None}
            for m in HERD if m.get("email")]


def inventory(cur, _sleep=None) -> list:
    return fleet_keys(cur, _sleep=_sleep) + slack_tokens(cur) + herd_senders()


# ── flags (pure) ────────────────────────────────────────────────────────────

def flags(row: dict, rotation_days: int = 180, now=None) -> list:
    now = now or datetime.now(timezone.utc)
    f = [k for k in ("owner", "purpose") if not row.get(k)]
    f += ["expiry"] if not row.get("expires_at") else ["expired"] if row["expires_at"] < now else []
    if row.get("last_rotated") and now - row["last_rotated"] > timedelta(days=rotation_days):
        f.append("rotation")
    return ["no " + x if x in ("owner", "purpose", "expiry") else x for x in f]


def merge(found: list, stored: dict) -> list:
    """Overlay the human-filled columns from the stored ledger onto fresh rows."""
    for r in found:
        s = stored.get((r["kind"], r["host"], r["ident"]), {})
        for k in ("owner", "purpose", "invited_by"):
            r[k] = s.get(k)
        r["expires_at"] = s.get("expires_at") or r["expires_at"]
        r["new"] = bool(stored) and not s
    return found


def line(r: dict) -> str:
    where = f"{r['host']}:" if r["host"] else ""
    return f"{r['kind']:<11} {where}{r['label'] or '-'}  {(r['fingerprint'] or '')[:20]}"


# ── PG ──────────────────────────────────────────────────────────────────────

def stored(cur) -> dict:
    exists = _q(cur, "SELECT to_regclass('threshold_ledger')")
    if not exists or exists[0][0] is None:
        return {}
    rows = _q(cur, "SELECT kind, host, ident, owner, purpose, invited_by, expires_at FROM threshold_ledger")
    return {(k, h, i): {"owner": o, "purpose": p, "invited_by": b, "expires_at": e} for k, h, i, o, p, b, e in rows}


def upsert(cur, rows: list) -> None:
    ensure_schema(cur)
    for r in rows:
        cur.execute(
            "INSERT INTO threshold_ledger (kind, host, ident, label, fingerprint, sender_bound, detail, "
            "expires_at, last_rotated) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) "
            "ON CONFLICT (kind, host, ident) DO UPDATE SET label=EXCLUDED.label, "
            "fingerprint=EXCLUDED.fingerprint, sender_bound=EXCLUDED.sender_bound, detail=EXCLUDED.detail, "
            "expires_at=COALESCE(threshold_ledger.expires_at, EXCLUDED.expires_at), "
            "last_rotated=EXCLUDED.last_rotated, last_seen=now()",
            (r["kind"], r["host"], r["ident"], r["label"], r["fingerprint"], r["sender_bound"],
             json.dumps(r["detail"], default=str), r["expires_at"], r["last_rotated"]))


def file_queue(cur, unpurposed: list, new: list) -> int | None:
    """One claude_queue item per RESURFACE_DAYS. Returns its id."""
    rows = _q(cur, "SELECT id FROM claude_queue WHERE description=%s AND created_at > now() - make_interval(days => %s) "
                   "ORDER BY id DESC LIMIT 1", (QUEUE_DESC, RESURFACE_DAYS))
    if rows:
        return rows[0][0]
    ctx = (f"{len(unpurposed)} invitation(s) into Nova have no purpose (nova_threshold.py). Propose an owner, "
           "purpose and expiry for each (UPDATE threshold_ledger SET owner=..., purpose=..., expires_at=...), "
           "or propose rotation/revocation to Little Mister. Revocation is his decision.\n\n"
           + "\n".join(line(r) for r in unpurposed[:150])
           + ("\n\nNew since the last run:\n" + "\n".join(line(r) for r in new) if new else ""))
    # claude_queue.session_id is a foreign key: register this organ's session first (2026-10-09 audit).
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "VALUES (%s,'pending',4,%s,%s) RETURNING id", (QUEUE_SESSION, QUEUE_DESC, ctx))
    return cur.fetchone()[0]


def run(dry: bool = False, _sleep=None) -> list:
    conn = W.connect()
    try:
        cur = conn.cursor()
        rot = _q(cur, "SELECT value FROM service_config WHERE service='threshold' AND key='rotation_days'")
        rotation_days = int(rot[0][0]) if rot else 180
        rows = merge(inventory(cur, _sleep=_sleep), stored(cur))
        for r in rows:
            r["flags"] = flags(r, rotation_days)
        unpurposed = [r for r in rows if "no purpose" in r["flags"]]
        new = [r for r in rows if r["new"]]
        counts = {}
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        log(f"{'DRY RUN ' if dry else ''}{len(rows)} invitations {counts}; {len(unpurposed)} with no purpose; "
            f"{len(new)} new")
        for r in rows:
            print(f"  {'NEW ' if r['new'] else '    '}{line(r)}  [{', '.join(r['flags']) or 'ok'}]")
        if not dry:
            upsert(cur, rows)
            qid = file_queue(cur, unpurposed, new) if unpurposed else None
            log(f"ledger upserted; claude_queue #{qid}" if qid else "ledger upserted")
        return rows
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        rows = _q(conn.cursor(), "SELECT kind, host, label, fingerprint, owner, purpose, expires_at, last_seen "
                                 "FROM threshold_ledger WHERE last_seen > now() - make_interval(days => %s) "
                                 "ORDER BY purpose IS NOT NULL, kind, host, label", (CURRENT_DAYS,))
        for k, h, lb, fp, o, p, e, seen in rows:
            print(f"{k:<11} {h + ':' if h else ''}{lb or '-'}  {(fp or '')[:20]}  owner={o or '-'} "
                  f"purpose={p or 'NONE'} expires={e or '-'} seen={seen:%Y-%m-%d}")
        if not rows:
            print("ledger empty")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    blob = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 36).decode()
    text = (f'# comment\n\ncommand="~/bin/gate.sh",from="*.example.org",expiry-time="20270101" ssh-ed25519 {blob} gate\n'
            f"ssh-ed25519 {blob} me@laptop\nssh-rsa !!notbase64 junk\n")
    rows = parse_authorized_keys(text, "h")
    assert len(rows) == 2, rows
    a, b = rows
    assert a["detail"]["forced_command"] == "~/bin/gate.sh" and a["sender_bound"] and not b["sender_bound"], a
    assert a["expires_at"] == datetime(2027, 1, 1, tzinfo=timezone.utc) and a["fingerprint"].startswith("SHA256:")
    assert set(a["detail"]["options"]) == {"command", "from", "expiry-time"}, a["detail"]
    assert b["label"] == "me@laptop" and b["expires_at"] is None
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert flags(b, now=now) == ["no owner", "no purpose", "no expiry"]
    ok = dict(b, owner="x", purpose="y", expires_at=now + timedelta(days=1), last_rotated=now - timedelta(days=400))
    assert flags(ok, now=now) == ["rotation"]
    m = merge([dict(b)], {("ssh_key", "h", b["ident"]): {"purpose": "backup", "owner": "j"}})
    assert m[0]["purpose"] == "backup" and not m[0]["new"]
    assert merge([dict(b)], {("x", "", "y"): {}})[0]["new"] and not merge([dict(b)], {})[0]["new"]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="inventory invitations, upsert ledger, file the no-purpose list")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the ledger, write nothing")
    ap.add_argument("--show", action="store_true", help="current ledger, rows with no purpose first")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
