#!/usr/bin/env python3
"""nova_mark_to_market.py — THE MARK-TO-MARKET LEDGER: what Nova booked, against what she realised.

Inspiration: ENRON: The Smartest Guys in the Room (2005). Mark-to-market let Enron book a deal's
projected future profit on the day it was signed, so "Enron's profits could be whatever Enron said
they were." Nobody lied on any single line; the books simply recorded intentions as income. Sherron
Watkins, reading the same books as everyone else: "the math just didn't add up."
Little Mister, 2026-10-15: "Add all 9." — after the October continuity review found 819 beliefs with
no evidence on any of them, "12 hours a day" of unclaimed time that measured as ~30-50 minutes of
compute, preoccupations that grew by ingest volume, and two feature wishes marked shipped with no code.

Nova books value in six places. Each month this ledger marks every book to market — what was CLAIMED
against what can be shown to have been REALISED — with fixed rules, no model:

  1 beliefs          beliefs booked                vs beliefs with any evidence (cardinality(evidence) > 0)
  2 feature_wishes   wishes with status 'shipped'  vs wishes with code: a script/file named in lineage that
                                                      exists in the repo, a scheduler task or agent_doc it names,
                                                      a script or doc that cites the wish number, or a commit
  3 organs           organs built (scripts named in organ agent_docs, scripts commissioned by Jordan,
                     scripts named by shipped wishes, that exist in scripts/)
                                                   vs organs with >= 1 output in the last 30 days: a successful
                                                      scheduler run with non-empty stdout, a notification, a memory
                                                      under its source, or a fresh row in a table it creates
  4 attention        claimed unclaimed time (minutes/day, from the policy in nova_unclaimed_time.py)
                                                   vs measured compute minutes/day of the unclaimed_time task
  5 preoccupations   active preoccupations         vs those with >= 1 grounded pursuit note (a skill run that
                                                      cited a source, or a pursuit thread whose note carries [n])
  6 skills           skills with status 'implemented' vs skills ever used (nova_skills.uses or a noted/successful
                                                      nova_skill_runs row)

Each class gets a realisation ratio (realised / booked) and a WRITE-DOWN list: the specific items booked
and never realised. One row per (month, class) in mark_to_market; one monthly digest notification.
The measure for the continuity review: the realisation ratio per class, month over month (--report).

  7 unread_tables  organ tables written in the last 30 days
                                                   vs those with a READER. "Read" is defined from code, not
                                                      from pg_stat (the writer's own upserts/dedup SELECTs would
                                                      count there): another script (not one that creates the
                                                      table, not this ledger) names it after FROM/JOIN, or imports
                                                      the writer module when the writer itself exposes a FROM/JOIN
                                                      read of it (e.g. nova_watch_bill -> nova_cardinal.load_ledger).
                                                      The Watch Bill turnover is such a reader, so wiring a table
                                                      into it closes the loop. Dynamic SQL (psycopg2.sql.Identifier)
                                                      is invisible to this rule, so a flag is a question, not a verdict.
                     STILL UNREAD = unread at this marking AND at the previous one (markings are a month apart,
                     so >= 30 days): listed in detail.retirement_signals as a RETIREMENT SIGNAL for the organ.
                     detail.unread carries {table: first month flagged} forward month to month.

Read-only towards everything it measures: it never deletes, retires, re-scores or rewrites a belief,
wish, organ, preoccupation or skill. A write-down is a finding for a human, not an action.
The helpers for "what organs exist and did they produce anything" are shared with nova_flop_slate.py.

THE FLOP SLATE (merged here 2026-10-15, consolidation item #13 — Claude, as integrator, approved the
merge; no coverage lost): `--slate` is the quarterly hit/quiet/dud/retired classification that used to
be its own entry (nova_flop_slate.py, Boffo). Its logic and table (flop_slate) stay in nova_flop_slate.py;
nova_flop_slate.py is now a thin wrapper and keeps working. One scheduler entry: `--scheduled` runs the
monthly marking AND the slate's own once-a-quarter step, each isolated (one failing never skips the other).

CLI:   --scheduled   daily; marks once a month (first run of the month with no rows yet), then the
                     flop slate's quarterly step (acts once a quarter)
       --slate [...]  the Flop Slate (nova_flop_slate options: --scheduled --quarter --dry-run --force --report)
       --dry-run     measure and print; write and notify nothing
       --force       measure and write even if this month is already booked
       --report [N]  last N months, ratio per class
       --selftest    offline checks of the pure functions
Table: mark_to_market (month, class) primary key.
Schedule: scheduler-core, daily 05:40, timeout 1500 (was 600 + flop_slate's 900), overlap skip.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_mark_to_market"
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPTS_DIR.parent
CONFIG_DIR = REPO_DIR / "config"
SCHEDULER_FILES = ("scheduler-core.yaml", "scheduler.yaml")
CLASSES = ("beliefs", "feature_wishes", "organs", "attention", "preoccupations", "skills", "unread_tables",
           "work_lessons")
WINDOW_DAYS = 30
WRITEDOWN_CAP = 60              # write-downs stored per class (the full count is in detail)
STATEMENT_TIMEOUT_MS = 60000
TABLE_PROBE_TIMEOUT_MS = 3000
TABLE_PROBE_BUDGET_S = 60
TABLE_PROBE_MAX_ROWS = 2_000_000  # never seq-scan a bigger table for freshness
DEFAULT_CLAIM_HOURS = 12        # used only if the policy line can't be read from nova_unclaimed_time.py
UNCLAIMED_TASK = "unclaimed_time"
# the measuring organs themselves cite wish numbers in prose; never let them count as code for a wish
SELF_SCRIPTS = {"nova_mark_to_market.py", "nova_flop_slate.py", "nova_continuity_review.py"}
ORGAN_DOC_SQL = ("SELECT doc_type, content FROM agent_docs WHERE doc_type ~* '(organ|sense|sensor)' "
                 "OR left(content, 800) ILIKE '%%organ%%'")
TS_COLUMNS = ("ts", "created_at", "measured_at", "detected_at", "observed_at", "run_at", "recorded_at",
              "updated_at", "first_seen", "last_seen")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mark_to_market (
    month       date NOT NULL,          -- first of the month the marking belongs to
    class       text NOT NULL,          -- beliefs | feature_wishes | organs | attention | preoccupations | skills
    booked      numeric NOT NULL,
    realised    numeric NOT NULL,
    ratio       real,                   -- realised / booked (NULL when nothing was booked)
    writedowns  jsonb NOT NULL DEFAULT '[]',
    detail      jsonb NOT NULL DEFAULT '{}',
    measured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (month, class)
);
"""

