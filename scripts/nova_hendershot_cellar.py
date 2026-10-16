#!/usr/bin/env python3
"""nova_hendershot_cellar.py — HENDERSHOT'S CELLAR: capabilities Nova holds and no longer uses.

From Maximum Overdrive (1986): Bubba Hendershot, who owns the Dixie Boy truck stop, keeps a
cellar full of grenades, an M60 and rocket launchers, bought "just in case" and sold on the
side ("buy cheap and sell dear, it's the American way"). Nobody upstairs knows what is down
there until the trucks come, and then everything in the cellar is a weapon for whoever reaches
it first. Nova's version: every key, token, sudo rule and tool permission she holds is a
weapon in the cellar. One that has not been touched in 30 days is held "just in case", and
this organ lists it with the evidence, so Little Mister can decide whether to throw it out.

It EXTENDS the Threshold Ledger (nova_threshold.py: who was invited in, with owner/purpose);
the Cellar adds the other half, WHEN each capability was last used, from real evidence:
  kind          holding (names/metadata only)                 last-use evidence
  secret        nova.secrets names; Keychain item names        scripts naming it (plus everything
                (acct/service nova; attributes, never -w)      that imports them) -> scheduler_runs,
                                                               or a live process on any node
  config_token  service_config rows whose KEY looks like a     scripts naming service + key
                token/key/secret (value never selected)
  ssh_key       threshold_ledger ssh_key rows                  auth logs need root: 'unknown'
  sudo_rule     `sudo -n -l` NOPASSWD rules per node (ssh)     the node's journal, _COMM=sudo
  ha_token      Home Assistant refresh/long-lived tokens       HA's own last_used_at (the auth
                (parsed with every non-metadata key dropped)   store is parsed key-filtered)
  mcp_server    Claude Code MCP servers                        tool_use blocks in transcripts
  claude_allow  Claude Code Bash allow rules                   commands in transcripts
  gateway_tool  Nova gateway TOOL_REGISTRY (read by AST)       gateway_traces.tool_calls
No secret VALUE is ever read, decrypted, selected, printed or stored.

A capability unused for >= 30 days (or never used inside an evidence window of >= 30 days) is
status 'proposed' ("consider revoking"). A source that cannot be read leaves its items
'unknown', never a crash. 'kept' (set by a human) is never overwritten; a capability that
disappears from a source that was read is marked 'revoked' with revoked_at. READ-ONLY: this
organ never revokes, rotates, edits or restarts anything — it proposes.

CLI:   --scheduled   --dry-run   --report   --selftest
Table: cellar_inventory (PK kind, holder, capability)
service_config: hendershot_cellar/latest (held, dormant, unknown, revoked_since_last_month),
                hendershot_cellar/dormant_days (default 30)
Schedule: monthly, the 2nd at 08:00 (digest notify, info).

MERGED 2026-10-15 (consolidation #11) into nova_capability_ledger.py as `--dormant`: logic and table
stay here; main() is a thin wrapper. TMA-1 decoys are dropped in gather() and inventory().
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

SERVICE = "hendershot_cellar"
SCRIPTS_DIR = Path(__file__).resolve().parent
SKIP_DIRS = {"tests", "_archive", "archive", "__pycache__", "_ingest", "data"}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
DORMANT_DAYS = 30
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 — patched in tests
SECRET_KEY_RE = re.compile(r"(?i)token|secret|passw|api[_-]?key|credential|_key$|^key$|scope")
KEYCHAIN_ACCT = "nova"
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
REPO_DIR = SCRIPTS_DIR.parent
# Code outside scripts/ that can name a secret but whose runs Nova does not log (use not observable).
EXTRA_DIRS = ("apps", "gateway", "deploy", "plugins", "discord", "config", "docker", "scripts/systemd")
EXTRA_HOME_DIRS = (".openclaw/bin", ".openclaw/apps", ".openclaw/gateway", ".openclaw/skills",
                   ".openclaw/extensions", ".openclaw/launchd", ".claude/mcp-servers", ".claude/hooks",
                   "Library/LaunchAgents")
EXTRA_SUFFIXES = {".py", ".sh", ".yaml", ".yml", ".json", ".plist", ".service", ".js", ".ts", ".swift", ".toml"}

# Metadata keys kept when parsing Home Assistant's .storage JSON. Every other key — token,
# jwt_key, password, data payloads — is dropped by the parser hook, so no value survives.
HA_KEEP = frozenset({"data", "refresh_tokens", "users", "entities", "devices", "entries", "id",
                     "client_name", "client_id", "token_type", "created_at", "last_used_at",
                     "entity_id", "platform", "disabled_by", "domain"})

# One read-only command per node: sudo rules, journal start, last sudo use per binary,
# running script/module names, and whether the scripts dir is a network mount.
PROBE = ("echo '@@SUDO'; sudo -n -l 2>/dev/null | sed -n '/may run the following/,$p'; "
         "echo '@@JSTART'; journalctl -q -o short-unix --no-pager 2>/dev/null | head -1 | cut -d' ' -f1; "
         "echo '@@SUDOUSE'; journalctl -q --since=-400d -o short-unix _COMM=sudo 2>/dev/null | "
         "awk '{for(i=1;i<=NF;i++) if($i ~ /^COMMAND=/) last[substr($i,9)]=$1} END{for(k in last) print last[k], k}'; "
         "echo '@@PS'; ps -eo args= 2>/dev/null | grep -oE '[A-Za-z0-9_.-]+[.](py|sh)|-m [A-Za-z0-9_.]+' | sort -u; "
         "echo '@@FS'; df -P \"$HOME/.openclaw/scripts\" 2>/dev/null | tail -1 | cut -d' ' -f1; echo '@@END'")


def log(m: str) -> None:
    print(f"[cellar {datetime.now():%H:%M:%S}] {m}", flush=True)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {str(e).splitlines()[0]}")
        return []


def _ts(ms_or_s, ms=False):
    try:
        return datetime.fromtimestamp(float(ms_or_s) / (1000 if ms else 1), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _iso(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── fleet probe (shared with nova_goblin_map) ───────────────────────────────

def parse_probe(text: str) -> dict:
    """Sections of PROBE output -> {sudo_rules, journal_start, sudo_use, procs, scripts_fs}.
    The scripts filesystem is reduced to 'network'/'local' — the mount source is not kept."""
    sec, cur = {}, None
    for ln in (text or "").splitlines():
        if ln.startswith("@@"):
            cur = ln[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(ln.rstrip())
    rules = [ln.strip() for ln in sec.get("SUDO", []) if "NOPASSWD:" in ln]
    use = {}
    for ln in sec.get("SUDOUSE", []):
        p = ln.split(None, 1)
        if len(p) == 2 and _ts(p[0]):
            use[p[1].strip()] = _ts(p[0])
    fs = (sec.get("FS") or [""])[0].strip()
    return {"sudo_rules": sorted(set(rules)), "journal_start": _ts((sec.get("JSTART") or [""])[0].strip()),
            "sudo_use": use, "procs": sorted({p.strip() for p in sec.get("PS", []) if p.strip()}),
            "scripts_fs": "network" if fs.startswith("//") or ":/" in fs else ("local" if fs else None),
            "complete": "END" in sec}


def _run_probe(ip: str, local: bool, timeout: int = 25):
    cmd = ["/bin/sh", "-c", PROBE] if local else ["ssh", *SSH_OPTS, ip, PROBE]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    p = parse_probe(r.stdout)
    return p if p["complete"] else None


def probe_host(name: str, ip: str, local: bool, _sleep=None) -> dict | None:
    """One node's probe, retried; None when unreachable (its capabilities stay 'unknown')."""
    res = W.retry(_run_probe, ip, local, attempts=2, delay=2.0, tag="cellar", _sleep=_sleep)
    return res or None


