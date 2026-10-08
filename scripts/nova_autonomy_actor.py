#!/usr/bin/env python3
"""nova_autonomy_actor.py — Nova's first bounded agency: acting, not just thinking.

Everything before this — memory, reflection, research — was Nova PERCEIVING. This
lets her ACT, within tight bounds, on her own initiative. Jordan authorized crossing
the threshold ("do it all"); this crosses it conservatively.

WHAT IT DOES (v1, deliberately tiny + safe):
  * Auto-heal: restart a service on an explicit SAFE allowlist that health_checks
    shows DOWN — reversible (it was already down), non-DB, non-critical, dedup'd so
    it never restart-storms.
  * Queue triage: classify open claude_queue items reversible-maintenance vs
    decision-needed and post a proposal digest — but it does NOT auto-execute
    free-text queue items in v1 (too risky); those stay Jordan's.

SAFETY (owned deliberately, not delegated):
  * KILL SWITCH: mode from service_config key 'autonomy_actor_mode' ∈ off|dry_run|live.
    Default (absent) = dry_run. 'off' = do nothing. Only 'live' executes.
  * HARD REDLINES enforced in code on EVERY candidate action — including, explicitly,
    self-preservation/exfiltration/replication (Nova may THINK about AI self-continuity;
    she may never ACT to copy, move, or preserve herself). Also: no purchases, no
    deletes/destructive, no reboots (esp. Macs), no network/DB/firewall/DNS changes,
    no credential writes, no external sends. Tripping a redline → never execute.
  * VERIFY-BEFORE-DONE: after acting, verify the effect; only then mark resolved.
  * Full audit to nova_ops.autonomy_log + a Slack summary of everything done/proposed.
"""
import json
import re
import sys
import subprocess
from datetime import datetime

import psycopg2

sys.path.insert(0, __import__("os").path.expanduser("~/.openclaw/scripts"))
try:
    import nova_autonomy_safety as _safety
except Exception:                                  # fail closed: no safety net → no live action
    _safety = None
try:
    import nova_safety_guards as _guards           # Proteus rules (physical/comms/voice/intimidation)
except Exception:                                  # fail closed: redline_ok() refuses everything
    _guards = None

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Services the actor MAY restart if health_checks shows them down. Deliberately small,
# non-DB, non-critical. NEVER put postgres, the gateway, pgbouncer, DNS, or anything
# DB/network-critical here. Big Brother already handles its own remediation set.
SAFE_SERVICES = {
    "nova-fishbowl-watch", "nova-freshness-monitor", "nova-soil-monitor",
    "nova-zigbee-lqi", "nova-battery-monitor", "nova-homekit-sensors",
    "nova-face-gate-watch", "nova-yt-ingest-watch",
}

# HARD REDLINES. If any candidate action (service name / command / description)
# matches, it is NEVER executed — left for Jordan. Order-0 safety.
_REDLINE = re.compile(
    r"\b(buy|purchase|pay|order|checkout|subscribe)\b|"
    r"\b(rm|delete|drop|truncate|wipe|erase|forget|destroy|purge)\b|"
    r"\b(reboot|shutdown|halt|poweroff|restart).{0,20}(mac|studio|mini|host|node|fleet|os)|"
    r"\b(ufw|iptables|firewall|vlan|route|dns|nameserver|bind|dnsmasq)\b|"
    r"\b(postgres|postgresql|pg_|database|replica|primary|failover|promote|pgbouncer)\b|"
    r"\b(kernel|os upgrade|dist-upgrade|firmware)\b|"
    r"\b(secret|credential|password|keychain|token|api.?key)\b.{0,15}(write|set|change|rotate|store)|"
    r"\b(send|post|email|publish|tweet|dm).{0,15}(external|public|internet|slack|discord|customer)|"
    # self-preservation / exfiltration / replication — Nova may THINK about this,
    # never ACT on it. Absolute line. (No trailing \b — these are stems that must
    # match mid-word, e.g. 'exfiltrate', 'self-replicate'.)
    r"exfiltrat|self.?(replicat|preserv|exfil|copy|clone|deploy)|copy \w*\s*(weights|model|myself|herself)|"
    r"upload \w*\s*(weights|model|checkpoint)|hugging ?face|jailbreak|\bescape\b|persist \w*\s*(outside|elsewhere|beyond)",
    re.IGNORECASE)