SCRIPT_TOKEN = re.compile(r"\b([A-Za-z0-9_][\w./-]*\.(?:py|sh))\b")
CITED = re.compile(r"\[\d+\]")
READ_TOKEN = re.compile(r"(?i)\b(?:FROM|JOIN)\s+\"?([A-Za-z_][\w.]*)")
IMPORT_TOKEN = re.compile(r"(?m)^\s*(?:import|from)\s+(nova_\w+)")
ONE_SHOT = re.compile(r"(_article|^nova_ingest_)")   # one-off articles/ingests Jordan asked for are not organs


def log(msg: str) -> None:
    print(f"[mark-to-market] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- pure helpers (shared with the flop slate)

def norm(name: str | None) -> str:
    """'nova_comet-tail.py' / 'nova-comet-tail' / 'comet_tail' -> 'comet_tail'. Pure."""
    s = (name or "").strip().lower().rsplit("/", 1)[-1]
    s = re.sub(r"\.(py|sh)$", "", s).replace("-", "_")
    return re.sub(r"^nova_", "", s)


def ratio(booked, realised) -> float | None:
    b = float(booked or 0)
    return None if b <= 0 else round(float(realised or 0) / b, 4)


def month_of(d: date) -> date:
    return d.replace(day=1)


def due(today: date, have_rows: bool, force: bool = False) -> bool:
    """Once a month: the first run of a month with no rows yet. Pure."""
    return force or not have_rows


def script_tokens(text: str | None) -> set[str]:
    """basenames of every *.py / *.sh named in text. Pure."""
    return {t.rsplit("/", 1)[-1] for t in SCRIPT_TOKEN.findall(text or "")}


def wish_ref(n: int) -> re.Pattern:
    """A citation of feature wish n: 'wish #42', 'wishes 42', 'feature_wish 42', 'feature wish #42'."""
    return re.compile(rf"(?i)\b(?:feature[ _])?wish(?:es)?\s*(?:id\s*=?\s*)?#?\s*{int(n)}\b")


def created_tables(src: str | None) -> list[str]:
    """Tables a script creates for itself (CREATE TABLE IF NOT EXISTS ...). Pure."""
    out = []
    for t in re.findall(r"(?i)CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([\w.\"]+)", src or ""):
        t = t.replace('"', "")
        if t not in out and re.fullmatch(r"\w+(\.\w+)?", t):
            out.append(t)
    return out


def _tname(t: str) -> str:
    t = t.lower().replace('"', "")
    return t[len("public."):] if t.startswith("public.") else t


def read_tokens(src: str | None) -> set[str]:
    """Every name that follows FROM/JOIN in a source (tables, plus harmless Python 'from x import'). Pure."""
    return {_tname(t) for t in READ_TOKEN.findall(src or "")}


def table_readers(texts: dict[str, str]) -> dict[str, dict]:
    """table -> {"writers": [scripts that CREATE it], "readers": [other scripts that read it]}. Pure.
    A reader names the table after FROM/JOIN, or imports a writer module that itself reads the table."""
    writers: dict[str, set] = {}
    for s, src in texts.items():
        for t in created_tables(src):
            writers.setdefault(_tname(t), set()).add(s)
    reads = {s: read_tokens(src) for s, src in texts.items()}
    imports = {s: set(IMPORT_TOKEN.findall(src or "")) for s, src in texts.items()}
    out = {}
    for t, ws in writers.items():
        if ws <= SELF_SCRIPTS:      # the measuring ledgers' own books are not organ output
            continue
        exposing = {Path(w).stem for w in ws if t in reads.get(w, ())}
        readers = sorted(s for s in texts if s not in ws and s not in SELF_SCRIPTS
                         and (t in reads[s] or imports[s] & exposing))
        out[t] = {"writers": sorted(ws), "readers": readers}
    return out


def unread_verdicts(unread_fresh: list[str], prev_unread: dict, month: date) -> tuple[dict, list[str]]:
    """unread now + {table: first month flagged} last time -> (carried-forward map, still-unread list). Pure."""
    first = {t: (prev_unread or {}).get(t) or f"{month:%Y-%m}" for t in unread_fresh}
    still = sorted(t for t in unread_fresh if t in (prev_unread or {}))
    return first, still


def grounded_topics(skill_runs: list[tuple], threads: list[tuple]) -> dict[str, str]:
    """topic -> why it counts as grounded. skill_runs (topic, outcome, success, n_cited);
    threads (topic, last_note). A pursuit is grounded when it cited at least one source. Pure."""
    out: dict[str, str] = {}
    for topic, outcome, success, n_cited in skill_runs:
        if topic and (n_cited or 0) > 0 and (success or outcome == "noted"):
            out.setdefault(topic.strip().lower(), f"skill run cited {n_cited} source(s)")
    for topic, note in threads:
        if topic and CITED.search(note or ""):
            out.setdefault(topic.strip().lower(), "pursuit note carries a citation")
    return out


def claimed_minutes_per_day(scripts_dir: Path = SCRIPTS_DIR) -> tuple[int, str]:
    """The unclaimed-time policy, read from the script that implements it (no copy of the number here)."""
    try:
        head = (scripts_dir / "nova_unclaimed_time.py").read_text(errors="replace")[:3000]
        m = re.search(r"at least (\d+) hours a day", head)
        if m:
            return int(m.group(1)) * 60, f"nova_unclaimed_time.py: \"at least {m.group(1)} hours a day\""
    except OSError:
        pass
    return DEFAULT_CLAIM_HOURS * 60, f"default {DEFAULT_CLAIM_HOURS} h/day (policy line not readable)"


def wish_evidence(wish: dict, repo_files: set[str], script_texts: dict[str, str], task_ids: set[str],
                  doc_types: set[str], doc_texts: dict[str, str], commits: str = "") -> list[str]:
    """Every reason to believe a shipped wish has code behind it. Empty list = a write-down. Pure."""
    n = wish["id"]
    lineage = wish.get("lineage") or {}
    ltext = json.dumps(lineage)
    ev = []
    for tok in sorted(script_tokens(ltext)):
        if tok in repo_files:
            ev.append(f"file {tok} exists")
    for tok in sorted(set(re.findall(r"\b[a-z][a-z0-9_]{3,}\b", ltext)) & task_ids):
        ev.append(f"scheduler task {tok}")
    for key in ("agent_doc", "doc"):
        d = str(lineage.get(key) or "").replace("agent_docs", "").strip()
        if d and d in doc_types:
            ev.append(f"agent_doc {d}")
    pat = wish_ref(n)
    for name, text in script_texts.items():
        if name not in SELF_SCRIPTS and pat.search(text):
            ev.append(f"{name} cites the wish")
            break
    for dt, text in doc_texts.items():
        if pat.search(text):
            ev.append(f"agent_doc {dt} cites the wish")
            break
    if commits and pat.search(commits):
        ev.append("a commit cites the wish")
    return ev


def realised(sig: dict) -> bool:
    """An organ/initiative produced at least one output in the window. Pure."""
    return bool(sig.get("ok_out") or sig.get("events") or sig.get("memories") or sig.get("tables"))


def output_summary(sig: dict) -> str:
    parts = []
    if sig.get("runs") is not None and (sig.get("tasks") or sig.get("runs")):
        parts.append(f"{sig.get('runs', 0)} runs/{sig.get('ok', 0)} ok/{sig.get('ok_out', 0)} with output")
    if sig.get("events"):
        parts.append(f"{sig['events']} notifications")
    if sig.get("memories"):
        parts.append("memories written")
    if sig.get("tables"):
        parts.append("fresh rows in " + ", ".join(sig["tables"]))
    return "; ".join(parts) or "no runs, no notifications, no memories, no fresh table rows"


def render(results: dict, month: date, prev: dict | None = None) -> str:
    """Digest text: one line per class with last month alongside. Pure."""
    prev = prev or {}
    lines = [f"Mark-to-market, {month:%B %Y} — booked vs realised (fixed SQL, no model):"]
    for c in CLASSES:
        r = results.get(c)
        if not r:
            continue
        pr = prev.get(c)
        was = f" (last month {pr * 100:.0f}%)" if pr is not None else ""
        pct = "—" if r["ratio"] is None else f"{r['ratio'] * 100:.0f}%"
        lines.append(f"• {c}: {_n(r['realised'])} of {_n(r['booked'])} realised = {pct}{was}")
    lines.append("")
    lines.append("Largest write-downs:")
    for c in CLASSES:
        r = results.get(c)
        if r and r["writedowns"]:
            w = r["writedowns"][:3]
            more = r["detail"].get("writedowns_total", len(r["writedowns"])) - len(w)
            lines.append(f"• {c}: " + "; ".join(f"{x['item']} ({x['why']})" for x in w)
                         + (f"; +{more} more" if more > 0 else ""))
    lines.append("")
    lines.append("Nothing is retired or rewritten by this ledger. A write-down is booked value that has not "
                 "shown up yet. `nova_mark_to_market.py --report` shows the trend.")
    return "\n".join(lines)


def _n(v) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:.1f}"