def probe_fleet(cur, _sleep=None) -> dict:
    """{node_name: probe dict or None}, all nodes in parallel."""
    nodes = _q(cur, "SELECT node_name, host(node_ip) FROM node_status ORDER BY node_name")
    try:
        from nova_fleet_exec import _local_ips
        local = _local_ips()
    except Exception:  # noqa: BLE001 — without it every node is probed over ssh
        local = set()
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as ex:
        futs = {n: ex.submit(probe_host, n, ip, ip in local, _sleep) for n, ip in nodes}
        out = {}
        for n, f in futs.items():
            try:
                out[n] = f.result(timeout=70)
            except Exception as e:  # noqa: BLE001 — one node never sinks the audit
                log(f"{n}: probe failed: {type(e).__name__}")
                out[n] = None
    return out


# ── script index (shared with nova_goblin_map) ──────────────────────────────

def module_key(p: Path, root: Path = SCRIPTS_DIR) -> str:
    rel = p.relative_to(root)
    if p.suffix == ".sh":
        return ".".join(rel.parts)              # shell scripts keep their suffix; never imported
    rel = rel.with_suffix("")
    return ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)


def script_index(root: Path = SCRIPTS_DIR) -> dict:
    """{module key: source text} for every .py/.sh under scripts/ (tests and archives excluded)."""
    out = {}
    for p in root.rglob("*"):
        if p.suffix not in (".py", ".sh") or SKIP_DIRS & set(p.relative_to(root).parts[:-1]):
            continue
        try:
            out[module_key(p, root)] = p.read_text(errors="replace")
        except (OSError, ValueError):
            continue
    return out


