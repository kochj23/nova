#!/usr/bin/env python3
"""nova_jade_amulet.py — THE JADE AMULET: where everything Nova runs came from, and what changed.

From Lovecraft's "The Hound": two collectors, St. John and the narrator, dig up a grave in a
Holland churchyard and carry off a small amulet of carved green jade for the secret museum
under their house. The amulet keeps its origin. A great hound's baying follows it home, St.
John dies saying "The amulet—that damned thing—", and when the narrator tries to take it back
to Holland it is stolen on the way and turns up again in the hand of the grave's occupant.
Nova's version: everything she runs is catalogued each day with its origin and digest, and
anything that arrives or changes with no logged hand behind it is entered in the Buick 8
Logbook as unexplained (cause unknown). This organ is about software and its sources, nothing else.

Minimal first version (Studio only):
  * ollama models from the local HTTP API /api/tags: name = tag as `ollama list` prints it,
    digest = the model digest (ollama list's ID is its first 12 chars)
  * Nova launchd plists (LaunchAgents + LaunchDaemons, Nova prefixes): name = path, digest = sha256
Each --run writes one row per item to jade_amulet_manifest, diffs against the previous snapshot
(added / removed / changed), and matches each change to a claude_actions row in the window that
mentions the item. Unmatched changes go to the Buick 8 Logbook as 'substrate_change'.

CLI:   --run [--dry-run]   --show   --diff   --selftest
Table: jade_amulet_manifest (contract: the Doorstep Test reads it)
Schedule: daily 04:30.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import urllib.request
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
PLIST_DIRS = (Path.home() / "Library/LaunchAgents", Path("/Library/LaunchDaemons"))
PLIST_PREFIXES = ("net.digitalnoise", "com.nova", "com.jordankoch")
# ponytail: empty until a real auto-updater shows up as churn; fnmatch patterns on item name.
EXPECTED_UPDATERS: tuple = ()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jade_amulet_manifest (
  ts timestamptz NOT NULL DEFAULT now(),
  host text, kind text, name text, version text, digest text, origin text);
CREATE INDEX IF NOT EXISTS jade_amulet_manifest_kind_name_ts ON jade_amulet_manifest (kind, name, ts);
"""


