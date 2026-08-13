#!/usr/bin/env python3
"""nova_overnight_review.py — 6:30am daily operations review.

Reads the past 24h of the five alert channels (nova-alerts, nova-critical, nova-digest,
nova-feed, nova-warning), de-duplicates the storm into DISTINCT incidents, separates REAL
problems from monitor NOISE / FALSE ALARMS, auto-applies a whitelist of safe fixes (idempotent
re-runs only — never destructive, per redline), queues the rest for a session, and publishes a
SANITIZED public operations postmortem plus a #nova-digest summary.

Design notes:
- The channels re-fire the same alerts every 30-60 min all night, so raw counts lie. We cluster
  by a normalized signature (numbers/IPs/timestamps stripped) and report DISTINCT incidents with
  their occurrence counts — that alone kills ~90% of the apparent volume.
- FALSE-ALARM signatures are the broken-checker patterns we've confirmed by hand: the mem_headroom
  metric (uses free instead of available — screams at healthy nodes), CINC "all nodes unreachable"
  (SSH-from-scheduler context, flags the host it runs on), phantom/weekly task staleness, and
  self-referential noise (Nova's own session-starts, fishbowl dossiers, media downloads, anything
  auto-resolved/recovered).
- PUBLIC output is sanitized: internal IPs and hostnames are replaced BEFORE the text ever reaches
  the LLM or the site (redline: no internal topology, no third-party data).

Run: 6:30am daily (scheduler task overnight_review). Manual: `python3 nova_overnight_review.py`
(add --dry-run to skip publishing/fixing).
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw" / "scripts"))
import nova_config as nc

CHANNELS = {
    "nova-critical": "C0B3G7J6N07",
    "nova-alerts":   "C0BMK83BLFJ",
    "nova-warning":  "C0ATAF7NZG9",
    "nova-digest":   "C0BLJLKQMMZ",
    "nova-feed":     "C0BLNUEM9JS",
}
WINDOW_H = 24
DSN = "host=localhost dbname=nova_ops user=kochj"


def log(m):
    print(f"[overnight_review {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── 1. fetch ──────────────────────────────────────────────────────────────────
def fetch_channel(cid: str, oldest: float) -> list:
    tok = nc.slack_bot_token()
    out, cursor = [], ""
    for _ in range(25):
        p = {"channel": cid, "oldest": f"{oldest:.0f}", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        u = "https://slack.com/api/conversations.history?" + urllib.parse.urlencode(p)
        try:
            r = json.loads(urllib.request.urlopen(
                urllib.request.Request(u, headers={"Authorization": f"Bearer {tok}"}),
                timeout=20).read())
        except Exception as e:
            log(f"fetch error on {cid}: {e}")
            break
        if not r.get("ok"):
            break
        out += r.get("messages", [])
        cursor = r.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
        time.sleep(0.3)
    return out


def msg_text(m: dict) -> str:
    t = m.get("text", "") or ""
    for b in m.get("blocks", []) or []:
        for el in (b.get("elements", []) or []):
            for e in (el.get("elements", []) or []):
                if e.get("type") == "text":
                    t += " " + e.get("text", "")
    return t.strip()


# ── 2. cluster ────────────────────────────────────────────────────────────────
def signature(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "«ip»", t)
    t = re.sub(r"\b\d+(\.\d+)?\s*(h|m|s|hours|min|minutes|sec|%|gib|gb|mb)?\b", "#", t)
    t = re.sub(r"[^\w\s«»]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:140]


# ── 3. classify ───────────────────────────────────────────────────────────────
# (regex, label, note) — first match wins. label in {false, noise, real}.
CLASSIFIERS = [
    # ── FALSE ALARMS (broken checkers — confirmed by hand) ──
    (r"mem[_ ]?headroom|memory headroom", "false",
     "mem_headroom metric uses free instead of available — fires on healthy nodes with GiB of reclaimable cache"),
    (r"node down or heartbeat stale|node_unreachable|node unreachable|main host .* unreachable", "false",
     "heartbeat flapping, correlates with the mem_headroom false criticals; node is reachable"),
    (r"cinc daily|packages cataloged|drift detected", "false",
     "CINC reachability runs over SSH-from-scheduler and flags the host it runs on; nodes are up"),
    (r"journal/(research|tech-today|after-dark|essays?) stale|scheduled task .* is stale|task .* stale", "false",
     "task_sentinel flags removed tasks and mis-learned weekly-cron cadence"),
    # ── NOISE (informational / self-referential / already-resolved) ──
    (r"auto-resolved|capacity resolved|recovered:|healed|resolved after|:white_check_mark:", "noise",
     "self-healed / resolved"),
    (r"fishbowl|dossier|hourly watch|flagged message|matched alarm heuristic", "noise",
     "heuristic scanner flagging Nova's own content"),
    (r"claude code session started|scheduler heartbeat|nightly report|nightly media|:arrow_down:", "noise",
     "routine informational"),
    (r"big brother hourly digest|big brother report", "noise",
     "hourly digest wrapper (its contents are classified individually)"),
    # ── REAL (genuine problems worth fixing) ──
    (r"backup stale|backup failed|manifest-sync failed|nas manifest", "real",
     "backup/sync failure"),
    (r"energy flow stalled|z-wave|zigbee|mqtt broker", "real",
     "home-automation energy/telemetry poller down"),
    (r"gpu contended|metal .* deadlock|ollama inference timed out", "real",
     "GPU/Metal contention on the inference host"),
    (r"memory server crashed", "real",
     "vector memory server crash-loop"),
    (r"ingest pipeline idle|0 memories stored", "real",
     "memory ingestion stalled"),
    (r"gateway down|keystone .* down|service .* down|service registry", "real",
     "a service is down"),
    (r"soil|moisture", "real",
     "garden soil moisture (physical action)"),
]


def classify(sig: str) -> tuple[str, str]:
    for rx, label, note in CLASSIFIERS:
        if re.search(rx, sig):
            return label, note
    return "unknown", "unclassified"


# ── 4. sanitize (public safety — redline) ─────────────────────────────────────
_HOSTS = ["nova-core5", "nova-core4", "nova-core3", "nova-core2", "nova-core6", "nova-core",
          "mac-studio", "mac-mini", "tv-movies-mini", "tv-movies-3", "tv-movies", "office-m2",
          "office-m4", "unas-pro-8", "unas-pro", "unas", "synology nas", "synology", "udm-pro",
          "udmpro", "digitalnoise.net", "digitalnoise"]

# Household members — HARD redline: never in public content. Kept as a self-contained list (this
# module is redline-critical and must never fail OPEN on an import error) in a PRIVATE repo. The
# names leak in because presence/WiFi/BLE alerts label devices by owner ("Amy's iPhone" at -77 dBm)
# and those labels flow alert -> Slack -> this recap. 2026-08-13: 'Amy's iPhone' reached the live
# site before this guard existed. Jordan/Little Mister is fine (it's his journal); Amy & Dylan are not.
_HOUSEHOLD = ["amy", "dylan"]


def sanitize(text: str) -> str:
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "an internal host", text)
    for h in sorted(_HOSTS, key=len, reverse=True):
        text = re.sub(re.escape(h), "an internal node", text, flags=re.I)
    # Household first names -> "a resident" (word-boundary; "Amy's iPhone" -> "a resident's iPhone").
    for n in _HOUSEHOLD:
        text = re.sub(rf"\b{re.escape(n)}\b", "a resident", text, flags=re.I)
    # Presence/surveillance method names reveal how the house watches itself — generalize them
    # (redline spirit: no internal topology / surveillance detail in public).
    text = re.sub(r"\b(vehicle_vision|gps_tracker|av_power|ha_motion|ha_lights|ha_presence|"
                  r"wifi_presence|ble_presence|face_recognition|anpr|license_plate)\b",
                  "a presence sensor", text, flags=re.I)
    return text


# ── 4b. STATE AWARENESS — what git already fixed & what daemon runs stale code ─
# The old review read only the alert STREAM: it had no idea (a) what was already
# FIXED in git, nor (b) whether the daemon computing a metric was even running the
# current code. So it re-recommended shipped fixes and completely missed a daemon
# running 12-day-old code while its fix sat un-loaded (2026-08-12: nova_capacity
# cried CPU/mem false-crits for hours AFTER both fixes had landed on disk — the
# long-lived process never reloaded them). These two checks give it that context.
SCRIPTS_DIR = Path.home() / ".openclaw" / "scripts"


def _sh(cmd: str, host=None, timeout=15) -> str:
    full = (["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", f"kochj@{host}", cmd]
            if host else ["bash", "-c", cmd])
    try:
        return subprocess.run(full, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _proc_start_epoch(pid: int, host=None):
    """Epoch when a pid started — portable across the mac (.6, `ps lstart`) and Linux (.2, /proc)."""
    if host:
        out = _sh(f"stat -c %Y /proc/{pid} 2>/dev/null", host)
        return int(out) if out.isdigit() else None
    out = _sh(f"ps -o lstart= -p {pid}")
    if not out:
        return None
    try:  # macOS lstart, e.g. "Wed Jul 30 11:37:04 2026" (day may be space-padded)
        return time.mktime(time.strptime(re.sub(r"\s+", " ", out).strip(), "%a %b %d %H:%M:%S %Y"))
    except Exception:
        return None


def _code_mtime(script: str, host=None):
    if host:
        out = _sh(f"stat -c %Y /home/kochj/.openclaw/scripts/{script} 2>/dev/null", host)
        return float(out) if out.replace(".", "", 1).isdigit() else None
    p = SCRIPTS_DIR / script
    return p.stat().st_mtime if p.exists() else None


# The monitor/metric daemons whose staleness directly corrupts the alert channels. Scoped to
# these (not all ~70 launchd jobs) so ordinary dev-churn on unrelated scripts doesn't cry stale.
# auto=True: a pure poller safe to bounce unattended (idempotent). auto=False: may be mid-task
# (the scheduler) — flag + queue for a human, never auto-restart. Extend as monitors are added.
# Local daemons carry their exact launchd `label` so we read the RIGHT process (three of these
# share one script under different labels); remote daemons carry their systemd `unit`.
def _L(name, script, label, auto=True):
    return {"name": name, "script": script, "host": None, "label": label, "auto": auto}


MONITOR_DAEMONS = [
    _L("nova-capacity",        "nova_capacity.py",       "net.digitalnoise.nova-capacity"),
    _L("nova-snmp-poller",     "nova_snmp_poller.py",    "net.digitalnoise.nova-snmp-poller"),
    # nova_big_brother.py runs under three separate launchd labels (each its own process); check &
    # reload each so a fix isn't left stranded in the two that share the script.
    _L("big-brother",          "nova_big_brother.py",    "net.digitalnoise.big-brother"),
    _L("nova-service-monitor", "nova_big_brother.py",    "net.digitalnoise.nova-service-monitor"),
    _L("nova-system-monitor",  "nova_big_brother.py",    "net.digitalnoise.nova-system-monitor"),
    _L("nova-backup-monitor",  "nova_backup_monitor.py", "net.digitalnoise.nova-backup-monitor"),
    _L("nova-core-liveness",   "nova_core_liveness.py",  "net.digitalnoise.core-liveness"),
    {"name": "nova-scheduler-core", "script": "nova_scheduler.py", "host": "192.168.1.2",
     "unit": "nova-scheduler-core.service", "auto": False},  # may be mid-task — queue, don't bounce
]
_STALE_GRACE_S = 300  # code must be newer than the process by > this to count (not a fresh redeploy)


def _restart_cmd(d):
    if d["host"]:
        return f"sudo systemctl restart {d['unit']}"
    return f"launchctl kickstart -k gui/{os.getuid()}/{d['label']}"


def _pid_of(d):
    if d["host"]:
        out = _sh(f"systemctl show {d['unit']} -p MainPID --value", d["host"])
        return int(out) if out.isdigit() and int(out) > 0 else None
    # exact per-label PID (not pgrep-by-script — three labels share one script)
    out = _sh(f"launchctl list {d['label']}")
    m = re.search(r'"PID"\s*=\s*(\d+)', out)
    return int(m.group(1)) if m else None


def stale_daemons() -> list:
    """Monitor daemons whose on-disk code is newer than the running process — a fix that shipped
    but was never loaded. THE check that would have caught 2026-08-12 on its own."""
    found = []
    for d in MONITOR_DAEMONS:
        pid = _pid_of(d)
        if not pid:
            continue  # not running is a 'service down' concern other checkers own
        start = _proc_start_epoch(pid, d["host"])
        mt = _code_mtime(d["script"], d["host"])
        if not start or not mt or mt <= start + _STALE_GRACE_S:
            continue
        found.append({**d, "pid": pid, "stale_h": (mt - start) / 3600.0,
                      "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(start))})
    found.sort(key=lambda x: -x["stale_h"])
    return found


def restart_stale(stale: list, dry: bool) -> list:
    """Auto-reload the safe (auto=True) stale monitors; the rest are queued for a human."""
    actions = []
    for s in stale:
        if not s["auto"]:
            continue
        if dry:
            actions.append(f"[dry-run] would reload {s['name']} (running {s['stale_h']:.0f}h-old code)")
            continue
        _sh(_restart_cmd(s), s["host"], timeout=30)
        actions.append(f"reloaded stale daemon {s['name']} — was running code {s['stale_h']:.0f}h older "
                       f"than the process (fix had shipped but never loaded)")
    return actions


def queue_stale(stale: list, dry: bool):
    """Queue the daemons we won't auto-restart (e.g. the scheduler — may be mid-task)."""
    todo = [s for s in stale if not s["auto"]]
    if dry or not todo:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(DSN); conn.autocommit = True; cur = conn.cursor()
        cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES ('overnight-review','active') "
                    "ON CONFLICT (session_id) DO NOTHING")
        for s in todo:
            desc = f"STALE DAEMON: restart {s['name']} — running {s['stale_h']:.0f}h-old code"
            cur.execute(
                """INSERT INTO claude_queue (session_id, status, priority, description, context)
                   SELECT 'overnight-review','queued',2,%s,%s
                   WHERE NOT EXISTS (SELECT 1 FROM claude_queue WHERE description=%s AND status IN ('queued','in_progress'))""",
                (desc, f"Process up since {s['since']}; on-disk code is newer. Restart: {_restart_cmd(s)}", desc))
        cur.close(); conn.close()
    except Exception as e:
        log(f"stale queue error: {e}")


