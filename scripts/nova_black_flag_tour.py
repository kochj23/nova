#!/usr/bin/env python3
"""nova_black_flag_tour.py — THE BLACK FLAG TOUR: copy-pasted code, and fixes that only reached one town.

Little Mister, 2026-10-15 (after Nova watched American Hardcore): "Add all 9."

In the film Black Flag tours relentlessly, and every town they play starts a scene that
behaves like them: the van leaves, the habits stay. Nova's scripts grew the same way. A
helper (slack_post, remember, llm, load_state) was written once and then carried script to
script by copy-paste, each copy drifting a little. When a bug is fixed in one copy, the
other towns never hear about it.

Weekly:
  1. parse every scripts/*.py and the gateway package (gateway/nova_gateway/**.py) with ast;
     for each top-level function compute
        variant hash  — ast.dump with the docstring removed (formatting and comments ignored),
        shape hash    — the same with identifiers and constants folded (renames do not hide a clone),
        shingles      — 4-token shingles of the shape stream (for "is this sibling a near copy");
  2. join functions into COPY GROUPS: the same name in two or more files, or an identical shape of
     at least MIN_SHAPE_TOKENS tokens under another name (a renamed clone), kept only when at least
     two members are near copies of each other. Entry-point conventions (main, selftest, report,
     run, ...) are left out: every organ has them by design;
  3. DIVERGENT FIXES: from git, the version of every changed file at the start of the window
     (--days, default 8) vs HEAD. A copy whose body changed while its NEAR siblings did not (same
     old body, or shape Jaccard >= NEAR_MIN to the old body: a same-named but unrelated function is
     not a sibling), and whose new body matches none of them, is "fix landed in m of K copies"; the
     commit that changed it (subject, date) and the siblings still carrying the old body are recorded;
  4. ranks groups as consolidation candidates by copies x body variants.

Tables: black_flag_groups (one row per copy group, current), black_flag_snapshots (one row per
function in a group: path, name, body hashes), black_flag_divergences (one row per divergent
change: group, path, function, commit), black_flag_runs (one row per run: the measurements).
Notify: one weekly info digest (category black_flag_tour). Read-only toward the code: it
proposes consolidation, it never edits a file.

Measured (--report, monthly): copy groups with >= 5 copies, total redundant copies (copies
minus one per group), divergent fixes detected this month (distinct group x commit).

CLI:   --scheduled     weekly run: analyse, write tables, digest
       --dry-run       analyse and print; write nothing, notify nothing
       --days N        divergence window (default 8, so weekly runs overlap by a day)
       --report [N]    top N consolidation candidates (from the last run) + monthly measurements
       --selftest      offline check of the hashing and grouping
Schedule: scheduler-core, Mondays 06:40, timeout 300, overlap skip. Must finish < 2 min.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import time
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_black_flag_tour"
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = SCRIPTS_DIR.parent
GATEWAY_PKG = REPO / "gateway" / "nova_gateway"
# entry-point conventions every organ has by design (CLI, --selftest, --report): same name, not a copy
SKIP_NAMES = frozenset({"main", "selftest", "_selftest", "demo", "run", "report", "show", "parse_args",
                        "_parse_args", "cli", "build_parser", "_args"})
NEAR_MIN = 0.5                 # shape-shingle Jaccard: a sibling close enough to have needed the same fix
MIN_SHAPE_TOKENS = 60          # identical shape under different names only counts for real bodies
SHINGLE = 4
DEFAULT_DAYS = 8
BIG_GROUP = 5                  # measurement: groups with >= this many copies
TOP_DIGEST = 8
GIT_TIMEOUT_S = 60
MAX_COMMITS_PER_FILE = 12
TIME_BUDGET_S = 100            # stop commit attribution past this; the run must finish < 2 min
STATEMENT_TIMEOUT_MS = 30000

SCHEMA = """
CREATE TABLE IF NOT EXISTS black_flag_groups (
  group_key text PRIMARY KEY,
  label text NOT NULL,
  copies integer NOT NULL,
  variants integer NOT NULL,
  near_copies integer,
  score integer NOT NULL,
  files jsonb NOT NULL DEFAULT '[]',
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  active boolean NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS black_flag_snapshots (
  path text NOT NULL,
  func text NOT NULL,
  group_key text NOT NULL,
  lineno integer,
  body_hash text NOT NULL,
  shape_hash text NOT NULL,
  ntokens integer,
  seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (path, func));
CREATE TABLE IF NOT EXISTS black_flag_divergences (
  id bigserial PRIMARY KEY,
  detected_at timestamptz NOT NULL DEFAULT now(),
  group_key text NOT NULL,
  path text NOT NULL,
  func text NOT NULL,
  commit_sha text NOT NULL,
  commit_subject text,
  committed_at timestamptz,
  copies integer NOT NULL,
  changed integer NOT NULL,
  siblings jsonb NOT NULL DEFAULT '[]',
  siblings_with_old_body jsonb NOT NULL DEFAULT '[]',
  notified boolean NOT NULL DEFAULT false,
  UNIQUE (group_key, path, func, commit_sha));
CREATE TABLE IF NOT EXISTS black_flag_runs (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  files integer NOT NULL,
  functions integer NOT NULL,
  groups integer NOT NULL,
  groups_big integer NOT NULL,
  redundant_copies integer NOT NULL,
  divergences integer NOT NULL,
  seconds real NOT NULL);
CREATE INDEX IF NOT EXISTS black_flag_divergences_detected ON black_flag_divergences (detected_at);
CREATE INDEX IF NOT EXISTS black_flag_snapshots_group ON black_flag_snapshots (group_key);
"""


def log(m: str) -> None:
    print(f"[black-flag {datetime.now():%H:%M:%S}] {m}", flush=True)


# ---------------------------------------------------------------- pure: hashing

_CTX = (ast.expr_context,)


def _tokens(nodes: list) -> tuple[list[str], list[str]]:
    """Pre-order walk -> (full tokens, shape tokens). Full keeps identifiers and constants; shape
    folds them, so a renamed clone has the same shape. Node type + child count keeps it unambiguous.
    No copying of the tree (deepcopy was 30 s of a 60 s run). Pure."""
    full, shape = [], []
    stack = list(reversed(nodes))
    while stack:
        n = stack.pop()
        kids = [k for k in ast.iter_child_nodes(n) if not isinstance(k, _CTX)]
        t = f"{type(n).__name__}{len(kids)}"
        if isinstance(n, ast.Name):
            val = n.id
        elif isinstance(n, ast.arg):
            val = n.arg
        elif isinstance(n, ast.Attribute):
            val, t = n.attr, f"{t}.{n.attr}"          # attribute names are behaviour, not naming
        elif isinstance(n, ast.Constant):
            val = repr(n.value)[:60]
        elif isinstance(n, (ast.keyword, ast.alias)):
            val = n.arg if isinstance(n, ast.keyword) else n.name
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            val = n.name
        else:
            val = ""
        shape.append(t)
        full.append(f"{t}:{val}" if val else t)
        stack.extend(reversed(kids))
    return full, shape


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def fingerprint_function(fn: ast.AST) -> dict:
    """{body_hash, shape_hash, shingles, ntokens} for one FunctionDef: arguments + body, docstring and
    decorators left out; comments and formatting never reach the AST. Pure."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    full, shape = _tokens([fn.args] + body)
    shingles = {hash(tuple(shape[i:i + SHINGLE])) for i in range(max(1, len(shape) - SHINGLE + 1))}
    return {"body_hash": _h("\x1f".join(full)), "shape_hash": _h("\x1f".join(shape)),
            "shingles": frozenset(shingles), "ntokens": len(full)}


def functions_in(source: str) -> dict[str, dict]:
    """top-level function name -> fingerprint (+ lineno). Unparseable source -> {}. Pure."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")          # invalid escapes etc. in the scanned files are not ours
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in SKIP_NAMES:
            fp = fingerprint_function(node)
            fp["lineno"] = node.lineno
            out[node.name] = fp            # a redefinition later in the file wins, as at runtime
    return out


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a or b) else 0.0


# ---------------------------------------------------------------- pure: grouping

def near_copies(members: list[dict]) -> int:
    """How many members have a near copy (same shape, or shape Jaccard >= NEAR_MIN) in another file. Pure."""
    hit = [False] * len(members)
    for a in range(len(members)):
        for b in range(a + 1, len(members)):
            if hit[a] and hit[b]:
                continue
            ma, mb = members[a], members[b]
            if ma["path"] != mb["path"] and (ma["shape_hash"] == mb["shape_hash"]
                                             or jaccard(ma["shingles"], mb["shingles"]) >= NEAR_MIN):
                hit[a] = hit[b] = True
    return sum(hit)


def build_groups(funcs: list[dict]) -> list[dict]:
    """funcs: [{path, name, body_hash, shape_hash, shingles, ntokens, lineno}] -> copy groups,
    each {key, label, members, copies, variants, score}, biggest score first. Pure."""
    parent = list(range(len(funcs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    by_name: dict[str, list[int]] = defaultdict(list)
    by_shape: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(funcs):
        by_name[f["name"]].append(i)
        if f["ntokens"] >= MIN_SHAPE_TOKENS:
            by_shape[f["shape_hash"]].append(i)
    for idx in by_name.values():                 # same name in another file = a copy (the tour)
        for j in idx[1:]:
            union(idx[0], j)
    for idx in by_shape.values():
        for j in idx[1:]:
            if funcs[j]["path"] != funcs[idx[0]]["path"]:
                union(idx[0], j)
    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(len(funcs)):
        comps[find(i)].append(i)
    groups = []
    for idx in comps.values():
        members = [funcs[i] for i in idx]
        if len({m["path"] for m in members}) < 2:
            continue
        near = near_copies(members)
        if near < 2:
            continue                       # same name, but nothing was actually copied
        label = Counter(m["name"] for m in members).most_common(1)[0][0]
        variants = len({m["body_hash"] for m in members})
        groups.append({"label": label, "members": sorted(members, key=lambda m: (m["path"], m["name"])),
                       "copies": len(members), "variants": variants, "near": near,
                       "score": len(members) * variants})
    groups.sort(key=lambda g: (-g["score"], -g["copies"], g["label"]))
    seen: Counter = Counter()
    for g in groups:                       # stable-ish keys: label, then label~2, label~3 by rank
        seen[g["label"]] += 1
        g["key"] = g["label"] if seen[g["label"]] == 1 else f"{g['label']}~{seen[g['label']]}"
    return groups


def measures(groups: list[dict]) -> dict:
    """The monthly numbers from one analysis. Pure."""
    return {"groups": len(groups), "groups_big": sum(1 for g in groups if g["copies"] >= BIG_GROUP),
            "redundant_copies": sum(g["copies"] - 1 for g in groups),
            "functions_in_groups": sum(g["copies"] for g in groups)}


def find_divergences(groups: list[dict], base: dict[str, dict], head: dict[str, dict],
                     changed_paths: set[str]) -> list[dict]:
    """Divergent fixes. base/head: path -> {func: fp} for the CHANGED files only (absent = file did not
    exist). A member changed if it existed at base with another body. Its siblings are the unchanged
    members that were near copies of its OLD body (same body, or shape Jaccard >= NEAR_MIN). It diverged
    if it has such siblings and its new body matches none of them. Pure."""
    out = []
    for g in groups:
        changed, unchanged = [], []
        for m in g["members"]:
            if m["path"] not in changed_paths:
                unchanged.append((m, m))
                continue
            old = (base.get(m["path"]) or {}).get(m["name"])
            new = (head.get(m["path"]) or {}).get(m["name"])
            if old is None or new is None:
                continue                    # new copy, or not committed yet: neither a fix nor a sibling
            if old["body_hash"] == new["body_hash"]:
                unchanged.append((m, new))
            else:
                changed.append((m, old, new))
        if not changed or not unchanged:
            continue
        n_changed = len(changed)
        for m, old, new in changed:
            near = [(s, fp) for s, fp in unchanged
                    if fp["body_hash"] == old["body_hash"] or jaccard(old["shingles"], fp["shingles"]) >= NEAR_MIN]
            if not near or any(fp["body_hash"] == new["body_hash"] for _, fp in near):
                continue                    # no close copy to diverge from, or it caught up with one
            peers = [c for c in changed if c[0] is not m and
                     jaccard(old["shingles"], c[1]["shingles"]) >= NEAR_MIN]
            out.append({"group_key": g["key"], "label": g["label"], "path": m["path"], "func": m["name"],
                        "copies": len(near) + 1 + len(peers), "changed": 1 + len(peers),
                        "group_copies": g["copies"], "group_changed": n_changed,
                        "siblings": sorted(f"{s['path']}:{s['name']}" for s, _ in near),
                        "siblings_with_old_body": sorted(f"{s['path']}:{s['name']}" for s, fp in near
                                                         if fp["body_hash"] == old["body_hash"]),
                        "old_hash": old["body_hash"], "new_hash": new["body_hash"]})
    return out


def render_digest(groups: list[dict], meas: dict, divs: list[dict], days: int) -> tuple[str, str]:
    """(title, body) for the weekly info digest. Pure."""
    title = (f"Black Flag Tour: {meas['groups_big']} copy groups with >={BIG_GROUP} copies, "
             f"{meas['redundant_copies']} redundant copies, {len(divs)} divergent fix(es)")
    lines = []
    if divs:
        lines.append(f"Fixes that landed in only some copies (last {days} days):")
        for d in divs[:15]:
            lines.append(f"• {d['func']} in {d['path']}: fix landed in {d['changed']} of {d['copies']} copies — "
                         f"\"{d.get('commit_subject') or '?'}\" ({(d.get('commit_sha') or '?')[:8]})")
            lag = d["siblings_with_old_body"] or d["siblings"]
            lines.append(f"    {'still on the old body' if d['siblings_with_old_body'] else 'siblings'}: "
                         f"{', '.join(lag[:8])}{' …' if len(lag) > 8 else ''}")
        lines.append("")
    lines.append("Consolidation candidates (copies × body variants):")
    for g in groups[:TOP_DIGEST]:
        files = sorted({m["path"] for m in g["members"]})
        lines.append(f"• {g['label']}: {g['copies']} copies, {g['variants']} variants (score {g['score']}) — "
                     f"{', '.join(files[:4])}{' …' if len(files) > 4 else ''}")
    lines += ["", "Proposal only: no file was changed."]
    return title, "\n".join(lines)


def selftest() -> int:
    a = "def slack_post(msg):\n    '''doc'''\n    r = post(URL, json={'text': msg}, timeout=10)\n    return r.ok\n"
    b = "def slack_post(m):\n    # comment\n    r = post(URL, json={'text': m}, timeout=10)\n    return r.ok\n"
    fa, fb = functions_in(a)["slack_post"], functions_in(b)["slack_post"]
    assert fa["shape_hash"] == fb["shape_hash"] and fa["body_hash"] != fb["body_hash"]
    c = functions_in(a.replace("'''doc'''", "# other"))["slack_post"]
    assert c["body_hash"] == fa["body_hash"]           # docstrings and comments are not variants
    funcs = [dict(fp, path=p, name="slack_post") for p, fp in (("x.py", fa), ("y.py", fb))]
    g = build_groups(funcs)
    assert len(g) == 1 and g[0]["copies"] == 2 and g[0]["variants"] == 2
    print("selftest ok")
    return 0


# ---------------------------------------------------------------- filesystem + git

def source_files() -> list[Path]:
    files = sorted(SCRIPTS_DIR.glob("*.py"))
    if GATEWAY_PKG.is_dir():
        files += sorted(p for p in GATEWAY_PKG.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def rel(p: Path) -> str:
    return p.resolve().relative_to(REPO.resolve()).as_posix()


def scan(files: list[Path]) -> tuple[list[dict], int]:
    """Parse every file. -> (functions, files parsed). Unreadable/unparseable files are skipped."""
    funcs, parsed = [], 0
    for p in files:
        try:
            src = p.read_text(errors="replace")
        except OSError:
            continue
        fns = functions_in(src)
        parsed += 1
        r = rel(p)
        for name, fp in fns.items():
            funcs.append(dict(fp, path=r, name=name))
    return funcs, parsed


def git(*args, timeout=GIT_TIMEOUT_S) -> str:
    """git in the repo; '' on any failure (git missing, not a checkout, timeout). Never raises."""
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_blobs(specs: list[str]) -> dict[str, str | None]:
    """'rev:path' -> file text (None when missing), in one `git cat-file --batch`. Never raises."""
    if not specs:
        return {}
    try:
        r = subprocess.run(["git", "-C", str(REPO), "cat-file", "--batch"],
                           input=("\n".join(specs) + "\n").encode(),
                           capture_output=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return {s: None for s in specs}
    out, data, pos = {}, r.stdout, 0
    for spec in specs:
        nl = data.find(b"\n", pos)
        if nl < 0:
            out[spec] = None
            continue
        header = data[pos:nl].decode(errors="replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] == "missing":
            out[spec] = None
            continue
        size = int(header[2])
        out[spec] = data[pos:pos + size].decode(errors="replace")
        pos += size + 1
    return out


def git_window(days: int, tracked: set[str]) -> tuple[str, str, set[str]]:
    """(base_rev, head_rev, changed paths among `tracked`) for the last `days` days; ('', '', set()) without git."""
    head = git("rev-parse", "HEAD").strip()
    base = git("rev-list", "-1", f"--before={days} days ago", "HEAD").strip()
    if not head or not base or head == base:
        return base, head, set()
    names = git("diff", "--name-only", base, head, "--", "scripts", "gateway/nova_gateway").split()
    return base, head, {n for n in names if n in tracked}


def commits_by_path(base: str) -> dict[str, list[tuple[str, str, str]]]:
    """path -> [(sha, subject, committed_iso)] newest first for base..HEAD, from ONE git log
    (a per-file `git log -- path` cost ~2 s each over the NAS)."""
    out = git("log", "--name-only", "--format=\x1e%H\x1f%s\x1f%cI", f"{base}..HEAD", "--",
              "scripts", "gateway/nova_gateway")
    by: dict[str, list] = defaultdict(list)
    for chunk in out.split("\x1e"):
        head, _, names = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) != 3:
            continue
        for n in names.split():
            by[n].append(tuple(parts))
    return by


def file_history(path: str, commits: list[tuple[str, str, str]]) -> list[tuple[str, str, str, dict, dict]]:
    """[(sha, subject, committed_iso, funcs_after, funcs_before)] newest first; one batched cat-file."""
    commits = commits[:MAX_COMMITS_PER_FILE]
    blobs = git_blobs([f"{sha}{suffix}:{path}" for sha, _, _ in commits for suffix in ("", "^")])
    return [(sha, subj, when, functions_in(blobs.get(f"{sha}:{path}") or ""),
             functions_in(blobs.get(f"{sha}^:{path}") or "")) for sha, subj, when in commits]


def attribute(div: dict, history: list[tuple]) -> None:
    """Fill commit_sha / commit_subject / committed_at: the newest commit that changed this function's
    body. Best effort; falls back to the newest commit touching the file. Pure given history."""
    for sha, subj, when, after, before in history:
        now_f, prev_f = after.get(div["func"]), before.get(div["func"])
        if now_f and (not prev_f or now_f["body_hash"] != prev_f["body_hash"]):
            div.update(commit_sha=sha, commit_subject=subj, committed_at=when)
            return
    if history:
        sha, subj, when = history[0][:3]
        div.update(commit_sha=sha, commit_subject=subj, committed_at=when)
    else:
        div.update(commit_sha="unknown", commit_subject=None, committed_at=None)


def analyse(days: int, deadline: float | None = None) -> dict:
    """Scan, group, find divergences. Read-only (files + git)."""
    t0 = time.monotonic()
    files = source_files()
    funcs, parsed = scan(files)
    groups = build_groups(funcs)
    in_groups = {m["path"] for g in groups for m in g["members"]}
    base, head, changed = git_window(days, in_groups)
    blobs = git_blobs([f"{rev}:{p}" for p in sorted(changed) for rev in (base, head)])
    base_fns = {p: functions_in(blobs[f"{base}:{p}"]) for p in changed if blobs.get(f"{base}:{p}") is not None}
    head_fns = {p: functions_in(blobs[f"{head}:{p}"]) for p in changed if blobs.get(f"{head}:{p}") is not None}
    divs = find_divergences(groups, base_fns, head_fns, changed)
    # siblings provably still on the pre-fix body first: that is the clearest "fix didn't travel"
    divs.sort(key=lambda d: (-len(d["siblings_with_old_body"]), -d["copies"], d["label"], d["path"]))
    histories: dict[str, list] = {}
    commits = commits_by_path(base) if divs else {}
    for d in divs:
        if deadline is not None and time.monotonic() > deadline:
            d.update(commit_sha="unattributed", commit_subject="(time budget)", committed_at=None)
            continue
        if d["path"] not in histories:
            histories[d["path"]] = file_history(d["path"], commits.get(d["path"], []))
        attribute(d, histories[d["path"]])
    return {"files": parsed, "functions": len(funcs), "groups": groups, "measures": measures(groups),
            "divergences": divs, "git": bool(head), "changed_files": len(changed),
            "seconds": round(time.monotonic() - t0, 1)}


# ---------------------------------------------------------------- database

def connect():
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def record(cur, res: dict) -> list[dict]:
    """Write groups, snapshots, divergences, the run row. -> divergences that are NEW (not seen before)."""
    from psycopg2.extras import execute_values
    groups = res["groups"]
    cur.execute("BEGIN")
    try:
        cur.execute("UPDATE black_flag_groups SET active = false WHERE active")
        execute_values(cur, "INSERT INTO black_flag_groups (group_key, label, copies, variants, near_copies, score, files) "
                            "VALUES %s ON CONFLICT (group_key) DO UPDATE SET label = EXCLUDED.label, "
                            "copies = EXCLUDED.copies, variants = EXCLUDED.variants, near_copies = EXCLUDED.near_copies, "
                            "score = EXCLUDED.score, "
                            "files = EXCLUDED.files, last_seen = now(), active = true",
                       [(g["key"], g["label"], g["copies"], g["variants"], g.get("near"), g["score"],
                         json.dumps(sorted({m["path"] for m in g["members"]}))) for g in groups], page_size=500)
        cur.execute("DELETE FROM black_flag_snapshots")     # the snapshot is the current state, replaced whole
        execute_values(cur, "INSERT INTO black_flag_snapshots (path, func, group_key, lineno, body_hash, "
                            "shape_hash, ntokens) VALUES %s ON CONFLICT (path, func) DO NOTHING",
                       [(m["path"], m["name"], g["key"], m.get("lineno"), m["body_hash"], m["shape_hash"],
                         m["ntokens"]) for g in groups for m in g["members"]], page_size=1000)
        new = []
        for d in res["divergences"]:
            cur.execute("INSERT INTO black_flag_divergences (group_key, path, func, commit_sha, commit_subject, "
                        "committed_at, copies, changed, siblings, siblings_with_old_body) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (group_key, path, func, commit_sha) DO NOTHING RETURNING id",
                        (d["group_key"], d["path"], d["func"], d.get("commit_sha") or "unknown",
                         d.get("commit_subject"), d.get("committed_at"), d["copies"], d["changed"],
                         json.dumps(d["siblings"]), json.dumps(d["siblings_with_old_body"])))
            if cur.fetchone():
                new.append(d)
        m = res["measures"]
        cur.execute("INSERT INTO black_flag_runs (files, functions, groups, groups_big, redundant_copies, "
                    "divergences, seconds) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (res["files"], res["functions"], m["groups"], m["groups_big"], m["redundant_copies"],
                     len(new), res["seconds"]))
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    return new


def digest(cur, res: dict, new_divs: list[dict], days: int) -> bool:
    from nova_notify import notify
    title, body = render_digest(res["groups"], res["measures"], new_divs, days)
    ok = notify(title, body=body, level="info", category="black_flag_tour", source=SERVICE,
                dedup_key=f"black_flag_tour:{datetime.now():%G-W%V}",
                meta={"groups_big": res["measures"]["groups_big"],
                      "redundant_copies": res["measures"]["redundant_copies"], "divergences": len(new_divs)})
    if ok and new_divs:
        cur.execute("UPDATE black_flag_divergences SET notified = true WHERE NOT notified")
    return ok


def print_result(res: dict, days: int, top: int = 25) -> None:
    m = res["measures"]
    print(f"{res['files']} files, {res['functions']} top-level functions, {m['groups']} copy groups "
          f"({m['groups_big']} with >= {BIG_GROUP} copies), {m['redundant_copies']} redundant copies; "
          f"{res['seconds']}s")
    print(f"\nTop {top} consolidation candidates (copies x variants):")
    for g in res["groups"][:top]:
        names = Counter(x["name"] for x in g["members"])
        alias = "" if len(names) == 1 else f"  (also as {', '.join(n for n in names if n != g['label'])[:60]})"
        print(f"  {g['score']:>6}  {g['label']:<28} {g['copies']:>3} copies {g['variants']:>3} variants "
              f"{g['near']:>3} near copies{alias}")
    if not res["git"]:
        print("\n(no git history available: divergence check skipped)")
        return
    print(f"\nDivergent fixes, last {days} days ({res['changed_files']} grouped file(s) changed):")
    for d in res["divergences"]:
        print(f"  {d['func']} @ {d['path']}: landed in {d['changed']} of {d['copies']} — "
              f"{(d.get('commit_sha') or '?')[:8]} \"{d.get('commit_subject') or ''}\"")
        lag = d["siblings_with_old_body"]
        print(f"      siblings {len(d['siblings'])}, still on the exact old body {len(lag)}: {', '.join(lag[:5])}")


def report(cur, top: int) -> None:
    cur.execute("SELECT to_regclass('black_flag_runs') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No Black Flag Tour runs yet (tables are created by the first scheduled run).")
        return
    cur.execute("SELECT label, copies, variants, score FROM black_flag_groups WHERE active "
                "ORDER BY score DESC, copies DESC LIMIT %s", (top,))
    print(f"Top {top} consolidation candidates:")
    for label, c, v, s in cur.fetchall():
        print(f"  {s:>5}  {label:<32} {c:>3} copies {v:>3} variants")
    cur.execute("WITH m AS (SELECT date_trunc('month', ts) mo, (array_agg(groups_big ORDER BY ts DESC))[1] big, "
                "(array_agg(redundant_copies ORDER BY ts DESC))[1] red FROM black_flag_runs GROUP BY 1), "
                "d AS (SELECT date_trunc('month', detected_at) mo, count(DISTINCT (group_key, commit_sha)) n "
                "FROM black_flag_divergences GROUP BY 1) "
                "SELECT to_char(m.mo, 'YYYY-MM'), m.big, m.red, coalesce(d.n, 0) FROM m LEFT JOIN d USING (mo) "
                "ORDER BY m.mo DESC LIMIT 12")
    print("\nMonthly (continuity review; last run of the month):")
    for mth, big, red, divs in cur.fetchall():
        print(f"  {mth}  groups >= {BIG_GROUP} copies {big:>4}  redundant copies {red:>5}  divergent fixes {divs:>3}")


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="weekly run: analyse, write, digest")
    ap.add_argument("--dry-run", action="store_true", help="print only; write and notify nothing")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="divergence window in days (default 8)")
    ap.add_argument("--report", nargs="?", const=20, type=int, metavar="N", help="top N groups + monthly stats")
    ap.add_argument("--selftest", action="store_true", help="offline check of hashing and grouping")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not 1 <= a.days <= 365:
        ap.error("--days must be 1..365")
    if a.report is not None:
        conn = connect()
        try:
            report(conn.cursor(), a.report)
        finally:
            conn.close()
        return 0
    t0 = time.monotonic()
    res = analyse(a.days, deadline=t0 + TIME_BUDGET_S)
    if a.dry_run:
        print_result(res, a.days)
        print(f"\n[dry-run] {len(res['divergences'])} divergent fix(es) — nothing written")
        return 0
    conn = connect()
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        new = record(cur, res)
        sent = digest(cur, res, new, a.days)
        m = res["measures"]
        log(f"{m['groups']} groups ({m['groups_big']} >= {BIG_GROUP}), {m['redundant_copies']} redundant copies, "
            f"{len(new)} new divergent fix(es); digest {'sent' if sent else 'NOT sent'} "
            f"({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
