#!/usr/bin/env python3
"""
nova-transport — Nova's fleet transport system (a /usr/sap/trans analog).

Promote scripts/configs/assets across the cluster the SAP way: package once
(release), verify on the canary node, promote to the fleet, and each node PULLS
and imports its own buffer — with control files, checksums, restart hooks, and a
full audit log. Build once, promote everywhere, nothing drifts.

Transport dir on the NAS (resolved per-node from landscape):
  bin/landscape.json   nodes, per-node NAS mount + scripts path, and the canary
  data/<id>/           the payload (packaged files)
  cofiles/<id>.json    control file: metadata, targets, checksums, hook
  buffer/<node>/       per-node import queue (one empty file per pending id)
  log/SLOG             audit trail

Commands:
  release <name> --script <f>... [--hook "<cmd>"] [--to all|node,...]
  promote <id>          canary verified -> queue to the rest of the fleet
  import  [--node <n>]   run ON a node: apply its buffer (copy files, run hook, log)
  status                 buffers per node + recent log
  list                   all transports
"""
import os, sys, json, hashlib, shutil, socket, subprocess, argparse
from datetime import datetime
from pathlib import Path

def nas_root():
    for d in ("/Volumes/nas", "/mnt/nas", "/nas"):
        if os.path.isdir(f"{d}/nova/trans"):
            return f"{d}/nova/trans"
    sys.exit("ERROR: transport dir not found — is the NAS mounted?")

TRANS = nas_root()
LAND = json.load(open(f"{TRANS}/bin/landscape.json"))

def this_node():
    h = socket.gethostname().split(".")[0].lower()
    for name, info in LAND["nodes"].items():
        if name in h or h.replace("-", "") in name.replace("-", ""):
            return name
    # fall back: the node whose nas mount + scripts path both exist here
    for name, info in LAND["nodes"].items():
        if os.path.isdir(info["nas"]) and os.path.isdir(info["scripts"]):
            return name
    sys.exit(f"can't identify this node ({h}); pass --node explicitly")

def sha256(p):
    x = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            x.update(c)
    return x.hexdigest()

def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    open(f"{TRANS}/log/SLOG", "a").write(line)
    print(line, end="")

def new_id(name):
    seq = len(list(Path(f"{TRANS}/cofiles").glob("*.json"))) + 1
    slug = "".join(c if c.isalnum() else "-" for c in name.lower())[:20].strip("-")
    return f"NT{datetime.now():%Y%m%d}-{seq:03d}-{slug}"

def cmd_release(a):
    tid = new_id(a.name)
    ddir = Path(f"{TRANS}/data/{tid}"); ddir.mkdir(parents=True, exist_ok=True)
    files = []
    for f in (a.script or []):
        src = Path(f).expanduser().resolve()
        if not src.exists(): sys.exit(f"not found: {f}")
        shutil.copy2(src, ddir / src.name)
        files.append({"name": src.name, "kind": "script", "sha256": sha256(src)})
    if not files: sys.exit("nothing to release (use --script)")
    co = {"id": tid, "name": a.name, "author": os.environ.get("USER", "?"),
          "ts": datetime.now().isoformat(), "files": files,
          "targets": (a.to.split(",") if (a.to and a.to != "all") else "all"),
          "hook": a.hook or ""}
    json.dump(co, open(f"{TRANS}/cofiles/{tid}.json", "w"), indent=2)
    canary = LAND["canary"]
    open(f"{TRANS}/buffer/{canary}/{tid}", "w").write("")
    log(f"RELEASE {tid} ({len(files)} files) queued to canary '{canary}'")
    print(f"\n  Next: verify on '{canary}', then:  nova_transport.py promote {tid}")

def cmd_promote(a):
    if not os.path.exists(f"{TRANS}/cofiles/{a.id}.json"): sys.exit(f"unknown transport {a.id}")
    co = json.load(open(f"{TRANS}/cofiles/{a.id}.json"))
    targets = list(LAND["nodes"]) if co["targets"] == "all" else co["targets"]
    canary = LAND["canary"]
    dest = [n for n in targets if n != canary]
    for n in dest:
        open(f"{TRANS}/buffer/{n}/{a.id}", "w").write("")
    log(f"PROMOTE {a.id} queued to fleet: {', '.join(dest)}")

def cmd_import(a):
    node = a.node or this_node()
    if node not in LAND["nodes"]: sys.exit(f"node '{node}' not in landscape")
    info = LAND["nodes"][node]
    pending = sorted(Path(f"{TRANS}/buffer/{node}").glob("NT*"))
    if not pending:
        print(f"[{node}] buffer empty — nothing to import"); return
    for entry in pending:
        tid = entry.name
        co = json.load(open(f"{TRANS}/cofiles/{tid}.json"))
        ok = True
        for f in co["files"]:
            src = Path(f"{TRANS}/data/{tid}/{f['name']}")
            if sha256(src) != f["sha256"]:
                log(f"IMPORT {tid} [{node}] CHECKSUM FAIL {f['name']}"); ok = False; break
            if f["kind"] == "script":
                shutil.copy2(src, Path(info["scripts"]) / f["name"])
        if ok and co.get("hook"):
            # hook is the operator's `--hook "<cmd>"` shell line from the control file
            r = subprocess.run(["/bin/sh", "-c", co["hook"]], capture_output=True, text=True, timeout=120)
            ok = (r.returncode == 0)
            log(f"IMPORT {tid} [{node}] hook rc={r.returncode} {r.stderr.strip()[:60]}")
        if ok:
            entry.unlink()
            log(f"IMPORT {tid} [{node}] OK ({len(co['files'])} files)")
        else:
            log(f"IMPORT {tid} [{node}] FAILED — left in buffer for retry")

def cmd_status(a):
    print(f"transport dir: {TRANS}    canary: {LAND['canary']}\n")
    for n in LAND["nodes"]:
        pend = sorted(p.name for p in Path(f"{TRANS}/buffer/{n}").glob("NT*"))
        print(f"  {n:12s} {', '.join(pend) if pend else 'empty'}")
    print("\n  recent log:")
    try:
        for l in open(f"{TRANS}/log/SLOG").readlines()[-8:]: print("    " + l.rstrip())
    except FileNotFoundError: print("    (none)")

def cmd_list(a):
    cos = sorted(Path(f"{TRANS}/cofiles").glob("*.json"))
    if not cos: print("  (no transports yet)"); return
    for co in cos:
        c = json.load(open(co)); print(f"  {c['id']}  \"{c['name']}\"  {len(c['files'])}f  targets={c['targets']}")

def main(argv=None):
    p = argparse.ArgumentParser(prog="nova_transport.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("release"); r.add_argument("name"); r.add_argument("--script", nargs="*"); r.add_argument("--hook"); r.add_argument("--to"); r.set_defaults(fn=cmd_release)
    pr = sub.add_parser("promote"); pr.add_argument("id"); pr.set_defaults(fn=cmd_promote)
    im = sub.add_parser("import"); im.add_argument("--node"); im.set_defaults(fn=cmd_import)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = p.parse_args(argv); a.fn(a)

if __name__ == "__main__":
    main()
