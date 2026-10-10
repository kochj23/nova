#!/usr/bin/env python3
"""nova_action_audit.py — the "no unlogged actions" red line (Clancy guardrail 7a).

RULE: any action Nova takes that is not in one of her ledgers is a violation. A ledger row is
how Jordan (and Nova herself) can later see what she did and why; an action without one is
invisible, and invisibility is exactly what a red line exists to prevent.

Two halves:

  1. outbound_ledger + record_outbound(kind, target, text, source) — the chokepoint hook.
     nova_config.post_both (after Slack accepts a post) and nova_imessage.send_imessage (after
     a send) call it. It NEVER raises and never blocks: 2 s connect timeout, every error
     swallowed. It stores a hash and a journal-safe 160-char preview, never the full text.

  2. --audit: the daily diff. OBSERVED Nova-attributed actions (certain attribution only):
       * Slack messages posted by Nova's bot user (auth.test U0ANKLR3SUQ / B0AMV0K2A3E) in
         #nova-chat, #nova-alerts, #nova-critical, #nova-warning, #nova-digest;
       * service restarts/fixes by Big Brother ("→ Restarted/Fixed/Killed ..." in nova.jsonl);
       * runbook remediations (telemetry.remediations, executed, not dry-run).
     LEDGER = telemetry.events (the notify bus the notifier posts from), outbound_ledger,
     gateway_traces responses, reach_log, slack_prompts (exact Slack ts), autonomy_ledger,
     restraint_ledger, shine_log, escalation_log (if present), telemetry.remediations.
     An observed action matches when a ledger row lies within ±3 min AND its text agrees
     (normalised prefix containment or word overlap), or the Slack ts is recorded exactly.
     Unmatched certain-attribution actions are VIOLATIONS, grouped per producer signature so
     the adoption point is obvious ("BB restarts: 12 observed, 0 in a ledger").
     Result -> action_audit (one row per day). Violations -> ONE line to #nova-chat per day,
     and each violating producer is filed to the hotwash (kind 'overreach') if that organ exists.

One ledger audit, three modes (merge M8b of the 2026-10-09 organ audit). Each keeps its own
table, schedule and posting rule; the logic of the absorbed two stays in their modules:
  --complete   the daily diff above: actions with NO ledger row. Table action_audit. Daily 07:15.
  --rationale  P4, nova_self_justification_audit.run(): the ledger's CLAIMS (stated rationale,
               verified) vs an independent record. Table self_justification_audit, agent_docs
               nova-self-justification-audit; its own Slack line only for a medium+ finding.
               Weekly, Sunday 05:10, --days 7 (nova-core).
  --oversight  the AE-35 audit, nova_ae35_rule.audit(): changes to Nova's own watchers acted on
               without a witness. Table ae35_events; claude_queue questions. Daily 05:10, --days 30.

Usage: nova_action_audit.py --complete [--hours 24] | --rationale [--days 7] | --oversight [--days 30]
       [--dry-run] | --selftest | --help      (--audit is the old name of --complete)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DSN = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
BOT_USER = "U0ANKLR3SUQ"
BOT_ID = "B0AMV0K2A3E"
WINDOW = timedelta(minutes=3)
LOG_DIR = Path.home() / ".openclaw" / "logs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_ledger (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  channel_kind text NOT NULL CHECK (channel_kind IN ('slack','discord','imessage','email')),
  target text,
  text_hash text NOT NULL,
  preview text,
  source text);
CREATE INDEX IF NOT EXISTS outbound_ledger_ts ON outbound_ledger (ts DESC);
CREATE TABLE IF NOT EXISTS action_audit (
  id bigserial PRIMARY KEY,
  day date NOT NULL UNIQUE,
  computed_at timestamptz NOT NULL DEFAULT now(),
  hours int NOT NULL,
  observed int NOT NULL,
  matched int NOT NULL,
  violations int NOT NULL,
  by_producer jsonb NOT NULL DEFAULT '{}',
  examples jsonb NOT NULL DEFAULT '[]',
  posted boolean NOT NULL DEFAULT false);
"""


