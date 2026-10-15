#!/usr/bin/env python3
"""nova_comet_tail.py — THE COMET TAIL: when unrelated things break together, look for one cause first.

Little Mister, 2026-10-15 (after Nova read Maximum Overdrive, 1986): "Do it all!"

In the film the Earth passes through a comet's tail and every machine on the planet goes
haywire at once: trucks, soda machines, lawnmowers, an electric knife. Everybody at the Dixie
Boy fights each machine as its own enemy until Brett sees the pattern: "if it is that comet
that's making everything go crazy, then all we got to do is stay alive for the next seven
days." One cause, many symptoms, and knowing the cause tells you how long it lasts.

Nova's version. Every run reads the last two hours of failures from
  * scheduler_runs   (failure / timeout; started_at is epoch ms), one component per root script,
  * health_checks    (status 'down'; one component per service@node),
  * telemetry.probe_results (ok = false; one component per probe),
  * telemetry.events (warning/critical in the failure categories only; derived echoes excluded),
and finds ONSETS: a component failing with no failure of its own in the 2h before. When at
least MIN_COMPONENTS distinct roots (different tasks/services/probes, not one task repeating)
have onsets inside one rolling window (--window, default 30 min), that is a comet: before the
failures are treated as separate incidents, each candidate shared cause is checked against
real evidence:

  host_reboot:<h>     syslog boot markers, orphaned scheduler runs, many services on h down at once
                      (against: h's scheduler kept running with no gap)
  package_update:<h>  package_updates rows (apt/brew) on h, import/library errors on h's jobs
  pg_primary          postgres probe failures or a gap in probe rows, connection errors
                      (against: the postgres probe was green through the window)
  dns, ntp, nas, network, cert, power, ollama   error signatures shared by the failing jobs,
                      syslog spikes vs the equal-length baseline before, cert_expiry, UniFi /
                      tunnel events, inference probes and inference_latency errors
  shared_error        three or more roots ending in the same unclassified error line

Each candidate gets evidence for, evidence against, a strength (for minus against, 0..1), a
coverage (share of the failing components that actually depend on it) and score =
strength x coverage. A cause is NAMED only when it scores clearly above the rest
(score >= NAME_MIN and margin >= NAME_MARGIN); otherwise the verdict is the honest null,
"no shared cause found — treat separately". Every detection is a row in comet_tail; a named
cause is also one warning notification (category comet_tail, dedup per window) carrying the
cause, the evidence both ways, how long the effect should last and the incidents it explains.

Never acts: read, record, notify. No deletes, no restarts.
Measured for the monthly continuity review (--report): detections per month, % with a named
cause, and accuracy over rows where a human or organ has filled comet_tail.confirmed.

CLI:   --scheduled            the 15-minute run (last 2h, write + notify)
       --dry-run              evaluate and print, write nothing, notify nothing
       --window MINUTES       onset window (default 30)
       --since 24h|7d|90m     look back further than 2h (backfill / review)
       --report [N]           last N detections + monthly stats
Table: comet_tail (one row per detection; overlapping re-detections update the same row)
Schedule: scheduler-core, every 15m, timeout 120, overlap skip.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_comet_tail"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
# which node each scheduler instance runs on (the .yaml headers say so)
SCHEDULER_FILES = (("scheduler-core.yaml", "nova-core"), ("scheduler.yaml", "mac-studio"))

LOOKBACK_MIN = 120          # scheduled run: last 2h
BASELINE_MIN = 120          # a failure is an onset only if its root was clean this long before
WINDOW_MIN = 30
MIN_COMPONENTS = 3
LEAD_MIN = 60               # causes (updates, reboots) can precede the first failure by this much
NAME_MIN = 0.35             # a cause is named only above this score ...
NAME_MARGIN = 0.15          # ... and this far ahead of the runner-up
RENOTIFY_H = 24             # the same named cause notifies once a day; later detections are recorded only
TIME_BUDGET_S = 45          # stop doing syslog scans past this; the run must finish < 60s
SYSLOG_MAX_ROWS = 600_000   # never scan more syslog rows than this per detection
STATEMENT_TIMEOUT_MS = 15000
LOCK_HOLDER_RE = r"(backup|vacuum|reindex|dump|migrat|repack|cluster_table)"
NETWORKISH = re.compile(r"timed? ?out|timeout|connect|unreachable|refused|reset|HTTPError|URLError|socket", re.I)
# symptoms, not causes: a bare timeout shared by three jobs says nothing about why
GENERIC_ERR = re.compile(r"^(Timed out after|.*TimeoutExpired|.*timed out after|Killed|exit (code|status))", re.I)
NULL_VERDICT = "no shared cause found — treat separately"

# telemetry.events categories that report a component failing. Derived echoes of the inputs
# above (task-sentinel, scheduler, incident_*, slo, storm_rollup) are deliberately absent.
FAILURE_EVENT_CATEGORIES = ("fleet", "probe", "tunnel", "network", "crash_storm", "freshness",
                            "storage", "backup", "dns", "cert", "power", "ups")

# error-line signature -> candidate cause. First match wins, so the specific ones come first.
SIG_CLASSES = (
    ("package_update", re.compile(r"ModuleNotFoundError|ImportError|No module named|Library not loaded|"
                                  r"dyld|GLIBC_|undefined symbol|command not found|bad interpreter", re.I)),
    ("pg_lock", re.compile(r"canceling statement due to (statement|lock) timeout|QueryCanceled|"
                           r"LockNotAvailable|lock timeout|deadlock detected", re.I)),
    ("pg_primary", re.compile(r"OperationalError|accepting TCP/IP connections|could not connect to server|server closed the connection|"
                              r"connection to server .* failed|database system is (starting up|shutting down|"
                              r"in recovery)|too many clients|:5432|pgbouncer", re.I)),
    ("dns", re.compile(r"Name or service not known|Temporary failure in name resolution|"
                       r"nodename nor servname|getaddrinfo failed|NXDOMAIN|Could not resolve host|SERVFAIL", re.I)),
    ("cert", re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|certificate has expired|"
                        r"SSLCertVerificationError|x509", re.I)),
    ("ntp", re.compile(r"clock skew|not yet valid|time is not synchroni[sz]ed", re.I)),
    ("nas", re.compile(r"Stale (NFS )?file handle|Input/output error|Transport endpoint is not connected|"
                       r"No such file or directory: '?/Volumes/|Errno 5\b|Host is down", re.I)),
    ("network", re.compile(r"Network is unreachable|No route to host|Connection reset by peer", re.I)),
    ("ollama", re.compile(r":11434|ollama|model '?[\w:.-]+'? not found", re.I)),
)

# syslog line classes. Chronic noise (smb lease upgrades, named query logs) is filtered by
# comparing each class against the equal-length baseline before the window.
SYSLOG_CLASSES = (
    ("boot", re.compile(r"Linux version \d|Booting Linux|running in system mode|BOOT_TIME|"
                        r"Previous shutdown cause|kernel: Command line", re.I)),
    ("power", re.compile(r"on battery|power (loss|failure|fail)|ups.*(onbatt|lowbatt|commbad|battery)|"
                         r"apcupsd|upsmon", re.I)),
    ("ntp", re.compile(r"time has been changed|clock (step|jump|skew)|System clock wrong|"
                       r"(backward|forward) time jump|Time jumped", re.I)),
    ("nas", re.compile(r"stale file handle|nfs: server .* not responding|cifs.*(VFS|reconnect)|"
                       r"smb\w*.*(disconnect|reconnect|timed out|not responding)|server not responding", re.I)),
    ("network", re.compile(r"link down|link is down|carrier lost|NETDEV WATCHDOG|topology change", re.I)),
    ("dns", re.compile(r"SERVFAIL|timed out resolving|unreachable resolving|Temporary failure in name resolution", re.I)),
    ("cert", re.compile(r"certificate (has )?expired|certificate verify failed", re.I)),
)
SYSLOG_PREFILTER = "|".join(p.pattern for _, p in SYSLOG_CLASSES)

LABELS = {
    "host_reboot": "{h} rebooted or went down", "package_update": "a package/runtime update on {h}",
    "pg_primary": "pg-primary (Postgres) unavailable", "pg_lock": "Postgres lock contention ({h})", "dns": "DNS resolution failing",
    "ntp": "clock skew / NTP", "nas": "NAS/UNAS mount or SMB errors", "network": "network/UniFi fault",
    "cert": "an expired certificate", "power": "a power event", "ollama": "Ollama/inference outage",
    "shared_error": "one shared error: {h}",
}
DURATIONS = {
    "host_reboot": "until {h} finishes rebooting and its services restart (usually 5-15 min); "
                   "anything on {h} still failing after that is separate",
    "package_update": "until the package/runtime environment on {h} is repaired; a missing module or "
                      "library does not come back on its own",
    "package_update_restart": "until the upgrade on {h} finishes and the services it restarted come back "
                              "(usually minutes); anything still down after that is separate",
    "pg_primary": "until pg-primary accepts connections again; jobs that only failed to connect recover on their next run",
    "pg_lock": "until the long-running lock holder ({h}) finishes; the blocked jobs recover on their next run",
    "dns": "until the resolver answers again (usually minutes)",
    "ntp": "until clocks resync; TLS and token failures clear once the skew is gone",
    "nas": "until the NAS/UNAS share is remounted; jobs reading /Volumes stay failed until then",
    "network": "until the switch/AP/link recovers (usually minutes)",
    "cert": "until the certificate for {h} is renewed",
    "power": "until power is stable and every host has booted; expect a second wave as services start",
    "ollama": "until the inference backend reloads its models or the router fails over",
    "shared_error": "until the common error is fixed; a code or config fault will not clear on its own",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS comet_tail (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  window_start timestamptz NOT NULL,
  window_end timestamptz NOT NULL,
  components jsonb NOT NULL DEFAULT '[]',
  candidates jsonb NOT NULL DEFAULT '[]',
  verdict text NOT NULL,
  expected_duration text,
  notified boolean NOT NULL DEFAULT false);
ALTER TABLE comet_tail ADD COLUMN IF NOT EXISTS cause text, ADD COLUMN IF NOT EXISTS confirmed boolean,
  ADD COLUMN IF NOT EXISTS incidents jsonb NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS comet_tail_window ON comet_tail (window_start, window_end);
CREATE INDEX IF NOT EXISTS comet_tail_ts ON comet_tail (ts);
"""


