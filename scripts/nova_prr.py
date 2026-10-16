#!/usr/bin/env python3
"""nova_prr.py — Production Readiness Review for every Nova component (SRE organ, 2026-10-13).

Owner: Jordan Koch (Little Mister). Purpose: the Google SRE book's Production Readiness Review
(ch.27 "Reliable Product Launches at Scale", ch.32 "The Evolving SRE Engagement Model", Appendix E
launch checklist) applied to Nova. Every component that runs in production -- each ENABLED task in
both scheduler configs (.6 config/scheduler.yaml and nova-core's scheduler-core.yaml) plus every
long-running service seen in service_registry / health_checks -- is scored against seven checks:

  tests      a test file in scripts/tests/ (test_<stem>.py / test_<stem>_*.py, or without nova_)
  docs       agent_docs content mentions the script (or service) name
  health     tasks: a recent successful scheduler_runs row (window = 2x schedule period + 1h, min 2h)
             services: a recent up/ok health_checks row (within 1h)
  chronic    not chronically failing: failure ratio below threshold over 7d (longer for slow
             schedules); a dead-lettered (quarantined) task fails
  header     a header docstring that states purpose AND owner ("Written by" / "Owner" / "Author")
  slo        covered by a row in slo_definitions; "n/a" while that table does not exist
  alerts     alerts only via nova_notify -- no direct Slack (SLACK_* / post_both / chat.postMessage ...)
  tier       has a service tier with RTO/RPO in service_tiers (nova_service_tiers.py); "n/a" while
             that table does not exist. Little Mister, 2026-10-15 (#35): no tier = FAIL.

A check is pass / fail / n/a (n/a = not applicable, e.g. tests for a third-party service). An
explicit exception lives in prr_waivers (component, check_name, reason) -- that is where a
"no-SLO reason" goes; a waived check counts as a pass and says so in its evidence.

Tables (nova_ops): prr_results (latest result per kind/component/check, upserted), prr_scores
(view: per-component score), prr_history (one row per full run), prr_waivers.

  nova_prr.py                      evaluate everything, store, print a summary
  nova_prr.py --dry-run            evaluate everything, print, write nothing
  nova_prr.py --digest             evaluate + store + weekly digest via nova_notify (scheduler)
  nova_prr.py --organ-dod          organ Definition of Done board (test, metric+baseline+prediction,
                                   agent_doc, owner Nova, retirement criterion) -> organ_registry
  nova_prr.py --component NAME     print one component's checklist (task id, service name or
                                   script stem/file); exit 1 if any check fails -- the gate Claude
                                   sessions run before calling new work "done"

Runs on the Mac (.6) or nova-core: paths resolve from this file, scheduler-core.yaml is read
locally when present, else over `ssh nova-core`, else skipped with a note.

Written by Jordan Koch (via Claude).
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
TESTS_DIR = SCRIPTS_DIR / "tests"
SIX_YAML = SCRIPTS_DIR.parent / "config" / "scheduler.yaml"
CORE_YAML_LOCAL = Path.home() / ".openclaw" / "config" / "scheduler-core.yaml"
CORE_SSH_HOST = "nova-core"

CHECKS = ("tests", "docs", "health", "chronic", "header", "slo", "alerts", "tier")
TASK_FAIL_THRESHOLD = 0.20       # >20% of finished runs failed/timed out in the window = chronic
SERVICE_DOWN_THRESHOLD = 0.05    # >5% of health checks down over 7d = chronic
QUARANTINE_THRESHOLD = 10        # mirrors nova_scheduler.QUARANTINE_THRESHOLD (dead-letter)
WEEK_S = 7 * 86400
SERVICE_FRESH_S = 3600
OK_SERVICE_STATUSES = {"up", "ok", "slow", "healthy"}
NOTIFY_LAYER = {"nova_notify", "nova_notifier", "nova_config"}   # the sanctioned delivery path
DIRECT_SLACK_RE = re.compile(
    r"\bSLACK_[A-Z_]+|post_both\s*\(|chat\.postMessage|slack_post\s*\(|post_slack\s*\("
    r"|hooks\.slack\.com|slack_bot_token\s*\(")
OWNER_RE = re.compile(r"written by|\bowner\b|\bauthor\b", re.I)
TOKENISH_RE = re.compile(r"xox[a-z]-[A-Za-z0-9-]+")


# ── schedule math ────────────────────────────────────────────────────────────

def parse_interval(spec: str) -> int:
    """'every 5m' / '4h' / '30s' / '1d' -> seconds (0 if unparseable)."""
    m = re.search(r"(\d+)\s*([smhd])", str(spec or ""))
    if not m:
        return 0
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def cron_period_s(expr: str) -> int:
    """Rough period of a 5-field cron expression -- good enough to size a freshness window."""
    f = str(expr or "").split()
    if len(f) != 5:
        return 86400
    minute, hour, dom, mon, dow = f
    if dom != "*" or mon != "*":
        return 31 * 86400
    if dow != "*":
        return WEEK_S
    if hour == "*":
        if minute.startswith("*/"):
            return max(60, int(minute[2:] or 1) * 60)
        if minute == "*":
            return 60
        return 3600 // max(1, len(minute.split(",")))
    if hour.startswith("*/"):
        return int(hour[2:] or 1) * 3600
    if "," in hour:
        return 86400 // len(hour.split(","))
    if "-" in hour:
        return 3600
    return 86400


def expected_period_s(schedule: str) -> int:
    s = str(schedule or "").strip()
    if s.startswith("every"):
        return parse_interval(s) or 3600
    if s.startswith("cron"):
        return cron_period_s(s[4:].strip())
    return 86400


def health_window_s(period: int) -> int:
    return max(2 * period + 3600, 7200)


def chronic_window_s(period: int) -> int:
    return max(WEEK_S, 3 * period)


# ── inventory ────────────────────────────────────────────────────────────────

def tasks_from_yaml(text: str, host: str) -> dict:
    """{task_id: {script, schedule, hosts}} for ENABLED tasks (same parse as nova_scheduler.load_config)."""
    import yaml
    raw = yaml.safe_load(text) or {}
    out = {}
    for tid, t in (raw.get("tasks") or {}).items():
        t = t or {}
        if not t.get("enabled", True):
            continue
        out[str(tid)] = {"script": os.path.basename(str(t.get("script", ""))),
                         "schedule": str(t.get("schedule", "")), "hosts": [host]}
    return out


def merge_tasks(*maps) -> dict:
    merged = {}
    for m in maps:
        for tid, t in m.items():
            if tid in merged:
                merged[tid]["hosts"] = sorted(set(merged[tid]["hosts"]) | set(t["hosts"]))
            else:
                merged[tid] = dict(t, hosts=list(t["hosts"]))
    return merged


def read_core_yaml():
    """(text or None, note). Local file on nova-core, else ssh, else skipped."""
    if CORE_YAML_LOCAL.exists():
        return CORE_YAML_LOCAL.read_text(), f"local {CORE_YAML_LOCAL}"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", CORE_SSH_HOST,
                            "cat ~/.openclaw/config/scheduler-core.yaml"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout, f"ssh {CORE_SSH_HOST}"
        return None, f"skipped: ssh {CORE_SSH_HOST} rc={r.returncode}"
    except Exception as e:  # noqa: BLE001 -- fail open, PRR still runs on the .6 task set
        return None, f"skipped: {type(e).__name__}"


def service_script(name: str, script_names: set):
    """Map a service name to a script in scripts/ when one exists (big_brother -> nova_big_brother.py)."""
    n = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    for cand in (f"nova_{n}.py", f"{n}.py", f"nova_{n}.sh", f"{n}.sh"):
        if cand in script_names:
            return cand
    return None


# ── per-check pure evaluators (each returns (passed: bool|None, evidence: str)) ──

def stem_of(script: str) -> str:
    return re.sub(r"\.(py|sh)$", "", os.path.basename(script or ""))


def check_tests(script, test_names) -> tuple:
    if not script:
        return None, "n/a: no script in repo (third-party / external service)"
    stem = stem_of(script)
    stems = [stem] + ([stem[5:]] if stem.startswith("nova_") and len(stem) > 5 else [])
    hits = sorted(n for n in test_names for s in stems
                  if n == f"test_{s}.py" or (n.startswith(f"test_{s}_") and n.endswith(".py")))
    if hits:
        return True, "tests/" + ", tests/".join(hits[:3])
    return False, f"no tests/test_{stem}*.py"


def mention_re(term: str):
    return re.compile(r"(?<![a-z0-9_])" + re.escape(term.lower()) + r"(?![a-z0-9_])")


def check_docs(terms, docs) -> tuple:
    """docs: [(doc_type, content_lower)]. Pass when any term is mentioned in any doc."""
    terms = list(dict.fromkeys(t for t in terms if t))
    for t in terms:
        rx = mention_re(t)
        found = [dt for dt, body in docs if rx.search(body)]
        if found:
            return True, f"'{t}' in agent_docs: " + ", ".join(found[:3])
    return False, "agent_docs never mentions " + " / ".join(f"'{t}'" for t in terms)


def header_text(source: str, suffix: str) -> str:
    if suffix == ".py":
        try:
            return ast.get_docstring(ast.parse(source)) or ""
        except (SyntaxError, ValueError):
            m = re.search(r'^\s*(?:#[^\n]*\n\s*)*[rRbBuU]?("""|\'\'\')(.*?)\1', source, re.S)
            return m.group(2) if m else ""
    lines = []
    for ln in source.splitlines()[:40]:
        if ln.startswith("#!"):
            continue
        if ln.startswith("#"):
            lines.append(ln.lstrip("#").strip())
        elif ln.strip() or lines:
            break
    return "\n".join(lines)


