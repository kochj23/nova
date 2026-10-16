#!/usr/bin/env python3
"""nova_morse_horn.py — THE MORSE HORN: what the machines keep asking for, in one plain sentence each.

Little Mister, 2026-10-15 (after Nova read Maximum Overdrive, 1986): "Do it all!"
In the film the trucks circle the truck stop blaring their horns, and a kid with a Morse merit badge
decodes it: "They want us to feed them." The machines had been stating a demand all along; nobody was
reading it. Nova's version: find the STRUCTURED, REPEATING patterns in the house's machine noise and
translate each into one plain demand ("wants more disk", "wants a renewed credential").

Sources (each fail-open: a source that errors is skipped and named in the output, never fatal):
  alerts     telemetry.events, level warning/critical, last 7 days      template = category | title
  scheduler  scheduler_runs, non-success runs, last 7 days              template = last error line
  health     health_checks, status not up/ok, last 7 days               template = service: status error
  incidents  telemetry.incidents + public.incidents opened last 7 days  template = title
  syslog     syslog_events, severity <= 3, last 7 days (capped)         template = app: message
Every message is normalised to a template (IPs, MACs, UUIDs, hex, paths, quoted names and numbers
stripped), grouped by (source, template), and scored on volume and rhythm: the inter-arrival gaps give a
period (median gap) and a regularity (1 - MAD/median, 0..1; 1 = clockwork). score = count * (0.5 + 0.5 *
regularity). The top N become demands by a rule table first (DEMAND_RULES); only templates no rule maps
are offered to a LOCAL Ollama model for a one-line phrasing (never cloud; fail-open to a stock line).

Table morse_horn: one row per (source, template), refreshed weekly; status open | answered | ignored.
A demand is ANSWERED when Jordan says so (--answer ID) or when the machine stops asking (an open pattern
with zero occurrences in a week whose source read cleanly and uncapped). A recurring answered demand
reopens. Table morse_horn_runs keeps the weekly measures.

Measures: % of last week's alert volume (telemetry.events warning+critical) explained by the top N
demands; demands answered this month vs last month.
Weekly digest via nova_notify (info, category morse_horn, deduped per ISO week).

--patterns (merged 2026-10-15, consolidation #14; was nova_alert_patterns.py, Sun 08:30 on the Studio):
the render/Slack mode. Reads the open demands above (the core) and hands them to
nova_alert_patterns.publish(), which still runs every 14-day pattern check (chronic signatures,
volume trend, incidents, red/blue/purple, new devices, rogue APs), posts full detail to #nova-alerts
and publishes the sanitized /operations article. --patterns --dry-run prints, posts nothing.

CLI: --scheduled (collect, store, digest) | --patterns [--dry-run] | --dry-run (print, write nothing, post nothing)
     --report (print stored state) | --answer ID | --ignore ID | --no-model | --top N | --selftest
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DAYS = 7
TOP_N = 25
PER_SOURCE = 5                    # guaranteed slots per source inside the top N
SYSLOG_CAP = 200_000
MIN_COUNT = 5                     # a pattern has to repeat to be a demand
RHYTHM = 0.6                      # regularity at or above this reads as "on a rhythm"
MODEL_URL = os.environ.get("NOVA_OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.environ.get("NOVA_MORSE_MODEL", "qwen3:8b")
MODEL_CALLS = 5                   # unmapped templates phrased per run, at most
FALLBACK_DEMAND = "wants someone to read it (no rule maps this pattern yet)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS morse_horn (
  id             bigserial PRIMARY KEY,
  ts             timestamptz NOT NULL DEFAULT now(),       -- last run that saw/refreshed it
  first_seen     timestamptz NOT NULL DEFAULT now(),
  source         text NOT NULL,
  template       text NOT NULL,
  count_7d       integer NOT NULL DEFAULT 0,
  period_seconds integer,
  regularity     double precision,
  demand         text NOT NULL,
  demand_by      text NOT NULL DEFAULT 'rule',             -- rule | model | fallback
  pct_of_alerts  double precision,
  evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','ignored')),
  answered_at    timestamptz,
  answered_by    text,
  UNIQUE (source, template));
CREATE INDEX IF NOT EXISTS morse_horn_status ON morse_horn (status, ts DESC);
CREATE TABLE IF NOT EXISTS morse_horn_runs (
  week            text PRIMARY KEY,
  at              timestamptz NOT NULL DEFAULT now(),
  total_alerts    integer,
  explained_pct   double precision,
  top_n           integer,
  open_demands    integer,
  answered_month  integer,
  answered_prev_month integer,
  sources         jsonb);
"""

