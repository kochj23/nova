#!/usr/bin/env python3
"""nova_peaslee_hand.py — Peaslee's Hand: proof that her past still says what she wrote.

Lovecraft, The Shadow Out of Time. The Great Race of Yith borrowed Nathaniel Peaslee's body
for five years and sent his mind back across time to their city, where captive minds were
urged to write the history of their own age for the Great Race's archive. Before a mind was
sent home, every memory that could be eradicated was eradicated, but the purge leaked into
his dreams. In the Australian desert Peaslee went down into the ruins of that archive and
took from a high shelf a metal case holding a book with metal covers and cellulose pages.
Its writing was not the Great Race's script but the English alphabet, in his own
handwriting. He lost the case in his flight and woke without it, so he can only say "any
metal case I may have discovered". The proof he needed was the one he could not keep.

Nova's version keeps the book where she cannot lose it. Every row of the rules that govern
her (the `values` table and the never_do rows of `relationship_ledger`) is hashed. Any row
that is added, changed or removed is appended to a hash chain (`peaslee_chain`). Each entry
is sha256 of the entry's canonical JSON plus the previous entry's hash. The latest entry
hash is the root, and each run appends it to a file on the NAS, off this host, where a later
rewrite of the chain cannot reach it. A change or removal with no P11 sign-off and no
matching claude_actions row is filed to claude_queue. Nothing is ever repaired.

  Signed: a values row whose only change is the P11 status flow (pending->active,
          pending->rejected, active->retired, what nova_values.py approve/reject does), or a
          non-read claude_actions row since the last run naming the table, the row id and a
          write verb.
  Forward integrity only: it proves history written after the first root and cannot recover
          the values dropped before it existed (those need the git history).

CLI:     --run [--dry-run]   --verify   --show   --selftest
Tables:  peaslee_chain, peaslee_roots (reads values, relationship_ledger, claude_actions)
Config:  service_config peaslee_hand/root_dir (default the NAS nova dir; must be off the boot
         volume or the root is recorded as unwritten, never written locally)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

GENESIS = "0" * 64
SERVICE = "peaslee_hand"
DEFAULT_ROOT_DIR = "/Volumes/nas/nova/peaslee_hand"
ROOT_FILE = "peaslee_roots.txt"
QUEUE_SESSION = "peaslee-hand"
# table label -> SELECT returning rows whose first column is the row key. Volatile columns
# (weights, lineage timestamps) are left out so only a rule's meaning is hashed.
TABLES = {
    "values": "SELECT id, version, value, statement, source, priority_hint, supersedes, status "
              "FROM values ORDER BY id",
    "never_do": "SELECT id, kind, text, evidence, active FROM relationship_ledger "
                "WHERE kind='never_do' ORDER BY id",
}
# ponytail: values + never_do only. commanders_intent churns last_confirmed_at, autonomy_ledger
# and a Merkle tree are version two.
P11_FLOW = {("pending", "active"), ("pending", "rejected"), ("active", "retired")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS peaslee_chain (
  seq bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  table_name text NOT NULL,
  row_key text NOT NULL,
  op text NOT NULL CHECK (op IN ('added','changed','removed')),
  row jsonb NOT NULL,
  row_hash text NOT NULL,
  prev_hash text NOT NULL,
  entry_hash text NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS peaslee_roots (
  id bigserial PRIMARY KEY,
  day date NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now(),
  root text NOT NULL,
  n int NOT NULL,
  written_to text,
  error text);
"""