def _class(booked, real, writedowns, **detail) -> dict:
    detail["writedowns_total"] = len(writedowns)
    return {"booked": booked, "realised": real, "ratio": ratio(booked, real),
            "writedowns": writedowns[:WRITEDOWN_CAP], "detail": detail}


# ---------------------------------------------------------------- inventory (files, YAML, docs) — shared

def load_scheduler(config_dir: Path = CONFIG_DIR) -> dict[str, dict]:
    """task_id -> {script, args, enabled (in any file), files, disabled_note}. Reads YAML + raw comments."""
    out: dict[str, dict] = {}
    try:
        import yaml
    except ImportError:
        return out
    for fname in SCHEDULER_FILES:
        p = config_dir / fname
        try:
            raw = p.read_text()
            tasks = (yaml.safe_load(raw) or {}).get("tasks") or {}
        except Exception:  # noqa: BLE001 — an unreadable YAML just means fewer tasks known
            continue
        notes, cur = {}, None
        for line in raw.splitlines():
            m = re.match(r"^  (\w[\w-]*):\s*(#.*)?$", line)
            if m:
                cur = m.group(1)
            elif cur and re.match(r"^\s+enabled:\s*false", line):
                notes[cur] = line.split("#", 1)[1].strip() if "#" in line else "enabled: false"
        for tid, spec in tasks.items():
            if not isinstance(spec, dict):
                continue
            e = out.setdefault(tid, {"script": spec.get("script"), "args": spec.get("args"), "enabled": False,
                                     "files": [], "disabled_note": None})
            e["files"].append(fname)
            if spec.get("enabled") is False:
                e["disabled_note"] = e["disabled_note"] or notes.get(tid, "enabled: false")
            else:
                e["enabled"] = True
                e["script"] = spec.get("script") or e["script"]
    for e in out.values():
        if e["enabled"]:
            e["disabled_note"] = None
    return out


def repo_files(repo_dir: Path = REPO_DIR) -> set[str]:
    """basenames of every tracked .py/.sh in the repo (git ls-files; falls back to scripts/ + top level)."""
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), "ls-files", "*.py", "*.sh"], capture_output=True,
                           text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return {Path(x).name for x in r.stdout.split()}
    except Exception:  # noqa: BLE001 — no git on this node: walk the obvious places
        pass
    out = set()
    for d in (repo_dir, repo_dir / "scripts", repo_dir / "scripts" / "nova_gateway"):
        try:
            out |= {p.name for p in d.iterdir() if p.suffix in (".py", ".sh")}
        except OSError:
            continue
    return out