# ── recent-fix cross-reference — tell 'still broken' from 'fixed, alerts draining' ──
_FIX_STOP = {"which", "there", "their", "would", "could", "after", "before", "fixed", "fixes",
             "fixing", "should", "these", "those", "about", "where", "while", "nova", "into",
             "from", "with", "that", "this", "when", "then", "than", "alert", "alerts", "false"}


def _salient(text: str) -> set:
    """Distinctive tokens (subsystem names, snake_case identifiers) for fuzzy fix<->alert matching."""
    toks = re.findall(r"[a-z][a-z_]{4,}", text.lower())
    return {t for t in toks if ("_" in t or len(t) >= 7) and t not in _FIX_STOP}


def recent_fixes(days: int = 5) -> list:
    out = _sh(f'git -C {SCRIPTS_DIR.parent} log --since="{days} days ago" --no-merges '
              f'--pretty=%h%x1f%cs%x1f%s%x1f%b%x1e')
    fixes = []
    for rec in out.split("\x1e"):
        parts = rec.strip().split("\x1f")
        if len(parts) < 3 or not parts[0]:
            continue
        subj = parts[2].strip()
        body = parts[3] if len(parts) > 3 else ""
        fixes.append({"hash": parts[0], "date": parts[1], "subject": subj,
                      "tokens": _salient(subj + " " + body)})
    return fixes


