#!/opt/homebrew/bin/python3
"""nova_journal_publish_check.py — published-vs-expected verifier for the journal.

WHY (incident 2026-10-18 16:36 -> 10-19 09:27): `claude` was unreachable for the article
generators (the core scheduler leader failed over to nova-core3, whose CLI lives in ~/.local/bin,
off the scheduler's PATH; .6's Homebrew link was also mid-update). 16 article jobs failed and
several — journal_security (the BLUF), journal_emergency_* — logged the failure and EXITED 0.
The scheduler recorded success, nothing alerted, and Jordan found out by asking where the BLUF was.

WHAT: derive every article that SHOULD exist from the article-publishing tasks in BOTH scheduler
configs (core leader + .6): cron schedule (minute/hour/day-of-month/month/day-of-week), task args,
and the script -> section mapping; then check what actually landed on origin/main of the Hugo repo
(fresh `git fetch`), using
  1. nova_ops.journal_publish_log — the ledger nova_journal.publish_hugo/git_push now write
     (script + args identify the task exactly; the path is then confirmed on origin/main),
  2. the article watchdog's per-script section/matcher table, read against origin/main,
  3. scheduler_runs (never ran / failed / "succeeded" with an LLM-outage signature).
Missing articles are posted briefly to Slack #nova-alerts with each one's error tail, then each
missing job is re-run ONCE (journal_publish_reruns PK = the once-only guard), serially, under a PG
advisory lock, re-checking right before each run so nothing double-publishes.

Not an alert: a job that exited 0 on purpose ("no fresh material", "quiet day") — logged as SKIPPED.

Usage:
  nova_journal_publish_check.py              check, alert, re-run missing once
  nova_journal_publish_check.py --dry-run    report only: no Slack, no re-runs, no PG writes
  nova_journal_publish_check.py --no-rerun   check + alert, never re-run
  nova_journal_publish_check.py --hours 26   look-back window (default 26h)

Scheduled on .6 (scheduler.yaml: journal_publish_check, 10:00 / 14:00 / 22:30).
Written by Jordan Koch (via Claude), 2026-10-19.
"""
import argparse
import datetime as dt
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

HUGO = Path.home() / "nova-journal"
CONFIG_DIR = Path("/Volumes/nas/nova-fs/config")
SCHEDULER_CONFIGS = (("core", CONFIG_DIR / "scheduler-core.yaml"), (".6", CONFIG_DIR / "scheduler.yaml"))
LOOKBACK_H = 26
RERUN_MAX_AGE_H = 20          # older misses are reported, not re-run (tonight's run will cover it)
RERUN_LOCK_KEY = 47110816     # pg advisory lock: one checker / re-run at a time (push lock is ...815)
MIN_GRACE_MIN = 30
MAX_GRACE_MIN = 180

# Scripts whose source mentions publishing but which are not daily article generators.
NOT_ARTICLE_SCRIPTS = {
    "nova_article_watchdog.py", "nova_journal_publish_check.py", "nova_journal_lint.py",
    "nova_journal_stats_poller.py", "nova_fix_missing_images.py", "nova_backfill_tags.py",
    "nova_cross_linker.py", "nova_razor_blade.py", "nova_continuity_review.py",
    "nova_lessons_daily.py", "nova_meta_analysis.py",
    # 2026-10-19: generators run through the fold wrapper become SECTIONS of the evening digest
    # (no standalone article) — the digest itself (journal_digest) is what gets verified.
    "nova_digest_fold.py",
    "nova_journal_digest_email.py",     # e-mails the digest; publishes nothing
}
# publish_hugo/git_push calls, raw writers into the Hugo clone, or importing another generator's
# publish() (nova_weekly_flights borrows nova_local_burbank.publish).
_PUBLISH_CALL = re.compile(r"\b(?:publish_hugo|git_push)\s*\(|nova-journal|^\s*from\s+nova_\w+\s+import\s+[^\n]*\bpublish\b",
                           re.M)

# What a run that "succeeded" but really hit an LLM outage looks like in its output.
OUTAGE_MARKERS = (
    "no such file or directory: 'claude'", "claude code call failed", "timed out after",
    "llm unavailable", "llm produced nothing", "generation failed", "claude code cli not found",
    "not logged in",
)


