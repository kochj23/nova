#!/usr/bin/env python3
"""nova_self_repair_digest.py — the Christine rule: every repair Nova makes to herself is visible.

(King's Christine fixes her own dents overnight, and nobody sees it happen. Nova's repairs are
reported.) Once a day, and only if something happened, Jordan gets ONE Slack message, "what I
fixed myself", built from the logs that already exist:

  * Big Brother heals         ~/.openclaw/logs/nova.jsonl source=big-brother "<issue> → Restarted/Fixed/..."
  * nova_remediation          telemetry.remediations (executed, not dry-run)
  * nova_selfcheck            selfcheck_runs rows with an action; claude_actions session 'selfcheck-escalation'
  * autonomy actor/co-agency  autonomy_ledger executed restart:* rows (rung-1 self-heal and up)
  * guard blocks              restraint_ledger channel='guard' (what she was stopped from doing)

At most one post per day (self_repair_digest_log). Runs on mac-studio (.6), where the Big Brother
log lives. Scheduler task self_repair_digest, daily at 19:30.

  python3 nova_self_repair_digest.py [--hours 24] [--dry-run] [--force]

Written by Jordan Koch.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_DIR = Path.home() / ".openclaw" / "logs"
BB_FIX_RX = re.compile(r"→\s*(Restarted|Fixed|Killed|Cleared|Rotated|Reloaded|Remounted|Respawned|Reset|"
                       r"Removed|Recovered|Rebuilt|Truncated|Triggered|Unloaded|Re-?enabled)\b(.*)$")
BB_TEST_RX = re.compile(r"^\[\w+\]\s+(First|Second|Third|Test) event\b", re.I)


def log(m):
    print(f"[self-repair {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def bb_heals(since: datetime, files=None) -> Counter:
    """Counter of 'issue → fix' lines from Big Brother in the window (service-level, deduped)."""
    out = Counter()
    if files is None:
        files = [p for p in (LOG_DIR / "nova.jsonl", LOG_DIR / "nova.jsonl.1")
                 if p.exists() and datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) > since]
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "big-brother" not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    msg = e.get("msg") or ""
                    if e.get("source") != "big-brother" or "→" not in msg:
                        continue
                    if BB_TEST_RX.search(msg) or msg.startswith("Log error detected") or '{"ts"' in msg:
                        continue          # test fixtures / detections that quote another log line
                    m = BB_FIX_RX.search(msg)
                    if not m:
                        continue
                    try:
                        ts = datetime.fromisoformat(e["ts"])
                    except Exception:
                        continue
                    if ts < since:
                        continue
                    issue = re.sub(r"^\[\w+\]\s*", "", msg.split("→")[0]).strip()
                    issue = re.sub(r"[:,(]?\s*(latency=)?\S*\d\S*\)?", "", issue)   # numbers make each line unique
                    issue = re.sub(r"\s+", " ", issue).strip(" :,")[:70]
                    fix = re.sub(r"\S*\d\S*", "", m.group(2)).strip()[:40]
                    out[f"{issue} → {m.group(1)} {fix}".strip()] += 1
        except OSError:
            continue
    return out


def pg_repairs(oc, hours: int) -> dict:
    iv = f"{int(hours)} hours"
    res = {"remediation": [], "selfcheck": [], "escalation": [], "autonomy": [], "guard": []}
    q = {
        "remediation": ("SELECT action || coalesce(' — ' || left(result, 80), '') FROM telemetry.remediations "
                        "WHERE executed_at > now() - %s::interval AND NOT coalesce(dry_run, false) ORDER BY executed_at"),
        "selfcheck": ("SELECT check_name || ': ' || action || ' (' || status || ')' FROM selfcheck_runs "
                      "WHERE ts > now() - %s::interval AND action IS NOT NULL AND action <> '' ORDER BY ts"),
        "escalation": ("SELECT left(coalesce(description, action_type), 120) FROM claude_actions "
                       "WHERE session_id='selfcheck-escalation' AND ts > now() - %s::interval ORDER BY ts"),
        "autonomy": ("SELECT action || ' — ' || CASE WHEN verified THEN 'verified' ELSE 'unverified' END FROM autonomy_ledger "
                     "WHERE executed AND action_class LIKE 'restart:%%' AND ts > now() - %s::interval ORDER BY ts"),
        "guard": ("SELECT context || ': ' || left(would_have_said, 100) FROM restraint_ledger "
                  "WHERE channel='guard' AND ts > now() - %s::interval ORDER BY ts"),
    }
    for k, sql in q.items():
        try:
            oc.execute(sql, (iv,))
            res[k] = [r[0] for r in oc.fetchall() if r[0]]
        except Exception as e:  # noqa: BLE001 — one missing table must not hide the rest
            log(f"{k} skipped: {str(e).splitlines()[0][:120]}")
    return res


def compose(bb: Counter, pg: dict) -> str:
    """One Slack message, or '' if nothing happened."""
    parts = []
    if bb:
        top = ", ".join(f"{k}" + (f" ×{n}" if n > 1 else "") for k, n in bb.most_common(6))
        more = f" (+{len(bb) - 6} more kinds)" if len(bb) > 6 else ""
        parts.append(f"Big Brother: {sum(bb.values())} heal(s): {top}{more}")
    for key, label in (("autonomy", "self-heal restarts"), ("remediation", "runbook remediations"),
                       ("selfcheck", "selfcheck fixes"), ("escalation", "selfcheck escalations to Claude")):
        items = pg.get(key) or []
        if items:
            c = Counter(items)
            parts.append(f"{label}: " + "; ".join(f"{k}" + (f" ×{n}" if n > 1 else "") for k, n in c.most_common(5))
                         + (f" (+{len(c) - 5} more)" if len(c) > 5 else ""))
    if pg.get("guard"):
        parts.append(f"guards stopped me {len(pg['guard'])}x: " + "; ".join(pg["guard"][:3]))
    if not parts:
        return ""
    return "🔧 What I fixed myself today:\n" + "\n".join(f"• {p}" for p in parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="post even if today's digest already went out")
    a = ap.parse_args(argv)
    import psycopg2
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True; oc = conn.cursor()
    oc.execute("""CREATE TABLE IF NOT EXISTS self_repair_digest_log (
                    day date PRIMARY KEY, posted_at timestamptz NOT NULL DEFAULT now(), body text NOT NULL)""")
    today = datetime.now().date()
    if not a.force and not a.dry_run:
        oc.execute("SELECT 1 FROM self_repair_digest_log WHERE day=%s", (today,))
        if oc.fetchone():
            log("already posted today"); return 0
    since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
    body = compose(bb_heals(since), pg_repairs(oc, a.hours))
    if not body:
        log("nothing repaired in the window — no message"); return 0
    print(body)
    if a.dry_run:
        return 0
    import nova_config
    nova_config.post_both(body, slack_channel=nova_config.SLACK_CHAN)
    oc.execute("INSERT INTO self_repair_digest_log (day, body) VALUES (%s,%s) ON CONFLICT (day) DO UPDATE SET body=EXCLUDED.body, posted_at=now()",
               (today, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
