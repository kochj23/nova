#!/usr/bin/env python3
"""
nova_evidence_check.py — look at the EVIDENCE before an alert is believed (Jordan, 2026-10-05).

Incident #3675 ("Suspicious DNS — nova-core", recurring 6x/7d, "isolate the host") was a
substring bug: ".ga" inside attester.gateway.icloud.com. Triage, the correlator and
qwen3-coder each trusted the stage before it; none of them read the raw row. This organ
does, mechanically, what a careful human did before the first reply:

  RAW      — fetch the source rows the alert was derived from (syslog_events for the syslog
             detectors; nothing invented when there is no source table).
  RECHECK  — re-apply the detector's own claim to the raw line, deterministically, using the
             detector's own regexes: does ".ga" END the queried name? is "ET DNS" a signature
             or the tail of "fleET DNS"? was the sensitive read DENIED by the sandbox?
  WHO      — every IP in the raw line resolved through net_inventory (the resolver logs the
             LAN's queries; the client is in the message, not the attributed host).
  HISTORY  — 14 days of the same source+category: how often it fires, how many incidents it
             opened, how many of those were resolved as false positives.
  ADVICE   — one concrete next action: a detector fault says "fix the rule, not the host" and
             files a claude_queue item (once per rule per week); a supported claim gets the
             category playbook line, filled with the real client and name.

Verdicts: detector_fault (raw found, re-check fails) | supported (re-check holds) |
unverified (no source rows, or no deterministic re-check for this category).
Read-only over the world except the claude_queue bug row. Fail-open: any error returns
verdict=unverified and the caller proceeds as before. Used by nova_alert_triage (decision +
annotation) and nova_correlator.llm_summarize (evidence block in the summary prompt).

  nova_evidence_check.py --event <telemetry.events id>   # print the bundle for one event
  nova_evidence_check.py --selftest                       # pure-logic assertions, no DB
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
HISTORY_DAYS = 14
RAW_WINDOW_S = 180          # source rows are written in batches; the event is emitted first
RAW_N = 3
BUG_RESURFACE_DAYS = 7      # one claude_queue item per broken rule per week
CHRONIC_N = 50              # fires this often in 14d with zero real incidents = a rule, not a threat
QUEUE_SESSION = "nova-evidence-check"
STATEMENT_TIMEOUT_MS = 8000

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
VERDICTS = ("detector_fault", "supported", "unverified")


def log(m):
    print(f"[evidence-check {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── deterministic re-checks: the detector's own claim, re-applied to the raw line ─────────
# Each returns (holds: bool | None, note). They import the detector module so there is ONE
# definition of each rule; if the module is missing the category is simply unverifiable.

def _syslog():
    import nova_syslog_server as s
    return s


def recheck_suspicious_dns(msg):
    s = _syslog()
    m = s.DNS_QUERY_RE.search(msg)
    if not m:
        return False, "no queried name in the line — this is not a DNS query log"
    q = m.group(1).lower().rstrip(".")
    tld = "." + q.rsplit(".", 1)[-1]
    if tld in s.SUSPICIOUS_TLDS:
        return True, f"queried name {q} ends in watched TLD {tld}"
    return False, f"queried name {q} ends in {tld}, not a watched TLD (substring hit inside the name)"


def recheck_ips(msg):
    s = _syslog()
    if s.UNIFI_IPS_RE.search(msg):
        return True, "UniFi IPS block line"
    m = re.search(r"(?<![A-Za-z])(ET|GPL) [A-Z_]+(?![A-Za-z])", msg)
    if m:
        return True, f"IPS signature family {m.group(0)}"
    return False, "no IPS signature at a word boundary (the match was inside another word)"


def recheck_auth(msg):
    s = _syslog()
    if any(p.search(msg) for p in s.AUTH_PATTERNS) or s.SUDO_RE.search(msg):
        return True, "auth pattern present in the line"
    return False, "no auth-failure/sudo pattern in the line"


def recheck_sensitive(msg):
    s = _syslog()
    if not s.SENSITIVE_PATHS_RE.search(msg):
        return False, "no sensitive path in the line"
    if re.search(r"\bdeny\(", msg):
        return True, "sensitive path touched but the sandbox DENIED it — blocked, nothing read"
    return True, "a process reached a sensitive path"


RECHECKS = {
    "suspicious_dns": recheck_suspicious_dns,
    "ips": recheck_ips,
    "auth_failure": recheck_auth,
    "off_hours_auth": recheck_auth,
    "sensitive_access": recheck_sensitive,
}

# ── playbook: what to DO when the claim holds (filled with the real who/what) ─────────────
PLAYBOOK = {
    "suspicious_dns": "Name the client from the raw line, not the resolver ({who}); find the app that resolved "
                      "{qname}. If it is not ours, block the client at the UDM (nova_unifi_ctl.py block <mac>) "
                      "and keep the DNS log.",
    "ips": "Read the signature and the SRC/DST pair ({who}). DST on the LAN: check that host's listening "
           "service and its log at that minute. SRC external and blocked: no action, note it.",
    "auth_failure": "Check who is at the source ({who}). From a LAN host it is a stale key or a script with "
                    "the wrong user — fix the caller. From outside: confirm the port is not forwarded at the UDM.",
    "off_hours_auth": "Confirm the actor: a system updater (ucs-update, apt, launchd) is routine; a human "
                      "login at this hour is not — ask Jordan.",
    "sensitive_access": "If the sandbox denied it, nothing was read. If a real process read /etc/passwd or "
                        ".ssh, identify the process and why before anything else.",
    "crash_storm": "Take the top process in the breakdown: CloudKit/Spotlight storms self-heal; "
                   "kernel/WindowServer storms need a host restart (nova_fleet_exec.py).",
    "lateral_movement": "A LAN host scanning LAN ports ({who}): find the process on the SRC (ss -tunp) — "
                        "usually one of our monitors (nmap, prober). Confirm before blocking.",
    "fleet": "Service down: read its log first, then nova_fleet_exec.py restart <node> <svc> — the one "
             "restart path. If it fails twice, hand it to Claude with the log tail.",
    "task-sentinel": "Stale scheduler task: read scheduler_runs.error_tail for it, run it by hand with "
                     "--dry-run, then let the scheduler retry.",
    "output_drift": "A communicator producing empty output: run it by hand and read stderr — usually a "
                    "model or a dependency moved.",
    "core-liveness": "nova-core not answering: check the leader lock (leader_standby) and whether .5 took "
                     "over; then the host itself (ping, ssh, systemd).",
}


# ── pure logic (unit-tested in demo()) ───────────────────────────────────────────────────

def ip_from_dedup_key(dedup_key):
    m = IP_RE.search(dedup_key or "")
    return m.group(0) if m else None


def verdict_for(raw, category):
    """(verdict, holds, note) from the raw rows and the category's re-check."""
    fn = RECHECKS.get(category or "")
    if not raw:
        return "unverified", None, "no source rows found inside the window"
    if not fn:
        return "unverified", None, f"no deterministic re-check for '{category}'"
    holds, note = fn(raw[0]["message"])
    if holds is None:
        return "unverified", None, note
    return ("supported" if holds else "detector_fault"), holds, note