def extra_index(root: Path = REPO_DIR, home: Path | None = None) -> dict:
    """{'repo:<relpath>': text} for code outside scripts/ (repo top level + EXTRA_DIRS, and the
    home-dir tool/launchd/MCP dirs). Its runs are not logged, so a name found only here is 'unknown'."""
    home = home or Path.home()
    out = {}
    roots = [(root, [p for p in root.glob("*") if p.suffix in EXTRA_SUFFIXES])]
    roots += [(root, list((root / d).rglob("*"))) for d in EXTRA_DIRS if (root / d).is_dir()]
    roots += [(home, list((home / d).rglob("*"))) for d in EXTRA_HOME_DIRS if (home / d).is_dir()]
    for base, files in roots:
        for p in files:
            if p.suffix not in EXTRA_SUFFIXES or {"node_modules", ".venv", "venv"} & set(p.parts):
                continue
            try:
                if p.is_file() and p.stat().st_size < 2_000_000:
                    out["repo:" + str(p.relative_to(base))] = p.read_text(errors="replace")
            except (OSError, ValueError):
                continue
    return out


IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)


def import_graph(index: dict) -> dict:
    """{module: set(local modules it imports)}. Files of one package are treated as one unit."""
    pkgs = {k.split(".")[0] for k in index if "." in k}
    g = {}
    for k, src in index.items():
        deps = set()
        for a, b in IMPORT_RE.findall(src if not k.endswith(".sh") else ""):
            m = (a or b).split(".")[0]
            if m in pkgs:
                deps |= {x for x in index if x == m or x.startswith(m + ".")}
            elif m in index:
                deps.add(m)
        top = k.split(".")[0]
        if top in pkgs:
            deps |= {x for x in index if x == top or x.startswith(top + ".")}
        g[k] = deps - {k}
    return g


def closure(mod: str, graph: dict, _memo=None) -> set:
    """Every local module reachable from mod (iterative, cycle-safe)."""
    seen, stack = {mod}, [mod]
    while stack:
        for d in graph.get(stack.pop(), ()):
            if d not in seen:
                seen.add(d)
                stack.append(d)
    return seen


def name_refs(names, index: dict) -> dict:
    """{module: set(names it mentions literally, or by an f-string prefix 'name-{')}."""
    names = sorted({n for n in names if n and len(n) >= 4}, key=len, reverse=True)
    if not names:
        return {}
    rx = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, names)) + r")(?![\w-])")
    prefixes = {}
    for n in names:
        if "-" in n:
            prefixes.setdefault(n.rsplit("-", 1)[0] + "-{", set()).add(n)
    out = {}
    for k, src in index.items():
        found = set(rx.findall(src))
        for pre, ns in prefixes.items():
            if pre in src:
                found |= ns
        if found:
            out[k] = found
    return out


def runnable(mod: str) -> str:
    """The basename a scheduler/ps sees for a module key."""
    return mod.rsplit(".", 2)[-2] + ".sh" if mod.endswith(".sh") else mod.split(".")[-1] + ".py"


# ── usage evidence ──────────────────────────────────────────────────────────

def script_runs(cur) -> tuple:
    """({script basename: last start}, history start) from scheduler_runs."""
    rows = _q(cur, "SELECT task_script, max(started_at) FROM scheduler_runs GROUP BY 1")
    runs = {}
    for s, ms in rows:
        b = Path(str(s).split()[0]).name if s else ""
        t = _ts(ms, ms=True)
        if b and t and (b not in runs or t > runs[b]):
            runs[b] = t
    start = _q(cur, "SELECT min(started_at) FROM scheduler_runs")
    return runs, (_ts(start[0][0], ms=True) if start and start[0][0] else None)


def running_now(probes: dict) -> dict:
    """{script basename or module: node} for processes alive right now on any probed node."""
    out = {}
    for node, p in probes.items():
        for x in (p or {}).get("procs", []):
            out.setdefault(x[3:] if x.startswith("-m ") else Path(x).name, node)
    return out


def usage_for(name: str, refs: dict, graph: dict, runs: dict, live: dict, now: datetime):
    """(last_used, evidence, observable) for a name, from every module whose import closure
    mentions it. Named only outside scripts/ (apps, compose files ...) = not observable."""
    allrefs = sorted(m for m, ns in refs.items() if name in ns)
    direct = [m for m in allrefs if not m.startswith("repo:")]
    if not allrefs:
        return None, "no code in the repo names it", True
    if not direct:
        return None, f"named only in {', '.join(m[5:] for m in allrefs[:2])}; its use is not logged", False
    users = {m for m in graph if closure(m, graph) & set(direct)} | set(direct)
    best, why = None, None
    for m in sorted(users):
        b = runnable(m)
        node = live.get(b) or live.get(m) or live.get(m.split(".")[0])
        if node:
            return now, f"live process {b} on {node} (named in {', '.join(runnable(d) for d in direct[:3])})", True
        t = runs.get(b)
        if t and (best is None or t > best):
            best, why = t, f"scheduler ran {b} {t:%Y-%m-%d}"
    names = ", ".join(runnable(d) for d in direct[:3]) + (" …" if len(direct) > 3 else "")
    return best, (f"{why} (named in {names})" if best else f"named in {names}; none of them ran in history"), True


# ── holdings ────────────────────────────────────────────────────────────────