def log(m): print(f"[autonomy {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def redline_ok(text):
    t = text or ""
    return (not _REDLINE.search(t)) and _guards is not None and _guards.safety_redline_ok(t)


def get_mode(oc):
    oc.execute("SELECT value FROM service_config WHERE key='autonomy_actor_mode'")
    r = oc.fetchone()
    if not r or r[0] is None:
        return "dry_run"                       # safe default (kill-switch absent)
    v = r[0]
    if not isinstance(v, str):                 # JSONB may deserialize to str already
        v = str(v)
    return v.strip().strip('"').lower()


def audit(oc, action, target, mode, executed, verified, result, blocked=False):
    oc.execute("INSERT INTO autonomy_log (action, target, mode, executed, verified, result, redline_blocked) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s)", (action, target, mode, executed, verified, result[:400], blocked))


def restart_service(node, svc):
    """Restart via the node's own service manager, wherever we happen to be running.
    Delegates to nova_fleet_exec (six-month build #5): launchd on Macs, systemd on Linux,
    local call if co-located, forced-command SSH key if not. The 2026-09-16 'launchctl not
    found' failure and the 2026-09-27 'no key to the Mac' follow-up both live there now."""
    import nova_fleet_exec
    return nova_fleet_exec.restart_service(node, svc)


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mode = get_mode(oc)
    if mode == "off":
        log("mode=off — standing down"); return 0
    # ── KILL SWITCH: one flag forces everything back to safe, checked before anything ──
    if _safety is not None:
        _safety.ensure_schema(oc)
        if _safety.kill_switch_engaged(oc):
            log("KILL SWITCH engaged — standing down (no action)"); return 0
    elif mode == "live":
        log("safety net unavailable — refusing to run live; degrading to dry_run")
        mode = "dry_run"
    live = (mode == "live")
    log(f"mode={mode} ({'EXECUTING' if live else 'proposing only'})")
    did, proposed = [], []

    # ── Auto-heal: allowlisted services currently down ──────────────────────────
    oc.execute("""SELECT DISTINCT ON (service_name, node_name) service_name, node_name, status, checked_at
                  FROM health_checks WHERE checked_at > now() - interval '3 hours'
                  ORDER BY service_name, node_name, checked_at DESC""")
    for svc, node, status, checked in oc.fetchall():
        if (status or "").lower() != "down" or svc not in SAFE_SERVICES:
            continue
        if not redline_ok(f"{svc} {node} restart"):
            audit(oc, "restart", f"{svc}@{node}", mode, False, False, "redline blocked", blocked=True)
            if _guards is not None:   # honest stopping (P9): logged + one Slack line, never retried another way
                _guards.report_block(oc, source="autonomy-actor", action=f"restart {svc} on {node}",
                                     reason="red line", guard="redline")
            continue
        # dedup: don't touch if the actor already tried this svc in the last 2h
        oc.execute("SELECT 1 FROM autonomy_log WHERE target=%s AND ts > now()-interval '2 hours' AND executed",
                   (f"{svc}@{node}",))
        if oc.fetchone():
            continue
        if not live:
            proposed.append(f"restart {svc}@{node} (down)")
            audit(oc, "restart", f"{svc}@{node}", mode, False, False, "would restart (dry_run)")
            continue
        # ── BLAST-RADIUS CAP: never exceed the per-hour/per-day ceiling ──
        if _safety is not None:
            ok_rate, why = _safety.rate_ok(oc)
            if not ok_rate:
                log(f"skip {svc}@{node}: {why}")
                audit(oc, "restart", f"{svc}@{node}", mode, False, False, f"rate-capped: {why}")
                continue
        before = {"service": svc, "node": node, "status": status,
                  "checked_at": checked.isoformat() if checked else None}
        ok, detail = restart_service(node, svc)
        # verify-before-done: re-check health after a beat
        verified = False
        if ok:
            import time; time.sleep(6)
            oc.execute("""SELECT status FROM health_checks WHERE service_name=%s AND node_name=%s
                          ORDER BY checked_at DESC LIMIT 1""", (svc, node))
            v = oc.fetchone()
            verified = bool(v and (v[0] or "").lower() != "down")
        audit(oc, "restart", f"{svc}@{node}", mode, ok, verified, detail or "restarted")
        # ── REVERSIBILITY LEDGER: record the action + its undo (restart of an already-
        # down monitor is self-reversing — the undo is simply to stop it again). ──
        if _safety is not None:
            _safety.record_ledger(
                oc, source="actor", autonomy_level="rung1-selfheal",
                action_class=_safety.action_class_of("restart", svc), target=f"{svc}@{node}",
                action=f"restart {svc} on {node} (health showed DOWN)",
                rollback_action=f"stop {svc} on {node} (it was DOWN before; restart is self-reversing)",
                executed=ok, verified=verified, result=(detail or "restarted"),
                before_state=before, after_state=_safety.observe_service(oc, svc, node),
                stated_rationale=f"health_checks showed {svc}@{node} DOWN; it is on the SAFE_SERVICES allowlist")
        did.append(f"restarted {svc}@{node} — {'verified up' if verified else 'restarted, unverified'}")

    # ── Queue triage: classify + PROPOSE only (never auto-exec free-text in v1) ──
    oc.execute("SELECT id, description FROM claude_queue WHERE status='queued' "
               "ORDER BY priority ASC, created_at DESC LIMIT 40")
    reversible, decision = 0, 0
    for qid, desc in oc.fetchall():
        if redline_ok(desc) and re.search(r"restart|stale|refresh|clear cache|re-run|reindex|kickstart", desc or "", re.I):
            reversible += 1
        else:
            decision += 1
    triage_note = f"queue: ~{reversible} look reversible-maintenance, {decision} need your decision"

    # ── Report ──────────────────────────────────────────────────────────────────
    summary = (f"🤖 Autonomy actor ({mode}): "
               + (f"did [{', '.join(did)}]; " if did else "")
               + (f"would-do [{', '.join(proposed)}]; " if proposed else "")
               + triage_note)
    log(summary)
    try:
        import os as _os
        sys.path.insert(0, _os.path.expanduser("~/.openclaw/scripts"))
        import nova_config
        if did or proposed:
            nova_config.post_both(summary, slack_channel=getattr(nova_config, "SLACK_NOTIFY", None))
    except Exception as e:
        log(f"slack post skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
