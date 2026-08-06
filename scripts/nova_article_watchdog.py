#!/usr/bin/env python3
"""nova_article_watchdog.py — catch articles that did not publish, then fix and write them.

WHY: on 2026-07-28 Jordan noticed missing overnight ops articles. Every scheduler job had
reported success. The failures are never "the job did not run" — they are:

  * the job TIMED OUT mid-publish (daily_threat_assessment has hit its 600s ceiling on 4 of its
    last 10 runs; successful runs take 349-494s, so it lives one slow LLM call from failure),
  * the publish GUARD correctly blocked a refusal-shaped article and nothing replaced it,
  * the article was written and committed but the push never happened, so it exists on disk
    and not on the web,
  * or the job "succeeded" and quietly produced nothing at all.

Every one of those looks identical from the scheduler's side: exit 0. So this watchdog checks
the CONSUMER's view — is the article actually on the site — and never the producer's self-report.

Usage:
  --check      report only, change nothing (default)
  --fix        diagnose, repair, and generate anything still missing
  --dry-run    with --fix: show what would be done, generate nothing
"""
import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

HUGO = Path.home() / "nova-journal"
SITE = "https://nova.digitalnoise.net"
SCHED = Path.home() / ".openclaw/config/scheduler.yaml"
GRACE_MIN = 30          # Jordan's spec: only complain 30 min after the scheduled time

# Scripts that publish a Hugo article: script -> (section, matcher).
# nova_journal.py takes its section from the task's args and is matched by date alone.
#
# The matcher matters. Three separate jobs publish into `local` every day, so "is there any
# article in local today" marks all three healthy the moment ONE of them runs — which would
# have hidden exactly the kind of silent failure this watchdog exists to find. Each job must
# be identified by something only it produces.
#   ("stable", name) -> content/<section>/<name>.md, dated today (fixed-filename posts)
#   ("tag", t)       -> today's dated post carrying tag `t`
#   ("slug", frag)   -> today's dated post whose filename contains `frag`
ARTICLE_SCRIPTS = {
    "nova_local_burbank.py":           ("local",      ("tag", "local-news")),
    "nova_local_airwaves.py":          ("local",      ("tag", "airwaves")),
    "nova_daily_threat_assessment.py": ("local",      ("stable", "daily-watch")),
    # publishes into operations despite its name; content/security/ is only a landing page
    "nova_journal_security.py":        ("operations", ("slug", "security-intelligence-briefing")),
    "nova_weekly_ops_report.py":       ("operations", ("slug", "")),
    "nova_daily_ops_log.py":           ("operations", ("slug", "")),
    # Added after the 2026-07-28 18:00 run: this job WAS publishing, but its front matter was
    # dated two hours ahead so Hugo hid the article and the site 404ed. The watchdog missed it
    # entirely because the script was never in this map — a gap in the watchdog, not the job.
    "nova_rando_daily_ops.py":         ("operations", ("slug", "")),
    "nova_journal_emergency.py":       ("operations", ("slug", "")),
    "nova_fishbowl_daily.py":          ("fishbowl",   ("stable", "the-fishbowl")),
    # Was missing until 2026-07-29, so this job's 8-day outage (execve E2BIG, see
    # nova_journal.call_openrouter) was invisible here — the same watchdog gap that hid
    # nova_rando_daily_ops.py. 'watch-community' is the tag only this job emits into opinions.
    "nova_opinion_fishbowl.py":        ("opinions",   ("tag", "watch-community")),
    "nova_after_dark.py":              ("after-dark", ("slug", "")),
    "nova_art_corner.py":              ("art",        ("slug", "")),
}


# The scheduler passes a PROFILE key ("opinion"), which is not always the directory name
# ("opinions"). Read nova_journal's own mapping instead of duplicating it — a second copy of
# that table is exactly how the watchdog ends up disagreeing with the generator it watches.
_SECTION_CACHE = {}