def check_header(script, source) -> tuple:
    if not script:
        return None, "n/a: no script in repo"
    if source is None:
        return False, f"{script} not found in scripts/"
    doc = header_text(source, Path(script).suffix)
    first = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
    if len(doc.strip()) < 20:
        return False, "no header docstring (purpose)"
    if not OWNER_RE.search(doc):
        return False, f"docstring has no owner line ('Written by ...'): {first[:70]}"
    return True, first[:90]


def docstring_lines(source: str) -> set:
    """Line numbers covered by module/class/function docstrings (prose, not code)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                skip.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return skip


def direct_slack_hits(source: str, python: bool = True) -> list:
    """[(lineno, snippet)] of direct-Slack usage, ignoring comments and docstrings."""
    hits = []
    skip = docstring_lines(source) if python else set()
    for i, ln in enumerate(source.splitlines(), 1):
        s = ln.strip()
        if not s or s.startswith("#") or i in skip:
            continue
        if DIRECT_SLACK_RE.search(s):
            hits.append((i, TOKENISH_RE.sub("xox?-***", s)[:80]))
    return hits


def check_alerts(script, source) -> tuple:
    if not script:
        return None, "n/a: no script in repo"
    if stem_of(script) in NOTIFY_LAYER:
        return True, "is the sanctioned notification layer"
    if source is None:
        return False, f"{script} not found in scripts/"
    hits = direct_slack_hits(source, python=script.endswith(".py"))
    if hits:
        ln, snip = hits[0]
        return False, f"{len(hits)} direct-Slack line(s); first L{ln}: {snip}"
    return True, "no direct Slack posting (alerts go through nova_notify or none)"


def check_task_health(stats, period_s, now_ms) -> tuple:
    """stats: dict with last_success_ms (or None)."""
    win = health_window_s(period_s)
    last = (stats or {}).get("last_success_ms")
    if not last:
        return False, f"no successful scheduler_runs row (window {win // 3600}h)"
    age = (now_ms - last) / 1000
    when = datetime.fromtimestamp(last / 1000, timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    if age <= win:
        return True, f"last success {when} ({age / 3600:.1f}h ago, window {win / 3600:.0f}h)"
    return False, f"last success {when} is {age / 3600:.1f}h old (> {win / 3600:.0f}h window)"


def check_task_chronic(stats, period_s, threshold=TASK_FAIL_THRESHOLD) -> tuple:
    stats = stats or {}
    if (stats.get("last_status") in ("failure", "timeout")
            and (stats.get("last_cfs") or 0) + 1 >= QUARANTINE_THRESHOLD):
        return False, f"dead-lettered: {stats['last_cfs'] + 1} consecutive failures"
    long_win = chronic_window_s(period_s) > WEEK_S
    total = stats.get("total_long" if long_win else "total_7d") or 0
    fails = stats.get("fails_long" if long_win else "fails_7d") or 0
    label = f"{chronic_window_s(period_s) // 86400}d"
    if total == 0:
        return None, f"n/a: no finished runs in {label}"
    ratio = fails / total
    ok = ratio < threshold
    return ok, f"{fails}/{total} failed in {label} ({ratio:.0%}; threshold {threshold:.0%})"


def check_service_health(stats, now_s) -> tuple:
    stats = stats or {}
    last_ok = stats.get("last_ok_s")
    if last_ok and now_s - last_ok <= SERVICE_FRESH_S:
        return True, f"health_checks up {int((now_s - last_ok) // 60)}m ago"
    if stats.get("reg_status") in OK_SERVICE_STATUSES and stats.get("reg_hb_s") \
            and now_s - stats["reg_hb_s"] <= SERVICE_FRESH_S:
        return True, f"service_registry {stats['reg_status']}, heartbeat {int((now_s - stats['reg_hb_s']) // 60)}m ago"
    if last_ok:
        return False, f"last up health check {(now_s - last_ok) / 3600:.1f}h ago"
    return False, "no up health_checks row in 7d"


def check_service_chronic(stats, threshold=SERVICE_DOWN_THRESHOLD) -> tuple:
    stats = stats or {}
    total, down = stats.get("total_7d") or 0, stats.get("down_7d") or 0
    if total == 0:
        return None, "n/a: no health_checks rows in 7d"
    ratio = down / total
    return ratio < threshold, f"{down}/{total} checks down in 7d ({ratio:.1%}; threshold {threshold:.0%})"


def slo_cover_columns(cols) -> list:
    exact = [c for c in cols if c in ("components", "covered_components", "covers")]
    if exact:
        return exact
    picked = [c for c in cols if re.search(r"compon|cover|service|task|target|scope", c, re.I)]
    return picked or list(cols)


def check_slo(slo_rows, cols, names, kind=None) -> tuple:
    """slo_rows None = table absent -> n/a. Else pass if any covering column mentions any name.
    A row listing the component "scheduler:*" (nova_slo's scheduled_jobs) covers every scheduled task."""
    if slo_rows is None:
        return None, "n/a: slo_definitions does not exist yet"
    cover = slo_cover_columns(cols)
    label_col = next((c for c in ("name", "slo_name", "slo_id", "id") if c in cols), cols[0] if cols else None)
    if kind == "task":
        for row in slo_rows:
            if row.get("enabled") is not False and "scheduler:*" in (row.get("components") or []):
                return True, f"SLO {row.get(label_col)!s:.60} covers every scheduled task"
    variants = set()
    for n in filter(None, names):
        base = n[5:] if n.startswith("nova_") else n
        variants |= {n, base, "nova_" + base, base.replace("_", "-"), base.replace("-", "_")}
    rxs = [mention_re(v) for v in sorted(variants)]
    for row in slo_rows:
        if row.get("enabled") is False:
            continue
        blob = " ".join(json.dumps(row.get(c), default=str).lower() for c in cover)
        if any(rx.search(blob) for rx in rxs):
            return True, f"SLO {row.get(label_col)!s:.60} covers it"
    return False, "no slo_definitions row covers it (add one, or a prr_waivers no-SLO reason)"


def check_tier(tier_rows, names) -> tuple:
    """tier_rows None = service_tiers absent -> n/a. Else pass iff a tier resolves (exact or glob)."""
    if tier_rows is None:
        return None, "n/a: service_tiers does not exist yet"
    from nova_service_tiers import tier_for
    r = tier_for([n for n in names if n], tier_rows)
    if not r:
        return False, "no service tier (nova_service_tiers.py --set <name> <0|1|2>)"
    return True, f"Tier{r['tier']} via {r['component']} (RTO {r['rto_s']}s, RPO {r['rpo_s']}s)"


def apply_waiver(result, reason) -> tuple:
    passed, ev = result
    if reason and passed is not True:
        return True, f"waived: {reason[:150]} (was: {ev[:80]})"
    return result


# ── evaluation ───────────────────────────────────────────────────────────────

def evaluate_component(comp, ctx) -> dict:
    """comp: {kind, name, script, schedule, hosts}. ctx: inventory + DB facts. -> {check: (passed, ev)}."""
    script = comp.get("script")
    source = ctx["sources"].get(script) if script else None
    stem = stem_of(script) if script else ""
    names = [comp["name"]] + ([stem] if stem and stem != comp["name"] else [])
    r = {
        "tests": check_tests(script, ctx["test_names"]),
        "docs": check_docs([stem or comp["name"]] + ([comp["name"]] if stem else []), ctx["docs"]),
        "header": check_header(script, source),
        "slo": check_slo(ctx.get("slo_rows"), ctx.get("slo_cols") or [], names, comp["kind"]),
        "alerts": check_alerts(script, source),
        "tier": check_tier(ctx.get("tier_rows"), names),
    }
    if comp["kind"] == "task":
        period = expected_period_s(comp.get("schedule"))
        st = ctx["task_stats"].get(comp["name"])
        r["health"] = check_task_health(st, period, ctx["now_ms"])
        r["chronic"] = check_task_chronic(st, period)
    elif comp["kind"] == "service":
        st = ctx["svc_stats"].get(comp["name"])
        r["health"] = check_service_health(st, ctx["now_ms"] / 1000)
        r["chronic"] = check_service_chronic(st)
    else:  # ad-hoc script not yet in production
        r["health"] = (False, "no scheduler entry and no health_checks rows -- not in production yet")
        r["chronic"] = (None, "n/a: never run in production")
    for chk in CHECKS:
        r[chk] = apply_waiver(r[chk], ctx["waivers"].get((comp["name"], chk)))
    return r


def score(results: dict) -> tuple:
    """-> (passed, failed, na, pct-or-None)."""
    p = sum(1 for v, _ in results.values() if v is True)
    f = sum(1 for v, _ in results.values() if v is False)
    n = sum(1 for v, _ in results.values() if v is None)
    return p, f, n, (round(100.0 * p / (p + f), 1) if p + f else None)


def summarize(evals) -> dict:
    """evals: [(comp, results)] -> overall %, worst components, most-failed checks."""
    P = F = N = 0
    fail_by_check = {c: 0 for c in CHECKS}
    per = []
    for comp, res in evals:
        p, f, n, pct = score(res)
        P, F, N = P + p, F + f, N + n
        for c, (v, _) in res.items():
            if v is False:
                fail_by_check[c] += 1
        per.append((pct if pct is not None else 100.0, -f, comp["kind"], comp["name"], f))
    per.sort()
    return {
        "components": len(evals), "passed": P, "failed": F, "na": N,
        "readiness_pct": round(100.0 * P / (P + F), 1) if P + F else 0.0,
        "fully_ready": sum(1 for x in per if x[4] == 0),
        "worst": [(k, n, pct, f) for pct, _, k, n, f in per[:10] if f],
        "fail_by_check": sorted(fail_by_check.items(), key=lambda kv: -kv[1]),
    }


def digest_text(summary, prev_pct=None) -> tuple:
    s = summary
    trend = "" if prev_pct is None else f" (last week {prev_pct:.1f}%)"
    title = f"PRR weekly: Nova is {s['readiness_pct']:.1f}% production-ready{trend}"
    lines = [f"{s['components']} components, {s['fully_ready']} fully ready; "
             f"{s['passed']} checks pass, {s['failed']} fail, {s['na']} n/a.", "",
             "10 least-ready components:"]
    lines += [f"  {pct:5.1f}%  {name} ({kind}, {f} failing)" for kind, name, pct, f in s["worst"]] or ["  (none)"]
    lines += ["", "Checks failed most often:"]
    lines += [f"  {c}: {n}" for c, n in s["fail_by_check"] if n] or ["  (none)"]
    lines += ["", "Gate before calling new work done: nova_prr.py --component <name>"]
    return title, "\n".join(lines)


# ── IO ───────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS prr_results (
  kind text NOT NULL, component text NOT NULL, check_name text NOT NULL,
  passed boolean, evidence text NOT NULL DEFAULT '', script text, hosts text,
  checked_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, component, check_name));