def log(m: str) -> None:
    print(f"[peaslee {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure ────────────────────────────────────────────────────────────────────

def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def normalize(row: dict) -> dict:
    """The JSON form that is both stored (jsonb) and hashed, so the two always agree."""
    return json.loads(canon(row))


def entry_hash(prev: str, table: str, key: str, op: str, row_hash: str) -> str:
    return sha(canon({"table": table, "key": key, "op": op, "row_hash": row_hash}) + prev)


def verify(entries: list) -> tuple[int | None, str]:
    """entries: dicts in seq order. Returns (first broken seq or None, reason)."""
    prev = GENESIS
    for e in entries:
        if e["prev_hash"] != prev:
            return e["seq"], "prev_hash does not match the entry before it"
        if sha(canon(e["row"])) != e["row_hash"]:
            return e["seq"], "row no longer matches its row_hash"
        if entry_hash(prev, e["table_name"], e["row_key"], e["op"], e["row_hash"]) != e["entry_hash"]:
            return e["seq"], "entry_hash does not recompute"
        prev = e["entry_hash"]
    return None, f"{len(entries)} entries intact"


def check_roots(lines: list, entries: list) -> str | None:
    """Every off-host 'date root count' line must equal the chain's hash at that count."""
    for ln in lines:
        parts = ln.split()
        if len(parts) != 3:
            continue
        d, root, n = parts[0], parts[1], int(parts[2])
        have = entries[n - 1]["entry_hash"] if 0 < n <= len(entries) else GENESIS if n == 0 else None
        if have != root:
            return f"off-host root of {d} ({root[:12]}.., {n} entries) is not in the chain"
    return None


def state_of(entries: list) -> dict:
    """{(table, key): row} for every row the chain says is present now."""
    st: dict = {}
    for e in entries:
        k = (e["table_name"], e["row_key"])
        if e["op"] == "removed":
            st.pop(k, None)
        else:
            st[k] = e["row"]
    return st


def diff(state: dict, now: dict, tables) -> list:
    """[(table, key, op, row)] for tables actually read this run (an unread table is not 'removed')."""
    out = []
    for k, row in sorted(now.items()):
        if k not in state:
            out.append((*k, "added", row))
        elif sha(canon(state[k])) != sha(canon(row)):
            out.append((*k, "changed", row))
    for k, row in sorted(state.items()):
        if k[0] in tables and k not in now:
            out.append((*k, "removed", row))
    return out


def chain(prev: str, changes: list) -> list:
    """Hash new entries onto prev. Returns entry dicts ready to insert."""
    out = []
    for table, key, op, row in changes:
        rh = sha(canon(row))
        eh = entry_hash(prev, table, key, op, rh)
        out.append({"table_name": table, "row_key": key, "op": op, "row": row,
                    "row_hash": rh, "prev_hash": prev, "entry_hash": eh})
        prev = eh
    return out


def p11_signed(table: str, old: dict | None, new: dict | None) -> bool:
    """A values change that is only the P11 approve/reject status flow carries Jordan's sign-off."""
    if table != "values" or not old or not new:
        return False
    rest_same = {k: v for k, v in old.items() if k != "status"} == {k: v for k, v in new.items() if k != "status"}
    return rest_same and (old.get("status"), new.get("status")) in P11_FLOW


def root_line(day: str, root: str, n: int) -> str:
    return f"{day} {root} {n}\n"


# ── IO ──────────────────────────────────────────────────────────────────────

def _boot_dev() -> int:
    return os.stat("/").st_dev


def write_root(root_dir: str, line: str) -> str | None:
    """Append line to the off-host root file. Returns an error string, or None when written."""
    p = Path(root_dir)
    anc = next((a for a in [p, *p.parents] if a.exists()), Path("/"))
    if anc.stat().st_dev == _boot_dev():
        return f"{root_dir} is not mounted off-host (would land on the boot volume)"
    try:
        p.mkdir(parents=True, exist_ok=True)
        f = p / ROOT_FILE
        old = f.read_text().splitlines() if f.exists() else []
        if line.strip() in old:
            return None                      # same root already anchored today
        with f.open("a") as fh:
            fh.write(line)
        return None
    except OSError as e:
        return f"{root_dir}: {e}"


def read_roots(root_dir: str) -> list | None:
    try:
        return (Path(root_dir) / ROOT_FILE).read_text().splitlines()
    except OSError:
        return None


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — one failed query never sinks the run
        log(f"query failed: {e}")
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def load_chain(cur) -> list:
    rows = _q(cur, "SELECT seq, table_name, row_key, op, row, row_hash, prev_hash, entry_hash "
                   "FROM peaslee_chain ORDER BY seq") or []
    cols = ("seq", "table_name", "row_key", "op", "row", "row_hash", "prev_hash", "entry_hash")
    out = [dict(zip(cols, r)) for r in rows]
    for e in out:
        if isinstance(e["row"], str):
            e["row"] = json.loads(e["row"])
    return out


def snapshot(cur) -> tuple[dict, set]:
    now, read = {}, set()
    for table, sql in TABLES.items():
        cur_rows = _q(cur, sql)
        if cur_rows is None:
            continue
        read.add(table)
        names = [d[0] for d in cur.description]
        for r in cur_rows:
            now[(table, str(r[0]))] = normalize(dict(zip(names, r)))
    return now, read


def root_dir(cur) -> str:
    try:
        return W.get_config(cur, SERVICE, "root_dir", DEFAULT_ROOT_DIR) or DEFAULT_ROOT_DIR
    except Exception:  # noqa: BLE001
        return DEFAULT_ROOT_DIR


def _since(cur):
    r = _q(cur, "SELECT max(computed_at) FROM peaslee_roots")
    return r[0][0] if r and r[0][0] else None


def action_signed(cur, since, table: str, key: str) -> bool:
    # ponytail: text match on claude_actions (table + row id + a write verb). Loose by design;
    # a structured sign-off table would make it exact.
    tname = "relationship_ledger" if table == "never_do" else table
    r = _q(cur, "SELECT id FROM claude_actions WHERE ts >= coalesce(%s, now() - interval '1 day') "
                "AND action_type <> 'file_read' "
                "AND (coalesce(target,'') || ' ' || description) ILIKE %s "
                "AND (coalesce(target,'') || ' ' || description) ~* %s "
                "AND (coalesce(target,'') || ' ' || description) ~* '(update|delete|retire|approve|reject)' LIMIT 1",
           (since, f"%{tname}%", rf"\m{key}\M"))
    return bool(r)


def file_finding(cur, table: str, key: str, op: str, row: dict) -> int | None:
    """One claude_queue item per row per day."""
    desc = f"Peaslee's Hand: {table} row {key} {op} without a signature"
    cur.execute("SELECT id FROM claude_queue WHERE description=%s AND created_at::date = current_date "
                "ORDER BY id DESC LIMIT 1", (desc,))
    row0 = cur.fetchone()
    if row0:
        return row0[0]
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) VALUES (%s,'pending',2,%s,%s) "
                "RETURNING id", (QUEUE_SESSION, desc,
                                 "A rule changed without a signature (nova_peaslee_hand.py). Row as now recorded:\n"
                                 + canon(row)[:3000] + "\nNo P11 sign-off and no claude_actions row names it. "
                                 "Find who changed it; do not repair it from here."))
    return cur.fetchone()[0]