def keychain_names(text: str | None = None) -> dict:
    """{service name: modified time} for generic-password items with account 'nova' or a nova-*
    service. Attributes only (`security dump-keychain` without -d never prints a value)."""
    if text is None:
        try:
            text = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True,
                                  timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
    out = {}
    for item in re.split(r"(?m)^keychain: ", text or ""):
        if 'class: "genp"' not in item:
            continue
        acct = re.search(r'"acct"<blob>="([^"]*)"', item)
        svce = re.search(r'"svce"<blob>="([^"]*)"', item)
        mdat = re.search(r'"mdat"<timedate>=\S+\s+"(\d{14})Z', item)
        if not svce or not (acct and acct[1] == KEYCHAIN_ACCT or svce[1].startswith("nova")):
            continue
        t = datetime.strptime(mdat[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) if mdat else None
        if svce[1] not in out or (t and (out[svce[1]] is None or t > out[svce[1]])):
            out[svce[1]] = t
    return out


def ha_config_dir(cur=None) -> Path | None:
    """Home Assistant's --config dir from its running process, else service_config override."""
    try:
        ps = subprocess.run(["ps", "-A", "-o", "args="], capture_output=True, text=True, timeout=10).stdout
        for ln in ps.splitlines():
            if "homeassistant" in ln:
                m = re.search(r"(?:--config|-c)\s+(\S+)", ln)
                if m and Path(m[1]).is_dir():
                    return Path(m[1])
    except (OSError, subprocess.SubprocessError):
        pass
    if cur is not None:
        try:
            v = W.get_config(cur, SERVICE, "ha_config_dir")
            return Path(v) if v and Path(v).is_dir() else None
        except Exception:  # noqa: BLE001
            return None
    return None


def ha_storage(cfg: Path | None, name: str):
    """A .storage JSON file parsed with every key outside HA_KEEP dropped at parse time."""
    if not cfg:
        return None
    try:
        return json.loads((cfg / ".storage" / name).read_text(),
                          object_pairs_hook=lambda kv: {k: v for k, v in kv if k in HA_KEEP})
    except (OSError, ValueError):
        return None


def gateway_tools(path: Path = SCRIPTS_DIR / "nova_gateway" / "tools.py") -> tuple:
    """(TOOL_REGISTRY names, read-only names) from the gateway source, by AST (no import)."""
    reg, ro = [], set()
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return None, set()
    for n in tree.body:
        tg = n.target if isinstance(n, ast.AnnAssign) else (n.targets[0] if isinstance(n, ast.Assign) else None)
        if not isinstance(tg, ast.Name):
            continue
        v = n.value
        keys = v.keys if isinstance(v, ast.Dict) else getattr(v, "elts", [])
        vals = [k.value for k in keys if isinstance(k, ast.Constant)]
        if tg.id == "TOOL_REGISTRY":
            reg = vals
        elif tg.id == "_READONLY_TOOLS":
            ro = set(vals)
    return reg, ro


def claude_config(home: Path | None = None) -> dict:
    """{'mcp': [server names], 'allow': [Bash allow rules]} from Claude Code settings (names only)."""
    home = home or Path.home()
    mcp, allow = set(), []
    for f in (home / ".claude.json", home / ".claude" / "settings.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        mcp |= set((d.get("mcpServers") or {}).keys())
        for proj in (d.get("projects") or {}).values():
            mcp |= set(((proj or {}).get("mcpServers") or {}).keys())
        allow += [a for a in ((d.get("permissions") or {}).get("allow") or []) if str(a).startswith("Bash(")]
    return {"mcp": sorted(mcp), "allow": sorted(set(allow))}


def transcript_use(needles: dict, root: Path | None = None) -> tuple:
    """({key: newest transcript mtime containing its needle}, oldest transcript mtime) via rg."""
    root = root or Path.home() / ".claude" / "projects"
    rg = shutil.which("rg") or "/opt/homebrew/bin/rg"
    if not needles or not root.is_dir() or not os.access(rg, os.X_OK):
        return None, None
    args = [rg, "-o", "-N", "-F", "--no-messages", "-g", "*.jsonl"]
    for nd in needles.values():
        args += ["-e", nd]
    try:
        out = subprocess.run(args + [str(root)], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    rev = {v: k for k, v in needles.items()}
    found, mt = {}, {}
    for ln in out.splitlines():
        path, sep, hit = ln.partition(".jsonl:")
        path += ".jsonl"
        k = rev.get(hit)
        if not k or not sep:
            continue
        if path not in mt:
            try:
                mt[path] = datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc)
            except OSError:
                continue
        if k not in found or mt[path] > found[k]:
            found[k] = mt[path]
    try:
        oldest = min(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) for p in root.rglob("*.jsonl"))
    except (OSError, ValueError):
        oldest = None
    return found, oldest


def allow_needle(rule: str) -> str | None:
    m = re.match(r"Bash\(([^:*)]+)", rule)
    return f'"command":"{m[1].strip()}' if m else None


def _row(kind, holder, cap, last_used, evidence, history_since, held_since=None):
    return {"kind": kind, "holder": holder or "", "capability": cap, "last_used": last_used,
            "evidence": evidence, "history_since": history_since, "held_since": held_since}


def inventory(cur, ctx: dict) -> tuple:
    """(holdings, scopes read OK, sources unknown). ctx carries the shared evidence."""
    now, rows, scopes, unknown = ctx["now"], [], [], []
    runs, hist, live, refs, graph = ctx["runs"], ctx["runs_since"], ctx["live"], ctx["refs"], ctx["graph"]

    # secrets: fleet store + Keychain (names, never values)
    fleet = ctx.get("fleet_secrets")
    if fleet is None:
        unknown.append("fleet secret store")
    else:
        scopes.append(("secret", "fleet-store"))
    kc = ctx.get("keychain")
    if kc is None:
        unknown.append("keychain")
    else:
        scopes.append(("secret", f"keychain:{ctx['me']}"))
    threshold = ctx.get("threshold_names", set())
    # held_since stays None: the hourly vault mirror and Keychain copies reset updated_at/mdat,
    # so they date the last sync, not when the capability was acquired.
    for holder, items in ((f"keychain:{ctx['me']}", kc or {}), ("fleet-store", fleet or {})):
        for name in items:
            lu, ev, obs = usage_for(name, refs, graph, runs, live, now)
            ev += "; in Threshold Ledger" if name in threshold else ""
            rows.append(_row("secret", holder, name, lu, ev, hist if obs else None))

    # service_config token-like keys (key names only)
    for svc, key, upd in ctx.get("config_tokens", []):
        mods = [m for m, src in ctx["index"].items() if svc in src and key in src]
        lu = max((runs.get(runnable(m)) for m in mods if runs.get(runnable(m))), default=None)
        ev = f"named in {len(mods)} script(s)" + (f", last run {lu:%Y-%m-%d}" if lu else "") if mods else "no script names it"
        rows.append(_row("config_token", "service_config", f"{svc}/{key}", lu, ev, hist, upd))
    scopes.append(("config_token", "*"))

    # ssh keys authorized (from the Threshold Ledger); auth logs are root-only -> unknown
    for host, fp, label, first in ctx.get("ssh_keys", []):
        rows.append(_row("ssh_key", host, f"{(label or 'no-comment')[:40]} {fp[:20]}", None,
                         "authorized; sshd auth log needs root to read, last use not observable", None, first))

    # sudo NOPASSWD rules per node
    for node, p in sorted(ctx["probes"].items()):
        if p is None:
            unknown.append(f"sudo rules on {node}")
            continue
        scopes.append(("sudo_rule", node))
        for rule in p["sudo_rules"]:
            cmds = rule.split("NOPASSWD:", 1)[1].strip()
            if p["journal_start"] is None:
                rows.append(_row("sudo_rule", node, rule, None, "no readable journal on this node", None))
                continue
            if cmds == "ALL":
                lu = max(p["sudo_use"].values(), default=None)
            else:
                bins = {c.strip().split()[0] for c in cmds.split(",") if c.strip()}
                lu = max((t for b, t in p["sudo_use"].items() if b in bins), default=None)
            ev = f"journal: last sudo {lu:%Y-%m-%d}" if lu else "journal: no matching sudo use"
            rows.append(_row("sudo_rule", node, rule, lu, ev + f" (journal since {p['journal_start']:%Y-%m-%d})",
                             p["journal_start"]))

    # Home Assistant tokens: HA records last_used_at itself
    auth = ctx.get("ha_auth")
    if auth is None:
        unknown.append("home assistant auth store")
    else:
        scopes.append(("ha_token", "home-assistant"))
        for t in (auth.get("data") or {}).get("refresh_tokens") or []:
            if t.get("token_type") == "system":
                continue
            label = IP_RE.sub("<host>", str(t.get("client_name") or t.get("client_id") or "unnamed"))
            cap = f"{t.get('token_type')}:{label}:{str(t.get('id'))[:8]}"
            lu, cr = _iso(t.get("last_used_at")), _iso(t.get("created_at"))
            rows.append(_row("ha_token", "home-assistant", cap, lu,
                             f"HA last_used_at {lu:%Y-%m-%d}" if lu else "HA records no use", cr, cr))

    # Claude Code MCP servers and Bash allow rules
    cc, seen, tstart = ctx.get("claude") or {}, ctx.get("transcript_use"), ctx.get("transcripts_since")
    if seen is None:
        unknown.append("claude transcripts")
    for srv in cc.get("mcp", []):
        lu = (seen or {}).get(f"mcp:{srv}")
        rows.append(_row("mcp_server", "claude-code", srv, lu,
                         f"last tool_use in transcripts {lu:%Y-%m-%d}" if lu else "no tool_use in transcripts",
                         tstart if seen is not None else None))
    for rule in cc.get("allow", []):
        lu = (seen or {}).get(f"allow:{rule}")
        rows.append(_row("claude_allow", "claude-code", rule, lu,
                         f"last matching command {lu:%Y-%m-%d}" if lu else "no matching command in transcripts",
                         tstart if seen is not None and allow_needle(rule) else None))
    if cc:
        scopes += [("mcp_server", "claude-code"), ("claude_allow", "claude-code")]

    # gateway tools (state-changing ones are the ones worth throwing out)
    reg, ro = ctx.get("gateway_registry") or (None, set())
    if reg is None:
        unknown.append("gateway tool registry")
    else:
        scopes.append(("gateway_tool", "nova-gateway"))
        used, gstart = ctx.get("gateway_use", ({}, None))
        for tool in reg:
            lu = used.get(tool)
            rows.append(_row("gateway_tool", "nova-gateway", tool + ("" if tool in ro else " [acts]"), lu,
                             f"gateway_traces last call {lu:%Y-%m-%d}" if lu else "no call in gateway_traces",
                             gstart))
    import nova_capability_ledger as CL   # belt and braces: no decoy row is ever proposed or recorded
    return CL.drop_decoys(rows, key=lambda r: (r["holder"], r["capability"])), scopes, unknown


# ── classification (pure) ───────────────────────────────────────────────────

def classify(r: dict, now: datetime, dormant_days: int = DORMANT_DAYS) -> tuple:
    """(status, days_dormant). 'proposed' only on evidence covering at least dormant_days."""
    if r["last_used"]:
        d = max(0, (now - r["last_used"]).days)
        return ("proposed" if d >= dormant_days else "active"), d
    if r["history_since"] is None:
        return "unknown", None
    since = max(x for x in (r["held_since"], r["history_since"]) if x)
    d = max(0, (now - since).days)
    return ("proposed" if d >= dormant_days else "unknown"), d


def line(r: dict) -> str:
    dd = "-" if r.get("days_dormant") is None else f"{r['days_dormant']}d"
    return f"{r['status']:<8} {dd:>5}  {r['kind']:<12} {r['holder'][:22]:<22} {r['capability'][:60]}  ({r['evidence'][:110]})"


def summarize(rows: list) -> dict:
    by = {}
    for r in rows:
        k = by.setdefault(r["kind"], {"held": 0, "dormant": 0, "unknown": 0})
        k["held"] += 1
        k["dormant"] += r["status"] == "proposed"
        k["unknown"] += r["status"] == "unknown"
    return {"held": len(rows), "dormant": sum(r["status"] == "proposed" for r in rows),
            "unknown": sum(r["status"] == "unknown" for r in rows), "by_kind": by}


def digest(rows: list, summ: dict, revoked: int, top: int = 15) -> tuple:
    """(title, body) for the monthly notify. Names and evidence only; values are never held."""
    dormant = sorted((r for r in rows if r["status"] == "proposed"), key=lambda r: -(r["days_dormant"] or 0))
    title = f"Hendershot's Cellar: {summ['dormant']} of {summ['held']} capabilities dormant 30d+"
    body = [f"Held {summ['held']}, dormant {summ['dormant']}, unknown {summ['unknown']}, "
            f"revoked since last month {revoked}. Consider revoking (your call, nothing was changed):"]
    body += [f"• {r['kind']} {r['holder']}: {r['capability'][:60]} — {r['days_dormant']}d ({r['evidence'][:90]})"
             for r in dormant[:top]]
    if len(dormant) > top:
        body.append(f"…and {len(dormant) - top} more: nova_hendershot_cellar.py --report")
    return title, "\n".join(body)


# ── PG ──────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cellar_inventory (
  kind text NOT NULL, holder text NOT NULL DEFAULT '', capability text NOT NULL,
  last_used timestamptz, evidence text, days_dormant integer,
  status text NOT NULL DEFAULT 'unknown', reviewed_by text, held_since timestamptz,
  first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  PRIMARY KEY (kind, holder, capability));
