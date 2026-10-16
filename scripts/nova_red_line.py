#!/opt/homebrew/bin/python3
"""nova_red_line.py — the few decisions Nova does NOT make alone, and where they go instead.

Jordan, 2026-10-15: "Nova's organs are for Nova ... Her questions go back to her, she should do what
is right. She shouldn't wait for me to answer." Everything she used to put to him she now decides on
her own path — EXCEPT the hard red lines. For those she asks Claude: a claude_queue item, priority
high, tagged 'red_line_decision'. A Claude session decides and escalates to Jordan only if unsure.

The red lines (his answer (1), verbatim list):
  proteus_physical  locks / garage / alarms / covers / extreme climate (nova_safety_guards.physical_guard)
  two_man           irreversible or outward actions (delete/drop/wipe/money/publish to the world,
                    mail to anyone outside the herd)
  ae35              weakening or silencing one of her own watchers alone (nova_ae35_rule.classify)
  kill_switch       engaging or releasing the autonomy kill switch
  chaos_live        live chaos / wargame runs against production

  classify(text)                       -> sorted list of red-line classes the text touches ([] = hers to decide)
  is_red_line(text)                    -> bool
  route_to_claude(cur, title, context, source, ref=None) -> claude_queue id (deduped on source+ref) or None

Fails CLOSED: if a guard module is unavailable the text is treated as a red line.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TAG = "red_line_decision"
PRIORITY = 2            # claude_queue: 1 = most urgent, default 5; red-line decisions are high
SESSION_FALLBACK = "nova_red_line"

_TWO_MAN_RX = re.compile(
    r"\b(delete|drop (table|database|schema)|truncate|rm -rf|wipe|purge|destroy|format (the )?disk|factory reset"
    r"|revoke (key|cert|token)|rotate (the )?(root|master) (key|password)"
    r"|pay(ment)?|purchase|buy|wire (money|funds)|transfer (money|funds)|credit card|donat\w*"
    r"|publish (to|on) (twitter|x|mastodon|bluesky|reddit|hacker news|linkedin|github|the web)|press release"
    r"|tweet|post publicly|open a pr|file an? (issue|bug) (on|with) (github|upstream))\b", re.I)
_PROTEUS_RX = re.compile(r"\b(garage|siren|disarm|deadbolt|unlock\w*|security system|alarm panel|arm the alarm"
                         r"|lock (the )?(\w+ )?(door|gate))\b", re.I)   # belt to physical_guard's braces
_KILL_RX = re.compile(r"\b(kill.?switch|autonomy.?kill)\b", re.I)
_CHAOS_RX = re.compile(r"\b(chaos|wargame|war.?game|game ?day|fault injection)\b.{0,60}\b(live|prod(uction)?|real)\b"
                       r"|\b(live|prod(uction)?)\b.{0,40}\b(chaos|wargame|fault injection)\b", re.I)


def classify(text: str) -> list:
    """Red-line classes the text touches, [] when the decision is Nova's own. Pure apart from the
    guard modules it imports; any guard failure counts as a hit (fail closed)."""
    t = text or ""
    hits = set()
    try:
        import nova_safety_guards as g
        if g._physical_hits(t, (), (), None) or _PROTEUS_RX.search(t):
            hits.add("proteus_physical")
    except Exception:  # noqa: BLE001
        hits.add("proteus_physical")
    try:
        import nova_ae35_rule as ae35
        if any(cls == "change" for _, cls in ae35.classify(t)):
            hits.add("ae35")
    except Exception:  # noqa: BLE001
        hits.add("ae35")
    if _KILL_RX.search(t):
        hits.add("kill_switch")
    if _CHAOS_RX.search(t):
        hits.add("chaos_live")
    if _TWO_MAN_RX.search(t):
        hits.add("two_man")
    return sorted(hits)


def is_red_line(text: str) -> bool:
    return bool(classify(text))


def route_to_claude(cur, title: str, context: str, source: str, ref=None, classes=None) -> int | None:
    """File ONE claude_queue item for a red-line decision (priority PRIORITY, description tagged
    [red_line_decision]). Deduped on source+ref while not cancelled. Never raises; returns the id or None."""
    key = f"[{TAG}] {source}" + (f" #{ref}" if ref is not None else "")
    try:
        cur.execute("SELECT id FROM claude_queue WHERE description LIKE %s AND status <> 'cancelled' LIMIT 1",
                    (key.replace("%", r"\%") + ":%",))
        r = cur.fetchone()
        if r:
            return r[0]
        cur.execute("SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1")
        sid = (cur.fetchone() or [None])[0] or SESSION_FALLBACK
        cls = ", ".join(classes or classify(f"{title} {context}")) or "unspecified"
        ctx = (f"tag: {TAG}\nred-line classes: {cls}\nsource: {source}" + (f" ref {ref}" if ref is not None else "")
               + f"\n\n{context or ''}\n\nNova does not decide red lines alone. Decide it (do / do not / do "
               "differently) and record why in outcome; escalate to Jordan ONLY if you are unsure.")
        cur.execute("""INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context)
                       VALUES (%s, now(), now(), 'queued', %s, %s, %s) RETURNING id""",
                    (sid, PRIORITY, f"{key}: {(title or '')[:300]}", ctx[:8000]))
        return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        print(f"[red-line] route_to_claude failed for {key}: {e}", flush=True)
        return None


if __name__ == "__main__":
    print(classify(" ".join(sys.argv[1:])))
