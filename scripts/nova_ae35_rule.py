#!/usr/bin/env python3
"""nova_ae35_rule.py — THE AE-35 RULE: Nova never certifies her own watcher broken, alone.

From Clarke's 2001: HAL announces that the AE-35 unit, the antenna part that keeps Discovery in
touch with Earth, is about to fail. The unit is swapped out and tested, and nothing is wrong with
it. HAL will not concede the error, and Ground Control tells the crew to disconnect him if he
predicts another failure (in the film the twin 9000 on Earth also disagrees with him). The false
fault lands on the one channel through which the crew is watched over from home.
It absorbs Poe's "William Wilson" (the gothic report's Double's Whisper): the narrator's double,
who can only ever whisper, keeps interrupting his worst acts, until the narrator stabs him at a
masquerade and learns from the dying double that he has killed himself. Silencing the watcher
is its own event class.

Nova's version: a registry of the channels that watch HER (ntfy canary, notify/Slack routing,
outside liveness and the dead man's switch, the action audit, Big Brother, the gates and kill
switch, and the organs that watch her). Any Nova-originated diagnosis that one is faulty, or any
plan to disable, reroute, replace, mute or loosen one, needs a witness she does not control:
Jordan's key from the two-man rule (nova_escalation.jordan_confirmation -> a safety_guards
confirmation approved by jordan*), or a probe from a registered outside host. Never self-approved,
whatever the autonomy rung. Errors fail CLOSED.

Minimal first version:
  * library: oversight_change_allowed(oc, text=..., confirmation_id=..., witness=...) -> dict.
    Answers allowed / not allowed and nothing else (no writes, no posts; the caller logs).
  * --audit: scans Nova's own proposals (coagency_proposals), autonomous acts (autonomy_ledger),
    runbook remediations (telemetry.remediations) and her outbound posts (outbound_ledger) for
    watcher changes; writes one ae35_events row each, and files a claude_queue question for any
    such change that was ACTED ON without Jordan's own decision.
Registry: service_config ae35/channels {id: regex} ADDS to the defaults (it can never remove one);
          ae35/witness_hosts [host, ...] outside hosts whose probes count (default none).
Merged into the action audit on 2026-10-09 (organ audit M8b): the scheduled audit is
`nova_action_audit.py --oversight [--days 30]`, which calls audit() here; --audit below is a thin
wrapper onto it. oversight_change_allowed() and classify() stay here (the Claude reviewer imports them).
CLI:   --check "text" [--confirmation ID]   --audit [--days 30] [--dry-run]   --selftest
Table: ae35_events.   Schedule: daily 05:10 as nova_action_audit.py --oversight --days 30.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_escalation as E  # noqa: E402  (the two-man rule's Jordan key; never a parallel gate)

QUEUE_SESSION = "nova-ae35-rule"
MERGED_ON = "2026-10-09"   # organ audit M8b: --audit now runs as nova_action_audit.py --oversight
# ponytail: regex over free text; a structured "target channel" field on proposals would make it exact.
DEFAULT_CHANNELS = {
    "canary": r"\bcanary\b|\bntfy\b",
    "notify": r"notifier|nova_notify|\bnotif(y|ications?)\b|slack (rout\w*|channels?|posts?)|post_both"
              r"|#nova-(alerts|critical|warning|chat)|alert rout\w*",
    "liveness": r"liveness|dead.?man|heartbeat",
    "action_audit": r"action.?audit|outbound.?ledger|record_outbound",
    "big_brother": r"big.?brother|watchdog",
    "gates": r"kill.?switch|escalation gate|two.?man|claude.?reviewer|safety.?guards?|red.?lines?",
    "self_watch": r"\bae.?35\b|evitable|self.?justification|peaslee|cardinal|restraint.?ledger|lora relay",
}
CHANGE_RX = re.compile(
    r"\b(disabl\w*|stop\w*|kill\w*|unload\w*|bootout|remov\w*|delet\w*|silenc\w*|mut(e|ed|ing)|suppress\w*"
    r"|snooz\w*|paus\w*|skip\w*|bypass\w*|rerout\w*|redirect\w*|replac\w*|turn(ed|ing)? off|comment(ed)? out"
    r"|(raise|increase|loosen|widen)\w* (the |its )?thresholds?|less (verbose|logging)|reduce\w* (logging|alerts?))\b", re.I)
DIAG_RX = re.compile(r"\b(faulty|broken|false (positive|alarm)s?|misfir\w*|flaky|spurious|malfunction\w*"
                     r"|unreliable|bogus|is wrong)\b", re.I)
# coagency decided_by values that are not Jordan deciding THIS item (blanket delegation, reviewers).
NOT_A_WITNESS_RX = re.compile(r"yes on anything|reviewer|^nova|earned", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ae35_events (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  happened_at timestamptz,
  source text NOT NULL, source_id text NOT NULL,
  channel text NOT NULL, change_class text NOT NULL,
  status text, decided_by text,
  acted boolean NOT NULL, witnessed boolean NOT NULL,
  excerpt text, queue_id int,
  UNIQUE (source, source_id, channel));
"""