def match_recent_fix(rec: dict, fixes: list):
    """(hash, date, subject) of a recent commit that plausibly fixed this incident, else None.
    To avoid suppressing a real pending item on a coincidence, require either a shared snake_case
    identifier (e.g. mem_headroom, cpu_cores — highly specific) or at least two shared tokens."""
    inc = _salient(rec["sig"] + " " + rec.get("note", ""))
    if not inc:
        return None
    for f in fixes:
        shared = inc & f["tokens"]
        if any("_" in t for t in shared) or len(shared) >= 2:
            return (f["hash"], f["date"], f["subject"])
    return None


# ── 5. safe auto-fix (idempotent re-runs ONLY — never destructive) ────────────
SAFE_FIXES = {
    # signature substring -> (human label, shell cmd on the given host or local)
    "manifest-sync failed": ("re-run NAS manifest sync",
                             ["ssh", "kochj@192.168.1.2",
                              "cd ~/.openclaw/scripts && nohup python3 nova_nas_manifest_sync.py >/dev/null 2>&1 & echo requeued"]),
}


def apply_safe_fixes(real_incidents: list, dry: bool) -> list:
    applied = []
    for inc in real_incidents:
        for key, (label, cmd) in SAFE_FIXES.items():
            if key in inc["sig"]:
                if dry:
                    applied.append(f"[dry-run] would {label}")
                    continue
                try:
                    subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    applied.append(f"auto-fix: {label}")
                except Exception as e:
                    applied.append(f"auto-fix FAILED ({label}): {e}")
    return applied