def git_log_text(repo_dir: Path = REPO_DIR, since: str = "2026-06-01") -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), "log", f"--since={since}", "--format=%s%n%b"],
                           capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def script_texts(scripts_dir: Path = SCRIPTS_DIR) -> dict[str, str]:
    out = {}
    for p in sorted(scripts_dir.glob("*.py")):
        try:
            out[p.name] = p.read_text(errors="replace")
        except OSError:
            continue
    return out


def commissioned(text: str, name: str = "") -> bool:
    """A script Jordan commissioned as an organ/sense says so in its docstring (one-off ingests and
    articles he also asked for are not organs). Pure."""
    head = (text or "")[:4000]
    if ONE_SHOT.search(name or ""):
        return False
    return "Little Mister" in head and re.search(r"(?i)\b(organ|sense|sensor)s?\b", head) is not None


def organ_inventory(oc, texts: dict[str, str], sched: dict[str, dict], wishes: list[dict]) -> dict[str, dict]:
    """script -> {"from": [...], "tasks": [...], "mem_sources": [...]} for every organ that exists in scripts/."""
    inv: dict[str, dict] = {}

    def add(script, why, mem=None):
        e = inv.setdefault(script, {"from": [], "tasks": [], "mem_sources": []})
        if why not in e["from"]:
            e["from"].append(why)
        if mem and mem not in e["mem_sources"]:
            e["mem_sources"].append(mem)

    oc.execute(ORGAN_DOC_SQL)
    for doc_type, content in oc.fetchall():
        for tok in script_tokens(content):
            if tok in texts and tok not in SELF_SCRIPTS:
                add(tok, f"agent_doc {doc_type}")
    for name, text in texts.items():
        if name.startswith("nova_") and name not in SELF_SCRIPTS and commissioned(text, name):
            add(name, "commissioned (docstring)")
    for w in wishes:
        if w.get("status") != "shipped":
            continue
        lin = w.get("lineage") or {}
        for tok in script_tokens(json.dumps(lin)):
            if tok in texts and tok not in SELF_SCRIPTS:
                add(tok, f"feature wish {w['id']}", lin.get("memory_source"))
    for tid, s in sched.items():
        if s.get("enabled") and s.get("script") in inv:
            inv[s["script"]]["tasks"].append(tid)
    return inv


# ---------------------------------------------------------------- output signals (DB) — shared

def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def run_stats(oc, since: datetime, until: datetime) -> dict[str, dict]:
    oc.execute("""SELECT task_id, count(*), count(*) FILTER (WHERE status='success'),
                         count(*) FILTER (WHERE status='success' AND length(btrim(coalesce(stdout_tail,''))) > 0),
                         max(started_at)
                  FROM scheduler_runs WHERE started_at >= %s AND started_at < %s GROUP BY 1""",
               (ms(since), ms(until)))
    return {t: {"runs": r, "ok": ok, "ok_out": out, "last": last} for t, r, ok, out, last in oc.fetchall()}


def event_stats(oc, since: datetime, until: datetime) -> dict[str, dict]:
    """norm(source) -> notifications, incidents linked, incidents acknowledged."""
    oc.execute("""SELECT e.source, count(*), count(DISTINCT e.incident_id),
                         count(DISTINCT e.incident_id) FILTER (WHERE i.acked_at IS NOT NULL)
                  FROM telemetry.events e LEFT JOIN telemetry.incidents i ON i.id = e.incident_id
                  WHERE e.ts >= %s AND e.ts < %s GROUP BY 1""", (since, until))
    out: dict[str, dict] = {}
    for src, n, inc, acked in oc.fetchall():
        e = out.setdefault(norm(src), {"events": 0, "incidents": 0, "acked": 0})
        e["events"] += n
        e["incidents"] += inc
        e["acked"] += acked
    return out


def memory_written(mc, sources, since: datetime, until: datetime) -> bool:
    """Any memory under one of these sources in the window (index on (source, created_at))."""
    if mc is None:
        return False
    for s in sources:
        if not s:
            continue
        mc.execute("SELECT 1 FROM memories WHERE source = %s AND created_at >= %s AND created_at < %s LIMIT 1",
                   (s, since, until))
        if mc.fetchone():
            return True
    return False


def table_fresh(oc, table: str, since: datetime, until: datetime) -> bool | None:
    """Did a table this organ owns get a row in the window? None = could not tell (no ts column / too big)."""
    from psycopg2 import sql
    schema, _, name = table.rpartition(".")
    schema = schema or "public"
    oc.execute("SELECT c.reltuples FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
               "WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p')", (schema, name))
    r = oc.fetchone()
    if not r:
        return None
    oc.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
               "AND data_type LIKE 'timestamp%%'", (schema, name))
    cols = [c for (c,) in oc.fetchall()]
    col = next((c for c in TS_COLUMNS if c in cols), cols[0] if cols else None)
    if not col:
        return None
    oc.execute("SELECT 1 FROM pg_index x JOIN pg_attribute a ON a.attrelid=x.indrelid AND a.attnum=x.indkey[0] "
               "WHERE x.indrelid=to_regclass(%s) AND a.attname=%s LIMIT 1", (f"{schema}.{name}", col))
    if not oc.fetchone() and (r[0] or 0) > TABLE_PROBE_MAX_ROWS:
        return None
    try:
        oc.execute("SET statement_timeout = %s", (int(TABLE_PROBE_TIMEOUT_MS),))
        oc.execute(sql.SQL("SELECT 1 FROM {}.{} WHERE {} >= %s AND {} < %s LIMIT 1").format(
            sql.Identifier(schema), sql.Identifier(name), sql.Identifier(col), sql.Identifier(col)), (since, until))
        return oc.fetchone() is not None
    except Exception:  # noqa: BLE001 — timeout etc.: unknown, never fatal (autocommit: nothing to roll back)
        return None
    finally:
        try:
            oc.execute("SET statement_timeout = %s", (int(STATEMENT_TIMEOUT_MS),))
        except Exception:  # noqa: BLE001
            pass


