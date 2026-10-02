#!/usr/bin/env python3
"""
nova_notify_jordan.py — skill 'notify-jordan-of-system-observations' (co-agency #118, 2026-10-02).

Nova's own numbers: she held back 64% of what she wanted to tell Jordan, and 66% of his sessions
fall in the 10:00–12:59 band — outside it "a message from me is an interruption, not a
conversation". So her direct reaches to him are no longer posted the moment she has them:
nova_reach holds them outside the window (reach_log status 'held'), and this delivers the drawer
as ONE bundled #nova-chat post inside the window, once a day. Empty drawer -> no post.

  nova_notify_jordan.py            # deliver
  nova_notify_jordan.py --dry-run  # print the bundle, send nothing, mark nothing
"""
import argparse
import sys
from datetime import datetime

import psycopg2

import nova_config
from nova_reach import DIRECT_AUDIENCES, OPS_DSN

MAX_ITEMS = 6
MAX_AGE_DAYS = 7


def log(m): print(f"[notify-jordan {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def bundle(rows):
    lines = [f"*Things I noticed since we last talked* ({len(rows)}):"]
    for _, ts, topic, msg in rows:
        lines.append(f"• _{ts.strftime('%a %H:%M')}_ {('[' + topic + '] ') if topic else ''}{msg.strip()}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True
    oc = conn.cursor()
    oc.execute("SELECT id, ts, coalesce(topic,''), message FROM reach_log WHERE lower(audience) = ANY(%s) "
               "AND status='held' AND ts > now() - interval '%s days' ORDER BY ts LIMIT %s",
               (list(DIRECT_AUDIENCES), MAX_AGE_DAYS, MAX_ITEMS))
    rows = oc.fetchall()
    if not rows:
        log("drawer empty — nothing to deliver"); return 0
    text = bundle(rows)
    if args.dry_run:
        print(text); return 0
    nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)
    oc.execute("UPDATE reach_log SET status='sent' WHERE id = ANY(%s)", ([r[0] for r in rows],))
    log(f"delivered {len(rows)} held reach(es) to #nova-chat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
