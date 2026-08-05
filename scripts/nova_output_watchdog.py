#!/usr/bin/env python3
"""
nova_output_watchdog.py — reliability coverage watchdog for Nova.

THE PROBLEM THIS CLOSES
-----------------------
Nova's failures are invisible. For a 6-day window the incidents table recorded
ZERO rows while Burbank shipped 33-char "Not logged in" stubs, the NAS sync was
broken, and journal pushes silently dropped. Jordan finds out by NOTICING, not
by being paged. That is a detection gap, not a feature gap.

WHAT IT DOES
------------
For every EXPECTED recurring output (declared in EXPECTATIONS below) it verifies
the output actually HAPPENED, was FRESH, and is REAL (not a stub, and actually
served to readers). On a miss it:

  1. records a row in public.incidents (the system of record), deduped against
     any already-open incident for the same logical key, and
  2. alerts #nova-warning via nova_config.post_both — but ONLY on the transition
     to open, so a persistent failure does not respam every run.

When a previously-failing output is healthy again it RESOLVES the open incident
(status='resolved', resolved_at=now()). That is what makes MTBF measurable.

At the end it logs `coverage: X/Y expected outputs healthy` and records that
number in public.health_checks so reliability becomes a trend, not a vibe.

Adding a new expectation is a one-liner in EXPECTATIONS.

USAGE
-----
  nova_output_watchdog.py            # one real pass (record + alert + resolve)
  nova_output_watchdog.py --once     # same (explicit single pass)
  nova_output_watchdog.py --dry-run  # report what it WOULD flag; write nothing
  nova_output_watchdog.py --help
"""
import argparse
import os
import re
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# nova_config lives beside this script; import is best-effort so tests and
# --dry-run still work on a host without Slack creds.
try:
    import nova_config
except Exception:  # pragma: no cover - only when run outside ~/.openclaw/scripts
    nova_config = None

DSN = "host=localhost dbname=nova_ops user=kochj"
# System of record — the EXISTING tables. Constants (never user input) so tests
# can redirect to a throwaway temp table without touching production rows.
INCIDENTS_TABLE = "public.incidents"
HEALTH_TABLE = "public.health_checks"
TZ = ZoneInfo("America/Los_Angeles")
SITE_BASE = "https://nova.digitalnoise.net"
JOURNAL_ROOT = os.path.expanduser("~/nova-journal")
STUB_MIN_WORDS = 150
CADENCE_MULTIPLIER = 2.0          # alert if newest older than 2x median gap
BACKUP_MAX_AGE_HOURS = 30

# Only sections whose slug is one of these safe chars ever reaches a URL/glob.
_SAFE_SECTION = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})-")


# ── CONFIG-DRIVEN EXPECTATIONS ──────────────────────────────────────────────
# Every recurring output Nova is SUPPOSED to produce. One dict = one contract.
#   kind: "journal_daily"   -> must publish today by `deadline` (LA time)
#         "journal_cadence" -> cadence DERIVED from history; stalled = miss
#         "backup"          -> telemetry.backup_runs job must be fresh+ok
# Add a new watched output by appending one entry here.
EXPECTATIONS = [
    {"key": "journal:local",       "kind": "journal_daily",   "section": "local",
     "deadline": "11:30", "severity": "high",    "reader_check": True},
    {"key": "journal:operations",  "kind": "journal_daily",   "section": "operations",
     "deadline": "18:30", "severity": "high",    "reader_check": True},
    {"key": "journal:essays",      "kind": "journal_daily",   "section": "essays",
     "deadline": "13:00", "severity": "warning", "reader_check": True},
    {"key": "journal:opinions",    "kind": "journal_daily",   "section": "opinions",
     "deadline": "13:00", "severity": "warning", "reader_check": True},
    # Unknown cadence — DERIVED from post history, never hardcoded daily.
    {"key": "journal:research",    "kind": "journal_cadence", "section": "research",
     "severity": "warning"},
    {"key": "journal:tech-today",  "kind": "journal_cadence", "section": "tech-today",
     "severity": "warning"},
    {"key": "journal:synthesis",   "kind": "journal_cadence", "section": "synthesis",
     "severity": "warning"},
    # Backups: every manifest-sync job must have a fresh ok=true row. The
    # legacy `:incremental` jobs are being retired — deliberately NOT watched.
    {"key": "backups",             "kind": "backup",
     "job_pattern": "nova-backup:%:manifest-sync", "severity": "high"},
]