def log(m: str) -> None:
    print(f"[jade-amulet {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── inventory ───────────────────────────────────────────────────────────────

def _fetch_tags() -> list:
    with urllib.request.urlopen(OLLAMA_TAGS, timeout=10) as r:
        return json.load(r).get("models") or []


def ollama_origin(name: str) -> str:
    first = name.split("/")[0]
    return first if "." in first and "/" in name else "registry.ollama.ai"


def ollama_models(_sleep=None) -> list | None:
    """Items for every local model, or None if ollama could not be read (kind is then skipped,
    never reported as 'all removed'). Retries 3x with backoff."""
    models = W.retry(_fetch_tags, attempts=3, delay=2.0, tag="jade-amulet", _sleep=_sleep)
    if not models:
        return None
    return [{"kind": "ollama_model", "name": m["name"], "version": m.get("modified_at"),
             "digest": m.get("digest"), "origin": ollama_origin(m["name"])} for m in models]


def plists(dirs=PLIST_DIRS) -> list:
    out = []
    for d in dirs:
        for p in sorted(d.glob("*.plist")) if d.is_dir() else []:
            if not p.name.startswith(PLIST_PREFIXES):
                continue
            try:
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as e:
                log(f"unreadable {p}: {e}")
                continue
            out.append({"kind": "launchd_plist", "name": str(p), "version": None,
                        "digest": digest, "origin": str(d)})
    return out


def snapshot() -> list:
    items = plists()
    models = ollama_models()
    if models is None:
        log("ollama unreachable; models skipped this run")
    return (models or []) + items


# ── diff and matching (pure) ────────────────────────────────────────────────

def diff(prev: list, cur: list) -> list:
    """Changes between two snapshots, only for kinds present in `cur`."""
    kinds = {i["kind"] for i in cur}
    old = {(i["kind"], i["name"]): i["digest"] for i in prev if i["kind"] in kinds}
    new = {(i["kind"], i["name"]): i["digest"] for i in cur}
    ch = [{"change": "added", "kind": k, "name": n, "old": None, "new": d}
          for (k, n), d in new.items() if (k, n) not in old]
    ch += [{"change": "removed", "kind": k, "name": n, "old": d, "new": None}
           for (k, n), d in old.items() if (k, n) not in new]
    ch += [{"change": "changed", "kind": k, "name": n, "old": old[(k, n)], "new": d}
           for (k, n), d in new.items() if (k, n) in old and old[(k, n)] != d]
    return sorted(ch, key=lambda c: (c["kind"], c["name"]))


def needle(c: dict) -> str:
    """What a logged action would mention: the model tag, or the plist's label (file stem)."""
    return Path(c["name"]).stem if c["kind"] == "launchd_plist" else c["name"]


def match(changes: list, actions: list) -> list:
    """Attach `action_id` (or None) and `expected` to each change. actions = [(id, text)]."""
    low = [(aid, (t or "").lower()) for aid, t in actions]
    for c in changes:
        n = needle(c).lower()
        c["action_id"] = next((aid for aid, t in low if n in t), None)
        c["expected"] = any(fnmatch(c["name"], p) for p in EXPECTED_UPDATERS)
    return changes


# ── PG ──────────────────────────────────────────────────────────────────────

def previous(cur, host: str) -> tuple:
    """(ts, items) of the latest snapshot per kind, or (None, []) when there is none."""
    exists = _q(cur, "SELECT to_regclass('jade_amulet_manifest')")
    if not exists or exists[0][0] is None:   # first run, or a dry run before the table exists
        return None, []
    rows = _q(cur, "SELECT ts, kind, name, digest FROM jade_amulet_manifest m WHERE host=%s AND "
                   "ts = (SELECT max(ts) FROM jade_amulet_manifest WHERE host=%s AND kind=m.kind)",
              (host, host))
    if not rows:
        return None, []
    return min(r[0] for r in rows), [{"kind": r[1], "name": r[2], "digest": r[3]} for r in rows]


CHANGE_VERBS = (r"\m(pull|rm|create|install|upgrade|update|uninstall|write|edit|copy|cp|mv|replace|delete|"
                r"load|unload|bootstrap|bootout|kickstart|enable|disable)\M")


def actions_since(cur, since) -> list:
    # ponytail: read actions never explain a change; a row must also carry a verb that changes things.
    # Text match only; a structured change log would make it exact.
    return _q(cur, "SELECT id, concat_ws(' ', target, description, rationale) FROM claude_actions "
                   "WHERE ts >= %s AND action_type NOT IN ('file_read', 'staleness-check', 'reap') "
                   "AND concat_ws(' ', target, description, rationale) ~* %s ORDER BY ts", (since, CHANGE_VERBS))


def write(cur, host: str, ts, items: list) -> None:
    ensure_schema(cur)
    for i in items:
        cur.execute("INSERT INTO jade_amulet_manifest (ts, host, kind, name, version, digest, origin) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (ts, host, i["kind"], i["name"], i["version"], i["digest"], i["origin"]))


def report(cur, changes: list, ts) -> int:
    from nova_buick8_log import log_unexplained
    n = 0
    for c in changes:
        if c["action_id"] is not None or c["expected"]:
            continue
        sig = f"{c['kind']}:{c['name']}:{c['new'] or 'removed'}"
        desc = (f"{c['kind']} {c['name']} {c['change']} ({(c['old'] or '-')[:12]} -> "
                f"{(c['new'] or '-')[:12]}) with no logged action behind it")
        log_unexplained("substrate_change", sig, desc, evidence=c, occurrence_key=str(ts),
                        source="jade_amulet", cur=cur)
        n += 1
    return n


def run(dry: bool = False) -> list:
    host = socket.gethostname().split(".")[0]
    items = snapshot()
    ts = datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        prev_ts, prev = previous(cur, host)
        changes = match(diff(prev, items), actions_since(cur, prev_ts)) if prev_ts else []
        counts = {}
        for i in items:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
        log(f"{'DRY RUN ' if dry else ''}snapshot {counts}; previous {prev_ts or 'none'}; "
            f"{len(changes)} changes")
        for i in items if dry else []:
            print(f"  {i['kind']:<14} {(i['digest'] or '-')[:12]}  {i['name']}")
        for c in changes:
            how = f"action #{c['action_id']}" if c["action_id"] else "expected" if c["expected"] else "UNEXPLAINED"
            print(f"  {c['change']:<8} {c['kind']:<14} {c['name']}  [{how}]")
        if not dry:
            write(cur, host, ts, items)
            log(f"wrote {len(items)} rows; {report(cur, changes, ts)} to Buick 8")
        return changes
    finally:
        conn.close()


def show(only_diff: bool = False) -> int:
    host = socket.gethostname().split(".")[0]
    conn = W.connect()
    try:
        cur = conn.cursor()
        ts, items = previous(cur, host)
        if only_diff:
            rows = _q(cur, "SELECT DISTINCT ts FROM jade_amulet_manifest WHERE host=%s ORDER BY ts DESC LIMIT 2",
                      (host,)) if ts else []
            if len(rows) < 2:
                print("fewer than two snapshots")
                return 0
            older = _q(cur, "SELECT kind, name, digest FROM jade_amulet_manifest WHERE host=%s AND ts=%s",
                       (host, rows[1][0]))
            prev = [{"kind": k, "name": n, "digest": d} for k, n, d in older]
            for c in match(diff(prev, items), actions_since(cur, rows[1][0])):
                print(f"{c['change']:<8} {c['kind']:<14} {c['name']}  action={c['action_id']}")
            return 0
        print(f"snapshot {ts or 'none'}")
        for i in sorted(items, key=lambda i: (i["kind"], i["name"])):
            print(f"{i['kind']:<14} {(i['digest'] or '-')[:12]}  {i['name']}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    a = [{"kind": "ollama_model", "name": "m:1", "digest": "aa"},
         {"kind": "ollama_model", "name": "gone:1", "digest": "bb"},
         {"kind": "launchd_plist", "name": "/x/net.digitalnoise.foo.plist", "digest": "cc"}]
    b = [{"kind": "ollama_model", "name": "m:1", "digest": "zz"},
         {"kind": "ollama_model", "name": "new:1", "digest": "dd"}]
    ch = {(c["change"], c["name"]) for c in diff(a, b)}
    assert ch == {("changed", "m:1"), ("added", "new:1"), ("removed", "gone:1")}, ch  # plists skipped
    m = match(diff(a, a[:2] + [dict(a[2], digest="ee")]), [(7, "edited net.digitalnoise.foo label")])
    assert m[0]["action_id"] == 7 and not m[0]["expected"], m
    assert match(diff(a, b), [])[0]["action_id"] is None
    assert ollama_origin("qwen3:235b") == "registry.ollama.ai"
    assert ollama_origin("hf.co/org/model:q4") == "hf.co"
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="snapshot, diff, write, report unmatched changes")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print snapshot and diff, write nothing")
    ap.add_argument("--show", action="store_true", help="latest snapshot")
    ap.add_argument("--diff", action="store_true", help="diff of the last two stored snapshots")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.diff:
        return show(only_diff=True)
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