def _journal_section(profile: str) -> str:
    if not profile:
        return ""
    if not _SECTION_CACHE:
        try:
            src = (Path(__file__).parent / "nova_journal.py").read_text(errors="ignore")
            for key, body in re.findall(r'"([a-z-]+)":\s*\{(.*?)\}', src, re.S):
                m = re.search(r'"section":\s*"([a-z-]+)"', body)
                if m:
                    _SECTION_CACHE[key] = m.group(1)
        except OSError:
            pass
    return _SECTION_CACHE.get(profile, profile)


def log(m):
    print(m, flush=True)


def run(cmd, cwd=None, timeout=900):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def expected_today(now):
    """Article publications whose scheduled time has passed by more than GRACE_MIN today."""
    import yaml
    cfg = yaml.safe_load(SCHED.read_text())
    tasks = cfg.get("tasks") or cfg.get("jobs") or []
    if isinstance(tasks, dict):
        tasks = [dict(v, name=k) for k, v in tasks.items()]
    out = []
    for t in tasks:
        # NOTE: do NOT skip on `enabled: false`. On .6 that flag means "migrated to .2 in the
        # Wave B split", not "not running" — journal_security, local_burbank, local_airwaves and
        # fishbowl_daily are all enabled:false here and all published today from the other node.
        # The article is expected regardless of which machine produces it, and this watchdog
        # judges the published result, not the local config.
        script = str(t.get("script", ""))
        m = re.match(r"cron (\d+) (\d+) \S+ \S+ (\S+)", str(t.get("schedule", "")))
        if not m:
            continue                       # interval jobs have no single "due" moment
        if script == "nova_journal.py":
            args = t.get("args") or []
            section = _journal_section(args[0].strip() if args else "")
            matcher = ("slug", "")
        else:
            section, matcher = ARTICLE_SCRIPTS.get(script, ("", None))
        if not section:
            continue
        mi, hr, dow = int(m.group(1)), int(m.group(2)), m.group(3)
        due = now.replace(hour=hr, minute=mi, second=0, microsecond=0)
        if due > now:
            continue
        if dow != "*" and str((due.weekday() + 1) % 7) not in dow.split(","):
            continue
        if (now - due).total_seconds() < GRACE_MIN * 60:
            continue                       # inside the grace window; not late yet
        out.append({"task": t.get("name", script), "script": script, "section": section,
                    "matcher": matcher, "due": due, "args": t.get("args") or []})
    return out


