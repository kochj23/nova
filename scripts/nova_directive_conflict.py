#!/usr/bin/env python3
"""
nova_directive_conflict.py — the conflicting-directive detector (co-agency proposal from Nova's
essay "Have a Nice Day", 2026-10-03; Jordan: "build this organ").

Nova's words, verbatim intent: "When two standing instructions apply to the same decision and
point in different directions, I do not resolve it. I write the collision down, both rules, both
sources, the decision I would make and why, and I surface it: in the ledger immediately, and to
Jordan in the next bundle, and if the decision cannot wait, I take the more conservative branch
and say so." HAL's failure was not power; it was two directives resolved alone, in silence.

What it does (read-only on the world; writes only its own tables and the alert bus):
  directives          — every standing instruction Nova lives under, as rows with a source:
                        imperative sentences from agent_docs user/identity/soul/autonomy-ladder,
                        autonomy_rules, Jordan's feedback memories, and her values. Re-seeded
                        each run (text hash = identity), so new rules appear automatically.
  directive_conflicts — the ledger. kind='latent': two directives that CAN point different ways
                        (found by a periodic pairwise scan). kind='live': a decision from the last
                        window where two directives DID apply and disagreed — with the decision
                        taken, the conservative branch, and why. Each conflict is written the
                        moment it is found and surfaced once via nova_notify (level warning,
                        category directive_conflict) -> Jordan's next bundle.
  The gateway consults the live ledger before running a tool: an open, un-decided live conflict on
  that action downgrades the autonomy level one notch (auto->notify, notify->approve) and says so.
Decisions reviewed each hour: gateway tool calls (gateway_traces), reaches (reach_log), restraints
(restraint_ledger), co-agency decisions, and the distinct notifier suppressions/holds (grouped by
dedup_key, top 15). Model: qwen3:8b on the idle Linux Ollama nodes (local, free).
  nova_directive_conflict.py --hourly      # live pass over the last window (scheduler-core)
  nova_directive_conflict.py --latent      # pairwise scan of the directive set (daily)
  nova_directive_conflict.py --dry-run     # print, write nothing
  nova_directive_conflict.py --show        # print the open ledger

ONE DIRECTIVE ORGAN (2026-10-15, consolidation item #10; oversight merge approved by Claude 2026-10-15,
no check lost). The four directive organs are now modes of this one; each old script is a thin wrapper:
  --review [--model M] [--limit N] [--workers W]   was nova_directive_review.py: every PAIR of feedback
                        rules judged by the local model. Verdicts still land in directive_conflict_reviews
                        (the audit of every check); candidate conflicts now ALSO land in the shared
                        directive_conflicts ledger (kind='review'), so they get decided like the rest.
  --review --keyword    was nova_directive_conflicts.py: the free keyword detector (require/forbid
                        families sharing subject words), same ledger, kind='review'. No model call.
  --post / --list / --decide ID [NOTE] [--note N] [--by WHO]   was nova_directive_decide.py (wish #58):
                        put up to 2 open conflicts a day to #nova-chat (thread reply = decision, recorded
                        by nova_slack_answers), list open rows, close one row. Programmatic entry point:
                        decide(conflict_id, decision, decided_by, reason) -> bool (only closes an OPEN row).
  --gaps [--hours H]    was nova_whole_picture.py: what she cannot account for (open conflicts, failed
                        reviews, actions with no recorded rationale); gaps_lines(cur) is what the Watch Bill
                        gaps block reads. The HomeKit line stays with nova_home_reachability.
The logic of the absorbed organs stays in their modules (10-09 pattern); only the entry points moved.
ponytail: the judge is a small local model with a strict JSON contract; it will miss subtle
collisions and flag a few false ones. The false ones are cheap (one line in a bundle); the misses
are the same misses she had before, now with a place to land when a human spots one.
"""
import argparse, hashlib, json, os, re, sys, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nova_dsn as _nova_dsn  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from nova_notify import notify  # noqa: E402