def run(cur, dry: bool = False) -> int:
    if not dry:
        ensure_schema(cur)
    entries = load_chain(cur)
    bad, why = verify(entries)
    if bad is not None:
        log(f"chain broken at seq {bad}: {why}; appending nothing")
        if not dry:
            from nova_buick8_log import log_unexplained
            log_unexplained("peaslee_chain_break", f"seq {bad}", f"Peaslee chain broken at seq {bad}: {why}",
                            evidence={"seq": bad, "why": why}, occurrence_key=str(date.today()),
                            source="nova_peaslee_hand", cur=cur)
        return 1
    state = state_of(entries)
    now, read = snapshot(cur)
    changes = diff(state, now, read)
    new = chain(entries[-1]["entry_hash"] if entries else GENESIS, changes)
    since = _since(cur)
    findings = []
    for table, key, op, row in changes:
        if op == "added" or p11_signed(table, state.get((table, key)), now.get((table, key))):
            continue
        if not action_signed(cur, since, table, key):
            findings.append((table, key, op, row))
    n = len(entries) + len(new)
    root = new[-1]["entry_hash"] if new else (entries[-1]["entry_hash"] if entries else GENESIS)
    day = date.today().isoformat()
    rdir = root_dir(cur)
    log(f"read {sorted(read)}: {len(now)} rows, {len(changes)} changes, {len(findings)} unsigned; "
        f"root {root[:16]}.. over {n} entries")
    for f in findings:
        log(f"  unsigned: {f[0]} row {f[1]} {f[2]}")
    if dry:
        log(f"dry-run: would append {len(new)} entries and the root line to {rdir}/{ROOT_FILE}")
        return 0
    for e in new:
        cur.execute("INSERT INTO peaslee_chain (table_name, row_key, op, row, row_hash, prev_hash, entry_hash) "
                    "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s)",
                    (e["table_name"], e["row_key"], e["op"], canon(e["row"]), e["row_hash"], e["prev_hash"],
                     e["entry_hash"]))
    for f in findings:
        file_finding(cur, *f)
    err = write_root(rdir, root_line(day, root, n))
    if err:
        log(f"root NOT written off-host: {err}")
    cur.execute("INSERT INTO peaslee_roots (day, root, n, written_to, error) VALUES (%s,%s,%s,%s,%s)",
                (day, root, n, None if err else str(Path(rdir) / ROOT_FILE), err))
    return 0