def advice_for(verdict, category, source, raw, who, note, queue_id=None):
    if verdict == "detector_fault":
        line = (raw[0]["message"][:160] if raw else "?")
        s = f"Fix the detector, not the host: {source} rule '{category}' fired on \"{line}\" — {note}."
        if queue_id:
            s += f" Filed claude_queue #{queue_id}."
        return s
    tpl = PLAYBOOK.get(category or "")
    if not tpl:
        return ""
    qname = ""
    if raw:
        m = re.search(r"query(?:\[[A-Z0-9]+\])?:?\s+([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", raw[0]["message"], re.I)
        qname = m.group(1) if m else ""
    who_s = ", ".join(f"{ip} = {name}" for ip, name in who.items()) or "unknown client"
    return tpl.format(who=who_s, qname=qname or "the name")


def render(b):
    """Plain-text bundle for prompts and the Slack annotation."""
    lines = [f"verdict: {b['verdict']} — {b['note']}"]
    for r in b.get("raw", [])[:RAW_N]:
        lines.append(f"raw [{r.get('app') or '?'}@{r.get('host') or '?'}]: {r['message'][:220]}")
    if b.get("who"):
        lines.append("who: " + ", ".join(f"{ip} = {n}" for ip, n in b["who"].items()))
    h = b.get("history") or {}
    if h:
        lines.append(f"history {HISTORY_DAYS}d: {h.get('events', 0)} events on {h.get('days', 0)} days, "
                     f"{h.get('incidents', 0)} incidents opened, {h.get('false_positive', 0)} resolved as false positive"
                     + (" — CHRONIC: this rule fires constantly and has never been real" if h.get("chronic") else ""))
    if b.get("advice"):
        lines.append(f"do: {b['advice']}")
    return "\n".join(lines)