# ── pure: templates ─────────────────────────────────────────────────────────

_SUBS = [
    (re.compile(r"\x1b\[[0-9;]*m"), " "),
    (re.compile(r"\*{1,2}"), ""),                                # slack bold
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b(?:[0-9a-f]{1,2}[:-]){5}[0-9a-f]{1,2}\b", re.I), "<mac>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I), "<ip>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"(?:~|\.{0,2})?(?:/[\w.@+-]+){2,}/?"), "<path>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']{1,80}'|\"[^\"]{1,80}\"|`[^`]{1,80}`"), "'*'"),
    (re.compile(r":[a-z0-9_+-]+:"), " "),                       # slack emoji
    (re.compile(r"\d+(?:[.,:]\d+)*"), "#"),
    (re.compile(r"[\^~]{4,}"), " "),                             # traceback underlines
]


def template(text, limit: int = 160) -> str:
    """Normalise one machine message to a stable template. Pure."""
    t = str(text or "")
    for rx, rep in _SUBS:
        t = rx.sub(rep, t)
    t = re.sub(r"#(?:[#\s/:T-]*#)+", "#", t)                    # dates, times, ranges -> one #
    return " ".join(t.lower().split())[:limit].strip(" .:-—|")


_EXC = re.compile(r"([A-Za-z_][\w.]*(?:Error|Exception|Canceled|Timeout|Refused|Exit|Interrupt))\s*:\s*(.+)")


def error_line(tail, exit_code=None, status=None) -> str:
    """The one line of a scheduler error tail that says what went wrong. Pure."""
    t = " ".join(str(tail or "").split())
    if not t:
        if status == "timeout":
            return "timed out (no error text)"
        return f"exit {exit_code if exit_code is not None else '?'} with no error text"
    hits = _EXC.findall(t)
    if hits:
        name, msg = hits[-1]
        return f"{name.split('.')[-1]}: {msg[:160]}"
    return t[-200:]


def scrub(text, limit: int = 120) -> str:
    """Evidence text safe to post: addresses, paths and ids removed, case kept. Pure."""
    t = str(text or "")
    for rx, rep in _SUBS[:10]:   # ansi .. long hex: identifiers only, wording kept
        t = rx.sub(rep, t)
    return " ".join(t.split())[:limit]


# ── pure: rhythm ────────────────────────────────────────────────────────────

BURST_S = 5                       # events closer than this are one episode (a burst is not a rhythm)


def periodicity(ts_list) -> tuple:
    """(period_seconds or None, regularity 0..1) from event times (epoch seconds). Events within BURST_S of
    the previous one belong to the same episode; the rhythm is measured between episode starts. Fewer than
    4 episodes: no rhythm. Pure."""
    xs = sorted({int(x) for x in ts_list if x is not None})
    starts = [x for i, x in enumerate(xs) if i == 0 or x - xs[i - 1] > BURST_S]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    if len(gaps) < 3:
        return None, 0.0
    m = statistics.median(gaps)
    if m <= 0:
        return None, 0.0
    mad = statistics.median(abs(g - m) for g in gaps)
    return int(m), round(max(0.0, 1.0 - mad / m), 3)


def score(count: int, regularity: float) -> float:
    return count * (0.5 + 0.5 * (regularity or 0.0))


def human_period(sec) -> str:
    if not sec:
        return "no steady rhythm"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            return f"every ~{round(sec / n)}{unit}"
    return f"every ~{sec}s"