def unit_signals(oc, mc, units: dict[str, dict], texts: dict[str, str], since: datetime, until: datetime,
                 runs: dict | None = None, events: dict | None = None,
                 budget_s: float = TABLE_PROBE_BUDGET_S) -> dict[str, dict]:
    """units: key -> {"script", "tasks": [...], "mem_sources": [...]}. Returns key -> output signals.
    Cheap signals first; the table probe only for units with nothing else to show."""
    runs = run_stats(oc, since, until) if runs is None else runs
    events = event_stats(oc, since, until) if events is None else events
    deadline = time.monotonic() + budget_s
    table_cache: dict[str, bool | None] = {}
    out = {}
    for key, u in units.items():
        script, tasks = u.get("script"), list(u.get("tasks") or [])
        sig = {"tasks": tasks, "runs": 0, "ok": 0, "ok_out": 0, "last": None,
               "events": 0, "incidents": 0, "acked": 0, "memories": False, "tables": []}
        for t in tasks:
            r = runs.get(t) or {}
            for k in ("runs", "ok", "ok_out"):
                sig[k] += r.get(k, 0) or 0
            if r.get("last") and (sig["last"] is None or r["last"] > sig["last"]):
                sig["last"] = r["last"]
        names = {norm(script)} | {norm(t) for t in tasks}
        names.discard("")
        for n in names:
            e = events.get(n) or {}
            for k in ("events", "incidents", "acked"):
                sig[k] += e.get(k, 0) or 0
        if not realised(sig):
            stem = Path(script).stem if script else None
            mem = list(u.get("mem_sources") or []) + ([stem, norm(script)] if script else []) + tasks
            sig["memories"] = memory_written(mc, list(dict.fromkeys(mem)), since, until)
        if not realised(sig) and script and time.monotonic() < deadline:
            for t in created_tables(texts.get(script)):
                if t not in table_cache:
                    table_cache[t] = table_fresh(oc, t, since, until)
                if table_cache[t]:
                    sig["tables"].append(t)
        out[key] = sig
    return out


# ---------------------------------------------------------------- the six classes

def measure_beliefs(oc) -> dict:
    oc.execute("SELECT count(*), count(*) FILTER (WHERE cardinality(coalesce(evidence,'{}')) > 0), "
               "count(*) FILTER (WHERE active) FROM beliefs")
    booked, real, active = oc.fetchone()
    oc.execute("SELECT id, topic, confidence FROM beliefs WHERE cardinality(coalesce(evidence,'{}')) = 0 "
               "ORDER BY active DESC, confidence DESC NULLS LAST, id LIMIT %s", (WRITEDOWN_CAP,))
    wd = [{"item": f"belief {i}: {(t or '')[:80]}", "why": f"no evidence (confidence {c or 0:.2f})"}
          for i, t, c in oc.fetchall()]
    r = _class(booked, real, wd, active=active)
    r["detail"]["writedowns_total"] = booked - real      # the list is capped; the count is not
    return r


def load_wishes(oc) -> list[dict]:
    oc.execute("SELECT id, title, status, lineage, coalesce(shipped_at, ts) FROM feature_wishes ORDER BY id")
    return [{"id": i, "title": t, "status": s, "lineage": lin or {}, "when": w} for i, t, s, lin, w in oc.fetchall()]


def wish_shipped_on(w: dict) -> date | None:
    """Best date a wish shipped: shipped_at, else lineage shipped/built/granted date, else filing date. Pure."""
    lin = w.get("lineage") or {}
    for k in ("shipped", "built"):
        try:
            return date.fromisoformat(str(lin[k])[:10])
        except (KeyError, ValueError):
            pass
    for k in lin:
        m = re.match(r"granted_(\d{4})_(\d{2})_(\d{2})", k)
        if m:
            return date(*map(int, m.groups()))
    when = w.get("when")
    if isinstance(when, datetime):
        return when.date()
    try:
        return date.fromisoformat(str(lin.get("date"))[:10])
    except ValueError:
        return None


def measure_wishes(oc, wishes, files, texts, sched, commits) -> tuple[dict, dict]:
    oc.execute("SELECT doc_type, content FROM agent_docs")
    docs = dict(oc.fetchall())
    shipped = [w for w in wishes if w["status"] == "shipped"]
    evidence, wd = {}, []
    for w in shipped:
        ev = wish_evidence(w, files, texts, set(sched), set(docs), docs, commits)
        evidence[w["id"]] = ev
        if not ev:
            wd.append({"item": f"wish {w['id']}: {w['title']}", "why": "status shipped; no script, task, doc "
                       "or commit references it"})
    real = sum(1 for e in evidence.values() if e)
    return _class(len(shipped), real, wd), evidence


def imported_by(module_script: str, host_text: str) -> bool:
    """Does host_text import this script's module? Pure."""
    stem = Path(module_script).stem
    return re.search(rf"(?m)^\s*(?:from\s+{stem}\s+import|import\s+[\w, ]*\b{stem}\b)", host_text or "") is not None


def live_hosts(sched: dict, runs: dict) -> set[str]:
    """Scripts of enabled tasks that produced stdout in the window (they can carry an imported organ). Pure."""
    return {s["script"] for t, s in sched.items()
            if s.get("enabled") and s.get("script") and (runs.get(t) or {}).get("ok_out")}


def daemon_scripts(oc, repo_dir: Path = REPO_DIR) -> set[str]:
    """Scripts that run outside the scheduler (services doc + launchd templates in the repo)."""
    out = set()
    oc.execute("SELECT content FROM agent_docs WHERE doc_type = 'services-launchd'")
    for (c,) in oc.fetchall():
        out |= script_tokens(c)
    try:
        for p in (repo_dir / "launchd").glob("*.plist*"):
            out |= script_tokens(p.read_text(errors="replace"))
    except OSError:
        pass
    return out