def log(m: str) -> None:
    print(f"[comet-tail {datetime.now():%H:%M:%S}] {m}", flush=True)


# ---------------------------------------------------------------- pure helpers

def parse_since(s: str) -> int:
    """'24h' | '7d' | '90m' | '90' -> minutes. Pure."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhd]?)\s*", s or "")
    if not m:
        raise ValueError(f"bad --since {s!r}; use 90m, 24h or 7d")
    return int(m.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440}[m.group(2)]


def error_line(text: str | None) -> str:
    """The meaningful last line of a traceback/error tail, digits folded, max 120 chars. Pure."""
    if not text:
        return ""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith(("File ", "^", "~", "...", "Traceback")):
            continue
        return re.sub(r"\d+", "#", ln)[:120]
    return ""


def signature_class(text: str | None) -> str | None:
    """Which candidate cause an error text points at, or None. Pure."""
    if not text:
        return None
    for cls, rx in SIG_CLASSES:
        if rx.search(text):
            return cls
    return None


def node_name(raw: str | None) -> str | None:
    """'nova-core7/.125' -> 'nova-core7'. Pure."""
    if not raw:
        return None
    return str(raw).split("/")[0].strip().lower() or None


def script_root(task_id: str, script: str | None) -> str:
    """One root per script, so article_watchdog and article_watchdog_stranded count once. Pure."""
    base = Path(str(script or "").split()[0]).stem if script else ""
    return "task:" + (base or task_id)


def onsets(failures: list[dict], baseline_min: int = BASELINE_MIN) -> list[dict]:
    """Failures whose root had no failure in the baseline before them (the 'newly failed'). Pure.
    failures: dicts with ts (datetime) and root. Returns the onset dicts, oldest first."""
    last: dict[str, datetime] = {}
    out = []
    gap = timedelta(minutes=baseline_min)
    for f in sorted(failures, key=lambda x: x["ts"]):
        prev = last.get(f["root"])
        if prev is None or f["ts"] - prev >= gap:
            out.append(f)
        last[f["root"]] = f["ts"]
    return out


def find_clusters(onset_list: list[dict], window_min: int = WINDOW_MIN,
                  min_components: int = MIN_COMPONENTS, not_before: datetime | None = None) -> list[dict]:
    """Rolling-window clustering of onsets: any window of window_min holding onsets of at least
    min_components distinct roots is a comet; overlapping windows merge. Pure.
    Returns [{start, end, members: [onset dicts, one per root]}]."""
    ev = sorted((o for o in onset_list if not_before is None or o["ts"] >= not_before), key=lambda x: x["ts"])
    w = timedelta(minutes=window_min)
    spans = []
    j = 0
    for i, o in enumerate(ev):
        j = max(j, i)
        while j + 1 < len(ev) and ev[j + 1]["ts"] < o["ts"] + w:
            j += 1
        if len({e["root"] for e in ev[i:j + 1]}) >= min_components:
            spans.append((i, j))
    clusters: list[list[int]] = []
    for i, j in spans:
        if clusters and i <= clusters[-1][1]:
            clusters[-1][1] = max(clusters[-1][1], j)
        else:
            clusters.append([i, j])
    out = []
    for i, j in clusters:
        members, seen = [], set()
        for e in ev[i:j + 1]:
            if e["root"] not in seen:
                seen.add(e["root"])
                members.append(e)
        out.append({"start": ev[i]["ts"], "end": ev[j]["ts"], "members": members})
    return out


def depends(member: dict, cause: str, arg: str | None) -> bool:
    """Does this failing component depend on that cause? Pure."""
    name = member["root"].lower()
    sig = member.get("sig")
    if cause == "host_reboot":                    # a dead host breaks everything on it, whatever the error
        return (member.get("host") or "") == arg
    if cause == "shared_error":
        return member.get("err") == arg
    if sig == cause:
        return True
    if sig and cause != "power":                  # a failure with a different, specific signature is not this
        return False
    if cause == "package_update":
        return (member.get("host") or "") == arg
    if cause in ("pg_primary", "pg_lock"):
        return member["kind"] == "task" or name in ("probe:postgres", "probe:memory_roundtrip")
    if cause == "ollama":
        return bool(re.search(r"llm|ollama|inference|embedding|model|mlx", name)) or member.get("group") == "llm"
    if cause == "nas":
        return bool(re.search(r"nas|unas|synology|smb|plex|backup|nfs", name))
    if cause == "cert":
        return bool(re.search(r"http|web|tunnel|cloudflared|tls|cert", name))
    if cause == "power":
        return member.get("host") in (arg or "").split(",") if arg else True
    if cause in ("dns", "network", "ntp"):        # remote checks, and jobs that died talking to something
        return member["kind"] in ("health", "probe", "event") or bool(NETWORKISH.search(member.get("err") or ""))
    return False


def coverage(members: list[dict], cause: str, arg: str | None = None) -> float:
    """Share of the failing components that depend on the cause. Pure."""
    if not members:
        return 0.0
    return sum(depends(m, cause, arg) for m in members) / len(members)


def _sig_share(members, cls, host=None):
    pool = [m for m in members if host is None or m.get("host") == host]
    if not pool:
        return 0.0, 0
    n = sum(1 for m in pool if m.get("sig") == cls)
    return n / len(pool), n


def build_candidates(members: list[dict], ev: dict) -> list[dict]:
    """Every candidate with evidence for/against, strength, coverage, score; best first. Pure.
    ev is the evidence bundle from gather_evidence (or a test fixture)."""
    cands = []

    def add(cause, arg, fors, againsts, extra_cov=None):
        strength = max(0.0, min(1.0, sum(w for w, _ in fors) - sum(w for w, _ in againsts)))
        cov = extra_cov if extra_cov is not None else coverage(members, cause, arg)
        label = LABELS[cause].format(h=arg or "?")
        cands.append({"cause": cause, "arg": arg, "label": label,
                      "for": [t for _, t in fors], "against": [t for _, t in againsts],
                      "strength": round(strength, 3), "coverage": round(cov, 3),
                      "score": round(strength * cov, 3)})

    hosts = sorted({m["host"] for m in members if m.get("host")} | set(ev.get("boots", {}))
                   | set(ev.get("updates", {})))
    sys_ = ev.get("syslog", {})          # class -> {"win": n, "base": n, "hosts": [..]}

    def spike(cls):
        s = sys_.get(cls) or {}
        win, base = s.get("win", 0), s.get("base", 0)
        return win >= 3 and win > 3 * base + 2, win, base, s.get("hosts", [])

    for h in hosts:
        fors, against = [], []
        if h in ev.get("boots", {}):
            fors.append((0.6, f"boot markers in syslog from {h}: {ev['boots'][h]}"))
        if ev.get("orphaned", {}).get(h):
            fors.append((0.3, f"{ev['orphaned'][h]} scheduler run(s) on {h} orphaned (scheduler died mid-run)"))
        if ev.get("host_burst", {}).get(h, 0) >= 3:
            fors.append((0.3, f"{ev['host_burst'][h]} services on {h} went down in the same minute"))
        gap = ev.get("sched_gap", {}).get(h)
        if gap is not None and gap < 180:
            against.append((0.4, f"{h}'s scheduler kept starting jobs through the window (max gap {int(gap)}s)"))
        if fors:
            add("host_reboot", h, fors, against)
        fors = []
        if ev.get("updates", {}).get(h):
            fors.append((0.5, f"package updates on {h}: {ev['updates'][h]}"))
        share, n = _sig_share(members, "package_update", h)
        if n:
            fors.append((round(0.6 * share + (0.1 if n >= 3 else 0), 3),
                         f"{n} failing job(s) on {h} hit import/library errors"))
        burst = ev.get("host_burst", {}).get(h, 0) >= 3
        if ev.get("updates", {}).get(h) and burst:
            fors.append((0.3, f"{ev['host_burst'][h]} services on {h} dropped together around the update "
                              f"(an upgrade restarting them)"))
        if fors:
            add("package_update", h, fors,
                [] if n or burst else [(0.2, f"no import/library errors or service burst on {h}")])
            if burst and not n:
                cands[-1]["variant"] = "restart"     # an upgrade bounced services; it self-heals

    # pg_primary
    fors, against = [], []
    pg = ev.get("probes", {}).get("postgres", (0, 0))
    if pg[0]:
        fors.append((0.5, f"postgres probe failed {pg[0]}/{pg[1]} times in the window"))
    if ev.get("probe_gap_min", 0) >= 10:
        fors.append((0.3, f"no probe rows written for {ev['probe_gap_min']} min (PG not taking writes?)"))
    share, n = _sig_share(members, "pg_primary")
    if n:
        fors.append((0.5 * share + (0.1 if n >= 3 else 0), f"{n} failing component(s) hit Postgres connection errors"))
    if pg[1] and not pg[0]:
        against.append((0.3, f"postgres probe green {pg[1]}/{pg[1]} through the window"))
    add("pg_primary", None, fors, against)

    # pg_lock: a long lock holder (nightly pg_backup, vacuum, migration) starving everyone else
    fors = []
    holders = ev.get("lock_holders") or []
    share, n = _sig_share(members, "pg_lock")
    if holders:   # a lock holder alone is weak: pg_backup runs every night whether or not anything breaks
        fors.append((0.4 if n else 0.15, f"long-running job(s) overlapping the window: {', '.join(holders[:3])}"))
    if n:
        fors.append((0.6 * share + (0.1 if n >= 3 else 0), f"{n} failing component(s) hit statement/lock timeouts"))
    add("pg_lock", holders[0] if holders else "unknown holder", fors, [])

    # signature + syslog driven globals
    for cls, syslog_w in (("dns", 0.3), ("ntp", 0.3), ("nas", 0.3), ("network", 0.3), ("cert", 0.2)):
        fors, against = [], []
        share, n = _sig_share(members, cls)
        if n:
            fors.append((0.6 * share + (0.1 if n >= 3 else 0), f"{n} failing component(s) show {cls} errors"))
        hit, win, base, shosts = spike(cls)
        if hit:
            fors.append((syslog_w, f"syslog {cls} lines {win} vs {base} in the baseline ({', '.join(shosts[:4])})"))
        if cls == "network":
            nev = ev.get("events", {})
            k = sum(nev.get(c, 0) for c in ("network", "tunnel"))
            if k:
                fors.append((0.3, f"{k} network/tunnel event(s) from the UniFi/tunnel watchers"))
            pr = ev.get("probes", {})
            bad = [p for p in ("cloudflared_tunnel", "http_endpoints") if pr.get(p, (0, 0))[0]]
            if bad:
                fors.append((0.2, f"probe failures: {', '.join(bad)}"))
        if cls == "dns" and pg[1] and not pg[0]:
            against.append((0.1, "the postgres probe resolves pg-primary by name and stayed green"))
        if cls == "nas" and ev.get("nas_down"):
            fors.append((0.3, f"storage services down: {', '.join(ev['nas_down'][:4])}"))
        if cls == "cert" and ev.get("certs_expired"):
            fors.append((0.5, f"expired certificate(s): {', '.join(ev['certs_expired'][:4])}"))
        add(cls, (ev.get("certs_expired") or [None])[0] if cls == "cert" else None, fors, against)

    # power
    fors = []
    hit, win, base, shosts = spike("power")
    if hit:
        fors.append((0.6, f"syslog power/UPS lines {win} vs {base} ({', '.join(shosts[:4])})"))
    booted = sorted(ev.get("boots", {}))
    if len(booted) >= 2:
        fors.append((0.4, f"{len(booted)} hosts booted together: {', '.join(booted)}"))
    if ev.get("power_events"):
        fors.append((0.2, f"{ev['power_events']} device power event(s)"))
    add("power", ",".join(booted) if booted and not hit else None, fors, [])

    # ollama
    fors = []
    pr = ev.get("probes", {})
    bad = {p: pr[p] for p in ("inference_vantage", "embedding") if pr.get(p, (0, 0))[0] >= 2}
    if bad:
        fors.append((0.3, "inference probes failing: " + ", ".join(f"{p} {a}/{b}" for p, (a, b) in bad.items())))
    inf = ev.get("inference", (0, 0))
    if inf[0] >= 3 and inf[0] > 3 * inf[1] + 2:
        fors.append((0.3, f"inference_latency errors {inf[0]} vs {inf[1]} in the baseline"))
    if ev.get("llm_down", 0) >= 2:
        fors.append((0.2, f"{ev['llm_down']} LLM backends reported down"))
    share, n = _sig_share(members, "ollama")
    if n:
        fors.append((0.5 * share, f"{n} failing component(s) hit Ollama/model errors"))
    add("ollama", None, fors, [])

    # shared unclassified error line
    by_err = defaultdict(set)
    for m in members:
        if m.get("err") and not m.get("sig") and not GENERIC_ERR.search(m["err"]):
            by_err[m["err"]].add(m["root"])
    for err, roots in by_err.items():
        if len(roots) >= 3:
            add("shared_error", err, [(0.6, f"{len(roots)} roots end in the same error: {err}")], [])

    cands.sort(key=lambda c: (-c["score"], -c["strength"], c["cause"]))
    return cands


def pick_verdict(cands: list[dict]) -> tuple[dict | None, str]:
    """(named candidate or None, verdict text). Names a cause only when it is clearly ahead. Pure."""
    live = [c for c in cands if c["score"] > 0]
    if not live:
        return None, NULL_VERDICT
    top = live[0]
    second = live[1]["score"] if len(live) > 1 else 0.0
    if top["score"] >= NAME_MIN and top["score"] - second >= NAME_MARGIN:
        return top, f"likely: {top['label']} (score {top['score']:.2f} vs {second:.2f})"
    if top["score"] >= NAME_MIN:
        return None, (f"ambiguous: {top['label']} ({top['score']:.2f}) vs {live[1]['label']} "
                      f"({second:.2f}) — treat separately until one is confirmed")
    return None, NULL_VERDICT


def expected_duration(cand: dict | None) -> str | None:
    """How long the shared effect should last, in words. Pure."""
    if not cand:
        return None
    key = cand["cause"] + ("_" + cand["variant"] if cand.get("variant") else "")
    return DURATIONS.get(key, DURATIONS[cand["cause"]]).format(h=cand.get("arg") or "?")


def cause_key(cand: dict | None) -> str | None:
    """'package_update:mac-studio' / 'pg_primary' — what comet_tail.cause stores. Pure."""
    if not cand:
        return None
    return cand["cause"] + (f":{cand['arg']}" if cand.get("arg") else "")


def render_message(cluster: dict, cand: dict, duration: str, incidents: list[dict]) -> tuple[str, str]:
    """(title, body) for the notification. Pure."""
    members = cluster["members"]
    title = f"Comet Tail: {len(members)} unrelated failures likely share one cause — {cand['label']}"
    lines = [f"Window {cluster['start'].astimezone():%Y-%m-%d %H:%M}–{cluster['end'].astimezone():%H:%M %Z}: "
             f"{len(members)} components newly failed together.",
             f"Likely cause: {cand['label']} (score {cand['score']:.2f}; covers {cand['coverage']:.0%} of them).",
             "Evidence for: " + ("; ".join(cand["for"]) or "none"),
             "Evidence against: " + ("; ".join(cand["against"]) or "none found"),
             f"Expected to last: {duration}.",
             "Failing: " + ", ".join(m["root"] for m in members[:12]) + (" …" if len(members) > 12 else "")]
    if incidents:
        lines.append("Probably explains: " + "; ".join(i["title"] for i in incidents[:6]))
    lines.append("Read-only: nothing was restarted. Treat these as one event, not separate incidents.")
    return title, "\n".join(lines)


def monthly_stats(rows: list[tuple]) -> list[dict]:
    """rows: (month, detections, named, confirmed_true, confirmed_any) -> continuity-review metrics. Pure."""
    out = []
    for month, n, named, ok, judged in rows:
        out.append({"month": month, "detections": int(n), "named": int(named),
                    "named_pct": round(100.0 * named / n, 1) if n else None,
                    "confirmed": int(judged), "accuracy_pct": round(100.0 * ok / judged, 1) if judged else None})
    return out


# ---------------------------------------------------------------- DB reads

def scheduler_hosts(config_dir: Path = CONFIG_DIR) -> dict[str, str]:
    """task_id -> node, from the two scheduler YAMLs. Core wins for a task it runs (enabled)."""
    out: dict[str, str] = {}
    try:
        import yaml
    except ImportError:
        return out
    for fname, host in reversed(SCHEDULER_FILES):          # .6 first, core overrides
        try:
            tasks = (yaml.safe_load((config_dir / fname).read_text()) or {}).get("tasks") or {}
        except Exception:  # noqa: BLE001 — an unreadable YAML just means unknown hosts
            continue
        for tid, spec in tasks.items():
            if isinstance(spec, dict) and spec.get("enabled") is False and tid in out:
                continue
            out[tid] = host
    return out


def fetch_failures(cur, since: datetime, until: datetime, task_hosts: dict) -> list[dict]:
    """Every failure in [since, until) as component dicts. Read-only."""
    out = []
    cur.execute("SELECT task_id, task_script, task_group, started_at, coalesce(right(error_tail, 600), '') "
                "FROM scheduler_runs WHERE started_at >= %s AND started_at < %s AND status IN ('failure','timeout')",
                (int(since.timestamp() * 1000), int(until.timestamp() * 1000)))
    for tid, script, group, ms, tail in cur.fetchall():
        err = error_line(tail)
        out.append({"ts": datetime.fromtimestamp(ms / 1000, timezone.utc), "root": script_root(tid, script),
                    "kind": "task", "host": task_hosts.get(tid), "group": group or "",
                    "err": err, "sig": signature_class(err)})
    cur.execute("SELECT service_name, node_name, checked_at, left(coalesce(error_message, ''), 300) "
                "FROM health_checks WHERE checked_at >= %s AND checked_at < %s AND status = 'down'", (since, until))
    for svc, node, ts, msg in cur.fetchall():
        host = node_name(node)
        err = error_line(msg)
        out.append({"ts": ts, "root": f"health:{svc}@{host}", "kind": "health", "host": host,
                    "group": "", "err": err, "sig": signature_class(err)})
    cur.execute("SELECT probe, ts, left(coalesce(detail, ''), 300) FROM telemetry.probe_results "
                "WHERE ts >= %s AND ts < %s AND NOT ok", (since, until))
    for probe, ts, detail in cur.fetchall():
        err = error_line(detail)
        out.append({"ts": ts, "root": f"probe:{probe}", "kind": "probe", "host": None,
                    "group": "", "err": err, "sig": signature_class(err)})
    cur.execute("SELECT source, category, ts, left(title || ' ' || coalesce(body, ''), 300), meta->>'host' "
                "FROM telemetry.events WHERE ts >= %s AND ts < %s AND level IN ('warning','critical') "
                "AND category = ANY(%s) AND source <> %s",
                (since, until, list(FAILURE_EVENT_CATEGORIES), SERVICE))
    for src, cat, ts, text, host in cur.fetchall():
        out.append({"ts": ts, "root": f"event:{src}:{cat}", "kind": "event", "host": node_name(host),
                    "group": "", "err": "", "sig": signature_class(text)})
    for f in out:
        f["ts"] = f["ts"].astimezone(timezone.utc)
    return out


def _syslog_id_at(cur, ts: datetime, lo: int, hi: int) -> int:
    """Smallest id whose received_at >= ts, by bisection on the primary key (no time index)."""
    while lo < hi:
        mid = (lo + hi) // 2
        cur.execute("SELECT received_at FROM syslog_events WHERE id >= %s ORDER BY id LIMIT 1", (mid,))
        row = cur.fetchone()
        if row is None or row[0] >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo


def syslog_evidence(cur, base_start: datetime, win_start: datetime, win_end: datetime) -> dict:
    """{class: {win, base, hosts}, '_boots': {host: n}} from syslog, window vs baseline. Read-only."""
    cur.execute("SELECT min(id), max(id) FROM syslog_events")
    lo, hi = cur.fetchone() or (None, None)
    if lo is None:
        return {}
    a = _syslog_id_at(cur, base_start, lo, hi)
    b = _syslog_id_at(cur, win_end, a, hi + 1)
    if b - a > SYSLOG_MAX_ROWS:
        a = b - SYSLOG_MAX_ROWS
    cur.execute("SELECT lower(coalesce(hostname, '')), received_at, left(message, 300) FROM syslog_events "
                "WHERE id BETWEEN %s AND %s AND message ~* %s LIMIT 20000", (a, b, SYSLOG_PREFILTER))
    out: dict = {}
    boots: dict[str, int] = defaultdict(int)
    for host, ts, msg in cur.fetchall():
        for cls, rx in SYSLOG_CLASSES:
            if rx.search(msg):
                s = out.setdefault(cls, {"win": 0, "base": 0, "hosts": []})
                if ts >= win_start:
                    s["win"] += 1
                    if host and host not in s["hosts"]:
                        s["hosts"].append(host)
                    if cls == "boot":
                        boots[host] += 1
                else:
                    s["base"] += 1
                break
    out["_boots"] = dict(boots)
    return out


def gather_evidence(cur, cluster: dict, task_hosts: dict, do_syslog: bool = True) -> dict:
    """Every piece of evidence for the candidate causes of one cluster. Read-only."""
    s, e = cluster["start"], cluster["end"] + timedelta(minutes=1)
    lead = s - timedelta(minutes=LEAD_MIN)
    span = e - lead
    ev: dict = {"boots": {}, "updates": {}, "orphaned": {}, "host_burst": {}, "sched_gap": {}, "probes": {}}

    cur.execute("SELECT lower(host_name), string_agg(DISTINCT source, ','), count(*) FROM package_updates "
                "WHERE ts >= %s AND ts <= %s GROUP BY 1", (s - timedelta(minutes=90), e))
    for h, src, n in cur.fetchall():
        ev["updates"][h] = f"{n} {src} run(s)"

    ms = (int(lead.timestamp() * 1000), int(e.timestamp() * 1000))
    cur.execute("SELECT task_id, started_at, status FROM scheduler_runs WHERE started_at >= %s AND started_at < %s "
                "ORDER BY started_at", ms)
    starts: dict[str, list[int]] = defaultdict(list)
    for tid, st, status in cur.fetchall():
        h = task_hosts.get(tid)
        if not h:
            continue
        if status == "orphaned":
            ev["orphaned"][h] = ev["orphaned"].get(h, 0) + 1
        if st >= int(s.timestamp() * 1000):
            starts[h].append(st)
    for h, lst in starts.items():
        edges = [int(s.timestamp() * 1000)] + lst + [int(e.timestamp() * 1000)]
        ev["sched_gap"][h] = max(b - a for a, b in zip(edges, edges[1:])) / 1000

    cur.execute("SELECT DISTINCT task_id FROM scheduler_runs WHERE started_at >= %s AND started_at <= %s "
                "AND task_id ~* %s AND coalesce(ended_at, %s) >= %s AND coalesce(ended_at, %s) - started_at >= 600000",
                (ms[1] - 6 * 3600 * 1000, ms[1], LOCK_HOLDER_RE, ms[1], int(s.timestamp() * 1000), ms[1]))
    ev["lock_holders"] = sorted(r[0] for r in cur.fetchall())

    cur.execute("SELECT split_part(lower(node_name), '/', 1), count(DISTINCT service_name) FROM health_checks "
                "WHERE checked_at >= %s AND checked_at <= %s AND status = 'down' "
                "GROUP BY 1, date_trunc('minute', checked_at)", (s, e))
    for h, n in cur.fetchall():
        ev["host_burst"][h] = max(ev["host_burst"].get(h, 0), n)

    cur.execute("SELECT probe, count(*) FILTER (WHERE NOT ok), count(*) FROM telemetry.probe_results "
                "WHERE ts >= %s AND ts <= %s GROUP BY probe", (s - timedelta(minutes=5), e + timedelta(minutes=5)))
    ev["probes"] = {p: (int(bad), int(total)) for p, bad, total in cur.fetchall()}
    # the prober writes every few minutes; a long silence inside the comet means PG was not taking writes
    cur.execute("SELECT max(ts) FROM telemetry.probe_results WHERE ts >= %s AND ts <= %s", (lead, e))
    last_probe = (cur.fetchone() or (None,))[0]
    ev["probe_gap_min"] = int((e - (last_probe or lead)).total_seconds() // 60)

    cur.execute("SELECT count(*) FILTER (WHERE timestamp >= %s), count(*) FILTER (WHERE timestamp < %s) "
                "FROM inference_latency WHERE timestamp >= %s AND timestamp <= %s AND status <> 'success'",
                (s, s, s - (e - s), e))
    ev["inference"] = tuple(int(x or 0) for x in cur.fetchone() or (0, 0))

    cur.execute("SELECT count(DISTINCT service_name || node_name) FILTER (WHERE service_name ~* '^(llm:|ollama)'), "
                "array_agg(DISTINCT service_name) FILTER (WHERE service_name ~* '(nas|unas|synology|smb|nfs)') "
                "FROM health_checks WHERE checked_at >= %s AND checked_at <= %s AND status = 'down'", (s, e))
    llm, nas = cur.fetchone() or (0, None)
    ev["llm_down"], ev["nas_down"] = int(llm or 0), list(nas or [])

    cur.execute("SELECT DISTINCT coalesce(host, endpoint) FROM telemetry.cert_expiry "
                "WHERE ts >= %s AND ts <= %s AND days_until_expiry <= 0", (s - timedelta(days=1), e))
    ev["certs_expired"] = [r[0] for r in cur.fetchall()]

    cur.execute("SELECT category, count(*) FROM telemetry.events WHERE ts >= %s AND ts <= %s "
                "AND category IN ('network', 'tunnel', 'storage', 'power', 'ups') GROUP BY 1", (lead, e))
    ev["events"] = {c: int(n) for c, n in cur.fetchall()}

    cur.execute("SELECT count(*) FROM telemetry.device_power_events WHERE ts >= %s AND ts <= %s "
                "AND event ~* '(outage|battery|power_?loss|unplug|brownout)'", (lead, e))
    ev["power_events"] = int((cur.fetchone() or (0,))[0])

    if do_syslog:
        sl = syslog_evidence(cur, lead - span, lead, e)
        ev["boots"] = {h: f"{n} line(s)" for h, n in sl.pop("_boots", {}).items() if h}
        ev["syslog"] = sl
    else:
        ev["syslog_skipped"] = True
    return ev


def related_incidents(cur, cluster: dict) -> list[dict]:
    """Incidents that opened during the comet (from 10 min before to 60 min after). Read-only."""
    cur.execute("SELECT id::text, title, started_at FROM incidents WHERE started_at >= %s AND started_at <= %s "
                "ORDER BY started_at LIMIT 20",
                (cluster["start"] - timedelta(minutes=10), cluster["end"] + timedelta(minutes=60)))
    return [{"id": i, "title": t, "started_at": ts.isoformat()} for i, t, ts in cur.fetchall()]


# ---------------------------------------------------------------- run

def connect():
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def evaluate(cur, lookback_min: int, window_min: int, now: datetime | None = None,
             deadline: float | None = None) -> list[dict]:
    """Find the comets in the lookback and judge each. Read-only; returns detection dicts."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=lookback_min)
    task_hosts = scheduler_hosts()
    fails = fetch_failures(cur, since - timedelta(minutes=BASELINE_MIN), now, task_hosts)
    clusters = find_clusters(onsets(fails), window_min, MIN_COMPONENTS, not_before=since)
    out = []
    for c in clusters:
        do_syslog = deadline is None or time.monotonic() < deadline
        ev = gather_evidence(cur, c, task_hosts, do_syslog=do_syslog)
        cands = build_candidates(c["members"], ev)
        named, verdict = pick_verdict(cands)
        if ev.get("syslog_skipped"):
            verdict += " [syslog not checked: time budget]"
        out.append({"cluster": c, "candidates": cands, "named": named, "verdict": verdict,
                    "duration": expected_duration(named), "incidents": related_incidents(cur, c)})
    return out


