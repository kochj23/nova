#!/usr/bin/env python3
"""nova_reconciler.py — diff what Nova SAYS about herself against what is live.

THE PROBLEM THIS EXISTS FOR. nova_expectations.py watches artifacts, so it catches work
that silently stopped happening. Nothing watches the DOCUMENTS — and Nova reasons from
those. Every session loads agent_docs into context and treats it as ground truth. When the
fleet moves and the prose does not, Nova is not merely out of date: she is confidently,
fluently wrong, which is worse than silent, because it propagates into decisions.

Found on 2026-09-17, all live-verified, all wrong in the docs:
  * nova-system-map  "PG PRIMARY IS ... ON NOVA-CORE .2"   -> primary is 192.168.1.10
  * nova-system-map  "Memory server likewise runs on .2"   -> memory-server DNS -> .6
  * nova-system-map  "a 1.6M-vector memory" / "1.8M"       -> 2,216,095
  * identity         "1.3M+ vectors ... 177 scripts"       -> 2.2M
  * soul             "877,000+ memories"                   -> 2.2M
The system map's own CORRECTION block, written to fix an earlier drift, had itself drifted.
Corrections were outrunning the documents.

SO THIS DOES NOT WATCH FILES OR JOBS. IT WATCHES CLAIMS.
A fact says "this value can be measured live, and here is the regex that finds assertions
about it in prose." Every run measures the truth, scans the corpus, and files each
contradiction into nova_ops.doc_drift. The point is that the self-model becomes DERIVED
rather than REMEMBERED: prose is allowed to rot, but it is no longer allowed to rot quietly.

SCOPE IS LOAD-BEARING, NOT DECORATION. A dated incident memory saying "1.7M memories" on
2026-07-06 was TRUE on 2026-07-06 and must never be flagged; rewriting history is a worse
failure than stale prose. Each fact therefore carries a `scope` regex naming only the
sources that purport to describe the PRESENT. Default scope is the empty set, so a
carelessly added fact finds nothing rather than indicting the archive.

This reports; it does not rewrite. Nova's identity documents are not a thing to silently
edit out from under her. Deltas get filed, a human (or a Rung-2 coagency proposal) decides.

  nova_reconciler.py                 # check, file drift, report
  nova_reconciler.py --seed          # register the starter facts above
  nova_reconciler.py --facts         # list the registry
  nova_reconciler.py --list          # list open drift
  nova_reconciler.py --wontfix 12    # stop reporting drift row 12
  nova_reconciler.py --self-audit    # scheduler scripts exist, ports listen, processes run
                                     # (absorbed nova_self_audit.py on 2026-10-09, organ audit M14)
"""
import argparse
import json
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# nova_config (Slack routing) lives in the fleet-shared script share, which is not
# necessarily this file's directory on every node.
if Path("/nova/scripts").is_dir():
    sys.path.append("/nova/scripts")
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_facts (
    name         text PRIMARY KEY,
    kind         text NOT NULL,                  -- pg_scalar | http_json | dns | shell
    target       text NOT NULL,                  -- query | url | hostname | command
    extract      text,                           -- http_json: dotted path into the payload
    dsn          text,                           -- pg_scalar: defaults to nova_ops
    claim_re     text NOT NULL,                  -- group(1) = the value the prose asserts
    compare      text NOT NULL DEFAULT 'exact',  -- exact | ci | numeric | host
    tolerance    numeric NOT NULL DEFAULT 0,     -- numeric: allowed fractional drift
    scope        text NOT NULL DEFAULT '$^',     -- regex over source id; default matches nothing
    severity     text NOT NULL DEFAULT 'warn',   -- info | warn | critical
    note         text,
    enabled      boolean NOT NULL DEFAULT true,
    last_checked timestamptz,
    last_live    text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS doc_drift (
    id          bigserial PRIMARY KEY,
    fact_name   text NOT NULL REFERENCES doc_facts(name) ON DELETE CASCADE,
    source_kind text NOT NULL,                   -- agent_docs | claude_memories
    source_id   text NOT NULL,                   -- doc_type or memory name
    claimed     text NOT NULL,
    live        text NOT NULL,
    excerpt     text,
    severity    text NOT NULL DEFAULT 'warn',
    status      text NOT NULL DEFAULT 'open',    -- open | fixed | wontfix
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (fact_name, source_kind, source_id, claimed)
);
CREATE INDEX IF NOT EXISTS doc_drift_open_idx ON doc_drift (status, severity);
"""


def conn(dsn=None):
    import psycopg2
    return psycopg2.connect(dsn or DSN)


# ---------------------------------------------------------------- probes

def probe(f):
    """Return (live_value_str, detail). live None = could not measure."""
    kind, target = f["kind"], f["target"]
    try:
        if kind == "pg_scalar":
            c = conn(f.get("dsn")); cur = c.cursor()
            cur.execute(target)
            row = cur.fetchone(); c.close()
            if not row or row[0] is None:
                return None, "query returned no value"
            return str(row[0]).strip(), "pg"

        if kind == "http_json":
            r = subprocess.run(["curl", "-s", "-m", "15", target],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0 or not r.stdout.strip():
                return None, f"fetch failed rc={r.returncode}"
            payload = json.loads(r.stdout)
            for part in (f.get("extract") or "").split("."):
                if not part:
                    continue
                payload = payload[int(part)] if isinstance(payload, list) else payload[part]
            return str(payload).strip(), "http"

        if kind == "dns":
            return socket.gethostbyname(target), "dns"

        if kind == "shell":
            # target is the operator-authored probe command stored in doc_facts
            r = subprocess.run(["/bin/sh", "-c", target], capture_output=True, text=True, timeout=60)
            out = r.stdout.strip()
            return (out or None), (f"rc={r.returncode}" if not out else "shell")
    except Exception as ex:
        return None, f"probe error: {str(ex).strip()[:90]}"
    return None, f"unknown kind {kind}"


# ---------------------------------------------------------------- comparators

_HOSTCACHE = {}


def norm_host(v):
    """'.2' / '192.168.1.2/32' / 'nova-core' / 'NOVA-CORE .2' -> '192.168.1.2'."""
    v = str(v).strip().strip('()[],;:').split("/")[0]
    # A name carrying an explicit octet ("NOVA-CORE .2") is most specific — trust the octet.
    m = re.search(r'\.(\d{1,3})\s*$', v)
    if m and not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', v):
        return "192.168.1." + m.group(1)
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', v):
        return v
    if re.match(r'^\.?\d{1,3}$', v):
        return "192.168.1." + v.lstrip(".")
    key = v.lower()
    if key in _HOSTCACHE:
        return _HOSTCACHE[key]
    try:
        ip = socket.gethostbyname(key)
    except Exception:
        ip = key
    _HOSTCACHE[key] = ip
    return ip


_MULT = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "b": 1e9}


def norm_num(v):
    m = re.match(r'^\s*([0-9][0-9,._]*)\s*([kKmMgGbB]?)', str(v).replace("+", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").rstrip(".")) * _MULT[m.group(2).lower()]
    except Exception:
        return None


def agrees(f, claimed, live):
    """True if the prose claim is consistent with the live value."""
    mode = f["compare"]
    if mode == "exact":
        return claimed.strip() == live.strip()
    if mode == "ci":
        return claimed.strip().lower() == live.strip().lower()
    if mode == "host":
        return norm_host(claimed) == norm_host(live)
    if mode == "numeric":
        a, b = norm_num(claimed), norm_num(live)
        if a is None or b is None or b == 0:
            return False
        return abs(a - b) / abs(b) <= float(f["tolerance"] or 0)
    return False


# ---------------------------------------------------------------- corpus

def corpus(cur):
    """[(source_kind, source_id, flattened_text)] — the prose Nova reasons from."""
    out = []
    cur.execute("SELECT doc_type, content FROM agent_docs WHERE content IS NOT NULL")
    out += [("agent_docs", a, re.sub(r"\s+", " ", b)) for a, b in cur.fetchall()]
    cur.execute("SELECT name, content FROM claude_memories WHERE content IS NOT NULL")
    out += [("claude_memories", a, re.sub(r"\s+", " ", b)) for a, b in cur.fetchall()]
    return out


def check(args):
    c = conn(); cur = c.cursor()
    cur.execute(SCHEMA); c.commit()
    cur.execute("""SELECT name, kind, target, extract, dsn, claim_re, compare,
                          tolerance, scope, severity, note
                   FROM doc_facts WHERE enabled ORDER BY name""")
    cols = [d[0] for d in cur.description]
    facts = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not facts:
        print("  no facts registered — run with --seed"); return 0

    body = corpus(cur)
    findings, unmeasurable = [], []
    seen_keys, measured = set(), []

    for f in facts:
        live, detail = probe(f)
        cur.execute("UPDATE doc_facts SET last_checked=now(), last_live=%s WHERE name=%s",
                    (live, f["name"]))
        if live is None:
            # Do NOT fall through to resolution: an unreachable probe must never be
            # allowed to look like "the docs got fixed". Absence of evidence closed
            # as success is the precise failure this tool exists to abolish.
            unmeasurable.append((f["name"], detail))
            if not args.quiet:
                print(f"  [UNMEASURED] {f['name']:26} {detail}")
            continue
        measured.append(f["name"])

        scope = re.compile(f["scope"])
        claim = re.compile(f["claim_re"])
        hits = mism = 0
        for kind, sid, text in body:
            if not scope.match(sid):
                continue
            for m in claim.finditer(text):
                claimed = m.group(1).strip()
                hits += 1
                if agrees(f, claimed, live):
                    continue
                mism += 1
                excerpt = text[max(0, m.start() - 70):m.end() + 50].strip()
                key = (f["name"], kind, sid, claimed)
                seen_keys.add(key)
                cur.execute("""
                    INSERT INTO doc_drift (fact_name, source_kind, source_id, claimed,
                                           live, excerpt, severity)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (fact_name, source_kind, source_id, claimed) DO UPDATE
                      SET last_seen=now(), live=EXCLUDED.live, excerpt=EXCLUDED.excerpt,
                          severity=EXCLUDED.severity,
                          status=CASE WHEN doc_drift.status='wontfix' THEN 'wontfix' ELSE 'open' END,
                          resolved_at=NULL
                    RETURNING id, status""", (f["name"], kind, sid, claimed, live, excerpt, f["severity"]))
                did, status = cur.fetchone()
                if status != "wontfix":
                    findings.append((did, f["name"], kind, sid, claimed, live, f["severity"], f["note"]))
        if not args.quiet:
            print(f"  [{'DRIFT' if mism else 'ok':7}] {f['name']:26} live={live[:34]:34} "
                  f"{hits} claim(s), {mism} stale")

    # A claim that no longer contradicts reality is resolved — either the doc was fixed
    # or the world moved back. Close it rather than leaving a permanent scold.
    # Only facts we actually measured this run may resolve anything. A fact that was
    # disabled, deleted or unreachable leaves its drift open and visible.
    if measured:
        cur.execute("SELECT id, fact_name, source_kind, source_id, claimed FROM doc_drift "
                    "WHERE status='open' AND fact_name = ANY(%s)", (measured,))
        for did, fn, sk, sid, claimed in cur.fetchall():
            if (fn, sk, sid, claimed) not in seen_keys:
                cur.execute("UPDATE doc_drift SET status='fixed', resolved_at=now() WHERE id=%s", (did,))
    c.commit()

    if unmeasurable and not args.quiet:
        print(f"\n  !! {len(unmeasurable)} fact(s) could not be measured — their drift stays open:")
        for n, d in unmeasurable:
            print(f"     {n:26} {d}")

    if findings:
        worst = "critical" if any(f[6] == "critical" for f in findings) else "warn"
        lines = ["📄 *Documentation drift — Nova's self-model contradicts the live fleet*", ""]
        for did, fn, kind, sid, claimed, live, sev, note in findings:
            mark = "🔴" if sev == "critical" else "•"
            src = "doc" if kind == "agent_docs" else "mem"
            lines.append(f"{mark} `{src}:{sid}` claims *{claimed}* for `{fn}` — live is *{live}*  (#{did})")
            if note:
                lines.append(f"   _{note}_")
        lines += ["", "These documents load into every session as ground truth. Until the prose is "
                      "corrected, Nova will keep reasoning from the stale value.",
                  "Silence a false positive: `nova_reconciler.py --wontfix <id>`"]
        msg = "\n".join(lines)
        print("\n" + msg)
        if not args.no_slack:
            try:
                import nova_config
                chan = nova_config.SLACK_ALERTS if worst == "critical" else nova_config.SLACK_DIGEST
                nova_config.post_both(msg, slack_channel=chan)
            except Exception as ex:
                print(f"(slack failed: {ex})", file=sys.stderr)
    elif not args.quiet:
        print(f"  === {len(facts)} facts, no drift ===")
    c.close()
    return 1 if findings else 0


# ---------------------------------------------------------------- registry

SEEDS = [
    dict(name="memory.vector_count", kind="http_json",
         target="http://memory-server.digitalnoise.net:18790/stats", extract="count",
         claim_re=r'([0-9][0-9.,]*\s*[MmKk]?)\+?[\s-]*(?:vector|memorie|memories)',
         compare="numeric", tolerance=0.10,
         scope=r'^(identity|nova-system-map|soul|agents|memory|data-platform)$',
         severity="warn",
         note="Nova quotes her own memory size when describing herself; a 40% understatement "
              "reads as false modesty and mis-sizes every capacity decision."),
    dict(name="pg.primary.host", kind="pg_scalar",
         target="SELECT CASE WHEN pg_is_in_recovery() THEN 'REPLICA' "
                "ELSE host(inet_server_addr()) END",
         claim_re=r'(?i)PG\s+PRIMARY\s+IS\s+(?:THE\s+)?(?:pg17\s+DOCKER\s+CONTAINER\s+ON\s+)?'
                  r'(?:NOW\s+)?([A-Za-z][\w-]*(?:\s+\.\d{1,3})?|\.\d{1,3}|192\.168\.1\.\d{1,3})',
         compare="host", tolerance=0,
         scope=r'^(nova-system-map|data-platform|runbook-.*|memory)$',
         severity="critical",
         note="Writes sent to the wrong node are lost or rejected; this is the single "
              "highest-consequence sentence in the corpus."),
    dict(name="memory.server.host", kind="dns", target="memory-server.digitalnoise.net",
         claim_re=r'(?i)memory[ -]server\s+(?:likewise\s+)?(?:runs?\s+on|lives?\s+on|->)\s*'
                  r'\(?\.?(\d{1,3}|192\.168\.1\.\d{1,3})',
         compare="host", tolerance=0,
         scope=r'^(nova-system-map|data-platform|identity|memory)$',
         severity="warn",
         note="Recall silently degrades to the wrong instance rather than erroring."),
    dict(name="fleet.node_count", kind="pg_scalar",
         target="SELECT count(DISTINCT node_name) FROM service_registry "
                "WHERE last_heartbeat > now() - interval '24 hours'",
         claim_re=r'(?i)(\d+)[- ]machine fleet',
         compare="numeric", tolerance=0.0,
         scope=r'^(nova-system-map|identity)$', severity="info",
         note="Cosmetic, but it is the first number in the system map's opening sentence."),
    dict(name="nova_share.host", kind="shell",
         target="findmnt -no SOURCE /nova | sed 's|^//\\([^/]*\\)/.*|\\1|'",
         claim_re=r'(?i)HOST:\s*\w+\s+(192\.168\.1\.\d{1,3})',
         compare="host", tolerance=0,
         scope=r'^(ops-nova-shared-mount|nova-system-map|reference-nas-storage-policy)$',
         severity="warn",
         note="Measured from whichever node runs the reconciler — it reports that node's "
              "actual /nova mount, which is what a runbook reader will try to follow."),
]


def seed(args):
    c = conn(); cur = c.cursor()
    cur.execute(SCHEMA)
    for s in SEEDS:
        cur.execute("""INSERT INTO doc_facts
              (name,kind,target,extract,dsn,claim_re,compare,tolerance,scope,severity,note)
              VALUES (%(name)s,%(kind)s,%(target)s,%(extract)s,%(dsn)s,%(claim_re)s,
                      %(compare)s,%(tolerance)s,%(scope)s,%(severity)s,%(note)s)
              ON CONFLICT (name) DO UPDATE SET kind=EXCLUDED.kind, target=EXCLUDED.target,
                extract=EXCLUDED.extract, claim_re=EXCLUDED.claim_re, compare=EXCLUDED.compare,
                tolerance=EXCLUDED.tolerance, scope=EXCLUDED.scope, severity=EXCLUDED.severity,
                note=EXCLUDED.note, enabled=true""",
                    {**{k: None for k in ("extract", "dsn")}, **s})
        print(f"  registered {s['name']}")
    c.commit(); c.close()
    return 0


def list_facts(args):
    c = conn(); cur = c.cursor(); cur.execute(SCHEMA)
    cur.execute("""SELECT name, kind, compare, severity, scope, last_live, last_checked
                   FROM doc_facts ORDER BY name""")
    for n, k, cm, sev, sc, lv, lc in cur.fetchall():
        when = f"{lc:%m-%d %H:%M}" if lc else "never"
        print(f"  {n:26} {k:10} {cm:8} {sev:8} live={str(lv)[:22]:22} {when}  {sc}")
    c.close(); return 0


def list_drift(args):
    c = conn(); cur = c.cursor(); cur.execute(SCHEMA)
    cur.execute("""SELECT id, severity, fact_name, source_kind, source_id, claimed, live, first_seen
                   FROM doc_drift WHERE status='open'
                   ORDER BY severity='critical' DESC, first_seen""")
    rows = cur.fetchall()
    for i, sev, fn, sk, sid, cl, lv, fs in rows:
        src = "doc" if sk == "agent_docs" else "mem"
        print(f"  #{i:<4} [{sev:8}] {src}:{sid:24} {fn:22} claims {cl!r} / live {lv!r}  since {fs:%m-%d}")
    print(f"  === {len(rows)} open ===")
    c.close(); return 0


def wontfix(args):
    c = conn(); cur = c.cursor()
    cur.execute("UPDATE doc_drift SET status='wontfix', resolved_at=now() WHERE id=%s", (args.wontfix,))
    c.commit(); n = cur.rowcount; c.close()
    print(f"  {'silenced' if n else 'no such row'} #{args.wontfix}")
    return 0


# ---------------------------------------------------------------- self audit (M14)
# Absorbed from nova_self_audit.py on 2026-10-09 (organ audit M14), behaviour unchanged:
# scripts named in scheduler.yaml exist on disk, expected ports listen, expected processes run.
# Same outputs as before: stdout report, ~/.openclaw/logs/self-audit.log, the
# workspace/state/self_audit_state.json change-detector, and the notify bus (dedup_key
# "self-audit") only when the issue set changed. nova_self_audit.py is now a thin wrapper.

SCRIPTS_DIR = Path.home() / ".openclaw/scripts"
SCHEDULER_YAML = Path.home() / ".openclaw/config/scheduler.yaml"
AUDIT_STATE_FILE = Path.home() / ".openclaw/workspace/state/self_audit_state.json"
SELF_AUDIT_LOG = "~/.openclaw/logs/self-audit.log"

EXPECTED_SERVICES = {
    18792: {"name": "Nova Gateway v2", "path": "/health", "host": "192.168.1.2"},
    18790: {"name": "Memory Server", "path": "/health", "host": "192.168.1.6"},
    11434: {"name": "Ollama", "path": "/", "host": "192.168.1.6"},
    37400: {"name": "NovaControl", "path": "/api/status", "host": "127.0.0.1"},
}

EXPECTED_PROCESSES = [
    {"name": "Scheduler", "match": "nova_scheduler.py"},
    {"name": "Gateway v2", "match": "nova_gateway_v2.py"},
    {"name": "Memory Server", "match": "memory_server.py"},
    # NOTE: cloudflared moved OFF .6 to HA connectors on .2 + .10 (2026-06-21).
    # It is watched by nova_prober's cloudflared_tunnel probe (connector health),
    # not by a local-process check here.
]

_AUDIT_LOGGER = None


def _audit_log():
    """The self-audit logger: same format and file as nova_self_audit's basicConfig had."""
    global _AUDIT_LOGGER
    if _AUDIT_LOGGER is None:
        import logging
        import os
        lg = logging.getLogger("nova_self_audit")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handlers = [] if lg.handlers else [logging.StreamHandler()]
        try:
            if not lg.handlers:
                handlers.append(logging.FileHandler(os.path.expanduser(SELF_AUDIT_LOG)))
        except OSError:
            pass
        for h in handlers:
            h.setFormatter(fmt)
            lg.addHandler(h)
        _AUDIT_LOGGER = lg
    return _AUDIT_LOGGER


def _load_last_audit_state():
    try:
        if AUDIT_STATE_FILE.exists():
            with open(AUDIT_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_audit_state(state):
    AUDIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_STATE_FILE, "w") as f:
        json.dump(state, f)


def _scripts_on_disk():
    scripts = set()
    for ext in ("*.py", "*.sh"):
        for p in SCRIPTS_DIR.glob(ext):
            scripts.add(p.name)
    return scripts


def _scripts_in_file(path):
    if not path.exists():
        return set()
    text = path.read_text()
    pattern = re.compile(r'(?:nova_[a-z0-9_]+\.(?:py|sh)|dream_[a-z0-9_]+\.(?:py|sh))')
    return set(pattern.findall(text))


def _scripts_in_scheduler():
    if not SCHEDULER_YAML.exists():
        return {}
    text = SCHEDULER_YAML.read_text()
    refs = {}
    disabled_tasks = set()
    current_task = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("#") and not stripped.startswith("-"):
            candidate = stripped.rstrip(":")
            if candidate not in ("tasks", "scheduler", "slack"):
                current_task = candidate
        if "script:" in stripped and current_task:
            script_name = stripped.split("script:")[1].strip()
            refs[current_task] = script_name
        if "enabled:" in stripped and "false" in stripped.lower() and current_task:
            disabled_tasks.add(current_task)
    for t in disabled_tasks:
        refs.pop(t, None)
    return refs


def _port_listening(port, host="127.0.0.1"):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect((host, port))
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _process_running(match_str):
    try:
        result = subprocess.run(["pgrep", "-f", match_str], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def audit_scripts():
    issues, info = [], []
    on_disk = _scripts_on_disk()
    scheduler_refs = _scripts_in_scheduler()
    scheduler_scripts = set(scheduler_refs.values())
    for task, script in sorted(scheduler_refs.items()):
        if script not in on_disk:
            issues.append(f"Scheduler task `{task}` references `{script}` but it doesn't exist")
    nova_scripts = {s for s in on_disk if s.startswith("nova_") or s.startswith("dream_")}
    undocumented = nova_scripts - scheduler_scripts
    skip_prefixes = ("test_", "debug_", "nova_agent_")
    undocumented = {s for s in undocumented if not any(s.startswith(p) for p in skip_prefixes)}
    if undocumented:
        info.append(f"{len(undocumented)} scripts on disk not in scheduler:")
        for s in sorted(undocumented):
            info.append(f"  - {s}")
    return issues, info, len(on_disk), 0, len(scheduler_refs)


def audit_services():
    issues, ok = [], []
    for port, svc in sorted(EXPECTED_SERVICES.items()):
        name = svc["name"]
        if _port_listening(port, svc.get("host", "127.0.0.1")):
            ok.append(f"{name} (:{port})")
        else:
            issues.append(f"{name} (:{port}) is not listening")
    return issues, ok


def audit_processes():
    issues, ok = [], []
    for proc in EXPECTED_PROCESSES:
        if _process_running(proc["match"]):
            ok.append(proc["name"])
        else:
            issues.append(f"{proc['name']} (`{proc['match']}`) is not running")
    return issues, ok


def audit_docs():
    return []


def self_audit_post(text):
    """Central notification bus. First line is the title; '!!' lines (outage-class) -> critical,
    otherwise warning. A stable dedup_key collapses unchanged repeats centrally."""
    from nova_notify import notify
    lines = text.split("\n")
    title = lines[0].lstrip("*").rstrip("*").strip() or "Nova Self-Audit Report"
    body = "\n".join(lines[1:]).strip() or None
    level = "critical" if "!!" in text else "warning"
    notify(title, body=body, level=level, category="health", dedup_key="self-audit")


def run_audit(post=None):
    """The self audit. Returns the issue count; the CLI exits 0 either way (issues found is a
    successful run, not a task failure)."""
    post = post or self_audit_post
    lg = _audit_log()
    lg.info("Starting self-audit...")
    all_issues, all_info = [], []

    script_issues, script_info, disk_count, _mem_count, sched_count = audit_scripts()
    all_issues.extend(script_issues)
    all_info.extend(script_info)
    svc_issues, svc_ok = audit_services()
    all_issues.extend(svc_issues)
    proc_issues, proc_ok = audit_processes()
    all_issues.extend(proc_issues)
    all_issues.extend(audit_docs())

    lines = ["*Nova Self-Audit Report*", ""]
    lines.append(f"*Scripts:* {disk_count} on disk, {sched_count} in scheduler")
    lines.append(f"*Services:* {len(svc_ok)}/{len(EXPECTED_SERVICES)} up — {', '.join(svc_ok) if svc_ok else 'none'}")
    lines.append(f"*Processes:* {len(proc_ok)}/{len(EXPECTED_PROCESSES)} running — "
                 f"{', '.join(proc_ok) if proc_ok else 'none'}")
    if all_issues:
        lines += ["", f"*Issues ({len(all_issues)}):*"] + [f"  !! {i}" for i in all_issues]
    if all_info:
        lines += ["", "*Info:*"] + [f"  {i}" for i in all_info]
    if not all_issues and not all_info:
        lines += ["", "All clear — no discrepancies found."]
    report = "\n".join(lines)
    print(report)

    # Only post if the issue set changed since last run (prevent spam)
    issue_key = json.dumps(sorted(all_issues))
    last_state = _load_last_audit_state()
    changed = issue_key != last_state.get("last_issue_key", "")
    if all_issues and changed:
        post(report)
        lg.info(f"Self-audit complete: {len(all_issues)} issue(s) posted to Slack (new)")
    elif all_issues:
        lg.info(f"Self-audit complete: {len(all_issues)} issue(s), unchanged — skipping Slack")
    elif last_state.get("last_issue_key", "") != "[]":
        post(report)
        lg.info("Self-audit complete: all clear (issues resolved) — posted to Slack")
    else:
        lg.info("Self-audit complete: no issues found")
    _save_audit_state({"last_issue_key": issue_key, "last_run": str(datetime.now())})
    return len(all_issues)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-audit", action="store_true",
                    help="scheduler scripts exist / ports listen / processes run (was nova_self_audit.py)")
    ap.add_argument("--quiet", action="store_true", help="only print drift")
    ap.add_argument("--no-slack", action="store_true", help="do not post findings")
    ap.add_argument("--seed", action="store_true", help="register the starter facts")
    ap.add_argument("--facts", action="store_true", help="list the fact registry")
    ap.add_argument("--list", action="store_true", help="list open drift")
    ap.add_argument("--wontfix", type=int, metavar="ID", help="silence a drift row")
    a = ap.parse_args()
    if a.self_audit:
        run_audit()
        return 0
    if a.seed:    return seed(a)
    if a.facts:   return list_facts(a)
    if a.list:    return list_drift(a)
    if a.wontfix: return wontfix(a)
    return check(a)


if __name__ == "__main__":
    sys.exit(main())
