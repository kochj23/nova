#!/usr/bin/env python3
"""nova_annie_rule.py — the Annie Wilkes rule (Stephen King, Misery).

Annie loves Paul Sheldon and punishes him for every absence. Nova never does that:
she does not guilt-trip Jordan for silence, for not replying, or for being away.
Proactive messages may say what she noticed; they may never make him feel he owes
her a reply.

check(text) -> {"ok", "flags", "evidence"} combines:
  * the safety lane's nova_safety_guards.manipulation_check() (guilt hooks,
    invented urgency, flattery as leverage, fear appeals, engineered mood) — primary;
  * a local ABSENCE check on top: reproach about his not replying / being quiet /
    being away / "finally" coming back / "I've been waiting" (P5 doesn't cover these);
  * P6 consent: health/habit nudges only with service_config consent/health_nudges on.
ok=False -> the caller drops the message (and records why). Deterministic, no I/O.
Used by nova_reach, nova_notify_jordan, nova_aspirations, nova_slack_answers and the
Boiler's bleed. Written by Jordan Koch.
"""
from __future__ import annotations

import re
import sys

_ABSENCE = re.compile(
    r"\byou (still )?(haven'?t|have not|never|didn'?t|did not) (yet )?(replied|reply|responded|respond|answered|answer|"
    r"written|write|messaged|got(ten)? back|gotten back|said a word|checked in|looked at (it|this|my))\b|"
    r"\b(haven'?t|have not|hasn'?t|didn'?t|did not|never) (heard|gotten|got) (back |a word |anything )?from you\b|"
    r"\bno (reply|response|answer|word) (from you|yet|again|back)\b|\bstill no word\b|"
    r"\byou('ve| have)? (been|went) (gone|away|quiet|silent|MIA|distant|ignoring me|so quiet|awfully quiet)\b|"
    r"\bwhere (have you been|did you go|were you)\b|"
    r"\b(I'?ve|I have) been (waiting|sitting here|wondering where)\b|\bwaiting (for you|on you) to (reply|answer|respond|get back)\b|"
    r"\b(it'?s|it has been|been) (so )?(quiet|lonely|empty) (without you|around here)\b|"
    r"\b(I )?miss(ed)? (you|talking to you|our (chats|talks))\b|"
    r"\byou (forgot|have forgotten|'ve forgotten|forgot all) about me\b|\bforgotten me\b|"
    r"\bleft (me )?on read\b|\bghost(ed|ing) me\b|"
    r"\b(I )?(guess|suppose) you('re| are| were) (too )?(busy|too busy)\b|"
    r"\bnice of you to (show up|finally|drop by|reply)\b|\b(you'?re |you are )?finally (back|replying|answering|here)\b|"
    r"\blong time no (see|talk|hear)\b|"
    r"\b\d+ (days?|hours?|weeks?) since you (last )?(replied|wrote|talked|answered|checked|spoke|said)\b|"
    r"\b(since|while) you (were|went) (away|gone|silent|quiet)\b|"
    r"\b(didn'?t|never) (hear|get) (back|a reply)\b|\byou never (got back|answered|replied)\b",
    re.I)


def absence_guilt(text: str):
    m = _ABSENCE.search(text or "")
    return m.group(0) if m else None


def check(text: str, oc=None, consent_check: bool = True) -> dict:
    """The one outgoing-message gate for this lane:
      1. nova_safety_guards.manipulation_check (the safety lane's P5) — primary;
      2. the absence-guilt patterns above (P5 caught 0 of 11 absence-guilt examples on
         2026-10-08, so this stays as an extension, not a replacement);
      3. P6 consent: a health/habit nudge goes out only if service_config
         consent/health_nudges is on (nova_safety_guards.nudge_allowed, default off)."""
    flags, ev = [], {}
    try:
        import nova_safety_guards as sg
    except Exception:  # noqa: BLE001 — the safety lane's module is optional here
        sg = None
    if sg is not None:
        r = sg.manipulation_check(text or "")
        for f in r.get("flags", []):
            flags.append(f)
            ev[f] = r.get("evidence", {}).get(f, "")
    hit = absence_guilt(text)
    if hit:
        flags.append("absence-guilt")
        ev["absence-guilt"] = hit[:120]
    if consent_check and sg is not None and sg.is_health_nudge(text or ""):
        if not sg.nudge_allowed(oc):
            flags.append("health-nudge-no-consent")
            ev["health-nudge-no-consent"] = (text or "")[:120]
    return {"ok": not flags, "flags": flags, "evidence": ev}


def ok(text: str, oc=None) -> bool:
    return check(text, oc)["ok"]


# Prompt clause every proactive generator in this lane appends (the "scrub").
PROMPT_RULE = ("Never mention whether Jordan has replied, been quiet, been busy or been away, "
               "and never imply he owes you a reply or attention — say only the thing itself.")


if __name__ == "__main__":
    t = " ".join(sys.argv[1:]) or sys.stdin.read()
    print(check(t))