CREATE TABLE IF NOT EXISTS prr_waivers (
  component text NOT NULL, check_name text NOT NULL, reason text NOT NULL,
  waived_by text NOT NULL DEFAULT 'jordan', created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (component, check_name));
CREATE TABLE IF NOT EXISTS prr_history (
  run_at timestamptz PRIMARY KEY DEFAULT now(), components int, passed int, failed int,
  na int, fully_ready int, readiness_pct numeric(5,1));
CREATE OR REPLACE VIEW prr_scores AS
  SELECT kind, component, max(script) AS script, max(hosts) AS hosts,
         count(*) FILTER (WHERE passed) AS passed, count(*) FILTER (WHERE NOT passed) AS failed,
         count(*) FILTER (WHERE passed IS NULL) AS na,
         round(100.0 * count(*) FILTER (WHERE passed)
               / nullif(count(*) FILTER (WHERE passed IS NOT NULL), 0), 1) AS score_pct,
         string_agg(check_name, ',' ORDER BY check_name) FILTER (WHERE NOT passed) AS failing_checks,
         max(checked_at) AS checked_at
  FROM prr_results GROUP BY kind, component;
"""


def connect():
    sys.path.insert(0, str(SCRIPTS_DIR))
    import nova_dsn
    return nova_dsn.pg_connect("nova_ops")


def gather(cur, only_tasks=None, only_services=None) -> dict:
    """Pull every DB fact the checks need in a handful of queries (optionally scoped, for --component)."""
    now_ms = int(time.time() * 1000)
    cur.execute("SELECT doc_type, lower(content) FROM agent_docs")
    docs = cur.fetchall()
    long_ms = now_ms - 93 * 86400 * 1000
    wk_ms = now_ms - WEEK_S * 1000
    cur.execute("""
        SELECT task_id, max(started_at) FILTER (WHERE status='success'),
               count(*) FILTER (WHERE started_at > %s AND status IN ('failure','timeout')),
               count(*) FILTER (WHERE started_at > %s AND status IN ('success','failure','timeout')),
               count(*) FILTER (WHERE status IN ('failure','timeout')),
               count(*) FILTER (WHERE status IN ('success','failure','timeout'))
        FROM scheduler_runs WHERE started_at > %s AND (%s OR task_id = ANY(%s))
        GROUP BY task_id""", (wk_ms, wk_ms, long_ms, only_tasks is None, only_tasks or []))
    task_stats = {r[0]: {"last_success_ms": r[1], "fails_7d": r[2], "total_7d": r[3],
                         "fails_long": r[4], "total_long": r[5]} for r in cur.fetchall()}
    cur.execute("""
        SELECT DISTINCT ON (task_id) task_id, status, consecutive_failures_at_start
        FROM scheduler_runs WHERE started_at > %s AND status IN ('success','failure','timeout')
          AND (%s OR task_id = ANY(%s))
        ORDER BY task_id, started_at DESC""", (long_ms, only_tasks is None, only_tasks or []))
    for tid, status, cfs in cur.fetchall():
        task_stats.setdefault(tid, {}).update(last_status=status, last_cfs=cfs)
    cur.execute("""
        SELECT service_name,
               extract(epoch FROM max(checked_at) FILTER (WHERE lower(status) = ANY(%s))),
               count(*), count(*) FILTER (WHERE lower(status) <> ALL(%s))
        FROM health_checks WHERE checked_at > now() - interval '7 days'
          AND (%s OR service_name = ANY(%s)) GROUP BY 1""",
                (list(OK_SERVICE_STATUSES), list(OK_SERVICE_STATUSES),
                 only_services is None, only_services or []))
    svc = {r[0]: {"last_ok_s": float(r[1]) if r[1] else None, "total_7d": r[2], "down_7d": r[3]}
           for r in cur.fetchall()}
    cur.execute("""SELECT service_name, bool_or(lower(status) = ANY(%s)),
                          extract(epoch FROM max(last_heartbeat)) FROM service_registry
                   WHERE %s OR service_name = ANY(%s) GROUP BY 1""",
                (list(OK_SERVICE_STATUSES), only_services is None, only_services or []))
    for name, any_up, hb in cur.fetchall():
        svc.setdefault(name, {}).update(reg_status="up" if any_up else "down",
                                        reg_hb_s=float(hb) if hb else None)
    slo_rows, slo_cols = None, []
    cur.execute("SELECT to_regclass('public.slo_definitions') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT * FROM slo_definitions")
        slo_cols = [d[0] for d in cur.description]
        slo_rows = [dict(zip(slo_cols, r)) for r in cur.fetchall()]
    cur.execute("SELECT to_regclass('public.prr_waivers') IS NOT NULL")
    waivers = {}
    if cur.fetchone()[0]:
        cur.execute("SELECT component, check_name, reason FROM prr_waivers")
        waivers = {(c, k): r for c, k, r in cur.fetchall()}
    return {"now_ms": now_ms, "docs": docs, "task_stats": task_stats, "svc_stats": svc,
            "slo_rows": slo_rows, "slo_cols": slo_cols, "waivers": waivers}


def inventory():
    """-> (components, notes, script_names)."""
    notes = []
    script_names = {p.name for p in SCRIPTS_DIR.iterdir() if p.suffix in (".py", ".sh")}
    six = {}
    if SIX_YAML.exists():
        six = tasks_from_yaml(SIX_YAML.read_text(), ".6")
        notes.append(f".6 scheduler: {len(six)} enabled tasks ({SIX_YAML})")
    else:
        notes.append(f".6 scheduler: skipped, {SIX_YAML} missing")
    core_text, how = read_core_yaml()
    core = tasks_from_yaml(core_text, "core") if core_text else {}
    notes.append(f"core scheduler: {len(core)} enabled tasks ({how})")
    comps = [{"kind": "task", "name": tid, **t} for tid, t in sorted(merge_tasks(six, core).items())]
    return comps, notes, script_names


def add_services(comps, ctx, script_names):
    for name in sorted(ctx["svc_stats"]):
        comps.append({"kind": "service", "name": name, "script": service_script(name, script_names),
                      "schedule": "", "hosts": []})


def build_ctx(cur, only_tasks=None, only_services=None):
    ctx = gather(cur, only_tasks, only_services)
    ctx["test_names"] = [p.name for p in TESTS_DIR.glob("test_*.py")]
    try:                                   # #35 service tiers; absent table -> check is n/a
        sys.path.insert(0, str(SCRIPTS_DIR))
        from nova_service_tiers import load
        ctx["tier_rows"] = load(cur)
    except Exception as e:  # noqa: BLE001 - a tier lookup failure must not sink the whole review
        cur.connection.rollback() if not getattr(cur.connection, "autocommit", True) else None
        print(f"service tiers unavailable: {e}", file=sys.stderr)
        ctx["tier_rows"] = None
    return ctx


def load_sources(comps, ctx):
    srcs = {}
    for c in comps:
        s = c.get("script")
        if s and s not in srcs:
            p = SCRIPTS_DIR / s
            try:
                srcs[s] = p.read_text(errors="replace")
            except OSError:
                srcs[s] = None
    ctx["sources"] = srcs


def store(cur, evals, summary):
    rows = []
    for comp, res in evals:
        for chk in CHECKS:
            v, ev = res[chk]
            rows.append((comp["kind"], comp["name"], chk, v, ev, comp.get("script"),
                         ",".join(comp.get("hosts") or [])))
    from psycopg2.extras import execute_values
    execute_values(cur, """
        INSERT INTO prr_results (kind, component, check_name, passed, evidence, script, hosts)
        VALUES %s ON CONFLICT (kind, component, check_name) DO UPDATE SET
          passed=EXCLUDED.passed, evidence=EXCLUDED.evidence, script=EXCLUDED.script,
          hosts=EXCLUDED.hosts, checked_at=now()""", rows)
    # Components that left production (task removed/disabled, service gone) drop out of the board.
    # now() is the transaction start, i.e. exactly the checked_at every row upserted above carries.
    cur.execute("DELETE FROM prr_results WHERE checked_at < now()")
    cur.execute("""INSERT INTO prr_history (components, passed, failed, na, fully_ready, readiness_pct)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (summary["components"], summary["passed"], summary["failed"], summary["na"],
                 summary["fully_ready"], summary["readiness_pct"]))