CREATE INDEX IF NOT EXISTS cellar_inventory_status ON cellar_inventory (status);
"""


def ensure_schema(cur) -> None:
    from nova_pg_schema import safe_ddl
    safe_ddl(cur, SCHEMA)


def fleet_secret_names(cur) -> dict | None:
    """{name: updated_at} from the fleet store. The ciphertext column is never selected.
    TMA-1 decoy names are dropped (callers: gather() here and in nova_goblin_map)."""
    import nova_capability_ledger as CL
    try:
        cur.execute("SELECT name, updated_at FROM nova.secrets ORDER BY name")
        return CL.drop_decoys(dict(cur.fetchall()))
    except Exception as e:  # noqa: BLE001 — unreadable store = unknown, not a crash
        log(f"fleet secret names unreadable: {str(e).splitlines()[0]}")
        return None


def gather(cur, _sleep=None) -> dict:
    """Every evidence source, each fail-open (None = could not be read). TMA-1 decoys excluded."""
    import nova_capability_ledger as CL
    now = NOW()
    probes = probe_fleet(cur, _sleep=_sleep)
    index = script_index()
    fleet = fleet_secret_names(cur)
    kc = CL.drop_decoys(keychain_names() if sys.platform == "darwin" else None)
    names = set(fleet or {}) | set(kc or {})
    runs, runs_since = script_runs(cur)
    cc = claude_config()
    needles = {f"mcp:{s}": f'"name":"mcp__{s}__' for s in cc["mcp"]}
    needles.update({f"allow:{a}": allow_needle(a) for a in cc["allow"] if allow_needle(a)})
    tu, tstart = transcript_use(needles)
    gw = {t: ts for t, ts in _q(
        cur, "SELECT coalesce(t->>'name', t->>'tool'), max(created_at) FROM gateway_traces, "
             "jsonb_array_elements(CASE WHEN jsonb_typeof(tool_calls)='array' THEN tool_calls ELSE '[]' END) t "
             "GROUP BY 1")}
    gstart = _q(cur, "SELECT min(created_at) FROM gateway_traces WHERE jsonb_typeof(tool_calls)='array' "
                     "AND jsonb_array_length(tool_calls) > 0")
    return {
        "now": now, "me": os.uname().nodename.split(".")[0], "probes": probes, "index": index,
        "graph": import_graph(index), "refs": name_refs(names, {**index, **extra_index()}), "runs": runs, "runs_since": runs_since,
        "live": running_now(probes), "fleet_secrets": fleet, "keychain": kc,
        "threshold_names": {r[0] for r in _q(cur, "SELECT ident FROM threshold_ledger WHERE kind='slack_token'")},
        "ssh_keys": _q(cur, "SELECT host, fingerprint, label, first_seen FROM threshold_ledger WHERE kind='ssh_key' "
                            "AND last_seen > now() - interval '8 days'"),
        "config_tokens": CL.drop_decoys([r for r in _q(cur, "SELECT service, key, updated_at FROM service_config")
                                         if SECRET_KEY_RE.search(r[1])], key=lambda r: (r[0], r[1])),
        "ha_auth": ha_storage(ha_config_dir(cur), "auth"), "claude": cc,
        "transcript_use": tu, "transcripts_since": tstart, "gateway_registry": gateway_tools(),
        "gateway_use": (gw, gstart[0][0] if gstart and gstart[0][0] else None),
    }


def upsert(cur, rows: list, scopes: list, started: datetime) -> int:
    ensure_schema(cur)
    for r in rows:
        cur.execute(
            "INSERT INTO cellar_inventory (kind, holder, capability, last_used, evidence, days_dormant, status, "
            "held_since) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (kind, holder, capability) DO UPDATE SET "
            "last_used=EXCLUDED.last_used, evidence=EXCLUDED.evidence, days_dormant=EXCLUDED.days_dormant, "
            "held_since=COALESCE(EXCLUDED.held_since, cellar_inventory.held_since), last_seen=now(), "
            "revoked_at=NULL, status=CASE WHEN cellar_inventory.status='kept' THEN 'kept' ELSE EXCLUDED.status END",
            (r["kind"], r["holder"], r["capability"], r["last_used"], r["evidence"], r["days_dormant"],
             r["status"], r["held_since"]))
    gone = 0
    for kind, holder in scopes:   # only sources that were actually read can declare something gone
        cur.execute("UPDATE cellar_inventory SET status='revoked', revoked_at=now(), "
                    "reviewed_by=COALESCE(reviewed_by, 'cellar: no longer held') "
                    "WHERE kind=%s AND (%s='*' OR holder=%s) AND last_seen < %s AND status <> 'revoked'",
                    (kind, holder, holder, started))
        gone += cur.rowcount or 0
    return gone


def revoked_since(cur, days: int = 31) -> int:
    r = _q(cur, "SELECT count(*) FROM cellar_inventory WHERE status='revoked' "
                "AND revoked_at > now() - make_interval(days => %s)", (days,))
    return int(r[0][0]) if r else 0


def run(dry: bool = False, _sleep=None) -> list:
    t0 = NOW()
    conn = W.connect()
    try:
        cur = conn.cursor()
        try:
            dd = int(W.get_config(cur, SERVICE, "dormant_days", DORMANT_DAYS))
        except Exception:  # noqa: BLE001
            dd = DORMANT_DAYS
        ctx = gather(cur, _sleep=_sleep)
        rows, scopes, unknown = inventory(cur, ctx)
        for r in rows:
            r["status"], r["days_dormant"] = classify(r, ctx["now"], dd)
        summ = summarize(rows)
        log(f"{'DRY RUN ' if dry else ''}held {summ['held']}, dormant {summ['dormant']}, unknown {summ['unknown']}; "
            f"sources unreadable: {', '.join(unknown) or 'none'}")
        for k, v in sorted(summ["by_kind"].items()):
            print(f"  {k:<13} held {v['held']:>4}  dormant {v['dormant']:>4}  unknown {v['unknown']:>4}")
        for r in sorted(rows, key=lambda r: (r["status"] != "proposed", -(r["days_dormant"] or 0))):
            if r["status"] == "proposed":
                print("  " + line(r))
        if dry:
            print(f"  (would record {len(rows)} rows; {(NOW() - t0).total_seconds():.1f}s)")
            return rows
        gone = upsert(cur, rows, scopes, t0)
        rev = revoked_since(cur)
        W.set_config(cur, SERVICE, "latest", {"ts": t0.isoformat(), "held": summ["held"], "dormant": summ["dormant"],
                                              "unknown": summ["unknown"], "revoked_since_last_month": rev,
                                              "gone_this_run": gone, "unreadable": unknown,
                                              "by_kind": summ["by_kind"]}, by="nova_hendershot_cellar")
        title, body = digest(rows, summ, rev)
        from nova_notify import notify
        notify(title, body=body, level="info", category="security", source="nova_hendershot_cellar.py",
               dedup_key=f"hendershot-cellar-{t0:%Y-%m}")
        log(f"recorded {len(rows)}; {gone} no longer held; digest queued ({(NOW() - t0).total_seconds():.1f}s)")
        return rows
    finally:
        conn.close()


def report() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        if (_q(cur, "SELECT to_regclass('cellar_inventory')") or [(None,)])[0][0] is None:
            print("cellar not inventoried yet")
            return 0
        latest = W.get_config(cur, SERVICE, "latest") or {}
        print(f"held {latest.get('held')}  dormant {latest.get('dormant')}  unknown {latest.get('unknown')}  "
              f"revoked since last month {latest.get('revoked_since_last_month')}  (as of {latest.get('ts')})")
        for k, h, c, dd, ev, st, rb in _q(
                cur, "SELECT kind, holder, capability, days_dormant, evidence, status, reviewed_by FROM cellar_inventory "
                     "WHERE status IN ('proposed','kept','revoked') ORDER BY status, days_dormant DESC NULLS LAST"):
            print(f"  {st:<8} {'-' if dd is None else f'{dd}d':>5}  {k:<12} {h[:22]:<22} {c[:60]}  ({(ev or '')[:90]})"
                  + (f" [{rb}]" if rb else ""))
        return 0
    finally:
        conn.close()


def selftest() -> int:
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    p = parse_probe("@@SUDO\nUser x may run the following commands on h:\n    (ALL) ALL\n    (ALL) NOPASSWD: ALL\n"
                    "@@JSTART\n1780000000.1\n@@SUDOUSE\n1790000000.5 /usr/bin/apt\n@@PS\nnova_a.py\n-m nova_gateway\n"
                    "@@FS\n//u@h/nas\n@@END\n")
    assert p["sudo_rules"] == ["(ALL) NOPASSWD: ALL"] and p["scripts_fs"] == "network" and p["complete"], p
    assert running_now({"h": p}) == {"nova_a.py": "h", "nova_gateway": "h"}
    idx = {"nova_a": "import nova_b\n", "nova_b": "k = get('nova-tok')\n", "nova_c": "f'nova-bambu-{s}'\n"}
    refs = name_refs({"nova-tok", "nova-bambu-x1"}, idx)
    assert refs == {"nova_b": {"nova-tok"}, "nova_c": {"nova-bambu-x1"}}, refs
    g = import_graph(idx)
    lu, ev, obs = usage_for("nova-tok", refs, g, {"nova_a.py": now - timedelta(days=2)}, {}, now)
    assert lu == now - timedelta(days=2) and "nova_a.py" in ev and obs, ev
    assert usage_for("x", {"repo:apps/a.py": {"x"}}, g, {}, {}, now)[2] is False
    assert usage_for("y", refs, g, {}, {}, now) == (None, "no code in the repo names it", True)
    r = _row("secret", "s", "n", None, "", now - timedelta(days=90), now - timedelta(days=40))
    assert classify(r, now) == ("proposed", 40)
    assert classify(dict(r, history_since=None), now) == ("unknown", None)
    assert classify(dict(r, last_used=now - timedelta(days=3)), now) == ("active", 3)
    kc = keychain_names('keychain: "k"\nclass: "genp"\n    "acct"<blob>="nova"\n    "mdat"<timedate>=0x00  '
                        '"20260506024048Z\\000"\n    "svce"<blob>="nova-x"\nkeychain: "k"\nclass: "genp"\n'
                        '    "acct"<blob>="me"\n    "svce"<blob>="bank"\n')
    assert list(kc) == ["nova-x"] and kc["nova-x"].year == 2026, kc
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="inventory, record, send the monthly digest")
    ap.add_argument("--dry-run", action="store_true", help="inventory and print; write nothing, notify nothing")
    ap.add_argument("--report", action="store_true", help="stored proposals, kept and revoked items")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.dry_run or a.scheduled or a.report:   # merged 2026-10-15 (#11): nova_capability_ledger.py --dormant
        import nova_capability_ledger as CL
        log(f"merged into nova_capability_ledger.py --dormant on {CL.MERGED_ON}; delegating")
        if a.dry_run or a.scheduled:
            return CL.main(["--dormant"] + (["--dry-run"] if a.dry_run else []))
        return CL.main(["--dormant", "--show"])
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