# ── Findings ────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    """One evaluated fact about an expected output."""
    dedup_key: str                       # stable logical key (date-independent)
    ok: bool
    title: str = ""                      # human title for the incident
    severity: str = "warning"
    affected_services: list = field(default_factory=list)
    root_cause: str = ""
    note: str = ""                       # appended to events on dedup hit


# ── Pure helpers (unit-tested) ──────────────────────────────────────────────
def parse_post_date(filename):
    """Extract the YYYY-MM-DD date encoded in a journal filename, else None."""
    m = _DATE_PREFIX.match(os.path.basename(filename))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def strip_frontmatter(text):
    """Drop a leading YAML `---`…`---` block; return the body."""
    if text.lstrip().startswith("---"):
        # find the two fence lines
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def word_count(text):
    """Word count of the article body (frontmatter excluded)."""
    return len(strip_frontmatter(text).split())


def is_stub(text, min_words=STUB_MIN_WORDS):
    """A published-but-tiny article is still a failure (the Burbank 33-char bug)."""
    return word_count(text) <= min_words


def median_gap_days(dates):
    """Median gap in days between consecutive dated posts (newest-first ok)."""
    ds = sorted(set(dates))
    if len(ds) < 2:
        return None
    gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    return statistics.median(gaps)


def cadence_max_age(dates, multiplier=CADENCE_MULTIPLIER):
    """Max acceptable age (days) for a section's newest post, from its history.

    2x the median gap: a genuinely-weekly section will not false-alarm, but one
    that has clearly stalled will. Returns None when history is too thin.
    """
    med = median_gap_days(dates)
    if med is None:
        return None
    return max(multiplier * med, med + 1)


def dedup_logical_key(expectation_key, failure_type):
    """Stable, date-INDEPENDENT dedup key.

    Date-independent so tomorrow's miss of the same output opens a FRESH incident
    (after today's resolves) — that is what makes MTBF countable — while repeated
    runs during one outage fold into the one open incident.
    """
    return f"{expectation_key}:{failure_type}"


def safe_section(section):
    """Guard against URL/glob/path injection via a section name."""
    return bool(_SAFE_SECTION.match(section or ""))


def reader_url(section, base=SITE_BASE):
    """Build the live section-index URL. Rejects unsafe section names."""
    if not safe_section(section):
        raise ValueError(f"unsafe section name: {section!r}")
    return f"{base}/{section}/"


def slug_of(filename):
    """Filename stem == the live URL slug (date prefix included)."""
    return os.path.basename(filename)[:-3] if filename.endswith(".md") else os.path.basename(filename)


def deadline_passed(now_dt, deadline_str):
    """Has HH:MM (in now_dt's tz) already passed today?"""
    hh, mm = (int(x) for x in deadline_str.split(":"))
    return (now_dt.hour, now_dt.minute) >= (hh, mm)