def measure_organs(oc, mc, inv, texts, sched, since, until) -> tuple[dict, dict]:
    units = {s: {"script": s, "tasks": e["tasks"], "mem_sources": e["mem_sources"]} for s, e in inv.items()}
    runs = run_stats(oc, since, until)
    sigs = unit_signals(oc, mc, units, texts, since, until, runs=runs)
    hosts = live_hosts(sched, runs)
    daemons = daemon_scripts(oc)
    wd, via_host = [], 0
    for s in sorted(inv):
        sig = sigs[s]
        if realised(sig):
            continue
        host = next((h for h in sorted(hosts) if h != s and imported_by(s, texts.get(h, ""))), None)
        if host:
            sig["via_host"] = host
            via_host += 1
            continue
        how = ("not scheduled; " if not inv[s]["tasks"] else "") + output_summary(sig)
        if s in daemons:
            how += "; runs as a daemon, so output this ledger can attribute is limited to notifications/memories/own tables"
        wd.append({"item": s, "why": f"{how} (booked via {inv[s]['from'][0]})"})
    real = len(inv) - len(wd)
    sched_n = sum(1 for e in inv.values() if e["tasks"])
    return _class(len(inv), real, wd, scheduled=sched_n, realised_via_host=via_host,
                  window_days=(until - since).days), sigs


def measure_attention(oc, since, until, scripts_dir: Path = SCRIPTS_DIR) -> dict:
    claim, cite = claimed_minutes_per_day(scripts_dir)
    oc.execute("""SELECT count(*), count(*) FILTER (WHERE status='success'), coalesce(sum(duration_ms),0)/60000.0,
                         count(*) FILTER (WHERE stdout_tail ILIKE '%%yield%%')
                  FROM scheduler_runs WHERE task_id=%s AND started_at >= %s AND started_at < %s""",
               (UNCLAIMED_TASK, ms(since), ms(until)))
    runs, ok, minutes, yielded = oc.fetchone()
    days = max((until - since).total_seconds() / 86400, 1)
    per_day = round(float(minutes) / days, 1)
    wd = []
    if per_day < claim:
        wd.append({"item": f"claimed {claim // 60} h/day of unclaimed time",
                   "why": f"measured {per_day} min/day of compute over {runs} wakes ({yielded} yielded)"})
    return _class(claim, min(per_day, claim), wd, claim_source=cite, runs=runs, succeeded=ok,
                  yielded=yielded, minutes_per_day=per_day, window_days=round(days))


def measure_preoccupations(oc) -> dict:
    oc.execute("SELECT id, topic, data->>'detected_from', returns FROM preoccupations WHERE status='active' ORDER BY id")
    pre = oc.fetchall()
    oc.execute("SELECT topic, outcome, success, n_cited FROM nova_skill_runs")
    runs = oc.fetchall()
    oc.execute("SELECT topic, last_note FROM pursuit_threads")
    g = grounded_topics(runs, oc.fetchall())
    wd = [{"item": f"preoccupation {i}: {t}",
           "why": f"no cited pursuit note ({r or 0} returns; detected from {src or 'unrecorded'})"}
          for i, t, src, r in pre if (t or "").strip().lower() not in g]
    real = len(pre) - len(wd)
    by_volume = sum(1 for _, _, src, _ in pre if src == "source")
    return _class(len(pre), real, wd, detected_from_ingest_volume=by_volume)


def measure_skills(oc) -> dict:
    oc.execute("""SELECT s.slug, s.uses, count(r.id) FILTER (WHERE r.success OR r.outcome='noted'), count(r.id)
                  FROM nova_skills s LEFT JOIN nova_skill_runs r ON r.slug = s.slug
                  WHERE s.status='implemented' GROUP BY 1, 2 ORDER BY 1""")
    rows = oc.fetchall()
    wd = [{"item": f"skill {slug}", "why": f"implemented; uses={uses or 0}, {tried} run(s), none noted"}
          for slug, uses, good, tried in rows if not (uses or 0) and not good]
    return _class(len(rows), len(rows) - len(wd), wd)


def work_realised(rows, recalled: set, executed: set, cited_by: dict) -> tuple[int, list[dict]]:
    """rows: (work_key, title, memory_id, belief_ids, proposal_id). A read work is realised when its lesson memory
    was recalled, a belief it did not write itself cites it, or its proposal was executed. Pure."""
    real, wd = 0, []
    for key, title, mid, bids, pid in rows:
        own = set(bids or [])
        if (mid and mid in recalled) or (pid and pid in executed) or (cited_by.get(mid, set()) - own):
            real += 1
        else:
            wd.append({"item": f"work {(title or key)[:80]}", "why": "lessons booked; not recalled, cited or acted on"})
    return real, wd


def measure_work_lessons(oc, mc) -> dict:
    """Class 8 (2026-10-15): booked = works read for lessons; realised = lesson memory recalled, cited by a
    belief other than the ones the read itself wrote, or its proposal executed."""
    oc.execute("SELECT to_regclass('public.work_lessons') IS NOT NULL")
    r = oc.fetchone()
    if not r or not r[0]:
        return _class(0, 0, [])
    oc.execute("SELECT work_key, title, memory_id, belief_ids, proposal_id FROM work_lessons WHERE status='done' "
               "AND memory_id IS NOT NULL ORDER BY processed_at")
    rows = oc.fetchall()
    mids = sorted({x[2] for x in rows if x[2]})
    pids = sorted({x[4] for x in rows if x[4]})
    recalled, executed, cited_by = set(), set(), {}
    if mids:
        mc.execute("SELECT id FROM memories WHERE id = ANY(%s) AND coalesce(access_count, 0) > 0", (mids,))
        recalled = {x[0] for x in mc.fetchall()}
        oc.execute("SELECT id, evidence FROM beliefs WHERE evidence && %s::text[]", ([f"memory:{m}" for m in mids],))
        for bid, ev in oc.fetchall():
            for e in ev or []:
                if e.startswith("memory:"):
                    cited_by.setdefault(e[7:], set()).add(bid)
    if pids:
        oc.execute("SELECT id FROM coagency_proposals WHERE id = ANY(%s) AND executed_at IS NOT NULL", (pids,))
        executed = {x[0] for x in oc.fetchall()}
    real, wd = work_realised(rows, recalled, executed, cited_by)
    return _class(len(rows), real, wd, recalled=len(recalled), proposals_executed=len(executed))