def chronic(history):
    return bool(history) and history.get("events", 0) >= CHRONIC_N and history.get("real", 0) == 0


# ── gather (read-only) ───────────────────────────────────────────────────────────────────

def _raw_rows(oc, category, dedup_key, ts):
    ip = ip_from_dedup_key(dedup_key)
    oc.execute(
        "SELECT received_at, hostname, app_name, message FROM syslog_events "
        "WHERE threat_type=%s AND received_at BETWEEN %s - make_interval(secs => %s) AND %s + interval '5 seconds' "
        "AND (%s::text IS NULL OR source_ip=%s OR hostname=%s) ORDER BY received_at DESC LIMIT %s",
        (category, ts, RAW_WINDOW_S, ts, ip, ip, ip, RAW_N))
    return [{"ts": r[0], "host": r[1], "app": r[2], "message": r[3] or ""} for r in oc.fetchall()]


def _history(oc, source, category):
    oc.execute(
        "SELECT count(*), count(DISTINCT ts::date), count(DISTINCT incident_id) FROM telemetry.events "
        "WHERE source=%s AND category=%s AND ts > now() - make_interval(days => %s)",
        (source, category, HISTORY_DAYS))
    n, days, inc = oc.fetchone()
    oc.execute(
        "SELECT count(*) FILTER (WHERE resolution ILIKE 'FALSE POSITIVE%%'), "
        "count(*) FILTER (WHERE status='resolved' AND coalesce(resolution,'') NOT ILIKE 'FALSE POSITIVE%%' AND acked_by IS NOT NULL) "
        "FROM telemetry.incidents WHERE id IN (SELECT incident_id FROM telemetry.events WHERE source=%s AND category=%s "
        "AND incident_id IS NOT NULL AND ts > now() - make_interval(days => %s))",
        (source, category, HISTORY_DAYS))
    fp, real = oc.fetchone()
    h = {"events": n, "days": days, "incidents": inc, "false_positive": fp, "real": real}
    h["chronic"] = chronic(h)
    return h


def _who(oc, raw, dedup_key):
    ips = set()
    for r in raw:
        ips.update(IP_RE.findall(r["message"]))
    k = ip_from_dedup_key(dedup_key)
    if k:
        ips.add(k)
    if not ips:
        return {}
    oc.execute("SELECT ip, coalesce(name, '?') || ' (' || coalesce(tier, '?') || ')' FROM telemetry.net_inventory "
               "WHERE ip = ANY(%s)", (sorted(ips),))
    return dict(oc.fetchall())


def file_bug(oc, source, category, text):
    """One claude_queue item per broken rule per BUG_RESURFACE_DAYS. Returns the queue id or None."""
    desc = f"Detector fault: {source} rule '{category}' contradicted by its own evidence"
    oc.execute("SELECT id FROM claude_queue WHERE description=%s AND created_at > now() - make_interval(days => %s) "
               "ORDER BY id DESC LIMIT 1", (desc, BUG_RESURFACE_DAYS))
    row = oc.fetchone()
    if row:
        return row[0]
    oc.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) VALUES (%s,'pending',2,%s,%s) "
               "RETURNING id", (QUEUE_SESSION, desc, "Evidence bundle (nova_evidence_check.py):\n" + text[:3000]
                                + "\nFix the rule in the detector, add a regression case to its tests, restart the daemon."))
    return oc.fetchone()[0]


def check(oc, *, title, body="", level="info", category=None, source=None, dedup_key=None, ts=None, file_bug_row=True):
    """The public entry point. Never raises; a failure returns verdict=unverified."""
    b = {"verdict": "unverified", "note": "", "raw": [], "who": {}, "history": {}, "advice": "", "queue_id": None}
    try:
        oc.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
    except Exception:  # noqa: BLE001 — autocommit cursors reject SET LOCAL; the session default applies
        pass
    try:
        ts = ts or datetime.now(timezone.utc)
        if source == "nova_syslog_server.py" and category:
            b["raw"] = _raw_rows(oc, category, dedup_key, ts)
        if source and category:
            b["history"] = _history(oc, source, category)
        b["who"] = _who(oc, b["raw"], dedup_key) if (b["raw"] or dedup_key) else {}
        b["verdict"], _, b["note"] = verdict_for(b["raw"], category)
        if b["verdict"] == "unverified" and b["history"].get("chronic"):
            b["note"] += f"; chronic — {b['history']['events']} fires in {HISTORY_DAYS}d, none real"
        b["advice"] = advice_for(b["verdict"], category, source, b["raw"], b["who"], b["note"])
        b["text"] = render(b)
        if b["verdict"] == "detector_fault" and file_bug_row:
            try:
                b["queue_id"] = file_bug(oc, source, category, f"{title}\n{body or ''}\n\n{b['text']}")
            except Exception as e:  # noqa: BLE001 — a filing failure must not clobber the verdict
                log(f"could not file the detector bug ({e})")
            b["advice"] = advice_for(b["verdict"], category, source, b["raw"], b["who"], b["note"], b["queue_id"])
            b["text"] = render(b)
    except Exception as e:  # noqa: BLE001 — fail open
        b["note"] = f"evidence check failed ({e})"
        b["text"] = render(b)
    return b