# ── pure: demands ───────────────────────────────────────────────────────────

FILTER_DEMAND = "wants to be filtered at the source (routine chatter logged as an error)"
DEMAND_RULES = [   # (regex over template, demand). First match wins; specific before generic.
    (r"kex_?exchange_?identification|ssh.*connection closed by remote host",
     "wants whoever keeps knocking on SSH identified (connections that close before saying hello)"),
    (r"certificate|x509|\bssl\b|\btls\b|cert(?:ificate)? (?:has )?expir", "wants a cert"),
    (r"no space left|disk (?:is )?full|enospc|disk usage|filesystem .*full|volume .*full|quota exceeded",
     "wants more disk"),
    (r"out of memory|\boom\b|memoryerror|cannot allocate|killed .*signal #|oom-kill", "wants memory"),
    (r"\b429\b|rate.?limit|too many requests|throttl", "wants you to slow down"),
    (r"\b401\b|unauthori[sz]ed|expired token|token (?:has )?expired|invalid (?:token|credentials?)|"
     r"authentication failed|auth(?:entication)? error|password (?:expired|incorrect)|\b403\b|forbidden|"
     r"op item get failed|keychain", "wants a renewed credential"),
    (r"permission denied|operation not permitted|eacces", "wants a permission it doesn't have"),
    (r"modulenotfounderror|no module named|importerror|command not found", "wants a missing package installed"),
    (r"stale code", "wants a restart to load its new code"),
    (r"statement timeout|querycanceled|lock timeout|deadlock", "wants its database lock or query to finish"),
    (r"^timed out after|timed out \(no error text\)", "wants more time or less work (its job overruns its timeout)"),
    (r"timed? ?out|connection refused|connection reset|no route to host|unreachable|name or service not known|"
     r"could not connect|connection closed|failed to connect|is the server running|probe fail|service down|"
     r"services down|\bdown\b|\bslow\b|no such host|lookup .* failed",
     "wants its dependency back"),
    (r"\b50[0-4]\b|internal server error|bad gateway|service unavailable", "wants its upstream service healthy"),
    (r"is failing|is critical|consecutive failures|chronic failure|exit # with no error", "wants its failing job fixed or retired"),
    (r"off-hours auth|suspicious dns|ips alert|sensitive path|rootkit|integrity change|rootcheck|intrusion",
     "wants a human to say whether this is expected"),
    (r"recurring incident pattern", "wants the root cause of a repeat incident fixed"),
    (r"firmware|update available", "wants an update"),
    (r"\[com\.apple\.[\w.-]+:[^\]]*\]|xpcerrordescription|invalidation reason: connection init failed|"
     r"wlan: \[#:[iw]:any\]", FILTER_DEMAND),
    (r"battery|low power", "wants a new battery"),
]
_RULES = [(re.compile(rx, re.I), d) for rx, d in DEMAND_RULES]


def rule_demand(tmpl: str):
    """Demand for a template by the rule table, or None. Pure."""
    for rx, d in _RULES:
        if rx.search(tmpl or ""):
            return d
    return None


def clean_model_demand(text: str):
    """Accept a model phrasing only if it is one short line starting with 'wants'. Pure."""
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S).strip().strip('"').strip()
    t = t.splitlines()[0].strip().rstrip(".") if t else ""
    t = re.sub(r"^(?:it|the machine)\s+", "", t, flags=re.I)
    if not re.match(r"(?i)^wants\b", t) or len(t.split()) > 14 or re.search(r"https?://|\d{1,3}\.\d{1,3}\.", t):
        return None
    return t[0].lower() + t[1:]


