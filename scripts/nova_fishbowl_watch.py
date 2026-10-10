#!/usr/bin/env python3
"""nova_fishbowl_watch.py — early-warning tripwire on the watch-community ("fishbowl") feed.

Scans newly-ingested fishbowl memories for (a) mentions of Jordan by identity, or (b) Watch
Nicholas paired with threat/dox language, and alerts to Slack #nova-critical + a critical local
notification. Runs on a timer; advances a last-seen marker so each hit alerts exactly once.
First run baselines to 'now' so it never re-alerts the historical Oct-2024 thread.

Tune HANDLE below with your superchat/donor handle(s) to also catch mentions of your persona.
"""
import json
import re
import sys
from pathlib import Path

import psycopg2
import nova_config

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_memories")
STATE = Path.home() / ".openclaw" / "state" / "fishbowl_watch.json"
ALERT_CHANNEL = nova_config.SLACK_BB   # #nova-critical

# (a) Anyone referencing Jordan by identity -> alert. Word-bounded to avoid false hits.
HANDLE = []   # <-- add your community handle(s), e.g. ["mookiefan42"], to widen the net
_IDENT_TERMS = ["jordan koch", "kochj", "jordan\\.koch", "koch", "digitalnoise"] + \
               [re.escape(h) for h in HANDLE if h.strip()]
IDENTITY_RE = re.compile(r"\b(" + "|".join(_IDENT_TERMS) + r")\b", re.I)

# (b) Watch Nicholas + any threat term -> alert (even if Jordan isn't named).
NICK = ("watch nicholas", "watchnicholas", "nicholas watch", "watch nick")
# Clickable links for the alert. YouTube's /live URL redirects to a channel's CURRENT
# live stream when it's live, so "Watch Nicholas is on" links straight to the stream
# (falls back to the /streams page if he isn't live at click time).
NICK_LINKS = [
    # Corrected 2026-07-21 -- the old @WatchNicholasLivestream1/@watchnicholasstreams
    # handles both 404 (dead/renamed); real channel confirmed live via yt-dlp probe.
    ("Watch Nicholas — LIVE", "https://www.youtube.com/@watchnicholaslive/live"),
]
THREAT = ("dox", "doxx", "fired", "your job", "your employer", "your work", "end in tears",
          "coming after", "come after you", "expose you", "your family", "your address",
          "get you fired", "contact your", "real world", "and yours")


def _load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d))


def hits(text: str):
    """Return the list of trip reasons for a memory, or [] if clean."""
    t = (text or "").lower()
    reasons = []
    m = IDENTITY_RE.search(t)
    if m:
        reasons.append(f"identity:{m.group(1)}")
    if any(n in t for n in NICK) and any(th in t for th in THREAT):
        reasons.append("nicholas+threat")
    return reasons


def main():
    st = _load()
    last = st.get("last_seen")
    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as cur:
        if not last:                      # baseline: watch forward only, no backlog spam
            cur.execute("SELECT max(created_at) FROM memories WHERE source='fishbowl'")
            newest = cur.fetchone()[0]
            _save({"last_seen": newest.isoformat() if newest else None})
            print("baselined; watching forward")
            return
        cur.execute("SELECT id, created_at, text FROM memories "
                    "WHERE source='fishbowl' AND created_at > %s ORDER BY created_at", (last,))
        rows = cur.fetchall()

    alerts = 0
    newest = last
    for mid, ts, text in rows:
        newest = ts.isoformat()
        reasons = hits(text)
        if not reasons:
            continue
        alerts += 1
        snippet = " ".join((text or "").split())[:400]
        links = ""
        if any("nicholas" in r for r in reasons):
            links = "\n" + " · ".join(f"<{u}|▶️ {label}>" for label, u in NICK_LINKS)
        msg = (f":rotating_light: *Fishbowl watch* — {', '.join(reasons)}\n"
               f"_{ts}_\n> {snippet}{links}")
        try:
            nova_config.post_both(msg, slack_channel=ALERT_CHANNEL)
            nova_config.notify_local("Fishbowl watch", ", ".join(reasons), critical=True)
        except Exception as e:
            print(f"alert post failed: {e}", file=sys.stderr)
    _save({"last_seen": newest})
    print(f"scanned {len(rows)} new fishbowl memories, {alerts} alert(s)")


if __name__ == "__main__":
    main()