def print_checklist(comp, res):
    p, f, n, pct = score(res)
    hosts = ",".join(comp.get("hosts") or []) or "-"
    print(f"PRR {comp['kind']} {comp['name']}  script={comp.get('script') or '-'}  hosts={hosts}  "
          f"score={'-' if pct is None else f'{pct:.0f}%'} ({p} pass, {f} fail, {n} n/a)")
    for chk in CHECKS:
        v, ev = res[chk]
        mark = {True: "PASS", False: "FAIL", None: "n/a "}[v]
        print(f"  [{mark}] {chk:8s} {ev}")


# ── Organ Definition of Done (plan item #32, 2026-10-15) ─────────────────────
# Little Mister: "Nova's organs are for Nova." An organ is done only when Nova can tell whether it
# earns its keep: a test, a named metric with a recorded baseline and a predicted effect, an agent_doc,
# an owner (Nova), and a retirement criterion. Separate from the component checks above (no score impact);
# stored per organ in organ_registry and flagged in the weekly digest. flop_slate / mark_to_market can
# call check_predictions(cur) (or the pure prediction_check) to grade the prediction at 30 days.

DOD_CHECKS = ("test", "metric", "baseline", "prediction", "agent_doc", "owner", "retirement")
PREDICTION_DAYS = 30
ORGAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS organ_registry (
  organ text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE organ_registry
  ADD COLUMN IF NOT EXISTS metric text,
  ADD COLUMN IF NOT EXISTS metric_sql text,
  ADD COLUMN IF NOT EXISTS baseline numeric,
  ADD COLUMN IF NOT EXISTS baseline_at timestamptz,
  ADD COLUMN IF NOT EXISTS predicted_effect text,
  ADD COLUMN IF NOT EXISTS predicted_direction text,
  ADD COLUMN IF NOT EXISTS predicted_pct numeric,
  ADD COLUMN IF NOT EXISTS owner text DEFAULT 'nova',
  ADD COLUMN IF NOT EXISTS retirement_criterion text,
  ADD COLUMN IF NOT EXISTS inventory_from text[],
  ADD COLUMN IF NOT EXISTS dod_pass boolean,
  ADD COLUMN IF NOT EXISTS dod_failing text[],
  ADD COLUMN IF NOT EXISTS dod_evidence jsonb,
  ADD COLUMN IF NOT EXISTS dod_checked_at timestamptz,
  ADD COLUMN IF NOT EXISTS prediction_verdict text,
  ADD COLUMN IF NOT EXISTS prediction_detail text,
  ADD COLUMN IF NOT EXISTS prediction_checked_at timestamptz,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();
"""
REG_FIELDS = ("metric", "metric_sql", "baseline", "baseline_at", "predicted_effect", "predicted_direction",
              "predicted_pct", "owner", "retirement_criterion")
# Header declarations an organ can carry instead of (or before) a registry row, one per line:
#   Metric: <name>   Baseline: <number> (<YYYY-MM-DD>)   Predicted effect: <text, e.g. "down 20%">
#   Retire when: <criterion>   Owner: Nova
_DECL = {
    "metric": re.compile(r"^\s*metric\s*:\s*(.+)$", re.I | re.M),
    "baseline": re.compile(r"^\s*baseline\s*:\s*(-?\d+(?:\.\d+)?)\s*(?:\((\d{4}-\d{2}-\d{2})\))?", re.I | re.M),
    "predicted_effect": re.compile(r"^\s*predicted effect\s*:\s*(.+)$", re.I | re.M),
    "retirement_criterion": re.compile(r"^\s*(?:retire when|retirement(?: criterion)?)\s*:\s*(.+)$", re.I | re.M),
    "owner": re.compile(r"^\s*owner\s*:\s*(\w+)", re.I | re.M),
}
_EFFECT = re.compile(r"\b(up|down|rise|fall|increase|decrease|drop|grow|shrink)\w*\b[^0-9-]*(\d+(?:\.\d+)?)?\s*%?", re.I)
_READ_ONLY_SQL = re.compile(r"^\s*(SELECT|WITH)\b(?!.*;\s*\S)", re.I | re.S)


def parse_declarations(header: str) -> dict:
    """Organ header -> registry fields it declares. Pure."""
    out = {}
    for k, rx in _DECL.items():
        m = rx.search(header or "")
        if not m:
            continue
        if k == "baseline":
            out["baseline"] = float(m.group(1))
            if m.group(2):
                out["baseline_at"] = m.group(2)
        else:
            out[k] = m.group(1).strip()[:300]
    if out.get("predicted_effect"):
        out.update(effect_terms(out["predicted_effect"]))
    return out


def effect_terms(text: str) -> dict:
    """'down 20%' -> {'predicted_direction': 'down', 'predicted_pct': 20.0}. Pure."""
    m = _EFFECT.search(text or "")
    if not m:
        return {}
    d = "up" if m.group(1).lower()[:2] in ("up", "ri", "in", "gr") else "down"
    return {"predicted_direction": d, "predicted_pct": float(m.group(2)) if m.group(2) else 0.0}


def organ_dod(script, reg, test_names, docs) -> dict:
    """{check: (passed, evidence)} for one organ. reg = its organ_registry row (dict, may be empty). Pure."""
    reg = reg or {}
    stem = stem_of(script)
    return {
        "test": check_tests(script, test_names),
        "metric": (True, reg["metric"]) if reg.get("metric") else (False, "no named metric"),
        "baseline": ((True, f"{reg['baseline']} at {reg.get('baseline_at')}") if reg.get("baseline") is not None
                     and reg.get("baseline_at") else (False, "no recorded baseline (value + date)")),
        "prediction": (True, reg["predicted_effect"]) if reg.get("predicted_effect") else (False, "no predicted effect"),
        "agent_doc": check_docs([script, stem], docs),
        "owner": ((True, "nova") if str(reg.get("owner") or "").strip().lower() == "nova"
                  else (False, f"owner is {reg.get('owner')!r}, not Nova")),
        "retirement": ((True, reg["retirement_criterion"]) if reg.get("retirement_criterion")
                       else (False, "no retirement criterion")),
    }


def prediction_check(row: dict, now: datetime, current=None) -> tuple:
    """Grade an organ's prediction PREDICTION_DAYS after its baseline. -> (verdict, detail) where verdict is
    'not_due' | 'unmeasurable' | 'hit' | 'miss'. row: organ_registry fields. Pure — the hook flop_slate calls."""
    base, at = row.get("baseline"), row.get("baseline_at")
    direction = row.get("predicted_direction")
    if base is None or not at or direction not in ("up", "down"):
        return "unmeasurable", "no baseline/date/direction to grade against"
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = (now - at).days
    if age < PREDICTION_DAYS:
        return "not_due", f"baseline is {age}d old (< {PREDICTION_DAYS}d)"
    if current is None:
        return "unmeasurable", "metric has no current value (no metric_sql or it returned nothing)"
    base, current = float(base), float(current)
    change = (current - base) / abs(base) * 100.0 if base else (100.0 if current > 0 else -100.0 if current < 0 else 0.0)
    want = float(row.get("predicted_pct") or 0.0)
    hit = change >= want if direction == "up" else change <= -want
    hit = hit and change != 0.0
    return ("hit" if hit else "miss"), f"{base:g} -> {current:g} ({change:+.1f}%), predicted {direction} {want:g}%"


def safe_metric_sql(sql) -> bool:
    """Only a single SELECT/WITH statement is evaluated (and only on a read-only session). Pure."""
    return bool(sql) and bool(_READ_ONLY_SQL.match(sql))


def _readonly_value(sql, connect=None):
    """First column of the first row of sql, on its own READ ONLY session (a write raises in PG)."""
    conn = (connect or (lambda: __import__("nova_dsn").pg_connect("nova_ops")))()
    try:
        conn.set_session(readonly=True, autocommit=False)
        c = conn.cursor()
        c.execute("SET LOCAL statement_timeout = '30s'")
        c.execute(sql)
        r = c.fetchone()
        return r[0] if r else None
    finally:
        conn.rollback()
        conn.close()


def organ_inventory_scripts(cur) -> dict:
    """script -> [why it is an organ], from mark_to_market's shared inventory (read-only). Falls back to the
    registry if that module cannot load."""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import nova_mark_to_market as mtm
        inv = mtm.organ_inventory(cur, mtm.script_texts(), mtm.load_scheduler(), mtm.load_wishes(cur))
        return {k: v.get("from", []) for k, v in inv.items()}
    except Exception as e:  # noqa: BLE001
        print(f"organ inventory unavailable ({e}); using organ_registry only", file=sys.stderr)
        try:
            cur.connection.rollback()
            cur.execute("SELECT organ, coalesce(inventory_from, '{}') FROM organ_registry")
            return {o: list(f) for o, f in cur.fetchall()}
        except Exception:  # noqa: BLE001
            return {}


def load_registry(cur) -> dict:
    cur.execute("SELECT to_regclass('public.organ_registry') IS NOT NULL")
    if not cur.fetchone()[0]:
        return {}
    cur.execute("SELECT organ, " + ", ".join(REG_FIELDS) + " FROM organ_registry")
    return {r[0]: dict(zip(REG_FIELDS, r[1:])) for r in cur.fetchall()}


def evaluate_organs(cur, test_names=None, docs=None) -> list:
    """[(script, reg_merged, results, from)] for every organ in the inventory."""
    inv = organ_inventory_scripts(cur)
    reg = load_registry(cur)
    if docs is None:
        cur.execute("SELECT doc_type, lower(content) FROM agent_docs")
        docs = cur.fetchall()
    test_names = test_names if test_names is not None else [p.name for p in TESTS_DIR.glob("test_*.py")]
    out = []
    for script in sorted(set(inv) | set(reg)):
        try:
            hdr = header_text((SCRIPTS_DIR / script).read_text(errors="replace"), Path(script).suffix)
        except OSError:
            hdr = ""
        merged = {"owner": "nova", **{k: v for k, v in parse_declarations(hdr).items()},
                  **{k: v for k, v in (reg.get(script) or {}).items() if v is not None}}
        out.append((script, merged, organ_dod(script, merged, test_names, docs), inv.get(script, [])))
    return out


def dod_summary(organs) -> dict:
    fail_by = {c: 0 for c in DOD_CHECKS}
    failing = []
    for script, _, res, _ in organs:
        bad = [c for c in DOD_CHECKS if res[c][0] is False]
        for c in bad:
            fail_by[c] += 1
        if bad:
            failing.append((script, bad))
    return {"organs": len(organs), "pass": len(organs) - len(failing), "fail": len(failing),
            "fail_by_check": fail_by, "failing": failing}


def dod_digest_lines(s) -> str:
    lines = ["", f"Organ Definition of Done: {s['pass']}/{s['organs']} organs pass, {s['fail']} flagged.",
             "  missing most: " + ", ".join(f"{c} {n}" for c, n in sorted(s["fail_by_check"].items(),
                                                                          key=lambda kv: -kv[1]) if n)]
    lines += [f"  FLAG {o}: {', '.join(b)}" for o, b in s["failing"][:10]]
    return "\n".join(lines)


def store_organs(cur, organs):
    try:
        from nova_pg_schema import safe_ddl
        safe_ddl(cur, ORGAN_SCHEMA)
    except ImportError:
        cur.execute(ORGAN_SCHEMA)
    for script, merged, res, why in organs:
        bad = [c for c in DOD_CHECKS if res[c][0] is False]
        decl = {k: merged.get(k) for k in REG_FIELDS}
        cur.execute("""INSERT INTO organ_registry (organ, metric, metric_sql, baseline, baseline_at, predicted_effect,
                           predicted_direction, predicted_pct, owner, retirement_criterion, inventory_from,
                           dod_pass, dod_failing, dod_evidence, dod_checked_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,now(),now())
                       ON CONFLICT (organ) DO UPDATE SET
                           metric = coalesce(organ_registry.metric, EXCLUDED.metric),
                           baseline = coalesce(organ_registry.baseline, EXCLUDED.baseline),
                           baseline_at = coalesce(organ_registry.baseline_at, EXCLUDED.baseline_at),
                           predicted_effect = coalesce(organ_registry.predicted_effect, EXCLUDED.predicted_effect),
                           predicted_direction = coalesce(organ_registry.predicted_direction, EXCLUDED.predicted_direction),
                           predicted_pct = coalesce(organ_registry.predicted_pct, EXCLUDED.predicted_pct),
                           retirement_criterion = coalesce(organ_registry.retirement_criterion, EXCLUDED.retirement_criterion),
                           inventory_from = EXCLUDED.inventory_from, dod_pass = EXCLUDED.dod_pass,
                           dod_failing = EXCLUDED.dod_failing, dod_evidence = EXCLUDED.dod_evidence,
                           dod_checked_at = now(), updated_at = now()""",
                    (script, decl["metric"], decl["metric_sql"], decl["baseline"], decl["baseline_at"],
                     decl["predicted_effect"], decl["predicted_direction"], decl["predicted_pct"],
                     decl["owner"] or "nova", decl["retirement_criterion"], list(why), not bad, bad,
                     json.dumps({c: {"passed": v, "evidence": e} for c, (v, e) in res.items()})))


def check_predictions(cur, now=None, metric_connect=None) -> list:
    """The 30-day hook for flop_slate / mark_to_market: grade every organ whose baseline is >= 30 days old and
    has not been graded in the last 30 days. Runs the organ's metric_sql only if it is a single SELECT/WITH,
    on a separate READ ONLY session. Stores prediction_verdict/detail. -> [(organ, verdict, detail)]."""
    now = now or datetime.now(timezone.utc)
    cur.execute("SELECT to_regclass('public.organ_registry') IS NOT NULL")
    if not cur.fetchone()[0]:
        return []
    cur.execute("""SELECT organ, """ + ", ".join(REG_FIELDS) + """ FROM organ_registry
                   WHERE baseline_at IS NOT NULL AND baseline_at <= %s - make_interval(days => %s)
                     AND (prediction_checked_at IS NULL OR prediction_checked_at <= %s - make_interval(days => %s))""",
                (now, PREDICTION_DAYS, now, PREDICTION_DAYS))
    out = []
    for r in cur.fetchall():
        row = dict(zip(REG_FIELDS, r[1:]))
        current = None
        if safe_metric_sql(row.get("metric_sql")):
            try:
                current = _readonly_value(row["metric_sql"], metric_connect)
            except Exception as e:  # noqa: BLE001
                print(f"metric_sql for {r[0]} failed: {e}", file=sys.stderr)
        verdict, detail = prediction_check(row, now, current)
        if verdict != "not_due":
            cur.execute("""UPDATE organ_registry SET prediction_verdict=%s, prediction_detail=%s,
                           prediction_checked_at=%s WHERE organ=%s""", (verdict, detail, now, r[0]))
        out.append((r[0], verdict, detail))
    return out


def organ_dod_in_txn(cur):
    """run_organ_dod inside a savepoint: a DoD failure never sinks the PRR store/digest. -> summary or None."""
    try:
        cur.execute("SAVEPOINT organ_dod")
        s = run_organ_dod(cur, write=True, verbose=False)
        cur.execute("RELEASE SAVEPOINT organ_dod")
        return s if isinstance(s, dict) and "organs" in s else None
    except Exception as e:  # noqa: BLE001
        try:
            cur.execute("ROLLBACK TO SAVEPOINT organ_dod")
        except Exception:  # noqa: BLE001
            pass
        print(f"organ DoD skipped: {e}", file=sys.stderr)
        return None


def run_organ_dod(cur, write=True, verbose=True) -> dict:
    organs = evaluate_organs(cur)
    s = dod_summary(organs)
    if verbose:
        print(dod_digest_lines(s).strip())
    if write:
        store_organs(cur, organs)
    return s


def match_component(query, comps):
    q = query.strip()
    qs = stem_of(q)
    return [c for c in comps if q == c["name"] or (c.get("script") and qs == stem_of(c["script"]))]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova Production Readiness Review (SRE book ch.27/32, App. E)")
    ap.add_argument("--component", help="print one component's checklist (task id, service, or script)")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and print; write nothing")
    ap.add_argument("--digest", action="store_true", help="also send the weekly digest via nova_notify")
    ap.add_argument("--json", action="store_true", help="with --component: JSON output")
    ap.add_argument("--organ-dod", action="store_true", help="only the organ Definition of Done board (#32)")
    a = ap.parse_args(argv)
    if a.organ_dod:
        conn = connect(); conn.autocommit = False; cur = conn.cursor()
        s = run_organ_dod(cur, write=not a.dry_run)
        for o, bad in s["failing"]:
            print(f"  FLAG {o}: {', '.join(bad)}")
        conn.commit()
        return 0

    comps, notes, script_names = inventory()
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()
    if a.component:
        q = stem_of(a.component.strip())
        tasks = match_component(a.component, comps)
        ctx = build_ctx(cur, [c["name"] for c in tasks],
                        sorted({a.component.strip(), q, q[5:] if q.startswith("nova_") else q}))
        services = []
        add_services(services, ctx, script_names)
        hits = tasks + match_component(a.component, services)
        if not hits:
            fname = a.component if a.component.endswith((".py", ".sh")) else f"{a.component}.py"
            if fname in script_names:
                hits = [{"kind": "script", "name": stem_of(fname), "script": fname, "schedule": "", "hosts": []}]
        if not hits:
            print(f"no component, task, service or script matches {a.component!r}")
            return 2
        load_sources(hits, ctx)
        worst = 0
        out = []
        for c in hits:
            res = evaluate_component(c, ctx)
            out.append({"kind": c["kind"], "component": c["name"], "script": c.get("script"),
                        "checks": {k: {"passed": v, "evidence": e} for k, (v, e) in res.items()}})
            if not a.json:
                print_checklist(c, res)
            worst = max(worst, 1 if any(v is False for v, _ in res.values()) else 0)
        if a.json:
            print(json.dumps(out, indent=2))
        return worst

    ctx = build_ctx(cur)
    add_services(comps, ctx, script_names)
    load_sources(comps, ctx)
    evals = [(c, evaluate_component(c, ctx)) for c in comps]
    summary = summarize(evals)
    for n in notes:
        print(n)
    print(f"{summary['components']} components; readiness {summary['readiness_pct']}% "
          f"({summary['passed']} pass / {summary['failed']} fail / {summary['na']} n/a); "
          f"{summary['fully_ready']} fully ready")
    print("fails by check: " + ", ".join(f"{c}={n}" for c, n in summary["fail_by_check"]))
    for kind, name, pct, f in summary["worst"]:
        print(f"  worst {pct:5.1f}% {kind} {name} ({f} failing)")
    if a.dry_run:
        print("dry run: nothing written")
        return 0
    cur.execute(SCHEMA)
    prev = None
    cur.execute("""SELECT readiness_pct FROM prr_history WHERE run_at < now() - interval '6 days'
                   ORDER BY run_at DESC LIMIT 1""")
    r = cur.fetchone()
    if r:
        prev = float(r[0])
    dod = organ_dod_in_txn(cur) if a.digest else None   # #32: same transaction, flagged in the digest
    store(cur, evals, summary)
    conn.commit()
    print(f"stored {len(evals) * len(CHECKS)} results in prr_results")
    if a.digest:
        from nova_notify import notify
        title, body = digest_text(summary, prev)
        if dod:
            body += "\n" + dod_digest_lines(dod)
        week = datetime.now().strftime("%G-W%V")
        notify(title, body, level="info", category="digest", source="nova_prr.py",
               dedup_key=f"prr-weekly-{week}",
               meta={"readiness_pct": summary["readiness_pct"], "components": summary["components"]})
        print(f"digest queued: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