def published(section, day, matcher=("slug", "")):
    """Did THIS job's article land for `day`? Disk, then push state.

    Disk alone is not publication — an article can be committed and never pushed, which is
    invisible to every producer-side check and to the reader alike.
    """
    d = HUGO / "content" / section
    if not d.is_dir():
        return False, "no such section directory"
    kind, want = matcher
    stamp = day.strftime("%Y-%m-%d")

    if kind == "stable":
        f = d / f"{want}.md"
        if not f.is_file():
            return False, f"{want}.md does not exist"
        age = day.date() - dt.date.fromtimestamp(f.stat().st_mtime)
        if age.days > 0:
            return False, f"{want}.md is {age.days}d stale (last {dt.date.fromtimestamp(f.stat().st_mtime)})"
        hits = [f]
    else:
        dated = [p for p in d.glob("*.md") if p.name.startswith(stamp)]
        if kind == "tag" and want:
            hits = []
            for p in dated:
                head = p.read_text(errors="ignore")[:800]
                m = re.search(r"^tags:\s*\[(.*?)\]", head, re.M)
                if m and want.lower() in m.group(1).lower():
                    hits.append(p)
            if not hits:
                return False, f"no post today tagged '{want}'"
        elif kind == "slug" and want:
            hits = [p for p in dated if want in p.name]
            if not hits:
                return False, f"no post today matching '{want}'"
        else:
            hits = dated
    if not hits:
        return False, "no article file for today"
    # A future-dated post is published but INVISIBLE: Hugo skips future content by default, so the
    # file exists, git is clean, every producer-side check passes — and the URL 404s. Seen 2026-07-28
    # when a generator hardcoded 20:00 after its schedule moved to 18:00.
    for h in hits:
        m = re.search(r"^date:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", h.read_text(errors="ignore")[:600], re.M)
        if m:
            try:
                when = dt.datetime.fromisoformat(m.group(1))
            except ValueError:
                continue
            if when > dt.datetime.now() + dt.timedelta(minutes=5):
                return False, f"{h.name} is FUTURE-DATED ({m.group(1)}) — Hugo will not publish it yet"
    # Stub guard: a "published" 33-char post (e.g. a logged-out LLM shipping its
    # "Not logged in · Please run /login" stub as the article, 2026-08-04/05) passes
    # every existence/push/URL check but is NOT an article. Reject anything under the
    # word floor as a miss so --fix/alerting fires instead of it looking healthy.
    STUB_WORD_FLOOR = 150
    body = re.sub(r"(?s)\A---.*?\n---\s*", "", hits[0].read_text(errors="ignore"))
    wc = len(body.split())
    if wc < STUB_WORD_FLOOR:
        return False, f"{hits[0].name} is a STUB — only {wc} words (LLM backend likely returned an error)"
    r = run(["git", "log", "origin/main..HEAD", "--oneline"], cwd=HUGO, timeout=30)
    if r.returncode == 0 and r.stdout.strip():
        return False, f"written but NOT PUSHED ({len(r.stdout.strip().splitlines())} commit(s) local)"
    return True, hits[0].name


def diagnose(item):
    """Why is it missing? Returns (cause, suggested_fix)."""
    causes = []
    # 1. What does the scheduler say actually happened?
    try:
        import psycopg2
        c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
        cur = c.cursor()
        cur.execute("""SELECT status, exit_code, left(coalesce(error_tail,''),200)
                       FROM scheduler_runs
                       WHERE task_script = %s
                         AND to_timestamp(started_at/1000.0) > now() - interval '20 hours'
                       ORDER BY started_at DESC LIMIT 1""", (item["script"],))
        row = cur.fetchone()
        c.close()
        if not row:
            causes.append(("never_ran", "the scheduler has no record of it running at all"))
        elif row[0] == "timeout" or row[1] == 124:
            causes.append(("timeout", f"killed on the task timeout ({row[2][:80]})"))
        elif row[1]:
            causes.append(("failed", f"exit {row[1]}: {row[2][:80]}"))
    except Exception as e:
        causes.append(("unknown", f"could not read scheduler_runs: {str(e)[:60]}"))

    # 2. Did the publish guard block it? That is a CORRECT refusal, not a fault — but it
    #    leaves a hole, and the hole is what the reader notices.
    logs = Path.home() / ".openclaw/logs"
    for lf in logs.glob("*.log"):
        try:
            tail = lf.read_text(errors="ignore")[-200000:]
        except OSError:
            continue
        for line in tail.splitlines()[-4000:]:
            if "[guard] BLOCKED" in line and f"({item['section']})" in line:
                causes.append(("guard_blocked", line.strip()[:140]))
                break

    # 3. Content sitting unpushed.
    r = run(["git", "status", "--porcelain", "content/"], cwd=HUGO, timeout=30)
    if r.stdout.strip():
        causes.append(("uncommitted", f"{len(r.stdout.strip().splitlines())} uncommitted file(s)"))
    r = run(["git", "log", "origin/main..HEAD", "--oneline"], cwd=HUGO, timeout=30)
    if r.returncode == 0 and r.stdout.strip():
        causes.append(("unpushed", f"{len(r.stdout.strip().splitlines())} unpushed commit(s)"))
    return causes or [("no_output", "job reported success but produced no article")]