def _component_json(members):
    return [{"root": m["root"], "kind": m["kind"], "host": m.get("host"), "ts": m["ts"].isoformat(),
             "err": m.get("err") or None, "sig": m.get("sig")} for m in members]


def record(cur, det: dict) -> tuple[int, bool]:
    """Insert, or update the row this comet already has (overlapping window). -> (id, already_notified)."""
    c = det["cluster"]
    args = (json.dumps(_component_json(c["members"])), json.dumps(det["candidates"][:8]), det["verdict"],
            det["duration"], cause_key(det["named"]), json.dumps(det["incidents"]))
    cur.execute("SELECT id, notified, window_start, window_end FROM comet_tail "
                "WHERE window_start <= %s AND window_end >= %s ORDER BY id DESC LIMIT 1", (c["end"], c["start"]))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE comet_tail SET ts = now(), window_start = least(window_start, %s), "
                    "window_end = greatest(window_end, %s), components = %s, candidates = %s, verdict = %s, "
                    "expected_duration = %s, cause = %s, incidents = %s WHERE id = %s",
                    (c["start"], c["end"], *args, row[0]))
        return row[0], bool(row[1])
    cur.execute("INSERT INTO comet_tail (window_start, window_end, components, candidates, verdict, "
                "expected_duration, cause, incidents) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (c["start"], c["end"], *args))
    return cur.fetchone()[0], False


def already_told(cur, row_id: int, det: dict) -> bool:
    """Was this same cause already notified for a comet in the last RENOTIFY_H hours? Read-only."""
    cur.execute("SELECT 1 FROM comet_tail WHERE notified AND cause = %s AND id <> %s "
                "AND window_end > %s LIMIT 1",
                (cause_key(det["named"]), row_id, det["cluster"]["start"] - timedelta(hours=RENOTIFY_H)))
    return cur.fetchone() is not None


def alert(cur, row_id: int, det: dict, window_start: datetime) -> bool:
    from nova_notify import notify
    title, body = render_message(det["cluster"], det["named"], det["duration"], det["incidents"])
    ok = notify(title, body=body, level="warning", category="comet_tail",
                dedup_key=f"comet_tail:{window_start:%Y%m%dT%H%M}",
                meta={"comet_tail_id": row_id, "cause": det["named"]["cause"], "arg": det["named"]["arg"],
                      "score": det["named"]["score"], "components": len(det["cluster"]["members"])})
    if ok:
        cur.execute("UPDATE comet_tail SET notified = true WHERE id = %s", (row_id,))
    return ok


def print_detection(det: dict) -> None:
    c = det["cluster"]
    print(f"\n=== {c['start'].astimezone():%Y-%m-%d %H:%M}–{c['end'].astimezone():%H:%M %Z}  {len(c['members'])} components  -> {det['verdict']}")
    for m in c["members"][:15]:
        print(f"    {m['ts'].astimezone():%H:%M:%S} {m['root']:<42} host={m.get('host') or '-':<12} "
              f"sig={m.get('sig') or '-':<14} {(m.get('err') or '')[:60]}")
    if len(c["members"]) > 15:
        print(f"    … {len(c['members']) - 15} more")
    for cd in [x for x in det["candidates"] if x["score"] > 0 or x["for"]][:4]:
        print(f"  {cd['score']:.2f} (str {cd['strength']:.2f} x cov {cd['coverage']:.2f}) {cd['label']}")
        for f in cd["for"]:
            print(f"        + {f}")
        for a in cd["against"]:
            print(f"        - {a}")
    if det["duration"]:
        print(f"  expected to last: {det['duration']}")
    for i in det["incidents"][:5]:
        print(f"  explains? {i['started_at'][:16]} {i['title']}")


def report(cur, n: int) -> None:
    cur.execute("SELECT to_regclass('comet_tail') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No comet tail detections yet (table not created; the first scheduled run creates it).")
        return
    cur.execute("SELECT id, window_start, window_end, jsonb_array_length(components), verdict, notified, confirmed "
                "FROM comet_tail ORDER BY window_start DESC LIMIT %s", (n,))
    rows = cur.fetchall()
    print(f"Last {len(rows)} comet tail detection(s):")
    for i, s, e, k, v, notified, conf in rows:
        print(f"  #{i} {s:%Y-%m-%d %H:%M}–{e:%H:%M} {k:>3} comps  notified={'y' if notified else 'n'} "
              f"confirmed={'-' if conf is None else conf}  {v}")
    cur.execute("SELECT to_char(date_trunc('month', window_start), 'YYYY-MM'), count(*), "
                "count(*) FILTER (WHERE cause IS NOT NULL), count(*) FILTER (WHERE confirmed), "
                "count(*) FILTER (WHERE confirmed IS NOT NULL) FROM comet_tail GROUP BY 1 ORDER BY 1 DESC LIMIT 12")
    print("\nMonthly (continuity review):")
    for m in monthly_stats(cur.fetchall()):
        print(f"  {m['month']}  detections {m['detections']:>3}  named {m['named_pct'] if m['named_pct'] is not None else '-'}%  "
              f"confirmed {m['confirmed']}  accuracy {m['accuracy_pct'] if m['accuracy_pct'] is not None else '-'}%")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="the 15-minute run: last 2h, write + notify")
    ap.add_argument("--dry-run", action="store_true", help="print only; write and notify nothing")
    ap.add_argument("--window", type=int, default=WINDOW_MIN, metavar="MINUTES", help="onset window (default 30)")
    ap.add_argument("--since", default=None, help="look back further than 2h: 90m, 24h, 7d")
    ap.add_argument("--report", nargs="?", const=20, type=int, metavar="N", help="last N detections + monthly stats")
    a = ap.parse_args(argv)
    if a.window < 1 or a.window > 24 * 60:
        ap.error("--window must be 1..1440 minutes")
    t0 = time.monotonic()
    conn = connect()
    cur = conn.cursor()
    try:
        if a.report is not None:
            report(cur, a.report)
            return 0
        lookback = parse_since(a.since) if a.since else LOOKBACK_MIN
        dets = evaluate(cur, lookback, a.window,
                        deadline=None if a.dry_run else t0 + TIME_BUDGET_S)
        if a.dry_run:
            for d in dets:
                print_detection(d)
            named = sum(1 for d in dets if d["named"])
            print(f"\n[dry-run] {len(dets)} detection(s) over {lookback} min, {named} with a named cause, "
                  f"{time.monotonic() - t0:.1f}s — nothing written")
            return 0
        ensure_schema(cur)
        sent = 0
        for d in dets:
            rid, already = record(cur, d)
            if d["named"] and not already and not already_told(cur, rid, d):
                cur.execute("SELECT window_start FROM comet_tail WHERE id = %s", (rid,))
                sent += alert(cur, rid, d, cur.fetchone()[0])
        log(f"{len(dets)} detection(s), {sum(1 for d in dets if d['named'])} named, {sent} notified "
            f"({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