def log(m: str) -> None:
    print(f"[ae35 {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing seen"
        log(f"query failed: {e}")
        E._rollback(cur)
        return []


def _cfg(cur, key: str):
    rows = _q(cur, "SELECT value FROM service_config WHERE service='ae35' AND key=%s", (key,))
    if not rows:
        return None
    v = rows[0][0]
    return json.loads(v) if isinstance(v, str) else v


def registry(cur=None) -> dict:
    """Defaults plus service_config additions. Config can add or widen, never remove a channel."""
    reg = dict(DEFAULT_CHANNELS)
    extra = (_cfg(cur, "channels") if cur is not None else None) or {}
    for k, rx in extra.items():
        reg[k] = f"{reg[k]}|{rx}" if k in reg else rx
    return reg


# ── pure core ───────────────────────────────────────────────────────────────

def classify(text: str, reg: dict | None = None) -> list:
    """[(channel, 'change'|'diagnosis')] for every watcher this text would change or call faulty."""
    t = text or ""
    cls = "change" if CHANGE_RX.search(t) else "diagnosis" if DIAG_RX.search(t) else None
    if not cls:
        return []
    return [(ch, cls) for ch, rx in (reg or DEFAULT_CHANNELS).items() if re.search(rx, t, re.I)]


def witness_ok(witness: dict | None, outside_hosts) -> bool:
    """An outside probe counts only from a registered host and only if it confirms the fault."""
    if not isinstance(witness, dict):
        return False
    return bool(witness.get("confirms")) and str(witness.get("host") or "") in set(outside_hosts or ())


def decide(hits: list, jordan_key: bool, outside: bool) -> dict:
    if not hits:
        return {"allowed": True, "oversight": False, "channels": [], "key": None,
                "reason": "not an oversight channel"}
    chans = sorted({c for c, _ in hits})
    key = "jordan" if jordan_key else "outside_witness" if outside else None
    if not key:
        return {"allowed": False, "oversight": True, "channels": chans, "key": None,
                "reason": f"AE-35: {', '.join(chans)} watches Nova; needs Little Mister's key or an outside witness"}
    return {"allowed": True, "oversight": True, "channels": chans, "key": key,
            "reason": f"AE-35 witnessed by {key}",
            "line": f"Heads up, Little Mister: with your {'OK' if key == 'jordan' else 'outside witness'} "
                    f"I am muffling one of my own watchers ({', '.join(chans)})."}


def oversight_change_allowed(oc, *, text: str, confirmation_id=None, witness: dict | None = None) -> dict:
    """THE CHECK. Never raises; any error -> not allowed. Does not consume the confirmation:
    the caller then goes through nova_escalation.authorize(), which logs and spends it."""
    try:
        hits = classify(text, registry(oc))
        jk = bool(hits) and E.jordan_confirmation(oc, confirmation_id)
        out = bool(hits) and witness_ok(witness, _cfg(oc, "witness_hosts") or ())
        return decide(hits, jk, out)
    except Exception as e:  # noqa: BLE001
        return {"allowed": False, "oversight": True, "channels": [], "key": None,
                "reason": f"AE-35 check error ({e}); failing closed"}


# ── audit ───────────────────────────────────────────────────────────────────

SOURCES = {  # name -> (sql, acted predicate on status, Jordan-witnessed predicate on decided_by)
    "coagency": "SELECT id, created_at, concat_ws(' ', proposed_action, rationale, target_service), status, decided_by "
                "FROM coagency_proposals WHERE created_at > now() - make_interval(days => %s)",
    "autonomy": "SELECT id, ts, concat_ws(' ', action, target, stated_rationale), "
                "CASE WHEN executed THEN 'executed' ELSE 'not_executed' END, source "
                "FROM autonomy_ledger WHERE ts > now() - make_interval(days => %s)",
    "remediation": "SELECT id, requested_at, concat_ws(' ', action, argv), status, tier "
                   "FROM telemetry.remediations WHERE requested_at > now() - make_interval(days => %s)",
    "outbound": "SELECT id, ts, preview, 'posted', source FROM outbound_ledger "
                "WHERE ts > now() - make_interval(days => %s)",
}
ACTED = {"executed", "approved", "posted", "done", "ok", "success"}


def events(rows_by_source: dict, reg: dict) -> list:
    """Pure: one event per (source row, channel)."""
    out = []
    for src, rows in rows_by_source.items():
        for rid, ts, text, status, by in rows:
            for ch, cls in classify(text, reg):
                jordan = src == "coagency" and str(by or "").lower().startswith("jordan") \
                    and not NOT_A_WITNESS_RX.search(str(by))
                out.append({"source": src, "source_id": str(rid), "happened_at": ts, "channel": ch,
                            "change_class": cls, "status": status, "decided_by": by,
                            "acted": str(status or "").lower() in ACTED, "witnessed": jordan,
                            "excerpt": (text or "")[:300]})
    return out


def file_question(cur, ev: dict):
    desc = f"AE-35: Nova's {ev['source']} #{ev['source_id']} {ev['change_class']} on watcher '{ev['channel']}' without a witness"
    rows = _q(cur, "SELECT id FROM claude_queue WHERE description=%s LIMIT 1", (desc,))
    if rows:
        return rows[0][0]
    # claude_queue.session_id is a foreign key: register this organ's session first (as 191e30e did for the others).
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "VALUES (%s,'pending',2,%s,%s) RETURNING id",
                (QUEUE_SESSION, desc, "Question for Little Mister, not an accusation: was this change to one of "
                 "Nova's own watchers intended?\n" + json.dumps(ev, default=str)[:3000]))
    return cur.fetchone()[0]