def queue_for_session(real_incidents: list, dry: bool):
    """Queue non-auto-fixable real incidents to claude_queue for a session (respects the redline:
    destructive/unknown fixes are reviewed by a human/session, never auto-applied)."""
    if dry:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES ('overnight-review','active') "
                    "ON CONFLICT (session_id) DO NOTHING")
        for inc in real_incidents:
            if any(k in inc["sig"] for k in SAFE_FIXES):
                continue
            if inc.get("recent_fix"):
                continue  # already fixed in git — don't re-queue solved work as if it's pending
            desc = f"OVERNIGHT: {inc['example'][:80]}"
            cur.execute(
                """INSERT INTO claude_queue (session_id, status, priority, description, context)
                   SELECT 'overnight-review','queued',3,%s,%s
                   WHERE NOT EXISTS (SELECT 1 FROM claude_queue WHERE description=%s AND status IN ('queued','in_progress'))""",
                (desc, f"{inc['count']}x in 24h. {inc['note']}", desc))
        cur.close(); conn.close()
    except Exception as e:
        log(f"queue error: {e}")


# ── 6. article ────────────────────────────────────────────────────────────────
_META_OPENER = re.compile(
    r"^\s*(i'?ll |i will |let me |here'?s |here is |sure[,.]|okay[,.]|understood|i'?ve (structured|written)|"
    r"below is|i'?m going to (write|structure))", re.I)