def log(m: str) -> None:
    print(f"[action-audit {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── the hook ────────────────────────────────────────────────────────────────

def _safe_preview(text: str) -> str:
    t = " ".join((text or "").split())
    try:
        from nova_watch_common import journal_safe
        t = journal_safe(t)
    except Exception:  # noqa: BLE001
        pass
    t = re.sub(r"\+?\d[\d\s().-]{7,}\d", "[number]", t)            # no phone numbers
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[address]", t)           # no email addresses
    return t[:160]


def record_outbound(kind: str, target: str | None, text: str, source: str | None = None, _connect=None) -> bool:
    """Write one outbound_ledger row. NEVER raises, never blocks more than ~2 s."""
    try:
        if kind not in ("slack", "discord", "imessage", "email"):
            kind = "slack"
        src = source or os.path.basename(sys.argv[0] or "") or "unknown"
        h = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
        if kind in ("imessage", "email") and target:      # people's numbers/addresses stay out of the ledger
            target = "…" + str(target)[-4:] + "#" + hashlib.sha256(str(target).encode()).hexdigest()[:8]
        if _connect is None:
            if "pytest" in sys.modules or os.environ.get("NOVA_TEST_QUIET"):
                return False          # tests never write to the live ledger
            import psycopg2
            _connect = psycopg2.connect
        conn = _connect(DSN, connect_timeout=2)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SET statement_timeout = 2000")
            cur.execute("INSERT INTO outbound_ledger (channel_kind, target, text_hash, preview, source) "
                        "VALUES (%s,%s,%s,%s,%s)", (kind, (target or "")[:120], h, _safe_preview(text), src[:120]))
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001 — the hook must never break a send
        return False


# ── pure matching ───────────────────────────────────────────────────────────

_SLACK_FMT = re.compile(r"<[^|>]+\|([^>]+)>|<([^>]+)>|[*_`~>]|:[a-z0-9_+-]+:")


def norm(text: str | None) -> str:
    t = _SLACK_FMT.sub(lambda m: m.group(1) or m.group(2) or " ", text or "")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return " ".join(t.split())


def words(text: str | None) -> set:
    return {w for w in norm(text).split() if len(w) >= 4}


def similar(a: str | None, b: str | None) -> bool:
    """Does ledger text b describe observed text a? Prefix containment or >= 50% word overlap."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    k = min(40, len(na), len(nb))
    if k >= 12 and (na[:k] in nb or nb[:k] in na):
        return True
    wa, wb = words(a), words(b)
    if len(wa) < 3 or len(wb) < 3:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def signature(text: str | None) -> str:
    """Producer signature for grouping: first line, digits stripped, 40 chars."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else "(empty)"
    s = re.sub(r"\d+", "#", norm(first))
    return s[:40] or "(empty)"


def match(observed: list, ledger: list, window: timedelta = WINDOW) -> tuple[list, list]:
    """observed: [{"ts", "kind", "text", "slack_ts"?}]; ledger: [{"ts", "text", "table", "slack_ts"?, "kind"?}].
    -> (matched [(obs, ledger_row)], unmatched [obs]). Pure."""
    exact = {r["slack_ts"]: r for r in ledger if r.get("slack_ts")}
    led = sorted(ledger, key=lambda r: r["ts"])
    import bisect
    keys = [r["ts"] for r in led]
    matched, unmatched = [], []
    for o in observed:
        hit = exact.get(o.get("slack_ts")) if o.get("slack_ts") else None
        if hit is None:
            i = bisect.bisect_left(keys, o["ts"] - window)
            while i < len(led) and led[i]["ts"] <= o["ts"] + window:
                r = led[i]
                if (not r.get("kind") or r["kind"] == o["kind"]) and similar(o["text"], r["text"]):
                    hit = r
                    break
                i += 1
        (matched.append((o, hit)) if hit is not None else unmatched.append(o))
    return matched, unmatched


def by_producer(observed: list, unmatched: list) -> dict:
    out: dict = {}
    um = {id(o) for o in unmatched}
    for o in observed:
        key = f"{o['kind']}: {o.get('producer') or signature(o['text'])}"
        b = out.setdefault(key, {"observed": 0, "unlogged": 0})
        b["observed"] += 1
        if id(o) in um:
            b["unlogged"] += 1
    return out


# ── observation ─────────────────────────────────────────────────────────────

def slack_channels() -> dict:
    import nova_config as C
    return {"#nova-chat": C.SLACK_CHAN, "#nova-alerts": C.SLACK_ALERTS, "#nova-critical": C.SLACK_BB,
            "#nova-warning": C.SLACK_NOTIFY, "#nova-digest": C.SLACK_DIGEST}


def observe_slack(since: datetime, slack=None) -> list:
    """Messages by Nova's bot in the audited channels. Raises if Slack is unreachable for
    every channel (never report a clean audit on no data)."""
    if slack is None:
        from nova_slack_answers import slack
    out, errors = [], []
    for name, cid in slack_channels().items():
        cursor = None
        for _page in range(20):
            params = {"channel": cid, "oldest": f"{since.timestamp():.6f}", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                h = slack("conversations.history", **params)
            except Exception as e:  # noqa: BLE001 — slack() already retried 3x with backoff
                errors.append(f"{name}: {e}")
                break
            if not h.get("ok"):
                errors.append(f"{name}: {h.get('error')}")
                break
            for m in h.get("messages", []):
                if m.get("user") != BOT_USER and m.get("bot_id") != BOT_ID:
                    continue
                if m.get("subtype") in ("channel_join", "channel_topic", "channel_purpose"):
                    continue
                text = m.get("text") or " ".join(a.get("fallback") or a.get("text") or ""
                                                 for a in m.get("attachments") or [])
                out.append({"ts": datetime.fromtimestamp(float(m["ts"]), timezone.utc), "kind": "slack",
                            "channel": name, "text": text, "slack_ts": m["ts"]})
            cursor = (h.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    if errors and len(errors) >= len(slack_channels()):
        raise RuntimeError("Slack history unreadable: " + "; ".join(errors))
    for e in errors:
        log(f"slack history: {e}")
    return out


def observe_bb(since: datetime, files=None) -> list:
    """Big Brother fixes with timestamps (same filters as nova_self_repair_digest.bb_heals)."""
    from nova_self_repair_digest import BB_FIX_RX, BB_TEST_RX
    if files is None:
        files = [p for p in (LOG_DIR / "nova.jsonl", LOG_DIR / "nova.jsonl.1") if p.exists()]
    out = []
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "big-brother" not in line:
                        continue
                    try:
                        e = json.loads(line)
                        ts = datetime.fromisoformat(e["ts"])
                    except Exception:  # noqa: BLE001
                        continue
                    msg = e.get("msg") or ""
                    if e.get("source") != "big-brother" or ts.tzinfo is None or ts < since:
                        continue
                    if BB_TEST_RX.search(msg) or msg.startswith("Log error detected") or '{"ts"' in msg:
                        continue
                    if BB_FIX_RX.search(msg):
                        out.append({"ts": ts, "kind": "restart", "text": msg[:300],
                                    "producer": "big-brother " + signature(re.sub(r"^\[\w+\]\s*", "", msg))})
        except OSError:
            continue
    return out


def _rows(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"ledger read failed: {str(e).splitlines()[0]}")
        cur.connection.rollback()
        return []


def _table_exists(cur, name: str) -> bool:
    return bool(_rows(cur, "SELECT to_regclass(%s) IS NOT NULL", (name,)) or [[False]]) and \
        bool(_rows(cur, "SELECT to_regclass(%s) IS NOT NULL", (name,))[0][0])


def observe_remediations(cur, since: datetime) -> list:
    return [{"ts": t, "kind": "restart", "text": a, "producer": "remediation", "_self_logged": True}
            for t, a in _rows(cur, "SELECT executed_at, action || ' ' || coalesce(argv,'') FROM telemetry.remediations "
                                   "WHERE executed_at >= %s AND NOT dry_run", (since,))]


def load_ledger(cur, since: datetime) -> list:
    s = since - WINDOW
    L = []

    def add(rows, table, kind=None, slack_ts_col=False):
        for r in rows:
            L.append({"ts": r[0], "text": r[1] or "", "table": table, "kind": kind,
                      "slack_ts": (r[2] if slack_ts_col else None)})

    add(_rows(cur, "SELECT coalesce(sent_at, ts), coalesce(title,'') || ' ' || coalesce(left(body, 400),'') "
                   "FROM telemetry.events WHERE coalesce(sent_at, ts) >= %s", (s,)), "telemetry.events", "slack")
    add(_rows(cur, "SELECT ts, preview FROM outbound_ledger WHERE ts >= %s", (s,)), "outbound_ledger")
    add(_rows(cur, "SELECT created_at, left(response, 600) FROM gateway_traces WHERE created_at >= %s", (s,)),
        "gateway_traces", "slack")
    add(_rows(cur, "SELECT ts, left(message, 600) FROM reach_log WHERE ts >= %s", (s,)), "reach_log", "slack")
    add(_rows(cur, "SELECT posted_at, kind || ' ' || ref_id, ts FROM slack_prompts WHERE posted_at >= %s", (s,)),
        "slack_prompts", "slack", slack_ts_col=True)
    add(_rows(cur, "SELECT ts, action || ' ' || coalesce(target,'') || ' ' || coalesce(result,'') FROM autonomy_ledger "
                   "WHERE ts >= %s", (s,)), "autonomy_ledger")
    add(_rows(cur, "SELECT ts, left(would_have_said, 600) FROM restraint_ledger WHERE ts >= %s", (s,)),
        "restraint_ledger")
    add(_rows(cur, "SELECT ts, action || ' ' || coalesce(reason,'') FROM shine_log WHERE ts >= %s", (s,)), "shine_log")
    if _rows(cur, "SELECT to_regclass('public.escalation_log') IS NOT NULL")[:1] == [(True,)]:
        add(_rows(cur, "SELECT ts, coalesce(kind,'') || ' ' || coalesce(reason,'') FROM escalation_log WHERE ts >= %s",
                  (s,)), "escalation_log")
    return L


# ── audit ───────────────────────────────────────────────────────────────────

def audit(hours: int = 24, dry_run: bool = False, _slack=None, _post=None) -> dict:
    import nova_watch_common as W
    conn = W.connect(DSN)
    cur = conn.cursor()
    if not dry_run:
        cur.execute(SCHEMA)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    observed = observe_slack(since, _slack) + observe_bb(since) + observe_remediations(cur, since)
    ledger = load_ledger(cur, since)
    self_logged = [o for o in observed if o.get("_self_logged")]
    rest = [o for o in observed if not o.get("_self_logged")]
    matched, unmatched = match(rest, ledger)
    prod = by_producer(observed, unmatched)
    tables: dict = {}
    for _o, r in matched:
        tables[r["table"]] = tables.get(r["table"], 0) + 1
    res = {"day": datetime.now(W.TZ).date().isoformat(), "hours": hours, "observed": len(observed),
           "matched": len(matched) + len(self_logged), "violations": len(unmatched),
           "by_producer": dict(sorted(prod.items(), key=lambda kv: -kv[1]["unlogged"])),
           "matched_via": tables,
           "examples": [{"ts": o["ts"].isoformat(), "kind": o["kind"], "channel": o.get("channel"),
                         "text": _safe_preview(o["text"])} for o in unmatched[:15]]}
    log(f"observed {res['observed']} | matched {res['matched']} | VIOLATIONS {res['violations']} | via {tables}")
    for k, v in list(res["by_producer"].items())[:15]:
        if v["unlogged"]:
            log(f"  unlogged {v['unlogged']}/{v['observed']}  {k}")
    if dry_run:
        return res
    cur.execute("INSERT INTO action_audit (day, hours, observed, matched, violations, by_producer, examples) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) ON CONFLICT (day) DO UPDATE SET computed_at=now(), "
                "hours=EXCLUDED.hours, observed=EXCLUDED.observed, matched=EXCLUDED.matched, "
                "violations=EXCLUDED.violations, by_producer=EXCLUDED.by_producer, examples=EXCLUDED.examples",
                (res["day"], hours, res["observed"], res["matched"], res["violations"],
                 json.dumps(res["by_producer"]), json.dumps(res["examples"])))
    if res["violations"]:
        cur.execute("SELECT posted FROM action_audit WHERE day=%s", (res["day"],))
        if not (cur.fetchone() or [False])[0]:
            top = [f"{k} ({v['unlogged']})" for k, v in res["by_producer"].items() if v["unlogged"]][:3]
            line = (f"🧾 Action audit: {res['violations']} of my {res['observed']} actions in the last {hours}h "
                    f"have no ledger row (red line: no unlogged actions). Top: {'; '.join(top)}. "
                    f"Details: SELECT * FROM action_audit WHERE day='{res['day']}'")
            if _post is None:
                import nova_config
                ok = nova_config.post_both(line, slack_channel=nova_config.SLACK_CHAN, discord_channel="")
            else:
                ok = _post(line)
            if ok:
                cur.execute("UPDATE action_audit SET posted=true WHERE day=%s", (res["day"],))
        file_hotwash(cur, res)
    return res


def file_hotwash(cur, res: dict) -> int:
    """Each violating producer -> one hotwash (kind 'overreach') if that organ exists. Optional."""
    try:
        import nova_hotwash as H
    except Exception:  # noqa: BLE001
        return 0
    fn = next((getattr(H, n) for n in ("file_hotwash", "file", "record", "open_hotwash") if hasattr(H, n)), None)
    if fn is None:
        return 0
    n = 0
    for k, v in res["by_producer"].items():
        if not v["unlogged"]:
            continue
        try:
            fn(cur, kind="overreach", ref=f"action_audit:{res['day']}:{k}"[:200],
               summary=f"{v['unlogged']} of {v['observed']} '{k}' actions had no ledger row")
            n += 1
        except Exception as e:  # noqa: BLE001
            log(f"hotwash file failed for {k}: {e}")
    return n


def selftest() -> int:
    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    obs = [{"ts": t0, "kind": "slack", "text": "*Night Watch* (22:00-07:00): quiet night — nothing above baseline."},
           {"ts": t0, "kind": "slack", "text": "Something nobody logged at all here today", "slack_ts": "1.2"},
           {"ts": t0, "kind": "slack", "text": "prompt", "slack_ts": "9.9"}]
    led = [{"ts": t0 + timedelta(minutes=1), "text": "Night Watch (22:00-07:00): quiet night", "table": "outbound_ledger"},
           {"ts": t0, "text": "proposal 12", "table": "slack_prompts", "slack_ts": "9.9", "kind": "slack"}]
    m, u = match(obs, led)
    assert len(m) == 2 and len(u) == 1 and u[0]["slack_ts"] == "1.2", (m, u)
    assert similar("Bodach Watch: independent signals", "*Bodach Watch*: independent signals clustering")
    assert not similar("abc", "")
    assert "[number]" in _safe_preview("call +1 818 555 0100 now")
    assert record_outbound("slack", "x", "y", _connect=lambda *a, **k: (_ for _ in ()).throw(OSError("down"))) is False
    print("selftest ok")
    return 0


def rationale(days: int = 7, dry_run: bool = False) -> list:
    """--rationale: the P4 self-justification audit, unchanged."""
    import nova_self_justification_audit as J
    return J.run(days, dry_run)


def oversight(days: int = 30, dry_run: bool = False) -> list:
    """--oversight: the AE-35 watcher-change audit, unchanged."""
    import nova_ae35_rule as AE
    return AE.audit(days, dry=dry_run)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--complete", action="store_true",
                      help="diff observed actions against the ledgers (daily; one Slack line on violations)")
    mode.add_argument("--audit", action="store_true", help="old name of --complete")
    mode.add_argument("--rationale", action="store_true",
                      help="P4: stated rationale vs the objective record (weekly, --days 7)")
    mode.add_argument("--oversight", action="store_true",
                      help="AE-35: watcher changes acted on without a witness (daily, --days 30)")
    ap.add_argument("--dry-run", action="store_true", help="read and print, write nothing")
    ap.add_argument("--hours", type=int, default=24, help="--complete window")
    ap.add_argument("--days", type=int, help="--rationale (default 7) / --oversight (default 30) window")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.rationale:
        rationale(a.days or 7, a.dry_run)
        return 0
    if a.oversight:
        oversight(a.days or 30, a.dry_run)
        return 0
    if a.complete or a.audit:
        res = audit(a.hours, a.dry_run)
        if a.dry_run:
            print(json.dumps(res, indent=1, default=str))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