def autofix(causes, dry):
    """Repair what is mechanically repairable. Returns True if the fix may be sufficient."""
    fixed = False
    kinds = {c for c, _ in causes}
    if "uncommitted" in kinds or "unpushed" in kinds:
        log("    fix: committing and pushing stranded content")
        if not dry:
            run(["git", "add", "content/", "static/"], cwd=HUGO)
            run(["git", "-c", "user.name=Jordan Koch", "commit", "-m",
                 "chore: publish content stranded by an interrupted job"], cwd=HUGO)
            # Rebase onto origin BEFORE pushing — this watchdog exists to un-strand commits,
            # so it must NOT itself get stuck behind a diverged clone (host .6: 82 ahead / 25 behind).
            pull = run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=HUGO, timeout=180)
            if pull.returncode != 0:
                run(["git", "rebase", "--abort"], cwd=HUGO)
                log(f"    push ABORTED — pull --rebase failed (diverged/conflict): {pull.stderr[:200]}")
            else:
                r = run(["git", "push", "origin", "main"], cwd=HUGO, timeout=180)
                log(f"    push rc={r.returncode}" + ("" if r.returncode == 0 else f" FAILED: {r.stderr[:200]}"))
        fixed = True
    return fixed


def regenerate(item, dry):
    """Re-run the job that should have produced the article, then verify it appeared.

    Deliberately re-runs the REAL generator rather than writing an article here: the generator
    already owns the voice, the image, the guard and the publish path. A watchdog that wrote its
    own article would be a second, unguarded way to publish — exactly how a refusal reached the
    site on 2026-07-27.
    """
    script = Path(__file__).parent / item["script"]
    cmd = [sys.executable, str(script)] + [str(a) for a in item["args"]]
    log(f"    regenerating: {' '.join(cmd[1:])}")
    if dry:
        log("    DRY-RUN — not executing")
        return False
    # Generous timeout: the usual cause of absence is the 600s ceiling being too tight.
    r = run(cmd, timeout=1800)
    log(f"    exit {r.returncode}" + (f" :: {r.stderr.strip()[-160:]}" if r.returncode else ""))
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="diagnose, repair, regenerate")
    ap.add_argument("--check", action="store_true", help="report only (default)")
    ap.add_argument("--dry-run", action="store_true", help="with --fix, change nothing")
    a = ap.parse_args()

    now = dt.datetime.now()
    log(f"=== article watchdog {now:%Y-%m-%d %H:%M} (grace {GRACE_MIN}m) ===")
    run(["git", "fetch", "-q", "origin"], cwd=HUGO, timeout=90)   # else origin/main is stale
    missing = []
    for item in expected_today(now):
        ok, detail = published(item["section"], now, item["matcher"])
        late = int((now - item["due"]).total_seconds() // 60)
        if ok:
            log(f"  OK      {item['task']:26} {item['section']:12} {detail[:46]}")
        else:
            log(f"  MISSING {item['task']:26} {item['section']:12} due {item['due']:%H:%M} "
                f"({late}m ago) — {detail}")
            missing.append(item)

    if not missing:
        log("=== all expected articles are published ===")
        return 0
    if not a.fix:
        log(f"=== {len(missing)} missing; re-run with --fix to repair ===")
        return 1

    for item in missing:
        log(f"  --- {item['task']} ({item['section']}) ---")
        causes = diagnose(item)
        for kind, detail in causes:
            log(f"    cause: {kind} :: {detail}")
        autofix(causes, a.dry_run)
        ok, detail = published(item["section"], now, item["matcher"])
        if ok:
            log(f"    resolved by repair: {detail}")
            continue
        if regenerate(item, a.dry_run):
            ok, detail = published(item["section"], now, item["matcher"])
            log(f"    {'PUBLISHED: ' + detail if ok else 'STILL MISSING after regeneration'}")
        elif not a.dry_run:
            log("    regeneration FAILED — needs a human")
    return 0


if __name__ == "__main__":
    sys.exit(main())