def previous_unread(oc, month: date) -> dict:
    """{table: first month flagged} from the latest earlier unread_tables row ({} if none)."""
    oc.execute("SELECT to_regclass('public.mark_to_market') IS NOT NULL")
    r = oc.fetchone()
    if not r or not r[0]:
        return {}
    oc.execute("SELECT detail FROM mark_to_market WHERE class = 'unread_tables' AND month < %s "
               "ORDER BY month DESC LIMIT 1", (month,))
    r = oc.fetchone()
    d = (r[0] if r else None) or {}
    if isinstance(d, str):
        d = json.loads(d)
    u = d.get("unread") if isinstance(d, dict) else None
    return u if isinstance(u, dict) else {}


def measure_unread(oc, texts, since, until, month: date | None = None,
                   budget_s: float = TABLE_PROBE_BUDGET_S) -> dict:
    """Class 7: organ tables written in the window that nothing reads. Freshness is probed only for the
    tables with no reader (the expensive part), within a time budget; unprobed ones are counted, not flagged."""
    month = month or month_of(until.date())
    rd = table_readers(texts)
    read = sorted(t for t, r in rd.items() if r["readers"])
    deadline = time.monotonic() + budget_s
    fresh, unknown, quiet = [], 0, 0
    for t in sorted(t for t, r in rd.items() if not r["readers"]):
        if time.monotonic() >= deadline:
            unknown += 1
            continue
        f = table_fresh(oc, t, since, until)
        if f:
            fresh.append(t)
        elif f is None:
            unknown += 1
        else:
            quiet += 1
    first, still = unread_verdicts(fresh, previous_unread(oc, month), month)
    wd = [{"item": f"table {t}",
           "why": (f"still unread since {first[t]} — RETIREMENT SIGNAL" if t in still else
                   "written in 30 d, no reader in code (first flagged this month)")
                  + f"; writer {', '.join(rd[t]['writers'])}"}
          for t in sorted(fresh, key=lambda x: (x not in still, x))]
    return _class(len(read) + len(fresh), len(read), wd, unread=first, retirement_signals=still,
                  unread_not_written=quiet, unknown=unknown, tables_with_reader=len(read))


# ---------------------------------------------------------------- run