def main():
    ap = argparse.ArgumentParser(description="Nova's evidence check — read the raw row before believing the alert")
    ap.add_argument("--event", type=int, help="telemetry.events id to bundle (read-only, files nothing)")
    args = ap.parse_args()
    if not args.event:
        ap.print_help(); return 2
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True
    oc = conn.cursor()
    oc.execute("SELECT title, body, level, category, source, dedup_key, ts FROM telemetry.events WHERE id=%s", (args.event,))
    r = oc.fetchone()
    if not r:
        log("no such event"); return 1
    b = check(oc, title=r[0], body=r[1], level=r[2], category=r[3], source=r[4], dedup_key=r[5], ts=r[6], file_bug_row=False)
    print(b["text"])
    return 0


def demo():
    """Runnable check on the pure logic (uses the live detector regexes, no DB)."""
    apple = "client @0x1 192.168.1.43#54948 (attester.gateway.fe2.apple-dns.net): query: attester.gateway.fe2.apple-dns.net IN HTTPS + (192.168.1.138)"
    bad = "client @0x1 192.168.1.9#5555 (beacon-c2-check.xyz): query: beacon-c2-check.xyz IN A + (192.168.1.138)"
    pg = "2026-10-05 15:03:09 PDT [2848746] STATEMENT: SELECT ... WHERE detail::text ILIKE '%.ga%' OR event_type ILIKE '%dns%'"
    v, holds, note = verdict_for([{"message": apple}], "suspicious_dns")
    assert v == "detector_fault" and holds is False and "apple-dns.net" in note, (v, note)
    assert verdict_for([{"message": bad}], "suspicious_dns")[0] == "supported"
    assert verdict_for([{"message": pg}], "suspicious_dns")[0] == "detector_fault"
    assert verdict_for([], "suspicious_dns")[0] == "unverified"                     # no raw = no claim either way
    assert verdict_for([{"message": "x"}], "no-such-category")[0] == "unverified"
    # IPS: "ET DNS" inside "fleET DNS" is not a signature; a real ET line is
    assert recheck_ips("Starting nova-dns-sync.service - Nova fleet DNS sync (UniFi -> BIND)...")[0] is False
    assert recheck_ips("ET TROJAN Win32/Agent CnC checkin SRC=203.0.113.5 DST=192.168.1.9")[0] is True
    # sensitive: a sandbox deny still 'holds' but the note says nothing was read
    h, n = recheck_sensitive("kernel: (Sandbox) Sandbox: oahd(68603) deny(1) file-read-data /private/etc/passwd")
    assert h is True and "DENIED" in n, n
    # advice: fault names the rule and the raw line; supported fills the playbook with who/qname
    a = advice_for("detector_fault", "suspicious_dns", "nova_syslog_server.py", [{"message": apple}], {}, note, 42)
    assert "Fix the detector" in a and "claude_queue #42" in a and "apple-dns" in a, a
    a2 = advice_for("supported", "suspicious_dns", "nova_syslog_server.py", [{"message": bad}],
                    {"192.168.1.9": "Amys-iPhone (transient)"}, "")
    assert "beacon-c2-check.xyz" in a2 and "Amys-iPhone" in a2, a2
    assert advice_for("supported", "unknown-cat", "x", [], {}, "") == ""
    # chronic: constant firing with zero real incidents
    assert chronic({"events": 900, "real": 0}) and not chronic({"events": 900, "real": 1}) and not chronic({"events": 3, "real": 0})
    assert ip_from_dedup_key("syslog-threat-suspicious_dns-192.168.1.2") == "192.168.1.2" and ip_from_dedup_key("x") is None
    # render never raises on a minimal bundle and carries the verdict first
    t = render({"verdict": "unverified", "note": "n", "raw": [], "who": {}, "history": {}, "advice": ""})
    assert t.startswith("verdict: unverified")
    print("all evidence-check assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