def _strip_meta_preamble(body: str) -> str:
    """Drop any leading paragraphs where the model addresses the operator or narrates that it's
    about to write, instead of just writing (the 'I'll write this postmortem for you' tell)."""
    paras = body.strip().split("\n\n")
    while paras and (_META_OPENER.match(paras[0]) or paras[0].strip() in ("---", "")):
        paras.pop(0)
    return "\n\n".join(paras).strip()


def build_digest(clusters: dict) -> dict:
    buckets = {"real": [], "false": [], "noise": []}
    for sig, items in clusters.items():
        label, note = classify(sig)
        if label == "unknown":
            # an unmatched one-off is almost always noise; an unmatched RECURRING thing (fired
            # 3+ times overnight) is worth a human's eyes -> surface it as real.
            label = "real" if len(items) >= 3 else "noise"
        ex = max(items, key=len)  # most-complete example
        rec = {"sig": sig, "count": len(items), "example": ex, "note": note}
        buckets[label].append(rec)
    for b in buckets.values():
        b.sort(key=lambda r: -r["count"])
    return buckets


def write_article(buckets: dict, fixes: list, raw_total: int, dry: bool, stale: list | None = None):
    import nova_journal as j
    real, false, noise = buckets["real"], buckets["false"], buckets["noise"]
    stale = stale or []

    def fmt(recs, n=25):
        lines = []
        for r in recs[:n]:
            rf = r.get("recent_fix")
            tag = (f"  ⟶ ALREADY FIXED {rf[1]} ({rf[0]}: {rf[2][:60]}) — stale alerts draining, do NOT "
                   f"re-recommend fixing it") if rf else ""
            lines.append(f"- ({r['count']}x) {sanitize(r['example'])[:180]} — {r['note']}{tag}")
        return "\n".join(lines) or "- (none)"

    stale_block = "\n".join(
        f"- {s['name']} ({'this host' if not s['host'] else 'an internal node'}): running code "
        f"{s['stale_h']:.0f}h OLDER than the process (up since {s['since']}) — "
        + ("AUTO-RELOADED this run" if s['auto'] else "still needs a human restart (may be mid-task)")
        for s in stale) or "- (none)"

    distinct = len(real) + len(false) + len(noise)
    brief = sanitize(f"""OVERNIGHT OPERATIONS DATA — past {WINDOW_H}h.
{raw_total} raw alerts collapsed to {distinct} DISTINCT incidents ({len(real)} real, {len(false)} false-alarm, {len(noise)} noise).

REAL problems (worth fixing):
{fmt(real)}

FALSE ALARMS (broken monitors, not real outages):
{fmt(false)}

NOISE (self-healed / informational / self-referential):
{fmt(noise, 10)}

STALE DAEMONS (the fix shipped to disk but the long-lived process never reloaded it):
{stale_block}

AUTO-FIXES APPLIED THIS RUN:
{chr(10).join('- '+f for f in fixes) or '- (none)'}""")

    # Nova's actual ops persona (dad jokes, ruthless-about-broken-services, existential musing).
    from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS
    system = system_prompt(CONTEXT_JOURNAL_OPS) + (
        "\n\nADDITIONAL RULES FOR THIS PIECE:\n"
        "- This is your MORNING OPERATIONS REVIEW: the past 24h of your own alert channels.\n"
        "- The THESIS: an alert storm is mostly the monitoring crying wolf; the skill is telling the "
        "real fire from the smoke-detector-that-hallucinates-smoke. Be ruthless about the false alarms "
        "(name the broken behavior — a memory metric that reads 'free' instead of 'available', a "
        "reachability check that flags the host it runs on).\n"
        "- Cover: what ACTUALLY broke and what got fixed, then the false alarms as their own roast, "
        "then a nod to the noise. End on the alert-fatigue existential musing.\n"
        "- Some false alarms are tagged ALREADY FIXED with a commit hash and date. Do NOT tell the "
        "reader to fix those again — frame them honestly as 'the fix shipped on <date>; what you're "
        "seeing is stale alerts draining out of the 24h window,' and move on. Re-recommending solved "
        "work is the exact failure this review is meant to avoid.\n"
        "- If any STALE DAEMONS are listed, make THAT the sharp lesson of the morning: a metric fix "
        "that lands on disk changes nothing until the long-lived daemon that computes it reloads — a "
        "monitor can cry wolf for days after its bug is 'fixed' because the running process still holds "
        "the old code. Name the ones we auto-reloaded and any that still need a human. This is the "
        "difference between 'the code is fixed' and 'the running system is fixed.'\n"
        "- 3000-3600 words. Do NOT open by addressing me about the task or narrating that you're about "
        "to write — start IN the piece. No title line. Internal IPs/hostnames are already redacted to "
        "'an internal node'; keep them that way.")
    user = (f"Here is the deduplicated overnight data — write the review.\n\n{brief}")

    body = j.call_openrouter(system, user, model="anthropic/claude-sonnet", max_tokens=9000)
    if not body:
        log("article generation returned nothing — aborting publish")
        return None
    body = _strip_meta_preamble(sanitize(body))
    if len(body.split()) < 200:
        log("article too short after cleanup — aborting")
        return None
    # Clear the ops long-form floor ourselves (a controlled, voice-preserving expansion) rather
    # than letting publish_hugo's expander run — that one has a history of leaking "I've expanded
    # the article…" meta into the body.
    if len(body.split()) < 2900:
        log(f"expanding {len(body.split())}w -> 3200+ (own pass, avoids publish_hugo expander)")
        more = j.call_openrouter(
            system + "\n\nEXPAND the draft below to at least 3200 words. Keep every bit of the voice; "
            "add a per-monitor roast of each false alarm and a deeper reflection on alert fatigue. "
            "Return ONLY the complete expanded article — no preamble, no 'here is the expanded' line.",
            body, max_tokens=9000)
        more = _strip_meta_preamble(sanitize(more or ""))
        if len(more.split()) > len(body.split()):
            body = more
    title = j.call_openrouter(
        "Generate one wry, punchy title (<=14 words) for a morning ops postmortem about alert "
        "fatigue — most alerts were noise, few were real. Output ONLY the title, no quotes.",
        body[:1200], max_tokens=40).strip().strip('"').replace("#", "").strip()

    # Cover image — Nova's ops aesthetic (creative only, no internal detail).
    image_path = None
    try:
        from nova_image_utils import generate_image
        image_path = generate_image(
            "A weary AI watchkeeper at dawn in a dim control room, surrounded by hundreds of blaring "
            "red alert lights, calmly pointing at the single real fire among them. Moody cyberpunk, "
            "amber dawn light through blinds. Digital art.", section="operations")
    except Exception as e:
        log(f"image generation failed (publishing without cover): {e}")

    if dry:
        log(f"[dry-run] would publish: '{title}' ({len(body.split())} words, image={'yes' if image_path else 'no'})")
        Path("/tmp/overnight_review_preview.md").write_text(f"# {title}\n\n{body}")
        return "[dry-run] /tmp/overnight_review_preview.md"

    ok = j.publish_hugo(title=title, body=body, section="operations",
                        tags=["operations", "postmortem", "reliability", "alert-fatigue", "nova"],
                        description="Nova's morning operations review — separating real failures from monitor noise.",
                        image_path=image_path)
    if not ok:
        log("publish_hugo returned False (guard blocked or error)")
        return None
    j.git_push("operations", title)
    return title


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    dry = "--dry-run" in sys.argv
    oldest = time.time() - WINDOW_H * 3600
    log(f"reviewing {WINDOW_H}h across {len(CHANNELS)} channels" + (" [DRY-RUN]" if dry else ""))

    clusters = defaultdict(list)
    raw_total = 0
    for name, cid in CHANNELS.items():
        msgs = fetch_channel(cid, oldest)
        for m in msgs:
            txt = msg_text(m)
            if not txt or len(txt) < 8:
                continue
            raw_total += 1
            clusters[signature(txt)].append(txt)
    log(f"{raw_total} raw messages -> {len(clusters)} distinct incidents")

    buckets = build_digest(clusters)
    log(f"real={len(buckets['real'])} false={len(buckets['false'])} noise={len(buckets['noise'])}")

    # STATE AWARENESS: cross-reference git so we don't re-recommend shipped fixes, and check whether
    # any monitor daemon is running code older than its own file (a fix that landed but never loaded).
    fixes_log = recent_fixes()
    for name in ("false", "real"):
        for rec in buckets[name]:
            rec["recent_fix"] = match_recent_fix(rec, fixes_log)
    already = sum(1 for n in ("false", "real") for r in buckets[n] if r.get("recent_fix"))
    if already:
        log(f"{already} incident(s) already fixed in git — flagged as draining, not re-queued")

    stale = stale_daemons()
    if stale:
        log("STALE DAEMONS: " + ", ".join(f"{s['name']}({s['stale_h']:.0f}h,{'auto' if s['auto'] else 'queue'})" for s in stale))

    fixes = apply_safe_fixes(buckets["real"], dry)
    fixes += restart_stale(stale, dry)
    queue_for_session(buckets["real"], dry)
    queue_stale(stale, dry)

    title = write_article(buckets, fixes, raw_total, dry, stale)

    # #nova-digest summary
    if title and not dry:
        try:
            nc.post_both(
                f":sunrise: *Overnight Operations Review* — {raw_total} alerts → "
                f"{len(buckets['real'])} real / {len(buckets['false'])} false-alarm / {len(buckets['noise'])} noise. "
                f"{len(fixes)} auto-fixed.\n  _{title}_",
                slack_channel=nc.SLACK_DIGEST, discord_channel=None)
        except Exception as e:
            log(f"digest post failed: {e}")

    log(f"done. {'preview: '+str(title) if dry else 'published: '+str(title)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