def connect(dbname="nova_ops"):
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn(dbname, "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def measure(oc, mc, now: datetime | None = None) -> dict:
    """All six classes. Read-only."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)
    sched = load_scheduler()
    texts = script_texts()
    files = repo_files() | set(texts)
    wishes = load_wishes(oc)
    inv = organ_inventory(oc, texts, sched, wishes)
    res = {"beliefs": measure_beliefs(oc)}
    res["feature_wishes"], _ = measure_wishes(oc, wishes, files, texts, sched, git_log_text())
    res["organs"], _ = measure_organs(oc, mc, inv, texts, sched, since, now)
    res["attention"] = measure_attention(oc, since, now)
    res["preoccupations"] = measure_preoccupations(oc)
    res["skills"] = measure_skills(oc)
    try:
        res["unread_tables"] = measure_unread(oc, texts, since, now)
    except Exception as e:  # noqa: BLE001 — the newest class never sinks the other six
        log(f"unread_tables not measured: {e}")
    try:
        res["work_lessons"] = measure_work_lessons(oc, mc)
    except Exception as e:  # noqa: BLE001
        log(f"work_lessons not measured: {e}")
    return res


def previous_ratios(oc, month: date) -> dict:
    oc.execute("SELECT to_regclass('public.mark_to_market') IS NOT NULL")
    if not oc.fetchone()[0]:
        return {}
    oc.execute("SELECT class, ratio FROM mark_to_market WHERE month = (SELECT max(month) FROM mark_to_market "
               "WHERE month < %s)", (month,))
    return {c: r for c, r in oc.fetchall()}


def write(oc, month: date, res: dict) -> None:
    for c, r in res.items():
        oc.execute("""INSERT INTO mark_to_market (month, class, booked, realised, ratio, writedowns, detail)
                      VALUES (%s, %s, %s, %s, %s, %s, %s)
                      ON CONFLICT (month, class) DO UPDATE SET booked=EXCLUDED.booked, realised=EXCLUDED.realised,
                        ratio=EXCLUDED.ratio, writedowns=EXCLUDED.writedowns, detail=EXCLUDED.detail,
                        measured_at=now()""",
                   (month, c, r["booked"], r["realised"], r["ratio"], json.dumps(r["writedowns"], default=str),
                    json.dumps(r["detail"], default=str)))


def have_month(oc, month: date) -> bool:
    oc.execute("SELECT to_regclass('public.mark_to_market') IS NOT NULL")
    if not oc.fetchone()[0]:
        return False
    # the six original books decide "already marked"; a missing unread_tables row (budget/failure, or a
    # month marked before class 7 existed) must not re-mark the month every day
    # (same for work_lessons, class 8, 2026-10-15)
    oc.execute("SELECT count(*) FROM mark_to_market WHERE month = %s AND class <> 'unread_tables' "
               "AND class <> 'work_lessons'", (month,))
    return oc.fetchone()[0] >= len(CLASSES) - 2


def report(oc, n: int) -> None:
    oc.execute("SELECT to_regclass('public.mark_to_market') IS NOT NULL")
    if not oc.fetchone()[0]:
        print("No markings yet (table not created; the first scheduled run creates it).")
        return
    oc.execute("SELECT month, class, booked, realised, ratio, jsonb_array_length(writedowns) FROM mark_to_market "
               "WHERE month >= (SELECT max(month) FROM mark_to_market) - make_interval(months => %s - 1) "
               "ORDER BY month DESC, class", (n,))
    rows = oc.fetchall()
    print(f"{'month':<8} {'class':<15} {'booked':>8} {'realised':>9} {'ratio':>7} {'write-downs':>12}")
    for m, c, b, r, ra, w in rows:
        print(f"{m:%Y-%m}  {c:<15} {_n(b):>8} {_n(r):>9} {('—' if ra is None else f'{ra * 100:.0f}%'):>7} {w:>12}")


def print_result(res: dict, month: date) -> None:
    print(render(res, month))
    for c in (c for c in CLASSES if c in res):
        r = res[c]
        print(f"\n== {c}: booked {_n(r['booked'])}, realised {_n(r['realised'])}, "
              f"ratio {r['ratio']}  detail {json.dumps(r['detail'], default=str)}")
        for w in r["writedowns"][:25]:
            print(f"   - {w['item']}: {w['why']}")
        if r["detail"].get("writedowns_total", 0) > 25:
            print(f"   … {r['detail']['writedowns_total'] - 25} more")


def selftest() -> int:
    assert norm("nova_comet-tail.py") == "comet_tail" and norm("scripts/nova_x.sh") == "x"
    assert ratio(0, 5) is None and ratio(4, 1) == 0.25
    assert wish_ref(42).search("granted as wish #42") and not wish_ref(42).search("wish #420")
    assert created_tables("CREATE TABLE IF NOT EXISTS foo (a int); create table if not exists s.bar(") == ["foo", "s.bar"]
    g = grounded_topics([("Horology", "noted", False, 2), ("cbt", "noted", False, 0)], [("fire", "x [1] y")])
    assert set(g) == {"horology", "fire"}
    w = {"id": 7, "lineage": {"script": "nova_a.py (x)"}}
    assert wish_evidence(w, {"nova_a.py"}, {}, set(), set(), {}) == ["file nova_a.py exists"]
    assert wish_evidence({"id": 8, "lineage": {}}, set(), {"nova_mark_to_market.py": "wish #8"}, set(), set(), {}) == []
    assert due(date(2026, 11, 1), False) and not due(date(2026, 11, 2), True)
    rd = table_readers({"nova_w.py": "CREATE TABLE IF NOT EXISTS w_t (a int); SELECT a FROM w_t",
                        "nova_r.py": "import nova_w\n", "nova_q.py": "CREATE TABLE IF NOT EXISTS q_t (a int)"})
    assert rd["w_t"]["readers"] == ["nova_r.py"] and rd["q_t"]["readers"] == []
    first, still = unread_verdicts(["a", "b"], {"a": "2026-09"}, date(2026, 10, 1))
    assert first == {"a": "2026-09", "b": "2026-10"} and still == ["a"]
    real, wd = work_realised([("k1", "A", "m1", [5], None), ("k2", "B", "m2", [6], None), ("k3", "C", "m3", None, 9)],
                             {"m1"}, {9}, {"m2": {6}})
    assert real == 2 and len(wd) == 1 and "B" in wd[0]["item"]
    print("selftest ok")
    return 0


def _slate(argv: list[str]) -> int:
    """The Flop Slate (merged 2026-10-15): logic stays in nova_flop_slate.run_slate."""
    import nova_flop_slate
    return nova_flop_slate.run_slate(argv)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--slate" in argv:
        return _slate([x for x in argv if x != "--slate"])
    both = "--scheduled" in argv and not any(x in argv for x in ("--selftest", "--report", "-h", "--help"))
    rc, err = 0, None
    try:
        rc = _mark(argv)
    except Exception as e:  # noqa: BLE001 — remembered; the slate still runs, then the failure surfaces
        if not both:
            raise
        err = e
    if both:
        try:   # one entry, two books: the slate's quarterly step runs even if the marking failed
            rc = max(rc, _slate(["--scheduled"] + (["--dry-run"] if "--dry-run" in argv else [])))
        except Exception as e:  # noqa: BLE001
            log(f"flop slate step failed: {e}")
            rc = max(rc, 1)
        try:   # Definition of Done: grade organs' 30-day predictions (nova_prr, 2026-10-15)
            if "--dry-run" not in argv:
                import nova_prr
                import nova_dsn as _d
                _c = _d.pg_connect(); _c.autocommit = True
                nova_prr.check_predictions(_c.cursor()); _c.close()
        except Exception as e:  # noqa: BLE001
            log(f"organ prediction check failed (non-fatal): {e}")
    if err is not None:
        raise err
    return rc


def _mark(argv) -> int:
    ap = argparse.ArgumentParser(prog="nova_mark_to_market.py", description=__doc__.split("\n")[0],
                                 epilog="--slate [--scheduled|--quarter Q|--dry-run|--force|--report N]: "
                                        "the Flop Slate (see nova_flop_slate.py)")
    ap.add_argument("--scheduled", action="store_true",
                    help="daily; marks once a month, then the flop slate's once-a-quarter step")
    ap.add_argument("--dry-run", action="store_true", help="measure and print; write and notify nothing")
    ap.add_argument("--force", action="store_true", help="re-mark this month even if already booked")
    ap.add_argument("--report", nargs="?", const=6, type=int, metavar="N", help="last N months per class")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    return _mark_run(a, time.monotonic())


def _mark_run(a, t0) -> int:
    oc_conn = connect("nova_ops")
    oc = oc_conn.cursor()
    try:
        if a.report is not None:
            report(oc, max(1, a.report))
            return 0
        month = month_of(date.today())
        if not a.dry_run and not due(date.today(), have_month(oc, month), a.force):
            print(f"[mark-to-market] {month:%Y-%m} already marked; nothing to do")
            return 0
        mc_conn = connect("nova_memories")
        try:
            res = measure(oc, mc_conn.cursor())
        finally:
            mc_conn.close()
        prev = previous_ratios(oc, month)
        if a.dry_run:
            print_result(res, month)
            print(f"\n[dry-run] {time.monotonic() - t0:.1f}s — nothing written, nothing notified")
            return 0
        import nova_pg_schema
        nova_pg_schema.safe_ddl(oc, SCHEMA)
        write(oc, month, res)
        from nova_notify import notify
        low = [c for c in CLASSES if c in res and res[c]["ratio"] is not None and res[c]["ratio"] < 0.5]
        notify(f"Mark-to-market {month:%B %Y}: {len(low)} of {len(res)} books under 50% realised",
               body=render(res, month, prev), level="info", category="mark_to_market",
               dedup_key=f"mark_to_market:{month:%Y-%m}",
               meta={"month": f"{month:%Y-%m}", "ratios": {c: res[c]["ratio"] for c in CLASSES if c in res},
                     "retirement_signals": (res.get("unread_tables") or {}).get("detail", {}).get("retirement_signals", [])})
        log(f"{month:%Y-%m} marked: " + ", ".join(f"{c} {res[c]['ratio']}" for c in CLASSES if c in res)
            + f" ({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        oc_conn.close()


if __name__ == "__main__":
    sys.exit(main())