def log(msg):
    print(f"[publish-check {dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── cron ────────────────────────────────────────────────────────────────────────

def _cron_field(spec: str, lo: int, hi: int) -> set:
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return out


def parse_cron(schedule: str):
    """'cron M H DOM MON DOW' -> matcher(datetime) or None for non-cron (interval) schedules."""
    m = re.match(r"^\s*cron\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", str(schedule or ""))
    if not m:
        return None
    mi, hr, dom, mon, dow = m.groups()
    sets = (_cron_field(mi, 0, 59), _cron_field(hr, 0, 23), _cron_field(dom, 1, 31),
            _cron_field(mon, 1, 12), {d % 7 for d in _cron_field(dow, 0, 7)})
    dom_star, dow_star = dom == "*", dow == "*"

    def match(t: dt.datetime) -> bool:
        if t.minute not in sets[0] or t.hour not in sets[1] or t.month not in sets[3]:
            return False
        d_ok, w_ok = t.day in sets[2], ((t.weekday() + 1) % 7) in sets[4]
        if dom_star or dow_star:            # Vixie cron: both restricted -> either may match
            return d_ok and w_ok
        return d_ok or w_ok
    return match


def last_due(schedule: str, start: dt.datetime, end: dt.datetime):
    """Latest cron occurrence in (start, end], minute resolution; None if none."""
    match = parse_cron(schedule)
    if not match:
        return None
    t = end.replace(second=0, microsecond=0)
    while t > start:
        if match(t):
            return t
        t -= dt.timedelta(minutes=1)
    return None


# ── expected articles ───────────────────────────────────────────────────────────

def is_article_script(script: str) -> bool:
    name = Path(script).name
    if name in NOT_ARTICLE_SCRIPTS:
        return False
    try:
        src = (SCRIPTS / name).read_text(errors="ignore")
    except OSError:
        return False
    return bool(_PUBLISH_CALL.search(src))


def load_tasks(configs=SCHEDULER_CONFIGS) -> list:
    """Enabled cron article tasks from both schedulers, one entry per task id (core wins a tie:
    .6's `enabled: false` means 'migrated to the core leader', not 'retired')."""
    import yaml
    seen = {}
    for owner, path in configs:
        try:
            cfg = yaml.safe_load(Path(path).read_text()) or {}
        except Exception as e:  # noqa: BLE001
            log(f"cannot read {path}: {e}")
            continue
        tasks = cfg.get("tasks") or {}
        for tid, t in tasks.items():
            if not isinstance(t, dict) or t.get("enabled", True) is False:
                continue
            if tid in seen or not parse_cron(t.get("schedule")):
                continue
            script = str(t.get("script") or "")
            if not script.endswith(".py") or not is_article_script(script):
                continue
            seen[tid] = {"task": tid, "script": Path(script).name,
                         "args": [str(a) for a in (t.get("args") or [])],
                         "schedule": t["schedule"], "timeout": int(t.get("timeout") or 600),
                         "owner": owner}
    return list(seen.values())


def grace_minutes(timeout_s: int) -> int:
    return int(min(MAX_GRACE_MIN, max(MIN_GRACE_MIN, timeout_s / 60 + 10)))


def expected(now: dt.datetime, tasks: list, lookback_h: int = LOOKBACK_H) -> list:
    """The latest due occurrence of each task inside the window that is past its grace period."""
    out = []
    for t in tasks:
        due = last_due(t["schedule"], now - dt.timedelta(hours=lookback_h), now)
        if not due:
            continue
        if now - due < dt.timedelta(minutes=grace_minutes(t["timeout"])):
            continue                     # still inside its run/timeout window
        out.append(dict(t, due=due))
    return out


# ── evidence ───────────────────────────────────────────────────────────────────

def git(*args, timeout=60):
    try:
        return subprocess.run(["git", *args], cwd=HUGO, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timeout")


_ORIGIN_NAMES = []
_ORIGIN_ERRORS = []      # any git read of origin failed this pass -> evidence incomplete -> no re-runs


def origin_has(path: str) -> bool:
    """On origin/main at that path — or under any section (a section merge may have moved it)."""
    if git("cat-file", "-e", f"origin/main:{path}", timeout=30).returncode == 0:
        return True
    if not _ORIGIN_NAMES:
        r = git("ls-tree", "-r", "--name-only", "origin/main", "content/", timeout=60)
        _ORIGIN_NAMES.append({Path(p).name for p in r.stdout.split()})
    return Path(path).name in _ORIGIN_NAMES[0]


def origin_section_files(section: str) -> list:
    r = git("ls-tree", "--name-only", f"origin/main:content/{section}", timeout=30)
    if r.returncode != 0:
        _ORIGIN_ERRORS.append(f"ls-tree {section}: {r.stderr.strip()[:80]}")
        return []
    return r.stdout.split()


def disk_published(item: dict):
    """Fallback for runs the ledger has no row for: the watchdog's script -> (section, matcher)
    table, evaluated against origin/main. Returns (True|False|None, detail); None = no mapping."""
    try:
        import nova_article_watchdog as wd
    except Exception:  # noqa: BLE001
        return None, "watchdog map unavailable"
    if item["script"] == "nova_journal.py":
        section, (kind, want) = wd._journal_section(item["args"][0] if item["args"] else ""), ("slug", "")
    elif item["script"] in wd.ARTICLE_SCRIPTS:
        section, (kind, want) = wd.ARTICLE_SCRIPTS[item["script"]]
    else:
        return None, "no section mapping"
    if not section:
        return None, "no section mapping"
    files = origin_section_files(section)
    if kind == "stable":
        if f"{want}.md" not in files:
            return False, f"{section}/{want}.md not on origin"
        r = git("log", "-1", "--format=%cI", "origin/main", "--", f"content/{section}/{want}.md")
        try:
            when = dt.datetime.fromisoformat(r.stdout.strip()).astimezone().replace(tzinfo=None)
        except ValueError:
            return False, f"{want}.md: no commit date"
        return (when >= item["due"] - dt.timedelta(minutes=5)), f"{section}/{want}.md updated {when:%m-%d %H:%M}"
    stamps = {item["due"].strftime("%Y-%m-%d"), (item["due"] + dt.timedelta(days=1)).strftime("%Y-%m-%d")}
    dated = [f for f in files if f[:10] in stamps and f.endswith(".md")]
    if kind == "slug" and want:
        dated = [f for f in dated if want in f]
    elif kind == "tag" and want:
        hits = []
        for f in dated:
            head = git("show", f"origin/main:content/{section}/{f}", timeout=30).stdout[:800]
            m = re.search(r"^tags:\s*\[(.*?)\]", head, re.M)
            if m and want.lower() in m.group(1).lower():
                hits.append(f)
        dated = hits
    return (bool(dated), f"{section}/{dated[0]}" if dated else f"nothing in {section}/ for {sorted(stamps)[0]}")


def added_index(days: int = 35) -> list:
    """[(commit_epoch, 'content/<section>/<file>.md')] for every article ADDED on origin/main lately."""
    r = git("log", f"--since={days}.days", "--diff-filter=A", "--name-only", "--format=@%ct",
            "origin/main", "--", "content/", timeout=60)
    if r.returncode != 0:
        _ORIGIN_ERRORS.append(f"log: {r.stderr.strip()[:80]}")
    out, ct = [], None
    for ln in r.stdout.splitlines():
        if ln.startswith("@"):
            ct = int(ln[1:])
        elif ln.endswith(".md") and ct and not ln.endswith("_index.md"):
            out.append((ct, ln.strip()))
    return out


def _slug_tokens(path: str) -> list:
    name = Path(path).stem
    name = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", name)
    return [t for t in name.split("-") if t]


def learn_signatures(runs: list, index: list) -> list:
    """Learn how a task's articles are NAMED from its own history: the articles added on origin/main
    while each past SUCCESSFUL run was executing. A signature is a 1-4 word filename phrase that
    shows up in >=70% of those runs (e.g. 'watches-and-friends', 'security-intelligence-briefing')
    and is distinctive (rare elsewhere in the last 35 days). Jobs that share a start time (weekly_flights
    and journal_synthesis both at Sun 19:00) learn each other's names too — that only makes the check
    lean towards "published", which is the safe side: it can miss an alert, never double-publish.
    Section-agnostic on purpose: sections get renamed (2026-10-19 merged five of them)."""
    per_run = []
    for start, end in runs:
        files = [p for ct, p in index if start - 5 <= ct <= (end or start) + 180]
        if files:
            grams = set()
            for p in files:
                toks = _slug_tokens(p)[:8]
                grams |= {"-".join(toks[i:i + n]) for n in range(1, 5) for i in range(len(toks) - n + 1)}
            per_run.append(grams)
    if len(per_run) < 2:
        return []
    counts = {}
    for grams in per_run:
        for g in grams:
            counts[g] = counts.get(g, 0) + 1
    names = [Path(p).name for _, p in index]
    good = []
    for g, c in counts.items():
        if c < max(2, 0.7 * len(per_run)) or len(g) < 6 or re.fullmatch(r"[\d-]+", g):
            continue
        if sum(1 for n in names if g in n) > 1.5 * c + 2:      # too common: 'this-week', 'burbank'...
            continue
        good.append(g)
    return [g for g in good if not any(g != o and g in o for o in good)]   # keep the maximal phrases


def learned_published(item: dict, history: list, index: list):
    """(True|None, detail): an article carrying one of this task's learned signatures was added after due."""
    sigs = learn_signatures(history, index)
    if not sigs:
        return None, "no learned signature"
    since = item["due"].timestamp() - 300
    hits = [p for ct, p in index if ct >= since and any(g in Path(p).name for g in sigs)]
    return (True, hits[-1]) if hits else (None, f"no {'/'.join(sigs[:3])} article since due")


def run_history(cur, task_id: str, days: int = 30) -> list:
    """[(start_epoch, end_epoch)] of the task's successful runs — the training data for learn_signature."""
    cur.execute("""SELECT started_at/1000, coalesce(ended_at, started_at)/1000 FROM scheduler_runs
                   WHERE task_id = %s AND status = 'success' AND coalesce(exit_code, 0) = 0
                     AND started_at > (extract(epoch from now()) - %s * 86400) * 1000
                   ORDER BY started_at DESC LIMIT 40""", (task_id, days))
    return [(int(a), int(b)) for a, b in cur.fetchall()]


def ledger_rows(cur, item: dict, until: dt.datetime) -> list:
    cur.execute("""SELECT event, path, host, at FROM journal_publish_log
                   WHERE script = %s AND args = %s AND at >= %s AND at <= %s
                   ORDER BY at""",
                (item["script"], " ".join(item["args"]),
                 (item["due"] - dt.timedelta(minutes=2)).astimezone(), until.astimezone()))
    return cur.fetchall()


def ledger_instrumented(cur, script: str) -> bool:
    cur.execute("SELECT 1 FROM journal_publish_log WHERE script = %s AND at > now() - interval '30 days' LIMIT 1",
                (script,))
    return cur.fetchone() is not None


def runs_since(cur, task_id: str, since: dt.datetime) -> list:
    cur.execute("""SELECT status, exit_code, coalesce(error_tail,''), coalesce(stdout_tail,''),
                          to_timestamp(started_at/1000.0)
                   FROM scheduler_runs
                   WHERE task_id = %s AND started_at >= %s
                   ORDER BY started_at DESC LIMIT 5""",
                (task_id, int((since - dt.timedelta(minutes=1)).timestamp() * 1000)))
    return cur.fetchall()


def error_tail(run) -> str:
    """One short line explaining a failed run: the last meaningful line of its output."""
    if not run:
        return "no scheduler run recorded"
    status, code, err, out = run[0], run[1], run[2] or "", run[3] or ""
    lines = [ln.strip() for ln in (err + "\n" + out).splitlines() if ln.strip()]
    errs = [ln.strip() for ln in err.splitlines() if ln.strip()]
    # Root cause first: OUTAGE_MARKERS is ordered most-specific first ("No such file ... 'claude'"
    # beats the generator's own "generation failed"); else a traceback's last line beats stdout chatter.
    pick = next((ln for m in OUTAGE_MARKERS for ln in reversed(lines) if m in ln.lower()),
                (errs or lines or [""])[-1])
    pick = re.sub(r"^[^\[\]]*\]\s*", "", pick)                        # a tail cut mid-timestamp: "10-18 17:20:51] "
    pick = re.sub(r"^(\[[^\]]*\]\s*)+", "", pick)                     # drop "[ts] [journal]" prefixes
    return f"{status} exit {code}" + (f": {pick[:160]}" if pick else "")


def has_outage_marker(run) -> bool:
    if not run:
        return False
    text = f"{run[2]}\n{run[3]}".lower()
    return any(m in text for m in OUTAGE_MARKERS)


def classify(item: dict, ledger: list, runs: list, instrumented: bool, disk, learned=(None, ""),
             ever_ran: bool = True) -> tuple:
    """-> (state, detail). States: OK, RUNNING, STRANDED, SKIPPED, NEW, MISSING.
    disk = the watchdog-map check, learned = the learned-signature check: either finding the
    article wins (it may have been re-run by hand after a failure — never re-run it again)."""
    written = [r for r in ledger if r[0] == "written"]
    pushed = [r for r in ledger if r[0] in ("push:pushed", "push:committed_not_pushed", "push:nothing")]
    for r in written:
        if r[1] and origin_has(r[1]):
            return "OK", r[1]
    if written:
        return "STRANDED", f"{written[-1][1]} written on {written[-1][2]} but not on origin yet"
    if pushed:
        return "OK", f"pushed ({pushed[-1][0]}) from {pushed[-1][2]}"
    ok, detail = disk if disk else (None, "")
    if ok:
        return "OK", detail
    if learned and learned[0]:
        return "OK", learned[1]
    last = runs[0] if runs else None
    if last and last[0] == "running":
        return "RUNNING", "job is still running"
    if not last:
        if not ever_ran:
            return "NEW", "no successful run on record yet (new task?) — not re-running"
        return "MISSING", "never ran (no scheduler run since it was due)"
    failed = last[0] != "success" or (last[1] or 0) != 0
    if failed or has_outage_marker(last):
        return "MISSING", error_tail(last)
    if ok is False or instrumented:
        return "SKIPPED", "exit 0, nothing published (generator chose to skip) — " + error_tail(last)
    return "OK", "exit 0 (unverified: no ledger row / section mapping yet)"


# ── actions ─────────────────────────────────────────────────────────────────────

def _pg():
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=10)
    c.autocommit = True
    return c


def claim_rerun(cur, item: dict) -> bool:
    """Insert-first claim: True exactly once per (task, due) — the never-twice guarantee."""
    cur.execute("""INSERT INTO journal_publish_reruns (task_id, due_at, host, result)
                   VALUES (%s, %s, %s, 'claimed') ON CONFLICT DO NOTHING RETURNING task_id""",
                (item["task"], item["due"].astimezone(), socket.gethostname()))
    return cur.fetchone() is not None


def already_alerted(cur, item: dict) -> bool:
    cur.execute("SELECT 1 FROM journal_publish_reruns WHERE task_id = %s AND due_at = %s",
                (item["task"], item["due"].astimezone()))
    return cur.fetchone() is not None


def rerun(item: dict):
    """Re-run the REAL generator with the scheduler's exact args (it owns voice, guard, publish)."""
    cmd = [sys.executable, str(SCRIPTS / item["script"]), *item["args"]]
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS), HOME=str(Path.home()),
               PATH="/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin") + ":"
                    + os.environ.get("PATH", "/usr/bin:/bin"))
    timeout = max(900, int(item["timeout"] * 1.5))
    log(f"  re-running {item['task']}: {' '.join(cmd[1:])} (timeout {timeout}s)")
    try:
        r = subprocess.run(cmd, cwd=SCRIPTS, capture_output=True, text=True, timeout=timeout, env=env)
        tail = [ln for ln in ((r.stdout or "") + (r.stderr or "")).splitlines() if ln.strip()]
        return r.returncode, (tail[-1][-200:] if tail else "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def post_alert(text: str) -> bool:
    try:
        import nova_config
        return bool(nova_config.post_both(text, slack_channel=nova_config.SLACK_ALERTS, discord_channel=None))
    except Exception as e:  # noqa: BLE001
        log(f"Slack post failed: {e}")
        return False


def format_alert(missing: list, now: dt.datetime) -> str:
    lines = [f":rotating_light: *Journal: {len(missing)} expected article(s) missing* "
             f"(publish check {now:%a %H:%M})"]
    for it in missing:
        action = {"rerun": "re-running once", "stale": "too old to re-run",
                  "stranded": "written, waiting on push", "done": "already re-run once",
                  "deferred": "re-run deferred (origin unreadable)"}.get(it.get("action"), "")
        lines.append(f"• `{it['task']}` due {it['due']:%a %H:%M} ({it['owner']}) — {it['detail'][:170]}"
                     + (f" → {action}" if action else ""))
    return "\n".join(lines)


def evaluate(cur, item: dict, index: list, until: dt.datetime) -> tuple:
    ledger = ledger_rows(cur, item, until)
    runs = runs_since(cur, item["task"], item["due"])
    disk = learned = None
    history = []
    if not ledger:
        disk = disk_published(item)
        history = run_history(cur, item["task"])
        # nova_journal.py's section comes from its own PROFILE table, so that check is exact;
        # learned names are only for the hand-mapped / unmapped generators.
        if item["script"] != "nova_journal.py":
            learned = learned_published(item, history, index)
    return classify(item, ledger, runs, ledger_instrumented(cur, item["script"]), disk, learned,
                    ever_ran=bool(history) or bool(ledger))


def check(now: dt.datetime, cur, lookback_h: int) -> list:
    results = []
    _ORIGIN_NAMES.clear()
    index = added_index()
    for item in expected(now, load_tasks(), lookback_h):
        state, detail = evaluate(cur, item, index, now)
        item.update(state=state, detail=detail)
        log(f"  {state:8} {item['task']:28} due {item['due']:%m-%d %H:%M} [{item['owner']}] {detail[:110]}")
        results.append(item)
    if _ORIGIN_ERRORS:
        log(f"origin/main reads failed this pass ({'; '.join(_ORIGIN_ERRORS[:3])}) — re-runs deferred")
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report only: no Slack, no re-runs, no PG writes")
    ap.add_argument("--no-rerun", action="store_true", help="alert but never re-run")
    ap.add_argument("--hours", type=int, default=LOOKBACK_H, help="look-back window in hours")
    a = ap.parse_args(argv)

    now = dt.datetime.now()
    log(f"=== journal publish check {now:%Y-%m-%d %H:%M} (window {a.hours}h) ===")
    _ORIGIN_ERRORS.clear()
    f = git("fetch", "-q", "origin", timeout=120)
    if f.returncode != 0:
        log(f"git fetch failed ({f.stderr.strip()[:120]}) — judging against the last-fetched origin/main")
        _ORIGIN_ERRORS.append("fetch failed")
    conn = _pg()
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s)", (RERUN_LOCK_KEY,))
    if not cur.fetchone()[0]:
        log("another publish check is running — exiting")
        return 0
    try:
        results = check(now, cur, a.hours)
        missing = [r for r in results if r["state"] in ("MISSING", "STRANDED")]
        if not missing:
            log(f"=== all {len(results)} expected article(s) accounted for ===")
            return 0
        fresh = []
        for it in missing:
            if it["state"] == "STRANDED":
                it["action"] = "stranded"
            elif now - it["due"] > dt.timedelta(hours=RERUN_MAX_AGE_H):
                it["action"] = "stale"
            elif a.dry_run or a.no_rerun:
                it["action"] = ""
            elif _ORIGIN_ERRORS:
                # Incomplete evidence must never trigger a re-run: that is how a double publish happens.
                it["action"] = "deferred"
            else:
                it["action"] = "rerun"
            if a.dry_run or it["action"] == "deferred" or not already_alerted(cur, it):
                fresh.append(it)
        if a.dry_run:
            log("DRY-RUN — would alert:\n" + format_alert(missing, now))
            return 1
        if fresh:
            post_alert(format_alert(fresh, now))
        else:
            log("every missing article was already alerted/re-run earlier — not re-alerting")

        outcomes = []
        for it in fresh:
            if it["action"] != "rerun":
                if it["action"] in ("stale", "stranded"):
                    cur.execute("""INSERT INTO journal_publish_reruns (task_id, due_at, host, result)
                                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                (it["task"], it["due"].astimezone(), socket.gethostname(), it["action"]))
                continue
            if not claim_rerun(cur, it):
                continue                                   # someone already re-ran this (task, due)
            # Re-check RIGHT before running: the scheduler's own retry or a late job may have landed it.
            git("fetch", "-q", "origin", timeout=120)
            state, detail = evaluate(cur, it, added_index(), dt.datetime.now())
            if state in ("OK", "RUNNING", "STRANDED"):
                result = f"not needed ({state}: {detail[:100]})"
                rc = 0
            else:
                rc, tail = rerun(it)
                git("fetch", "-q", "origin", timeout=120)
                st2, d2 = evaluate(cur, it, added_index(), dt.datetime.now())
                result = (f"published: {d2[:100]}" if st2 == "OK"
                          else f"still missing (exit {rc}{': ' + tail[:140] if tail else ''})")
            cur.execute("""UPDATE journal_publish_reruns SET exit_code = %s, result = %s, attempted_at = now()
                           WHERE task_id = %s AND due_at = %s""",
                        (rc, result, it["task"], it["due"].astimezone()))
            log(f"  {it['task']}: {result}")
            outcomes.append(f"• `{it['task']}` — {result}")
        if outcomes:
            ok = sum(1 for o in outcomes if "— published" in o or "not needed" in o)
            post_alert(f"{':white_check_mark:' if ok == len(outcomes) else ':warning:'} *Journal re-runs:* "
                       f"{ok}/{len(outcomes)} recovered\n" + "\n".join(outcomes))
        return 1 if any(("still missing" in o) for o in outcomes) else 0
    finally:
        cur.execute("SELECT pg_advisory_unlock(%s)", (RERUN_LOCK_KEY,))
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