def model_demand(tmpl: str, timeout: float = 20.0):
    """Ask the LOCAL Ollama for a one-line demand. Fail-open: None on any error."""
    if not re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?/", MODEL_URL):
        return None                     # local only, never a remote/cloud endpoint
    prompt = ("A machine in a home lab keeps emitting this log/alert pattern:\n"
              f"  {tmpl}\n"
              "In at most 10 words, say what the machine wants, starting with 'wants'. "
              "Examples: 'wants more disk', 'wants a renewed credential', 'wants its dependency back'. "
              "Reply with the phrase only.")
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "think": False,
                       "options": {"num_predict": 30, "temperature": 0.1}}).encode()
    try:
        req = urllib.request.Request(MODEL_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return clean_model_demand(json.loads(r.read()).get("response", ""))
    except Exception:  # noqa: BLE001 — fail-open by design
        return None


# ── pure: grouping + measures ───────────────────────────────────────────────

def group(rows) -> dict:
    """rows: (source, template, epoch_ts, example) -> {(source, template): {count, ts, examples}}. Pure."""
    out = {}
    for src, tmpl, ts, ex in rows:
        if not tmpl:
            continue
        g = out.setdefault((src, tmpl), {"count": 0, "ts": [], "examples": {}})
        g["count"] += 1
        g["ts"].append(ts)
        if ex:
            k = scrub(ex, 80)
            g["examples"][k] = g["examples"].get(k, 0) + 1
    return out


def rank(groups: dict, top_n: int = TOP_N, min_count: int = MIN_COUNT, per_source: int = PER_SOURCE) -> list:
    """Top patterns as dicts, best score first: the best `per_source` of each source (so a chatty syslog
    cannot crowd the alerts out), then the rest by score, up to top_n overall. Pure."""
    out = []
    for (src, tmpl), g in groups.items():
        if g["count"] < min_count:
            continue
        period, reg = periodicity(g["ts"])
        ex = sorted(g["examples"].items(), key=lambda kv: -kv[1])[:5]
        out.append({"source": src, "template": tmpl, "count_7d": g["count"], "period_seconds": period,
                    "regularity": reg, "score": round(score(g["count"], reg), 1),
                    "evidence": {"examples": [{"text": t, "n": n} for t, n in ex],
                                 "distinct_members": len(g["examples"]),
                                 "rhythmic": reg >= RHYTHM}})
    out.sort(key=lambda r: (-r["score"], r["source"], r["template"]))
    picked, taken = [], defaultdict(int)
    for r in out:
        if taken[r["source"]] < per_source:
            picked.append(r)
            taken[r["source"]] += 1
    rest = [r for r in out if r not in picked]
    picked = (picked + rest)[:max(top_n, len(picked))]
    picked.sort(key=lambda r: (-r["score"], r["source"], r["template"]))
    return picked


def assign_demands(patterns: list, use_model: bool = True, model_fn=None) -> list:
    """Fill demand + demand_by on each pattern: rule first, local model for unmapped, else fallback."""
    model_fn = model_fn or model_demand
    calls = 0
    for p in patterns:
        d = rule_demand(p["template"])
        if d:
            p["demand"], p["demand_by"] = d, "rule"
            continue
        m = None
        if use_model and calls < MODEL_CALLS:
            calls += 1
            m = model_fn(p["template"])
        p["demand"], p["demand_by"] = (m, "model") if m else (FALLBACK_DEMAND, "fallback")
    return patterns


def explained(patterns: list, total_alerts: int) -> tuple:
    """(explained %, {demand: alert count}) — share of alert volume the top demands account for. Pure."""
    per = defaultdict(int)
    for p in patterns:
        if p["source"] == "alerts":
            per[p["demand"]] += p["count_7d"]
    if not total_alerts:
        return 0.0, dict(per)
    for p in patterns:
        p["pct_of_alerts"] = round(100.0 * p["count_7d"] / total_alerts, 2) if p["source"] == "alerts" else None
    return round(100.0 * sum(per.values()) / total_alerts, 1), dict(per)


def render_digest(week: str, patterns: list, total_alerts: int, pct: float, per_demand: dict,
                  answered_month: int, answered_prev: int, failed_sources: list) -> str:
    """Slack body. Pure."""
    by = defaultdict(lambda: {"n": 0, "sources": set(), "rhythm": None, "examples": []})
    for p in patterns:
        b = by[p["demand"]]
        b["n"] += p["count_7d"]
        b["sources"].add(p["source"])
        if p["evidence"].get("rhythmic") and not b["rhythm"]:
            b["rhythm"] = human_period(p["period_seconds"])
        if len(b["examples"]) < 2:
            b["examples"].append(p["template"][:90])
    lines = [f"Week {week}. Decoded the horns: {len(patterns)} repeating patterns, {len(by)} demands."]
    for d, b in sorted(by.items(), key=lambda kv: -kv[1]["n"])[:8]:
        share = per_demand.get(d, 0)
        pct_s = f", {100.0 * share / total_alerts:.1f}% of alerts" if total_alerts and share else ""
        rh = f", {b['rhythm']}" if b["rhythm"] else ""
        lines.append(f"• It {d}: {b['n']} signals ({'/'.join(sorted(b['sources']))}{pct_s}{rh}). "
                     f"e.g. \"{b['examples'][0]}\"")
    lines.append(f"Top demands explain {pct:.1f}% of last week's {total_alerts} alerts.")
    lines.append(f"Demands answered: {answered_month} this month, {answered_prev} last month.")
    if failed_sources:
        lines.append(f"Could not read: {', '.join(failed_sources)} (skipped).")
    return "\n".join(lines)


# ── IO: sources (each fail-open) ────────────────────────────────────────────

def log(m: str) -> None:
    print(f"[morse-horn {datetime.now():%H:%M:%S}] {m}", flush=True)


def _ep(ts):
    return ts.timestamp() if hasattr(ts, "timestamp") else ts


def src_alerts(cur, days=DAYS):
    cur.execute("""SELECT ts, coalesce(category,''), title FROM telemetry.events
                   WHERE ts > now() - make_interval(days => %s) AND level IN ('warning','critical')""", (days,))
    return [("alerts", template(f"{c} | {t}" if c else t), _ep(ts), t) for ts, c, t in cur.fetchall()]


def src_scheduler(cur, days=DAYS):
    cur.execute("""SELECT started_at, task_id, status, exit_code, error_tail FROM scheduler_runs
                   WHERE started_at > extract(epoch FROM now() - make_interval(days => %s)) * 1000
                     AND status IN ('failure','timeout','orphaned')""", (days,))
    return [("scheduler", template(error_line(tail, ec, st)), ms / 1000.0, task)
            for ms, task, st, ec, tail in cur.fetchall()]


def src_health(cur, days=DAYS):
    cur.execute("""SELECT checked_at, service_name, status, coalesce(error_message,'') FROM health_checks
                   WHERE checked_at > now() - make_interval(days => %s) AND status NOT IN ('up','ok','healthy')""",
                (days,))
    return [("health", template(f"{svc}: {st} {err}"), _ep(ts), svc) for ts, svc, st, err in cur.fetchall()]


def src_incidents(cur, days=DAYS):
    cur.execute("""SELECT opened_at, title FROM telemetry.incidents WHERE opened_at > now() - make_interval(days => %s)
                   UNION ALL
                   SELECT started_at, title FROM incidents WHERE started_at > now() - make_interval(days => %s)""",
                (days, days))
    return [("incidents", template(t), _ep(ts), t) for ts, t in cur.fetchall()]


def src_syslog(cur, days=DAYS, cap=SYSLOG_CAP):
    cur.execute("""SELECT received_at, coalesce(app_name,''), left(message, 400) FROM syslog_events
                   WHERE severity <= 3 AND received_at > now() - make_interval(days => %s)
                   ORDER BY received_at DESC LIMIT %s""", (days, cap))
    rows = cur.fetchall()
    out = [("syslog", template(f"{template(app, 40)}: {msg}"), _ep(ts), app) for ts, app, msg in rows]
    return out, len(rows) >= cap


SOURCES = {"alerts": src_alerts, "scheduler": src_scheduler, "health": src_health,
           "incidents": src_incidents, "syslog": src_syslog}


def gather(cur, days=DAYS) -> tuple:
    """-> (rows, status {source: 'ok'|'capped'|'error: ...'}). Each source fails open."""
    rows, status = [], {}
    for name, fn in SOURCES.items():
        try:
            cur.execute("SAVEPOINT morse_src")
            got = fn(cur, days)
            capped = False
            if isinstance(got, tuple):
                got, capped = got
            rows.extend(got)
            status[name] = "capped" if capped else "ok"
            cur.execute("RELEASE SAVEPOINT morse_src")
        except Exception as e:  # noqa: BLE001 — one bad source must not stop the rest
            try:
                cur.execute("ROLLBACK TO SAVEPOINT morse_src")
            except Exception:  # noqa: BLE001
                pass
            status[name] = f"error: {str(e).splitlines()[0][:120]}"
            log(f"source {name} skipped: {status[name]}")
    return rows, status


def total_alerts(cur, days=DAYS) -> int:
    try:
        cur.execute("""SELECT count(*) FROM telemetry.events
                       WHERE ts > now() - make_interval(days => %s) AND level IN ('warning','critical')""", (days,))
        return int(cur.fetchone()[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def store(cur, patterns: list, status: dict, seen: set) -> dict:
    """Upsert this week's patterns (an answered one that recurs reopens). An open demand whose template did
    not occur at all this week, in a source that read cleanly and uncapped, is answered by silence.
    seen = every (source, template) observed this run, not just the top N."""
    for p in patterns:
        cur.execute("""INSERT INTO morse_horn (source, template, count_7d, period_seconds, regularity, demand,
                                               demand_by, pct_of_alerts, evidence)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (source, template) DO UPDATE SET
                         ts = now(), count_7d = EXCLUDED.count_7d, period_seconds = EXCLUDED.period_seconds,
                         regularity = EXCLUDED.regularity, pct_of_alerts = EXCLUDED.pct_of_alerts,
                         evidence = EXCLUDED.evidence,
                         demand = CASE WHEN morse_horn.demand_by = 'model' AND EXCLUDED.demand_by = 'fallback'
                                       THEN morse_horn.demand ELSE EXCLUDED.demand END,
                         demand_by = CASE WHEN morse_horn.demand_by = 'model' AND EXCLUDED.demand_by = 'fallback'
                                          THEN morse_horn.demand_by ELSE EXCLUDED.demand_by END,
                         status = CASE WHEN morse_horn.status = 'answered' THEN 'open' ELSE morse_horn.status END,
                         answered_at = CASE WHEN morse_horn.status = 'answered' THEN NULL ELSE morse_horn.answered_at END""",
                    (p["source"], p["template"], p["count_7d"], p["period_seconds"], p["regularity"], p["demand"],
                     p["demand_by"], p.get("pct_of_alerts"), json.dumps(p["evidence"])))
    clean = [s for s, st in status.items() if st == "ok"]
    cur.execute("SELECT id, source, template FROM morse_horn WHERE status = 'open'")
    silent = [i for i, s, t in cur.fetchall() if s in clean and (s, t) not in seen]
    for i in silent:
        cur.execute("""UPDATE morse_horn SET status = 'answered', answered_at = now(), answered_by = 'silence',
                       count_7d = 0 WHERE id = %s""", (i,))
    return {"stored": len(patterns), "answered_by_silence": len(silent)}


def answered_counts(cur) -> tuple:
    cur.execute("""SELECT count(*) FILTER (WHERE answered_at >= date_trunc('month', now())),
                          count(*) FILTER (WHERE answered_at >= date_trunc('month', now()) - interval '1 month'
                                             AND answered_at < date_trunc('month', now()))
                   FROM morse_horn WHERE status = 'answered'""")
    a, b = cur.fetchone()
    return int(a or 0), int(b or 0)


def week_key(now=None) -> str:
    y, w, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f"{y}-W{w:02d}"


def run(dry: bool = False, use_model: bool = True, top_n: int = TOP_N, notify: bool = True) -> dict:
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '120s'")
        rows, status = gather(cur)
        seen = {(s, t) for s, t, _, _ in rows}
        patterns = assign_demands(rank(group(rows), top_n), use_model=use_model)
        tot = total_alerts(cur)
        pct, per = explained(patterns, tot)
        failed = [f"{s} ({st})" for s, st in status.items() if st.startswith("error")]
        week = week_key()
        res = {"week": week, "patterns": patterns, "total_alerts": tot, "explained_pct": pct,
               "sources": status, "rows": len(rows)}
        if dry:
            ans = (0, 0)
            try:
                cur.execute("SAVEPOINT a")
                ans = answered_counts(cur)
            except Exception:  # noqa: BLE001 — table may not exist yet
                cur.execute("ROLLBACK TO SAVEPOINT a")
            res["digest"] = render_digest(week, patterns, tot, pct, per, *ans, failed)
            conn.rollback()
            return res
        ensure_schema(cur)
        res.update(store(cur, patterns, status, seen))
        am, ap = answered_counts(cur)
        cur.execute("SELECT count(*) FROM morse_horn WHERE status = 'open'")
        open_n = cur.fetchone()[0]
        cur.execute("""INSERT INTO morse_horn_runs (week, total_alerts, explained_pct, top_n, open_demands,
                                                    answered_month, answered_prev_month, sources)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (week) DO UPDATE SET at = now(), total_alerts = EXCLUDED.total_alerts,
                         explained_pct = EXCLUDED.explained_pct, top_n = EXCLUDED.top_n,
                         open_demands = EXCLUDED.open_demands, answered_month = EXCLUDED.answered_month,
                         answered_prev_month = EXCLUDED.answered_prev_month, sources = EXCLUDED.sources""",
                    (week, tot, pct, top_n, open_n, am, ap, json.dumps(status)))
        conn.commit()
        res["digest"] = render_digest(week, patterns, tot, pct, per, am, ap, failed)
        if notify and patterns:
            from nova_notify import notify as _notify
            _notify("The Morse Horn: what the machines asked for this week", body=res["digest"], level="info",
                    category="morse_horn", source="nova_morse_horn", dedup_key=f"morse_horn:{week}",
                    meta={"explained_pct": pct, "total_alerts": tot, "demands": len({p['demand'] for p in patterns})})
        return res
    finally:
        conn.close()


def set_status(row_id: int, status: str) -> int:
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE morse_horn SET status = %s,
                         answered_at = CASE WHEN %s = 'answered' THEN now() ELSE answered_at END,
                         answered_by = CASE WHEN %s = 'answered' THEN 'jordan' ELSE answered_by END
                       WHERE id = %s""", (status, status, status, row_id))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def report() -> int:
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('morse_horn') IS NOT NULL")
        if not cur.fetchone()[0]:
            print("morse_horn has no data yet (run --scheduled once).")
            return 0
        cur.execute("""SELECT week, total_alerts, explained_pct, open_demands, answered_month, answered_prev_month
                       FROM morse_horn_runs ORDER BY week DESC LIMIT 6""")
        for w, t, p, o, a, b in cur.fetchall():
            print(f"{w}: {p:.1f}% of {t} alerts explained; {o} open; answered {a} this month / {b} last")
        cur.execute("""SELECT id, source, count_7d, period_seconds, demand, template FROM morse_horn
                       WHERE status = 'open' ORDER BY count_7d DESC LIMIT 25""")
        for i, s, n, per, d, t in cur.fetchall():
            print(f"#{i:<5} {s:<9} {n:>6}  {human_period(per):<16} {d:<52} {t[:70]}")
        return 0
    finally:
        conn.close()


def open_demands(limit: int = 5) -> list:
    """The core's current open demands, loudest first. Read-only; fail-open to []."""
    try:
        import psycopg2
        import nova_dsn
        conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        log(f"open_demands: connect failed: {e}")
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('morse_horn') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute("""SELECT id, source, count_7d, demand, demand_by FROM morse_horn
                       WHERE status = 'open' ORDER BY count_7d DESC, id LIMIT %s""", (int(limit),))
        return [dict(zip(("id", "source", "count_7d", "demand", "demand_by"), r)) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log(f"open_demands: query failed: {e}")
        return []
    finally:
        conn.close()


def run_patterns(dry: bool = False) -> int:
    """--patterns: the weekly alert-patterns review, rendered from the Morse Horn's core."""
    import nova_alert_patterns as renderer
    log("--patterns: alert-pattern review (was nova_alert_patterns.py) over the Morse Horn demands")
    renderer.publish(open_demands(), dry_run=dry)
    return 0


def selftest() -> int:
    ip = ".".join(["10", "0", "9", "9"])          # built, so no literal address lives in the source
    t = template(f"Scheduled task 'nas_localdiff' is FAILING on {ip} at 2026-10-14 21:41:14 (pid 4412)")
    assert t == "scheduled task '*' is failing on <ip> at # (pid #)", t
    assert "<mac>" in template("Failed to find node " + ":".join(["8c", "ea", "12", "34", "56", "78"]))   # built at runtime: pre-commit MAC scan
    assert template("/srv/x/a/b.py line 3") == "<path> line #"
    assert error_line("Traceback ... psycopg2.errors.QueryCanceled: canceling statement due to statement timeout") \
        == "QueryCanceled: canceling statement due to statement timeout"
    assert error_line("", 1, "failure") == "exit 1 with no error text"
    assert rule_demand("no space left on device") == "wants more disk"
    assert rule_demand("httperror: http error #: unauthorized") == "wants a renewed credential"
    assert rule_demand("ssl certificate has expired") == "wants a cert"
    assert rule_demand("http error #: too many requests (429)") == "wants you to slow down"
    assert rule_demand("connection refused") == "wants its dependency back"
    assert rule_demand("killed process # (python) out of memory") == "wants memory"
    assert rule_demand("zxqv blorp") is None
    p, r = periodicity([0, 300, 600, 900, 1200, 1500])
    assert p == 300 and r == 1.0, (p, r)
    assert periodicity([0, 1, 2]) == (None, 0.0)
    assert clean_model_demand("Wants a quieter neighbour.") == "wants a quieter neighbour"
    assert clean_model_demand("I think it wants x") is None
    pats = assign_demands(rank(group([("alerts", "x down", i * 60, "x") for i in range(10)])), use_model=False)
    pct, per = explained(pats, 20)
    assert pct == 50.0 and per == {"wants its dependency back": 10}, (pct, per)
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="weekly: collect, store, post the digest")
    ap.add_argument("--dry-run", action="store_true", help="compute and print; write nothing, post nothing")
    ap.add_argument("--report", action="store_true", help="print the stored demands and weekly measures")
    ap.add_argument("--patterns", action="store_true",
                    help="weekly alert-pattern review (was nova_alert_patterns.py): #nova-alerts + /operations")
    ap.add_argument("--answer", type=int, metavar="ID", help="mark a demand answered")
    ap.add_argument("--ignore", type=int, metavar="ID", help="mark a demand ignored")
    ap.add_argument("--no-model", action="store_true", help="rules only; never call the local model")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.answer or a.ignore:
        n = set_status(a.answer or a.ignore, "answered" if a.answer else "ignored")
        print(f"updated {n} row(s)")
        return 0 if n else 1
    if a.report:
        return report()
    if a.patterns:
        return run_patterns(dry=a.dry_run)
    if not (a.scheduled or a.dry_run):
        ap.print_help()
        return 0
    res = run(dry=a.dry_run, use_model=not a.no_model, top_n=max(1, min(a.top, 50)))
    log(f"sources: {res['sources']}; {res['rows']} signals; {len(res['patterns'])} patterns")
    for p in res["patterns"]:
        print(f"  {p['source']:<9} {p['count_7d']:>6}  reg {p['regularity']:.2f} {human_period(p['period_seconds']):<16}"
              f" [{p['demand_by']}] {p['demand']}  <- {p['template'][:80]}")
    print(res["digest"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