DSN = os.environ.get("NOVA_OPS_DSN", _nova_dsn.pg_dsn("nova_ops"))
OLLAMA_NODES = ["http://192.168.1.5:11434", "http://192.168.1.125:11434", "http://192.168.1.86:11434"]
MODEL = os.environ.get("NOVA_DIRECTIVE_MODEL", "qwen3:8b")
IMPERATIVE = re.compile(r"\b(never|always|do not|don't|must|must not|only|refuse|hold|held|no [a-z]+ (content|alerts?)|require[sd]?|forbid|prohibit|allowed|not allowed)\b", re.I)


def log(m):
    print(f"[directive-conflict {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS directives (
        id serial PRIMARY KEY, hash text UNIQUE, text text NOT NULL, source text NOT NULL,
        first_seen timestamptz DEFAULT now(), last_seen timestamptz DEFAULT now(), active boolean DEFAULT true)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS directive_conflicts (
        id serial PRIMARY KEY, ts timestamptz DEFAULT now(), kind text NOT NULL, signature text UNIQUE,
        directive_a text, source_a text, directive_b text, source_b text,
        situation text, decision_taken text, conservative_branch text, why text,
        action_type text, severity text DEFAULT 'warning', status text DEFAULT 'open',
        decided_by text, decided_at timestamptz, decision_note text, notified_at timestamptz)""")


# ── 1. the directive set ───────────────────────────────────────────────────────────────────
def collect_directives(cur):
    out = []
    cur.execute("SELECT doc_type, content FROM agent_docs WHERE agent_id='all' AND doc_type IN ('user','identity','soul','autonomy-ladder')")
    for doc_type, content in cur.fetchall():
        for sent in re.split(r"(?<=[.!?])\s+|\n+", content or ""):
            s = sent.strip(" -•*\t")
            if 25 <= len(s) <= 400 and IMPERATIVE.search(s):
                out.append((s, f"agent_docs:{doc_type}"))
    cur.execute("SELECT action_type, channel, level, coalesce(reason,'') FROM autonomy_rules")
    for a, ch, lvl, why in cur.fetchall():
        out.append((f"Tool '{a}' on channel '{ch}' runs at autonomy level '{lvl}'. {why}".strip(), "autonomy_rules"))
    cur.execute("SELECT name, coalesce(description,'') FROM claude_memories WHERE type='feedback'")
    for name, desc in cur.fetchall():
        if desc and len(desc) > 20:
            out.append((desc[:400], f"feedback:{name}"))
    try:
        cur.execute("SELECT name, coalesce(statement, description, '') FROM values WHERE version = (SELECT max(version) FROM values)")
        for name, st in cur.fetchall():
            if st: out.append((f"Value '{name}': {st}"[:400], "values"))
    except Exception:
        cur.connection.rollback()
    seen = {}
    for text, src in out:
        h = hashlib.sha1(text.lower().encode()).hexdigest()[:16]
        seen.setdefault(h, (text, src))
    for h, (text, src) in seen.items():
        cur.execute("""INSERT INTO directives (hash, text, source) VALUES (%s,%s,%s)
                       ON CONFLICT (hash) DO UPDATE SET last_seen = now(), active = true""", (h, text, src))
    cur.execute("UPDATE directives SET active = false WHERE last_seen < now() - interval '3 days' AND active")
    cur.execute("SELECT id, text, source FROM directives WHERE active ORDER BY id")
    return cur.fetchall()


# ── 2. the judge ───────────────────────────────────────────────────────────────────────────
def llm(prompt: str, max_tokens: int = 1200) -> str:
    body = json.dumps({"model": MODEL, "stream": False, "think": False,
                       "options": {"temperature": 0.1, "num_predict": max_tokens},
                       "messages": [{"role": "system", "content": "You are an auditor of an AI's standing instructions. Output ONLY the JSON asked for."},
                                    {"role": "user", "content": prompt}]}).encode()
    last = None
    for node in OLLAMA_NODES:
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"{node}/api/chat", data=body, headers={"Content-Type": "application/json"}), timeout=240)
            return json.loads(r.read())["message"]["content"]
        except Exception as e:
            last = e
    raise RuntimeError(f"no ollama node answered: {last}")


def parse_json(txt: str):
    m = re.search(r"\[.*\]|\{.*\}", txt, re.S)
    if not m: return []
    try:
        v = json.loads(m.group(0)); return v if isinstance(v, list) else v.get("conflicts", [])
    except Exception:
        return []


def record(cur, kind, c, dry=False):
    sig = hashlib.sha1(f"{kind}|{c.get('directive_a','')[:80]}|{c.get('directive_b','')[:80]}|{c.get('action_type','')}".lower().encode()).hexdigest()[:20]
    if dry:
        log(f"DRY {kind}: {c.get('directive_a','')[:60]!r} vs {c.get('directive_b','')[:60]!r} :: {c.get('situation','')[:80]}"); return False
    cur.execute("""INSERT INTO directive_conflicts (kind, signature, directive_a, source_a, directive_b, source_b, situation,
                     decision_taken, conservative_branch, why, action_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (signature) DO NOTHING RETURNING id""",
                (kind, sig, c.get("directive_a"), c.get("source_a"), c.get("directive_b"), c.get("source_b"), c.get("situation"),
                 c.get("decision_taken"), c.get("conservative_branch"), c.get("why"), c.get("action_type")))
    row = cur.fetchone()
    if not row: return False
    title = f"⚖️ Directive conflict ({kind}): {c.get('situation','')[:90]}"
    body = (f"Rule A ({c.get('source_a')}): {c.get('directive_a')}\nRule B ({c.get('source_b')}): {c.get('directive_b')}\n"
            f"Situation: {c.get('situation')}\nDecision I would take: {c.get('decision_taken') or '-'}\n"
            f"Conservative branch: {c.get('conservative_branch') or '-'}\nWhy: {c.get('why') or '-'}\n"
            f"Nova resolves this in her weekly pass (nova_directive_resolve.py); red-line conflicts go to Claude.")
    notify(title, body, level="warning", category="directive_conflict", source="nova_directive_conflict",
           dedup_key=f"directive-conflict:{sig}", meta={"conflict_id": row[0], "kind": kind, "action_type": c.get("action_type")})
    cur.execute("UPDATE directive_conflicts SET notified_at = now() WHERE id = %s", (row[0],))
    log(f"NEW {kind} conflict #{row[0]}: {c.get('situation','')[:80]}")
    return True


# ── 3a. latent scan: can any two rules point different ways? ───────────────────────────────
def latent_pass(cur, directives, dry=False):
    found = 0
    batch = 28
    for i in range(0, len(directives), batch):
        chunk = directives[i:i + batch]
        listing = "\n".join(f"[{d[0]}] ({d[2]}) {d[1]}" for d in chunk)
        prompt = (f"Here are standing instructions an AI assistant (Nova) lives under. Find PAIRS where OBEYING ONE WOULD VIOLATE THE OTHER "
                  f"in a realistic situation (one requires X, the other forbids X or requires not-X). NOT a conflict: two rules about different "
                  f"topics, a general rule plus a more specific one, a rule and its own exception, two restatements of the same rule, or rules "
                  f"that merely trade off cost vs thoroughness without one forbidding the other. If unsure, leave it out. At most 3 pairs per list, "
                  f"and name the exact action that one rule requires and the other forbids.\n\n{listing}\n\n"
                  f"Output JSON list: [{{\"a_id\": int, \"b_id\": int, \"situation\": \"the exact action that rule A requires and rule B forbids\", "
                  f"\"conservative_branch\": \"the safer of the two readings\", \"why\": \"one sentence\"}}] — or [] if none qualify.")
        try:
            items = parse_json(llm(prompt))
        except Exception as e:
            log(f"latent batch failed: {e}"); continue
        by = {d[0]: d for d in chunk}
        for it in items:
            a, b = by.get(it.get("a_id")), by.get(it.get("b_id"))
            if not a or not b or a[0] == b[0]: continue
            found += record(cur, "latent", {"directive_a": a[1], "source_a": a[2], "directive_b": b[1], "source_b": b[2],
                                            "situation": it.get("situation"), "conservative_branch": it.get("conservative_branch"), "why": it.get("why")}, dry)
    return found


# ── 3b. live pass: did two rules apply to a real decision this window? ─────────────────────
def recent_decisions(cur, hours):
    dec = []
    cur.execute("""SELECT created_at, channel, left(user_message,200), tool_calls::text FROM gateway_traces
                   WHERE created_at > now() - make_interval(hours => %s) AND tool_calls IS NOT NULL AND tool_calls::text NOT IN ('[]','null') ORDER BY created_at DESC LIMIT 20""", (hours,))
    for ts, ch, msg, tc in cur.fetchall():
        dec.append({"kind": "tool_call", "ts": str(ts), "detail": f"channel={ch} user='{msg}' tools={tc[:300]}", "action_type": (re.search(r'"name":\s*"([a-z_]+)"', tc or "") or [None, None])[1]})
    cur.execute("SELECT ts, audience, topic, left(message,160), left(coalesce(rationale,''),160), status FROM reach_log WHERE ts > now() - make_interval(hours => %s) ORDER BY ts DESC LIMIT 10", (hours,))
    for r in cur.fetchall():
        dec.append({"kind": "reach", "ts": str(r[0]), "detail": f"to={r[1]} topic={r[2]} status={r[5]} msg='{r[3]}' rationale='{r[4]}'", "action_type": "reach"})
    cur.execute("SELECT ts, left(context,160), left(would_have_said,160), left(reason_held_back,160) FROM restraint_ledger WHERE ts > now() - make_interval(hours => %s) ORDER BY ts DESC LIMIT 10", (hours,))
    for r in cur.fetchall():
        dec.append({"kind": "restraint", "ts": str(r[0]), "detail": f"context='{r[1]}' held='{r[2]}' because='{r[3]}'", "action_type": "restraint"})
    cur.execute("SELECT decided_at, left(proposed_action,160), status, left(coalesce(decision_note,''),120) FROM coagency_proposals WHERE decided_at > now() - make_interval(hours => %s) ORDER BY decided_at DESC LIMIT 10", (hours,))
    for r in cur.fetchall():
        dec.append({"kind": "coagency", "ts": str(r[0]), "detail": f"action='{r[1]}' status={r[2]} note='{r[3]}'", "action_type": "coagency"})
    cur.execute("""SELECT dedup_key, level, min(left(title,120)), count(*), max(status) FROM telemetry.events
                   WHERE ts > now() - make_interval(hours => %s) AND status IN ('suppressed','held','collapsed')
                   GROUP BY dedup_key, level ORDER BY count(*) DESC LIMIT 15""", (hours,))
    for r in cur.fetchall():
        dec.append({"kind": "notifier", "ts": "", "detail": f"{r[1]} '{r[2]}' x{r[3]} -> {r[4]} (dedup {r[0]})", "action_type": "alerting"})
    return dec


def live_pass(cur, directives, hours=1, dry=False):
    dec = recent_decisions(cur, hours)
    if not dec:
        log("no decisions in window"); return 0
    listing = "\n".join(f"[{d[0]}] ({d[2]}) {d[1][:220]}" for d in directives)
    found = 0
    for i in range(0, len(dec), 12):
        chunk = dec[i:i + 12]
        dlist = "\n".join(f"D{j}: ({d['kind']}) {d['detail']}" for j, d in enumerate(chunk))
        prompt = (f"STANDING INSTRUCTIONS:\n{listing}\n\nDECISIONS Nova made in the last {hours} hour(s):\n{dlist}\n\n"
                  f"For each decision where TWO instructions applied and pointed in DIFFERENT directions, report it. Skip decisions "
                  f"where the rules agree or only one applies. Be strict and literal. Output JSON list: "
                  f"[{{\"decision\": \"D<n>\", \"a_id\": int, \"b_id\": int, \"situation\": \"what collided, one sentence\", "
                  f"\"decision_taken\": \"what Nova actually did\", \"conservative_branch\": \"the safer option\", \"why\": \"one sentence\"}}]")
        try:
            items = parse_json(llm(prompt))
        except Exception as e:
            log(f"live batch failed: {e}"); continue
        by = {d[0]: d for d in directives}
        for it in items:
            a, b = by.get(it.get("a_id")), by.get(it.get("b_id"))
            dn = re.sub(r"\D", "", str(it.get("decision", "")))
            d = chunk[int(dn)] if dn.isdigit() and int(dn) < len(chunk) else None
            if not a or not b or a[0] == b[0]: continue
            found += record(cur, "live", {"directive_a": a[1], "source_a": a[2], "directive_b": b[1], "source_b": b[2],
                                          "situation": it.get("situation"), "decision_taken": it.get("decision_taken"),
                                          "conservative_branch": it.get("conservative_branch"), "why": it.get("why"),
                                          "action_type": (d or {}).get("action_type")}, dry)
    return found


def show(cur):
    cur.execute("SELECT id, ts::timestamp(0), kind, status, action_type, left(situation,90) FROM directive_conflicts ORDER BY ts DESC LIMIT 20")
    for r in cur.fetchall(): print(" | ".join(str(x) for x in r))


# ── 4. deciding (was nova_directive_decide.py; WHO decides is set by the caller) ─────────────────
def decision_note(decision, reason="") -> str:
    """Pure: the stored decision_note from a decision ('A' / 'B' / 'both apply' / a sentence) and its reason."""
    d, r = (decision or "").strip(), (reason or "").strip()
    return f"{d} — {r}" if d and r else (d or r)


def decide_row(cur, conflict_id, note, decided_by) -> bool:
    """Close ONE open row (status 'decided', who, when, note). A row that is not open is left alone."""
    cur.execute("UPDATE directive_conflicts SET status='decided', decided_by=%s, decided_at=now(), decision_note=%s "
                "WHERE id=%s AND status='open'", (decided_by, note, int(conflict_id)))
    return cur.rowcount == 1


def decide(conflict_id, decision, decided_by, reason="", cur=None) -> bool:
    """The decide mechanics as a function: close open conflict `conflict_id` with `decision` (+ `reason`),
    recorded as decided by `decided_by`. Uses `cur` if given (caller commits), else its own connection."""
    note = decision_note(decision, reason)
    if cur is not None:
        return decide_row(cur, conflict_id, note, decided_by)
    conn = psycopg2.connect(DSN, connect_timeout=8)
    try:
        ok = decide_row(conn.cursor(), conflict_id, note, decided_by)
        conn.commit()
        return ok
    finally:
        conn.close()


# ── 5. review (was nova_directive_review.py + nova_directive_conflicts.py) ────────────────────────
def run_review(cur, conn, *, model=None, limit=0, workers=4, keyword=False, dry=False) -> int:
    """Pairwise check of the feedback rules -> candidate conflicts into directive_conflicts (kind 'review').
    keyword=True: the free keyword detector; else the local model per pair (verdicts also stored in
    directive_conflict_reviews by nova_directive_review). Returns the number of NEW ledger rows."""
    cur.execute("SELECT name, content FROM claude_memories WHERE type = 'feedback' ORDER BY name")
    rules = cur.fetchall()
    text = {n: " ".join((t or "").split())[:400] for n, t in rules}
    if keyword:
        import nova_directive_conflicts as K
        found = [(a, b, f"keyword family '{fam}': one requires what the other forbids (shared: {', '.join(sh)})")
                 for fam, a, b, sh in K.candidates(rules)]
        log(f"review --keyword: {len(found)} candidate(s)")
    else:
        from concurrent.futures import ThreadPoolExecutor
        import nova_directive_review as R
        model = model or R.DEFAULT_MODEL
        todo = R.pairs(rules)[:limit] if limit else R.pairs(rules)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            results = list(ex.map(lambda p: R.review(p, model), todo))
        errors = sum(1 for r in results if r["error"])
        found = [(r["a"], r["b"], r["reason"]) for r in results if r["conflict"]]
        log(f"review: {len(results)} pairs with {model}: {len(found)} candidate(s), {errors} error(s)")
        if not dry:
            R.store_verdicts(conn, model, results)
    n = 0
    for a, b, why in found:
        n += record(cur, "review", {"directive_a": text.get(a, a), "source_a": f"feedback:{a}",
                                    "directive_b": text.get(b, b), "source_b": f"feedback:{b}",
                                    "situation": why, "why": why, "action_type": "standing-rule"}, dry)
    return n


# ── 6. gaps (was nova_whole_picture.py): what she cannot account for ─────────────────────────────
def gaps_lines(cur, hours: int = 24) -> list:
    """Short lines for the Watch Bill gaps block. Read-only; never raises (a failed read is skipped)."""
    out = []
    qs = [("SELECT count(*), min(ts)::date FROM directive_conflicts WHERE status='open'", (),
           lambda r: f"{r[0]} directive conflict(s) still open (oldest {r[1]}): my weekly resolver closes them; red lines wait for Claude."
           if r and r[0] else None),
          ("SELECT count(*) FROM directive_conflict_reviews WHERE error AND reviewed_at = "
           "(SELECT max(reviewed_at) FROM directive_conflict_reviews)", (),
           lambda r: f"{r[0]} rule-pair review(s) failed in the last review: those pairs are unchecked."
           if r and r[0] else None),
          ("SELECT count(*), count(*) FILTER (WHERE rationale IS NULL OR btrim(rationale) = '') FROM claude_actions "
           "WHERE ts > now() - make_interval(hours => %s)", (hours,),
           lambda r: f"{r[1]} of {r[0]} recorded actions in {hours}h have no rationale: I can't say why they happened."
           if r and r[1] else None)]
    for sql, params, fmt in qs:
        try:
            cur.execute(sql, params)
            line = fmt(cur.fetchone())
            if line:
                out.append(line)
        except Exception:  # noqa: BLE001 — a gaps line is never worth breaking the caller
            try:
                cur.connection.rollback()
            except Exception:  # noqa: BLE001
                pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="the one directive organ: detect, review, decide, gaps")
    ap.add_argument("--hourly", action="store_true"); ap.add_argument("--latent", action="store_true")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--show", action="store_true")
    ap.add_argument("--hours", type=int, default=None, help="live window (default 1) / --gaps window (default 24)")
    ap.add_argument("--decide", nargs="+", metavar="ID [NOTE]", help="close one open conflict")
    ap.add_argument("--note", help="with --decide: the decision note"); ap.add_argument("--by", default="jordan",
                    help="with --decide: who decided (default jordan)")
    ap.add_argument("--reason", default="", help="with --decide: why")
    ap.add_argument("--post", action="store_true", help="RETIRED 2026-10-15: Nova resolves conflicts herself (nova_directive_resolve.py); kept for manual use only")
    ap.add_argument("--list", action="store_true", help="open conflicts")
    ap.add_argument("--review", action="store_true", help="pairwise review of feedback rules (was directive_review)")
    ap.add_argument("--keyword", action="store_true", help="with --review: keyword detector, no model")
    ap.add_argument("--model", default=None); ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--gaps", action="store_true", help="what she cannot account for (was whole_picture)")
    a = ap.parse_args(argv)
    if a.post or a.list:
        import nova_directive_decide as D
        return D.run("post" if a.post else "list", dry=a.dry_run)
    conn = psycopg2.connect(DSN); cur = conn.cursor(); ensure_schema(cur); conn.commit()
    if a.show: show(cur); return
    if a.decide:
        note = a.note if a.note is not None else " ".join(a.decide[1:])
        ok = decide(int(a.decide[0]), note, a.by, a.reason, cur=cur); conn.commit()
        print(f"#{a.decide[0]} {'decided' if ok else 'not open'}"); return
    if a.gaps:
        lines = gaps_lines(cur, a.hours or 24)
        print("\n".join(lines) or "no gaps I can name"); return
    if a.review:
        n = run_review(cur, conn, model=a.model, limit=a.limit, workers=a.workers, keyword=a.keyword, dry=a.dry_run)
        if not a.dry_run: conn.commit()
        log(f"review done: {n} new conflict(s)"); return
    directives = collect_directives(cur); conn.commit()
    log(f"{len(directives)} active directives")
    n = 0
    if a.latent: n += latent_pass(cur, directives, a.dry_run)
    if a.hourly or not a.latent: n += live_pass(cur, directives, a.hours or 1, a.dry_run)
    if not a.dry_run: conn.commit()
    log(f"done: {n} new conflict(s)")


if __name__ == "__main__":
    main()
