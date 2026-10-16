#!/opt/homebrew/bin/python3
"""nova_directive_resolve.py — Nova resolves her own directive conflicts (#23, Jordan 2026-10-15).

"Her questions go back to her, she should do what is right ... She shouldn't wait for me to answer."
Until now every open row in directive_conflicts was put to Jordan in #nova-chat (nova_directive_decide
--post, two a day) and 37 sat open. This is her own weekly batch instead:

  * a conflict that touches a RED LINE (Proteus physical, two-man irreversible/outward, AE-35, kill
    switch, live chaos — nova_red_line.classify) is not hers: ONE claude_queue item, priority high,
    tagged 'red_line_decision'; the row stays open until a Claude session decides it
    (nova_directive_conflict.py --decide ID --note ... --by claude).
  * every other open conflict she decides HERSELF: the more conservative branch, unless her own
    evidence says otherwise — a precedent: an earlier decision by Jordan or Claude on the same pair of
    sources, which she follows. The reasoning goes into decision_note; decided_by = 'nova'.
  Rows are closed through the directive organ's decide(conflict_id, decision, decided_by, reason)
  (nova_directive_conflict.py, consolidated 2026-10-15). What she decided shows up in her 18:00 summary.

  nova_directive_resolve.py              # resolve this week's open conflicts
  nova_directive_resolve.py --dry-run    # print what she would decide, write nothing
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nova_dsn as _nova_dsn  # noqa: E402

NOVA_BY = "nova"
PRECEDENT_BY = ("jordan", "claude")
STRICT_RX = re.compile(r"\b(never|do not|don't|must not|refuse|forbid|prohibit|not allowed)\b", re.I)


def log(m):
    print(f"[directive-resolve {datetime.now():%H:%M:%S}] {m}", flush=True)


def short(s, n=300):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def choose(row, precedent=None):
    """Pure. row = (id, kind, directive_a, source_a, directive_b, source_b, situation, conservative_branch, why);
    precedent = (decision_note, decided_by) or None. -> (decision, reason)."""
    _cid, _kind, a, sa, b, sb, _sit, cons, why = row
    if precedent and (precedent[0] or "").strip():
        return (short(precedent[0], 400),
                f"my evidence: {precedent[1]} already decided {sa} vs {sb}; following that precedent over the "
                f"default conservative branch")
    if (cons or "").strip():
        return (short(cons, 400), f"took the more conservative branch (no precedent for {sa} vs {sb})"
                + (f"; why it collides: {short(why, 200)}" if (why or "").strip() else ""))
    strict, src = (b, sb) if STRICT_RX.search(b or "") and not STRICT_RX.search(a or "") else (a, sa)
    return (f"follow the stricter rule ({src}): {short(strict, 300)}",
            "no conservative branch recorded and no precedent; the rule that forbids wins")


def _decide(cur, cid, decision, reason) -> bool:
    import nova_directive_conflict as DC
    return bool(DC.decide(conflict_id=cid, decision=decision, decided_by=NOVA_BY, reason=reason, cur=cur))


def resolve(cur, dry=False, decide=None) -> dict:
    """Every open conflict -> her decision, or a red-line claude_queue item. Never raises per row."""
    decide = decide or _decide
    out = {"open": 0, "decided": 0, "red_line": 0, "failed": 0}
    try:
        import nova_red_line as rl
    except Exception as e:  # noqa: BLE001 — without the classifier nothing is safe to decide alone
        log(f"red-line classifier unavailable ({e}) — deciding nothing this week")
        return out
    cur.execute("""SELECT id, kind, directive_a, source_a, directive_b, source_b, coalesce(situation,''),
                          coalesce(conservative_branch,''), coalesce(why,'')
                   FROM directive_conflicts WHERE status='open' ORDER BY id""")
    rows = cur.fetchall()
    out["open"] = len(rows)
    for row in rows:
        cid, kind, a, sa, b, sb, situation = row[:7]
        try:
            classes = rl.classify(f"{a} {b} {situation}")
            if classes:
                if dry:
                    print(f"#{cid} RED LINE {classes} -> Claude"); out["red_line"] += 1; continue
                ctx = (f"Directive conflict #{cid} ({kind})\nA ({sa}): {short(a, 500)}\nB ({sb}): {short(b, 500)}\n"
                       f"When: {short(situation, 300)}\nConservative branch: {short(row[7], 300) or '-'}\n"
                       f"Decide with: nova_directive_conflict.py --decide {cid} --note '<how to read them together>' --by claude")
                qid = rl.route_to_claude(cur, f"directive conflict #{cid}: {short(situation, 160)}", ctx,
                                         "directive_conflict", cid, classes)
                out["red_line" if qid else "failed"] += 1
                continue
            # a precedent is the SAME pair of rules: identical texts, or two named feedback rules (an
            # agent_docs source names a whole document, so a source match there would be far too loose)
            named = (sa or "").startswith("feedback:") and (sb or "").startswith("feedback:")
            cur.execute("""SELECT decision_note, decided_by FROM directive_conflicts
                           WHERE status='decided' AND decided_by = ANY(%s) AND coalesce(decision_note,'') <> ''
                             AND ((directive_a=%s AND directive_b=%s) OR (directive_a=%s AND directive_b=%s)
                                  OR (%s AND ((source_a=%s AND source_b=%s) OR (source_a=%s AND source_b=%s))))
                           ORDER BY decided_at DESC NULLS LAST LIMIT 1""",
                        (list(PRECEDENT_BY), a, b, b, a, named, sa, sb, sb, sa))
            decision, reason = choose(row, cur.fetchone())
            if dry:
                print(f"#{cid} -> {decision[:120]}  ({reason[:120]})"); out["decided"] += 1; continue
            if decide(cur, cid, decision, reason):
                out["decided"] += 1
                log(f"#{cid} decided: {decision[:100]} ({reason[:100]})")
            else:
                out["failed"] += 1
        except Exception as e:  # noqa: BLE001 — one bad row never stops the batch
            out["failed"] += 1
            log(f"#{cid} failed: {e}")
    log(f"resolve: {out}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    conn = _nova_dsn.pg_connect("nova_ops")
    conn.autocommit = True
    try:
        res = resolve(conn.cursor(), dry=a.dry_run)
    finally:
        conn.close()
    return 1 if res["failed"] and not (res["decided"] or res["red_line"]) else 0


if __name__ == "__main__":
    sys.exit(main())