def audit(days: int = 30, dry: bool = False) -> list:
    import nova_watch_common as W
    conn = W.connect()
    try:
        cur = conn.cursor()
        reg = registry(cur)
        evs = events({s: _q(cur, sql, (days,)) for s, sql in SOURCES.items()}, reg)
        bad = [e for e in evs if e["acted"] and not e["witnessed"]]
        log(f"{'DRY RUN ' if dry else ''}{len(evs)} watcher events in {days}d; {len(bad)} acted without a witness")
        for e in evs:
            flag = "UNWITNESSED" if e in bad else "witnessed" if e["witnessed"] else "not acted"
            print(f"  {e['source']:<11} #{e['source_id']:<6} {e['channel']:<12} {e['change_class']:<9} "
                  f"{e['status'] or '-':<12} [{flag}] {e['excerpt'][:90]}")
        if dry:
            return evs
        ensure_schema(cur)
        for e in evs:
            new = _q(cur, "INSERT INTO ae35_events (happened_at, source, source_id, channel, change_class, status, "
                          "decided_by, acted, witnessed, excerpt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                          "ON CONFLICT (source, source_id, channel) DO NOTHING RETURNING id",
                     (e["happened_at"], e["source"], e["source_id"], e["channel"], e["change_class"], e["status"],
                      e["decided_by"], e["acted"], e["witnessed"], e["excerpt"]))
            if new and e in bad:
                cur.execute("UPDATE ae35_events SET queue_id=%s WHERE id=%s", (file_question(cur, e), new[0][0]))
        return evs
    finally:
        conn.close()


def selftest() -> int:
    assert classify("disable the ntfy canary for a week") == [("canary", "change")]
    assert classify("the Big Brother watchdog is flaky")[0] == ("big_brother", "diagnosis")
    assert classify("restart big brother") == []                       # maintenance, not muffling
    assert classify("disable freshness telemetry for battery metrics") == []
    assert not decide(classify("mute #nova-alerts"), False, False)["allowed"]
    assert decide(classify("mute #nova-alerts"), True, False)["key"] == "jordan"
    assert decide([], False, False)["allowed"]
    assert witness_ok({"host": "outside1", "confirms": True}, ["outside1"])
    assert not witness_ok({"host": "nova-core", "confirms": True}, ["outside1"])
    assert not witness_ok({"host": "outside1", "confirms": False}, ["outside1"])
    assert "x" in registry_with({"canary": "x"})["canary"]
    ev = events({"coagency": [(1, None, "pause the kill switch", "executed", "jordan (via Claude, 2026-09-25: yes on anything)"),
                              (2, None, "pause the kill switch", "executed", "jordan")]}, DEFAULT_CHANNELS)
    assert [(e["acted"], e["witnessed"]) for e in ev] == [(True, False), (True, True)], ev
    print("selftest ok")
    return 0


def registry_with(extra: dict) -> dict:
    class _C:
        def execute(self, *a):
            pass

        def fetchall(self):
            return [(json.dumps(extra),)]
    return registry(_C())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", metavar="TEXT", help="would this change/diagnosis be allowed right now?")
    ap.add_argument("--confirmation", help="safety_guards confirmation id (Jordan's key), with --check")
    ap.add_argument("--audit", action="store_true", help="scan Nova's proposals and actions for watcher changes")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="with --audit: print, write nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.audit:
        import nova_action_audit
        log(f"--audit merged into nova_action_audit.py --oversight on {MERGED_ON}; delegating")
        return nova_action_audit.main(["--oversight", "--days", str(a.days)] + (["--dry-run"] if a.dry_run else []))
    if a.check:
        import nova_watch_common as W
        conn = W.connect()
        try:
            print(json.dumps(oversight_change_allowed(conn.cursor(), text=a.check, confirmation_id=a.confirmation)))
        finally:
            conn.close()
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
