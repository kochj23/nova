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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"

# ── Routing policy ──────────────────────────────────────────────────────────
# Category overrides win over level. Everything else falls back to level.
CHANNEL = {
    "info":     nova_config.SLACK_INFO,     # #nova-info
    "warning":  nova_config.SLACK_NOTIFY,   # #nova-warning
    "critical": nova_config.SLACK_BB,       # #nova-critical
}
# Force specific categories somewhere regardless of the level the emitter set.
CATEGORY_OVERRIDE = {
    "security_news": nova_config.SLACK_INFO,   # CVE/threat NEWS is FYI, not your-network
    "claude_code":   nova_config.SLACK_INFO,   # Claude Code activity is FYI
    "calendar":      nova_config.SLACK_INFO,
}

# Dedup/rate-limit: a repeat of the same dedup_key within this window is folded
# into the prior sent alert (count bumped) instead of re-posted.
DEDUP_WINDOW_S = 3600


def _route(level: str, category: str | None) -> str:
    if category and category in CATEGORY_OVERRIDE:
        return CATEGORY_OVERRIDE[category]
    return CHANNEL.get(level, nova_config.SLACK_INFO)


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


def drain(verbose=False) -> int:
    """Process all 'new' events once. Returns number delivered."""
    sent = 0
    try:
        conn = _connect()
    except Exception as e:
        print(f"notifier: DB connect failed: {e}", file=sys.stderr)
        return 0
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM telemetry.events WHERE status='new' ORDER BY ts ASC LIMIT 200")
            events = cur.fetchall()
            for ev in events:
                # 1) Dedup/rate-limit: was the same key already sent in the window?
                if ev["dedup_key"]:
                    cur.execute(
                        "SELECT id FROM telemetry.events WHERE dedup_key=%s AND status='sent' "
                        "AND sent_at > now() - make_interval(secs => %s) ORDER BY sent_at DESC LIMIT 1",
                        (ev["dedup_key"], DEDUP_WINDOW_S))
                    prior = cur.fetchone()
                    if prior:
                        cur.execute(
                            "UPDATE telemetry.events SET status='suppressed', collapsed_into=%s "
                            "WHERE id=%s", (prior["id"], ev["id"]))
                        cur.execute(
                            "UPDATE telemetry.events SET dispatch_count = dispatch_count + 1 "
                            "WHERE id=%s", (prior["id"],))
                        if verbose:
                            print(f"  suppressed #{ev['id']} (dedup of #{prior['id']})")
                        continue
                # 2) Route + deliver
                channel = _route(ev["level"], ev["category"])
                try:
                    nova_config.post_both(_fmt(ev), slack_channel=channel)
                    cur.execute(
                        "UPDATE telemetry.events SET status='sent', channel=%s, sent_at=now() "
                        "WHERE id=%s", (channel, ev["id"]))
                    sent += 1
                    if verbose:
                        print(f"  sent #{ev['id']} [{ev['level']}/{ev['category']}] -> {channel}")
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
