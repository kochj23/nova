#!/usr/bin/env python3
"""
nova_notifier.py — the central notification daemon (Phase 2 event-bus consumer).

Drains telemetry.events (written by nova_notify.notify) and does what no single
emitter can: ROUTE by severity, DEDUP/rate-limit repeats, and (later) CORRELATE
related events into one incident. One audit trail, one routing policy.

  nova_notifier.py --drain     # process all 'new' events once (cron/scheduler)
  nova_notifier.py --daemon    # loop forever (launchd KeepAlive) — low latency

Routing policy lives in ROUTE() below — changing where a category goes is one line.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_correlator
import nova_remediation
try:  # room voice must never be able to break notification delivery
    import nova_voice_room
except Exception:
    nova_voice_room = None

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")

# ── AI alert-triage brain (fail-open) ─────────────────────────────────────────
# Right before an event is posted to Slack we ask nova_alert_triage what it turned
# out to be historically (page / downgrade / suppress + likely-cause annotation).
# HARD RULE: triage must never swallow an alert. Any failure — exception, timeout,
# or junk return — falls open to posting the alert exactly as it would have before
# triage existed. The brain enforces its own safety (hard-critical always pages).
_TRIAGE_TIMEOUT_S = 20


def _triage_event(ev: dict):
    """Run the triage brain with a hard timeout in a daemon thread.
    Returns the decision dict, or None on ANY failure (caller fails open to paging)."""
    import threading
    import nova_alert_triage
    box = {}

    def _run():
        try:
            box["v"] = nova_alert_triage.triage(
                ev.get("title") or "", ev.get("body") or "",
                ev.get("level") or "info", ev.get("category"),
                ev.get("source"), ev.get("dedup_key"), ev=dict(ev))
        except Exception as e:  # noqa: BLE001 — fail open
            box["err"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(_TRIAGE_TIMEOUT_S)
    if th.is_alive():
        return None  # timed out — leave the hung thread; fail open
    return box.get("v")

# Out-of-band relay: critical alerts also go over the Meshtastic mesh (LoRa),
# so Little Mister can be reached even if home internet/WiFi is fully down.
# Best-effort -- bridge/radio being unreachable must never block Slack delivery.
# Bridge runs on Jordans-Mac-mini (Heltec T114 on USB). mDNS name, not IP:
# the mini's DHCP lease moved .92 -> .251 on 2026-07-27 and the hardcoded IP
# silently killed the relay for ~2 days. .local resolves peer-to-peer on the
# LAN with no DNS server needed, so it also works when internet is down.
MESH_BRIDGE_URL = "http://jordans-mac-mini.local:37478/send"


def _mesh_relay(title: str, body: str | None) -> None:
    import urllib.request
    text = title if not body else f"{title}: {body}"
    payload = json.dumps({"text": text[:200]}).encode()
    req = urllib.request.Request(MESH_BRIDGE_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  mesh relay failed (non-fatal): {e}", file=sys.stderr)

# ── Routing policy ──────────────────────────────────────────────────────────
# Three-tier scheme (2026-07-29): route by intent, not by source.
#   #nova-alerts — actionable, state-change only (warning/critical)
#   #nova-digest — rollups (info-level events in DIGEST_CATEGORIES)
#   #nova-feed   — ambient firehose, muted (everything else at info)
CHANNEL = {
    "info":     nova_config.SLACK_FEED,     # #nova-feed
    "warning":  nova_config.SLACK_ALERTS,   # #nova-alerts
    "critical": nova_config.SLACK_ALERTS,   # #nova-alerts (+ mesh relay below)
}
# Hard overrides: these categories go here regardless of level.
CATEGORY_OVERRIDE = {
    "security_news": nova_config.SLACK_FEED,   # CVE/threat NEWS is FYI, not your-network
    "claude_code":   nova_config.SLACK_FEED,   # Claude Code activity is FYI
    "claude_fleet":  "C0B3RSRR0DD",            # #nova-claude — cross-node Claude coordination
                                               # (orphaned claims requeued by nova_claude_lease_reaper)
    "email":         nova_config.SLACK_EMAIL,  # mail digests -> #nova-email (its purpose). Without
                                               # this they fell to info->#nova-feed, so #nova-email
                                               # sat silent while the digest drowned in the firehose.
}
# Info-level events in these categories are rollups -> #nova-digest.
# (Only applies to level=info: a warning/critical in any of these still alerts.)
DIGEST_CATEGORIES = frozenset({
    "calendar", "telemetry", "syslog", "block_report", "network", "home",
    "analytics", "morning_brief", "finance", "tv", "security", "digest",
    "news", "garden", "backup",
})

# Dedup/rate-limit: a repeat of the same dedup_key within this window is folded
# into the prior sent alert (count bumped) instead of re-posted.
# Emitters can widen the window per-event via meta {"dedup_window_s": 86400}
# (an hourly job with a 1h window re-fires forever — see nova_analytics_aggregate).
DEDUP_WINDOW_S = 3600
# State-change alerting (2026-09-24): a persisting warning/critical condition pages ONCE a day,
# not once an hour. Hourly monitors (backup, staleness, sentinel, recurrence) were re-paging every
# cycle — 830 posts/week to #nova-alerts, real failures buried. Info stays 1h (feed/digest material).
# An emitter can still widen/narrow per-event via meta {"dedup_window_s": N}.
DEDUP_WINDOW_BY_LEVEL = {"warning": 86400, "critical": 86400}


# Maintenance gate (fail-open): during an authorized window, mute security-category
# Slack routing. Import guarded so a missing/broken gate never affects notification.
try:
    import nova_maintenance
    _MAINT_CATS = nova_maintenance.SECURITY_CATEGORIES
    _maint_active = nova_maintenance.is_active
except Exception:
    _MAINT_CATS = frozenset()
    def _maint_active() -> bool:
        return False


def _route(level: str, category: str | None) -> str:
    if category and category in CATEGORY_OVERRIDE:
        return CATEGORY_OVERRIDE[category]
    if level == "info" and category in DIGEST_CATEGORIES:
        return nova_config.SLACK_DIGEST
    return CHANNEL.get(level, nova_config.SLACK_FEED)


def _dedup_window(ev: dict) -> int:
    """Per-event dedup window: meta {"dedup_window_s": N}, else the default."""
    try:
        meta = ev.get("meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        return int(meta.get("dedup_window_s") or DEDUP_WINDOW_BY_LEVEL.get(ev.get("level"), DEDUP_WINDOW_S))
    except Exception:
        return DEDUP_WINDOW_BY_LEVEL.get(ev.get("level"), DEDUP_WINDOW_S)


def _fmt(ev: dict) -> str:
    emoji = {"info": ":information_source:", "warning": ":warning:",
             "critical": ":rotating_light:"}.get(ev["level"], "")
    lines = [f"{emoji} *{ev['title']}*"]
    if ev.get("body"):
        lines.append(ev["body"])
    tag = " · ".join(x for x in (ev.get("category"), ev.get("source")) if x)
    if tag:
        lines.append(f"_{tag}_")
    return "\n".join(lines)


def _connect():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(DSN, connect_timeout=5,
                            cursor_factory=psycopg2.extras.RealDictCursor)


def drain(verbose=False, only_source=None) -> int:
    """Process all 'new' events once. Returns number delivered."""
    sent = 0
    try:
        conn = _connect()
    except Exception as e:
        print(f"notifier: DB connect failed: {e}", file=sys.stderr)
        return 0
    with conn:
        with conn.cursor() as cur:
            q = "SELECT * FROM telemetry.events WHERE status='new'"
            params = []
            if only_source:  # test-scope: integration tests pass their own source so
                q += " AND source = %s"  # drain() doesn't pick up live production events
                params.append(only_source)
            q += " ORDER BY ts ASC LIMIT 200"
            cur.execute(q, params)
            events = cur.fetchall()
            try:
                _in_maint = _maint_active()   # checked once per drain, not per event
            except Exception:
                _in_maint = False
            for ev in events:
                # 0) Maintenance window: mute security-category Slack routing (the event
                #    row stays in telemetry.events for purple-team detection scoring).
                if _in_maint and ev.get("category") in _MAINT_CATS:
                    cur.execute("UPDATE telemetry.events SET status='suppressed', "
                                "channel='maintenance-muted', sent_at=now() WHERE id=%s", (ev["id"],))
                    if verbose:
                        print(f"  muted #{ev['id']} [{ev['category']}] — maintenance window")
                    continue
                # 0.5) STATE-CHANGE SAFETY NET: an event arriving with NO dedup_key skips the dedup
                #      below and floods — 39% of a recent 3-day window's sends did exactly that
                #      (producers that never set one, or an emit path that dropped it). Derive a
                #      stable fallback from source+category+title (volatile numbers stripped) and
                #      persist it, so EVERY event participates in dedup regardless of the emitter.
                #      Genuinely-distinct items (media titles differ by NAME, not just number) keep
                #      distinct keys and still send; only true repeats collapse. A producer that
                #      wants every copy opts out by passing its own unique key. (2026-08-12)
                if not ev.get("dedup_key"):
                    _t = re.sub(r"\d[\d:.,%\s-]*", "#", (ev.get("title") or ""))[:80]
                    ev["dedup_key"] = f"auto:{ev.get('source') or '-'}:{ev.get('category') or '-'}:{_t}"
                    cur.execute("UPDATE telemetry.events SET dedup_key=%s WHERE id=%s",
                                (ev["dedup_key"], ev["id"]))
                # 1) Dedup/rate-limit: was the same key already sent in the window?
                if ev["dedup_key"]:
                    cur.execute(
                        "SELECT id FROM telemetry.events WHERE dedup_key=%s AND status='sent' "
                        "AND sent_at > now() - make_interval(secs => %s) ORDER BY sent_at DESC LIMIT 1",
                        (ev["dedup_key"], _dedup_window(ev)))
                    prior = cur.fetchone()
                    if prior:
                        cur.execute(
                            # Carry the prior event's incident_id so a persisting condition
                            # keeps its incident's member clock fresh — otherwise the
                            # lifecycle auto-close (30m idle) fires while dedup (60m) still
                            # swallows repeats, producing open/close churn every ~40m.
                            "UPDATE telemetry.events SET status='suppressed', collapsed_into=%s, "
                            "incident_id=(SELECT incident_id FROM telemetry.events WHERE id=%s) "
                            "WHERE id=%s", (prior["id"], prior["id"], ev["id"]))
                        cur.execute(
                            "UPDATE telemetry.events SET dispatch_count = dispatch_count + 1 "
                            "WHERE id=%s", (prior["id"],))
                        if verbose:
                            print(f"  suppressed #{ev['id']} (dedup of #{prior['id']})")
                        continue
                # 1.2) Room voice (OfficePod): genuine life-safety / security-organ CRITICAL only.
                #      Placed BEFORE triage (up to 20 s) and correlation so neither delays nor
                #      folds away a smoke alarm. dispatch() only classifies and spawns a detached
                #      child (kill switch, quiet hours, rate limit live there); never blocks/raises.
                try:
                    if nova_voice_room:
                        nova_voice_room.dispatch(dict(ev))
                except Exception:
                    pass
                # 1.5) EVIDENCE CHECK before anything is believed (2026-10-05, incident #3675):
                #      triage runs here, BEFORE correlation, so a detector fault never opens an
                #      incident, never gets a qwen narrative and never recurs. Every other
                #      decision is kept and applied at delivery (3.5) exactly as before.
                t = _triage_event(ev)
                if isinstance(t, dict) and t.get("verdict") == "detector_fault" and t.get("decision") == "suppress":
                    cur.execute("UPDATE telemetry.events SET status='suppressed', "
                                "channel='detector-fault', sent_at=now() WHERE id=%s", (ev["id"],))
                    if verbose:
                        print(f"  detector-fault #{ev['id']} [{ev['category']}] — {t.get('reason')}")
                    continue
                # 2) Correlate: fold symptoms into incidents (deterministic + LLM).
                try:
                    corr = nova_correlator.correlate(conn, dict(ev))
                except Exception as e:
                    corr = {"action": "standalone", "suppress": False, "incident_id": None}
                    if verbose:
                        print(f"  correlate error #{ev['id']}: {e}")
                if corr.get("suppress"):
                    if verbose:
                        print(f"  folded #{ev['id']} into incident #{corr['incident_id']} ({corr['role']})")
                    continue  # symptom — the incident alert covers it

                # 3) Route + deliver (an incident summary if this opened one, else the event)
                channel = _route(ev["level"], ev["category"])
                if corr.get("action") == "opened":
                    summary, model = nova_correlator.llm_summarize(conn, corr["incident_id"])
                    # Propose remediation for the new incident (propose-only until
                    # REMEDIATION_ENABLED is flipped on — executes nothing yet).
                    try:
                        nova_remediation.propose_for_incident(conn, corr["incident_id"])
                    except Exception as e:
                        if verbose:
                            print(f"  remediation propose error #{corr['incident_id']}: {e}")
                    badge = ":rotating_light:" if ev["level"] == "critical" else ":warning:"
                    by = f" · _summary by {model}_" if model else ""
                    msg = (f"{badge} *Incident #{corr['incident_id']}: {ev['title']}*\n"
                           f"{summary}\n_correlating further events on this host into this incident{by}_")
                else:
                    msg = _fmt(ev)

                # 3.5) AI triage — fail-open to paging (see _triage_event above).
                #      suppress -> don't post; downgrade -> post to #nova-feed w/ note;
                #      page/downgrade -> append likely-cause + similar-incident context.
                if isinstance(t, dict) and t.get("decision") in ("page", "downgrade", "suppress"):
                    decision = t["decision"]
                    if decision == "suppress":
                        # Only ever reached for non-critical (brain's own rule). Mark the
                        # row so it isn't re-drained; triage already logged the decision.
                        cur.execute("UPDATE telemetry.events SET status='suppressed', "
                                    "channel='triage-suppressed', sent_at=now() WHERE id=%s", (ev["id"],))
                        if verbose:
                            print(f"  triage-suppressed #{ev['id']} ({t.get('verdict')}) — {t.get('reason')}")
                        continue
                    ann = (t.get("annotation") or "").strip()
                    if ann:
                        msg = f"{msg}\n{ann}"
                    if decision == "downgrade":
                        channel = nova_config.SLACK_FEED  # lower-priority ambient channel
                        msg = f"(downgraded) {msg}"
                        if verbose:
                            print(f"  triage-downgraded #{ev['id']} ({t.get('verdict')}) -> feed")
                # else: fail open — triage unavailable/junk; post normally, unmodified.

                try:
                    # post_both returns falsy when no destination accepted the message —
                    # never mark such an event 'sent' (it would vanish unseen).
                    if not nova_config.post_both(msg, slack_channel=channel):
                        raise RuntimeError(f"post_both delivered nowhere (channel={channel})")
                    if ev["level"] == "critical":
                        _mesh_relay(ev["title"], ev.get("body"))
                    cur.execute(
                        "UPDATE telemetry.events SET status='sent', channel=%s, sent_at=now() "
                        "WHERE id=%s", (channel, ev["id"]))
                    if corr.get("incident_id"):
                        cur.execute("UPDATE telemetry.incidents SET slack_ts='posted' WHERE id=%s",
                                    (corr["incident_id"],))
                    sent += 1
                    if verbose:
                        tag = f" [incident #{corr['incident_id']}]" if corr.get("action") == "opened" else ""
                        print(f"  sent #{ev['id']} [{ev['level']}/{ev['category']}] -> {channel}{tag}")
                except Exception as e:
                    cur.execute("UPDATE telemetry.events SET status='error' WHERE id=%s", (ev["id"],))
                    print(f"  deliver failed #{ev['id']}: {e}", file=sys.stderr)
    conn.close()
    return sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drain", action="store_true", help="process new events once")
    ap.add_argument("--daemon", action="store_true", help="loop forever")
    ap.add_argument("--interval", type=float, default=5.0, help="daemon poll seconds")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.daemon:
        print(f"nova_notifier daemon: polling every {a.interval}s")
        while True:
            try:
                drain(verbose=a.verbose)
            except Exception as e:
                print(f"notifier loop error: {e}", file=sys.stderr)
            time.sleep(a.interval)
    else:
        n = drain(verbose=True)
        print(f"drained: {n} delivered")


if __name__ == "__main__":
    main()