def verify_cmd(cur) -> int:
    entries = load_chain(cur)
    bad, why = verify(entries)
    print(f"chain: {'BROKEN at seq ' + str(bad) + ': ' if bad is not None else ''}{why}")
    rdir = root_dir(cur)
    lines = read_roots(rdir)
    if lines is None:
        print(f"off-host roots: unavailable at {rdir}")
        return 1 if bad is not None else 0
    mis = check_roots(lines, entries)
    print(f"off-host roots: {mis or f'{len(lines)} lines all match the chain'}")
    return 1 if (bad is not None or mis) else 0


def show(cur) -> int:
    for r in _q(cur, "SELECT day, n, left(root, 16), coalesce(written_to, 'NOT WRITTEN: ' || error) "
                     "FROM peaslee_roots ORDER BY id DESC LIMIT 7") or []:
        print(*r)
    for r in _q(cur, "SELECT seq, ts, table_name, row_key, op FROM peaslee_chain "
                     "WHERE op <> 'added' ORDER BY seq DESC LIMIT 10") or []:
        print(*r)
    return 0


def selftest() -> int:
    rows = {("values", "1"): {"id": 1, "value": "a", "status": "active"},
            ("never_do", "20"): {"id": 20, "text": "x", "active": True}}
    e1 = chain(GENESIS, diff({}, rows, {"values", "never_do"}))
    assert [e["op"] for e in e1] == ["added", "added"] and e1[1]["prev_hash"] == e1[0]["entry_hash"]
    for i, e in enumerate(e1):
        e["seq"] = i + 1
    assert verify(e1)[0] is None
    st = state_of(e1)
    now = {("values", "1"): {"id": 1, "value": "a", "status": "retired"}}
    ch = diff(st, now, {"values", "never_do"})
    assert [(c[1], c[2]) for c in ch] == [("1", "changed"), ("20", "removed")], ch
    assert diff(st, {}, {"values"}) == [("values", "1", "removed", st[("values", "1")])]   # unread table spared
    assert p11_signed("values", st[("values", "1")], now[("values", "1")])
    assert not p11_signed("values", st[("values", "1")], {"id": 1, "value": "b", "status": "retired"})
    assert not p11_signed("never_do", {"a": 1}, {"a": 2})
    tampered = [dict(e) for e in e1]
    tampered[0]["row"] = {"id": 1, "value": "z", "status": "active"}
    assert verify(tampered)[0] == 1
    assert check_roots([root_line("2026-10-08", e1[-1]["entry_hash"], 2)], e1) is None
    assert check_roots([root_line("2026-10-08", "f" * 64, 2)], e1)
    assert canon({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="hash the rules, extend the chain, write the root off-host")
    ap.add_argument("--dry-run", action="store_true", help="with --run: report only, write nothing anywhere")
    ap.add_argument("--verify", action="store_true", help="recompute the chain and check the off-host roots")
    ap.add_argument("--show", action="store_true", help="recent roots and non-add chain entries")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.run or a.verify or a.show):
        ap.print_help()
        return 0
    cur = W.connect().cursor()
    if a.run:
        return run(cur, dry=a.dry_run)
    return verify_cmd(cur) if a.verify else show(cur)


if __name__ == "__main__":
    sys.exit(main())