# ── Filesystem access ───────────────────────────────────────────────────────
def section_posts(root, section):
    """Sorted list of dated post paths for a section (oldest→newest)."""
    if not safe_section(section):
        raise ValueError(f"unsafe section name: {section!r}")
    d = os.path.join(root, "content", section)
    if not os.path.isdir(d):
        return []
    posts = []
    for name in os.listdir(d):
        if name.endswith(".md") and parse_post_date(name):
            posts.append(os.path.join(d, name))
    posts.sort(key=lambda p: (parse_post_date(p), os.path.basename(p)))
    return posts


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── Retry wrapper ───────────────────────────────────────────────────────────
def with_retry(fn, attempts=3, base_delay=0.5, label="op"):
    """Run fn() with retries. Raises the last error if ALL attempts fail — a
    watchdog that dies silently is the worst-case irony, so we never swallow."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - deliberate: retry any transient
            last = e
            if i + 1 < attempts:
                time.sleep(base_delay * (2 ** i))
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last}") from last


def http_get(url, timeout=10):
    """GET url -> (status_code, body_text). Retried for transient failures."""
    import urllib.request

    def _do():
        req = urllib.request.Request(url, headers={"User-Agent": "nova-output-watchdog"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")

    return with_retry(_do, label=f"http_get {url}")


# ── Checks (return list[Finding]) ───────────────────────────────────────────
def check_journal_daily(exp, root, now_dt, http=http_get):
    """Daily section: fresh today by deadline, not a stub, and live for readers."""
    key, section = exp["key"], exp["section"]
    sev = exp.get("severity", "warning")
    today = now_dt.date()
    posts = section_posts(root, section)
    findings = []

    if not posts:
        if deadline_passed(now_dt, exp["deadline"]):
            findings.append(Finding(
                dedup_logical_key(key, "missing"), ok=False,
                title=f"{section} daily article missing ({today})", severity=sev,
                affected_services=[key], root_cause="no dated posts in section",
                note=f"no posts found for {section} after deadline {exp['deadline']}"))
        return findings

    newest = posts[-1]
    newest_date = parse_post_date(newest)
    fresh_today = newest_date == today

    # 1) freshness / deadline
    if fresh_today:
        findings.append(Finding(dedup_logical_key(key, "missing"), ok=True,
                                affected_services=[key]))
    elif deadline_passed(now_dt, exp["deadline"]):
        findings.append(Finding(
            dedup_logical_key(key, "missing"), ok=False,
            title=f"{section} daily article missing ({today})", severity=sev,
            affected_services=[key],
            root_cause=f"newest post is {newest_date}, deadline {exp['deadline']} passed",
            note=f"newest={newest_date}, expected today={today}"))
    # else: before deadline and not yet published -> pending, not counted

    # 2) stub detection (only meaningful once something fresh exists)
    if fresh_today:
        wc = word_count(read_text(newest))
        findings.append(Finding(
            dedup_logical_key(key, "stub"), ok=wc > STUB_MIN_WORDS,
            title=f"{section} published but stub/short ({today})", severity=sev,
            affected_services=[key],
            root_cause=f"newest post only {wc} words (<= {STUB_MIN_WORDS})",
            note=f"word_count={wc}"))

    # 3) reader-visible check (a different SLI from "pushed to git")
    if fresh_today and exp.get("reader_check"):
        slug = slug_of(newest)
        try:
            status, body = http(reader_url(section))
            live = status == 200 and slug in body
            findings.append(Finding(
                dedup_logical_key(key, "not-live"), ok=live,
                title=f"{section} in git but not live ({today})", severity=sev,
                affected_services=[key],
                root_cause=f"HTTP {status}; slug present={slug in body}",
                note=f"reader-check status={status} slug_present={slug in body}"))
        except Exception as e:  # persistent fetch failure = its own miss, logged
            log(f"reader-check {section} failed persistently: {e}")
            findings.append(Finding(
                dedup_logical_key(key, "not-live"), ok=False,
                title=f"{section} live index unreachable ({today})", severity=sev,
                affected_services=[key], root_cause=str(e),
                note=f"reader-check unreachable: {e}"))

    return findings


def check_journal_cadence(exp, root, now_dt):
    """Unknown-cadence section: DERIVE expected gap, flag only if truly stalled."""
    key, section = exp["key"], exp["section"]
    sev = exp.get("severity", "warning")
    today = now_dt.date()
    posts = section_posts(root, section)
    findings = []

    if len(posts) < 2:
        log(f"cadence {section}: <2 posts, cannot derive cadence — skipping")
        return findings

    dates = [parse_post_date(p) for p in posts[-8:]]
    max_age = cadence_max_age(dates)
    newest = posts[-1]
    age = (today - parse_post_date(newest)).days
    med = median_gap_days(dates)
    log(f"cadence {section}: median_gap={med}d threshold={max_age}d age={age}d")

    if max_age is not None:
        findings.append(Finding(
            dedup_logical_key(key, "stalled"), ok=age <= max_age,
            title=f"{section} stalled — {age}d since last post (cadence ~{med}d)",
            severity=sev, affected_services=[key],
            root_cause=f"age {age}d > {max_age:.1f}d (2x median gap {med}d)",
            note=f"age={age}d median={med}d threshold={max_age:.1f}d"))

    # stub check on the newest post regardless of cadence
    wc = word_count(read_text(newest))
    findings.append(Finding(
        dedup_logical_key(key, "stub"), ok=wc > STUB_MIN_WORDS,
        title=f"{section} newest post is a stub ({parse_post_date(newest)})",
        severity=sev, affected_services=[key],
        root_cause=f"newest post only {wc} words", note=f"word_count={wc}"))

    return findings


def check_backups(exp, conn, now_dt):
    """Each manifest-sync job must have a fresh ok=true row (< max age)."""
    sev = exp.get("severity", "high")
    findings = []
    rows = with_retry(lambda: _query(
        conn,
        """
        SELECT DISTINCT ON (job) job, ok, ts
        FROM telemetry.backup_runs
        WHERE job LIKE %s
        ORDER BY job, ts DESC
        """,
        (exp["job_pattern"],)), label="backup query")
    if not rows:
        findings.append(Finding(
            dedup_logical_key(exp["key"], "no-jobs"), ok=False,
            title="No manifest-sync backup jobs recorded", severity=sev,
            affected_services=["backup:manifest-sync"],
            root_cause=f"no rows for {exp['job_pattern']}",
            note="no backup_runs rows matched pattern"))
        return findings

    cutoff = now_dt.astimezone(timezone.utc)
    for job, ok, ts in rows:
        age_h = (cutoff - ts).total_seconds() / 3600.0
        healthy = bool(ok) and age_h <= BACKUP_MAX_AGE_HOURS
        findings.append(Finding(
            dedup_logical_key("backup", job), ok=healthy,
            title=f"Backup stale/failed: {job}", severity=sev,
            affected_services=[f"backup:{job}"],
            root_cause=f"ok={ok}, age={age_h:.1f}h (max {BACKUP_MAX_AGE_HOURS}h)",
            note=f"ok={ok} age={age_h:.1f}h ts={ts.isoformat()}"))
    return findings


# ── PG helpers ──────────────────────────────────────────────────────────────
def connect():
    import psycopg2
    return with_retry(lambda: psycopg2.connect(DSN, connect_timeout=5),
                      label="pg connect")


def _query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def find_open_incident(conn, dedup_key):
    """Return (id, events) of an OPEN incident carrying this dedup key, else None."""
    import json
    rows = _query(
        conn,
        f"SELECT id, events FROM {INCIDENTS_TABLE} "
        "WHERE status = 'open' AND events @> %s::jsonb LIMIT 1",
        (json.dumps([{"dedup_key": dedup_key}]),))
    return rows[0] if rows else None


def open_or_append(conn, f, now_iso, dry_run):
    """New miss -> INSERT; already open -> append a timestamped event (dedup).

    Returns "opened" (transition to open, caller should alert), "appended", or
    "dry" — never a duplicate row.
    """
    import json
    existing = find_open_incident(conn, f.dedup_key)
    if existing:
        if dry_run:
            return "dry"
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {INCIDENTS_TABLE} SET events = events || %s::jsonb WHERE id = %s",
                (json.dumps([{"ts": now_iso, "note": f.note}]), existing[0]))
        conn.commit()
        return "appended"
    if dry_run:
        return "dry"
    events = json.dumps([{"dedup_key": f.dedup_key, "ts": now_iso, "note": f.note}])
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {INCIDENTS_TABLE} "
            "(title, root_cause, severity, status, affected_services, events) "
            "VALUES (%s, %s, %s, 'open', %s, %s::jsonb) RETURNING id",
            (f.title, f.root_cause, f.severity, f.affected_services, events))
        conn.commit()
    return "opened"


def resolve_if_open(conn, dedup_key, now_iso, dry_run):
    """Recovery: mark any open incident for this key resolved. Makes MTBF real."""
    import json
    existing = find_open_incident(conn, dedup_key)
    if not existing:
        return False
    if dry_run:
        return True
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {INCIDENTS_TABLE} "
            "SET status = 'resolved', resolved_at = now(), "
            "    events = events || %s::jsonb "
            "WHERE id = %s AND status = 'open'",
            (json.dumps([{"ts": now_iso, "note": "auto-resolved: output healthy again"}]),
             existing[0]))
    conn.commit()
    return True


def record_coverage(conn, healthy, total, dry_run):
    """Write the SLI to public.health_checks so reliability is trendable."""
    if dry_run:
        return
    status = "ok" if healthy == total else "degraded"
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {HEALTH_TABLE} "
            "(service_name, node_name, checked_by, status, latency_ms, error_message) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("nova_output_watchdog", socket.gethostname(), "nova_output_watchdog.py",
             status, None, f"coverage: {healthy}/{total} expected outputs healthy"))
    conn.commit()


# ── Orchestration ───────────────────────────────────────────────────────────
def log(msg):
    print(f"[output_watchdog] {msg}", file=sys.stderr)


def alert(msg):
    if nova_config is None:
        log(f"(no nova_config) would alert: {msg}")
        return
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY,
                              discord_channel=None)
    except Exception as e:  # never let a Slack blip abort recording
        log(f"alert failed: {e}")


def git_pull_journal():
    import subprocess
    try:
        subprocess.run(["git", "-C", JOURNAL_ROOT, "pull", "-q", "origin", "main"],
                       check=False, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"git pull skipped: {e}")


def evaluate_all(root, now_dt, conn):
    """Run every expectation. Returns (findings, per_output_health)."""
    findings = []
    for exp in EXPECTATIONS:
        try:
            if exp["kind"] == "journal_daily":
                fs = check_journal_daily(exp, root, now_dt)
            elif exp["kind"] == "journal_cadence":
                fs = check_journal_cadence(exp, root, now_dt)
            elif exp["kind"] == "backup":
                fs = check_backups(exp, conn, now_dt) if conn else []
            else:
                log(f"unknown expectation kind: {exp['kind']}")
                fs = []
        except Exception as e:
            # A check that throws must NOT silently pass — record it loudly.
            log(f"expectation {exp['key']} raised: {e}")
            fs = [Finding(dedup_logical_key(exp["key"], "watchdog-error"), ok=False,
                          title=f"watchdog check errored for {exp['key']}",
                          severity="warning", affected_services=[exp["key"]],
                          root_cause=str(e), note=f"check raised: {e}")]
        findings.extend(fs)
    return findings


def run(dry_run=False):
    started = time.time()
    now_dt = datetime.now(TZ)
    now_iso = now_dt.isoformat()

    git_pull_journal()

    conn = None
    try:
        conn = connect()
    except Exception as e:
        # We cannot record incidents without PG. Fail LOUD, never silent-pass.
        log(f"FATAL: cannot reach PG: {e}")
        if not dry_run:
            return 2

    findings = evaluate_all(JOURNAL_ROOT, now_dt, conn)

    # Group by affected output (expectation key) for coverage.
    opened_alerts = []
    outputs = {}  # exp_key -> healthy(bool)
    for f in findings:
        exp_key = f.affected_services[0] if f.affected_services else f.dedup_key
        outputs.setdefault(exp_key, True)
        if not f.ok:
            outputs[exp_key] = False
            if conn:
                res = open_or_append(conn, f, now_iso, dry_run)
                verb = "WOULD OPEN" if dry_run else res.upper()
                log(f"MISS {f.dedup_key}: {f.title} [{verb}]")
                if res == "opened":
                    opened_alerts.append(f)
            else:
                log(f"MISS {f.dedup_key}: {f.title} [NO-PG]")
        else:
            if conn:
                if resolve_if_open(conn, f.dedup_key, now_iso, dry_run):
                    log(f"RESOLVED {f.dedup_key}"
                        + (" [dry]" if dry_run else ""))

    healthy = sum(1 for v in outputs.values() if v)
    total = len(outputs)

    # Alert ONLY on transition-to-open, so persistent misses do not respam.
    if not dry_run:
        for f in opened_alerts:
            alert(f":rotating_light: *{f.title}*\n{f.root_cause}\n"
                  f"affected: {', '.join(f.affected_services)}")
        if conn:
            record_coverage(conn, healthy, total, dry_run)

    elapsed = time.time() - started
    log(f"coverage: {healthy}/{total} expected outputs healthy "
        f"({elapsed:.1f}s{', dry-run' if dry_run else ''})")
    print(f"coverage: {healthy}/{total} expected outputs healthy")

    if conn:
        conn.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what it WOULD flag; write no incidents, alert nothing")
    ap.add_argument("--once", action="store_true",
                    help="run a single pass (default behavior)")
    args = ap.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
